"""Session-owned RTP relay/resampler for SIP PCM calls."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
from pathlib import Path
import secrets
import socket
from typing import Any, Callable
import wave

from . import rtp
from .audio_format import AudioFormat
from .audio_pcm import PcmFrameConverter
from .debug_capture import (
    DEBUG_CAPTURE_DIR,
    capture_temp_path,
    capture_session_name,
    commit_capture_file,
    debug_capture_transaction,
    prune_debug_captures,
    release_debug_capture_write,
    try_reserve_debug_capture_write,
    wav_pcm_payload,
)
from .dtmf import (
    RtpDtmfDecoder,
    build_telephone_event_payload,
    telephone_event_code,
)
from .queue_utils import drain_queue, put_drop_oldest
from .sdp import RtpPcmFormat, audio_format_to_rtp
from .session_cleanup import async_wait_for_cleanup
from .sip_client import RtpPayloadDecoder, RtpPayloadEncoder

_LOGGER = logging.getLogger(__name__)
_DEBUG_CAPTURE_SECONDS = 8
_RTP_IP_TOS = 0xB8
_DTMF_UPDATE_SECONDS = 0.05
_RTP_PACER_QUEUE_FRAMES = 8

@dataclass(slots=True)
class RtpPeer:
    host: str
    port: int
    payload_type: int
    audio_format: AudioFormat
    rtp_format: RtpPcmFormat | None = None
    send_payload_type: int | None = None
    send_audio_format: AudioFormat | None = None
    send_rtp_format: RtpPcmFormat | None = None
    dtmf_payload_type: int | None = None
    dtmf_clock_rate: int = 8000
    dtmf_events: frozenset[int] = frozenset(range(16))
    send_dtmf_payload_type: int | None = None
    send_dtmf_clock_rate: int | None = None
    send_dtmf_events: frozenset[int] | None = None
    can_send: bool = True
    can_receive: bool = True
    connection_held: bool = False
    signaling_host: str = ""
    advertised_host: str = ""
    rx_ssrc: int | None = None
    sequence: int = field(default_factory=lambda: secrets.randbelow(0x10000))
    timestamp: int = field(default_factory=lambda: secrets.randbelow(0x100000000))
    ssrc: int = field(default_factory=lambda: secrets.randbelow(0x100000000))
    dtmf_sequence: int = field(default_factory=lambda: secrets.randbelow(0x10000))
    dtmf_timestamp: int = field(default_factory=lambda: secrets.randbelow(0x100000000))
    dtmf_ssrc: int = field(default_factory=lambda: secrets.randbelow(0x100000000))

    def __post_init__(self) -> None:
        if not self.advertised_host:
            self.advertised_host = self.host

    @property
    def outbound_payload_type(self) -> int:
        return self.send_payload_type if self.send_payload_type is not None else self.payload_type

    @property
    def outbound_audio_format(self) -> AudioFormat:
        return self.send_audio_format if self.send_audio_format is not None else self.audio_format

    @property
    def inbound_rtp_format(self) -> RtpPcmFormat:
        return self.rtp_format if self.rtp_format is not None else audio_format_to_rtp(self.audio_format, self.payload_type)

    @property
    def outbound_rtp_format(self) -> RtpPcmFormat:
        if self.send_rtp_format is not None:
            return self.send_rtp_format
        return audio_format_to_rtp(self.outbound_audio_format, self.outbound_payload_type)

    @property
    def outbound_dtmf_payload_type(self) -> int | None:
        return (
            self.send_dtmf_payload_type
            if self.send_dtmf_payload_type is not None
            else self.dtmf_payload_type
        )

    @property
    def outbound_dtmf_clock_rate(self) -> int:
        return (
            int(self.send_dtmf_clock_rate)
            if self.send_dtmf_clock_rate is not None
            else int(self.dtmf_clock_rate)
        )

    @property
    def outbound_dtmf_events(self) -> frozenset[int]:
        return (
            self.send_dtmf_events
            if self.send_dtmf_events is not None
            else self.dtmf_events
        )

    def accepts_source_host(self, source_host: str) -> bool:
        """Allow the SDP media host or the authenticated SIP flow host."""

        return str(source_host) in {
            self.host,
            self.advertised_host,
            self.signaling_host,
        }


class _RelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, relay: "SipRtpRelay", side: str) -> None:
        self.relay = relay
        self.side = side
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        self.relay.handle_packet(self.side, data, addr)


class SipRtpRelay:
    """Bidirectional RTP relay with explicit peer ownership.

    Each SIP leg keeps its negotiated PCM shape. HA converts between the two
    negotiated RTP formats when needed, including sample-rate and frame-size
    changes that are exact for the supported PCM profile.
    """

    def __init__(
        self,
        *,
        left: RtpPeer,
        right: RtpPeer,
        left_port: int,
        right_port: int,
        debug_capture: bool = False,
        capture_name: str = "",
        on_release: Callable[[tuple[int, int]], None] | None = None,
        on_dtmf: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.left = left
        self.right = right
        self.left_port = int(left_port)
        self.right_port = int(right_port)
        self.left_transport: asyncio.DatagramTransport | None = None
        self.right_transport: asyncio.DatagramTransport | None = None
        self._on_release = on_release
        self.on_dtmf = on_dtmf
        self._released = False
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._audio_queues: dict[str, asyncio.Queue[tuple[int, bytes]]] = {}
        self._audio_pacers: dict[str, asyncio.Task[None]] = {}
        self._audio_pacing_generation = 0
        self._dtmf_tasks: set[asyncio.Task[None]] = set()
        self._dtmf_locks = {"left": asyncio.Lock(), "right": asyncio.Lock()}
        self.video_relay: Any | None = None
        self.forwarded = 0
        self.dropped = 0
        self.drop_connection_hold = 0
        self.debug_capture_dropped_writes = 0
        self.left_rx_packets = 0
        self.left_rx_bytes = 0
        self.left_tx_packets = 0
        self.left_tx_bytes = 0
        self.right_rx_packets = 0
        self.right_rx_bytes = 0
        self.right_tx_packets = 0
        self.right_tx_bytes = 0
        self.pacing_dropped_packets = 0
        self.left_dtmf_tx_events = 0
        self.right_dtmf_tx_events = 0
        self._configure_media(left, right)
        self._capture_buffers: dict[str, bytearray] = {}
        self._capture_paths: dict[str, Path] = {}
        self._capture_formats: dict[str, AudioFormat] = {}
        self._capture_frames: dict[str, int] = {"left": 0, "right": 0}
        self._capture_snapshot: dict[str, tuple[bytes, AudioFormat, Path]] = {}
        if debug_capture:
            self._prepare_debug_capture(capture_name)

    @staticmethod
    def _build_media_state(left: RtpPeer, right: RtpPeer) -> dict[str, Any]:
        """Prepare converters without changing the live relay."""

        return {
            "left_to_right": PcmFrameConverter(left.audio_format, right.outbound_audio_format),
            "right_to_left": PcmFrameConverter(right.audio_format, left.outbound_audio_format),
            "left_decoder": RtpPayloadDecoder(left.inbound_rtp_format),
            "right_decoder": RtpPayloadDecoder(right.inbound_rtp_format),
            "left_encoder": RtpPayloadEncoder(left.outbound_rtp_format),
            "right_encoder": RtpPayloadEncoder(right.outbound_rtp_format),
            "dtmf_decoders": {
                "left": RtpDtmfDecoder(left.dtmf_payload_type)
                if left.dtmf_payload_type is not None
                else None,
                "right": RtpDtmfDecoder(right.dtmf_payload_type)
                if right.dtmf_payload_type is not None
                else None,
            },
        }

    def _apply_media_state(
        self,
        left: RtpPeer,
        right: RtpPeer,
        state: dict[str, Any],
    ) -> None:
        """Publish one fully prepared media state."""

        self.left = left
        self.right = right
        self.left_to_right = state["left_to_right"]
        self.right_to_left = state["right_to_left"]
        self.left_decoder = state["left_decoder"]
        self.right_decoder = state["right_decoder"]
        self.left_encoder = state["left_encoder"]
        self.right_encoder = state["right_encoder"]
        self._dtmf_decoders = state["dtmf_decoders"]
        self._reconfigure_audio_pacers()

    def _configure_media(self, left: RtpPeer, right: RtpPeer) -> None:
        """Build all codec state first, then publish one coherent peer pair."""

        self._apply_media_state(left, right, self._build_media_state(left, right))

    def prepare_peer_reconfiguration(
        self,
        side: str,
        peer: RtpPeer,
    ) -> Callable[[], None]:
        """Validate a negotiated peer update and return its infallible commit."""

        if side not in {"left", "right"}:
            raise ValueError(f"unknown RTP relay side: {side}")
        previous = self.left if side == "left" else self.right
        left = peer if side == "left" else self.left
        right = peer if side == "right" else self.right
        state = self._build_media_state(left, right)

        def _commit() -> None:
            current = self.left if side == "left" else self.right
            if current is not previous:
                raise RuntimeError("stale RTP relay reconfiguration")
            commit_left = peer if side == "left" else self.left
            commit_right = peer if side == "right" else self.right
            commit_state = (
                state
                if commit_left is left and commit_right is right
                else self._build_media_state(commit_left, commit_right)
            )
            # Preserve the sender identity at commit time so packets forwarded
            # while the SIP response was in flight cannot rewind the RTP clock.
            peer.sequence = current.sequence
            peer.timestamp = current.timestamp
            peer.ssrc = current.ssrc
            peer.rx_ssrc = None
            peer.dtmf_sequence = current.dtmf_sequence
            peer.dtmf_timestamp = current.dtmf_timestamp
            peer.dtmf_ssrc = current.dtmf_ssrc
            self._apply_media_state(commit_left, commit_right, commit_state)
            if side in self._capture_buffers:
                self._capture_buffers[side].clear()
                self._capture_frames[side] = 0
                self._capture_formats[side] = peer.audio_format

        return _commit

    def reconfigure_peer(self, side: str, peer: RtpPeer) -> None:
        """Atomically apply a negotiated RTP peer update to one dialog leg."""

        self.prepare_peer_reconfiguration(side, peer)()

    def _prepare_debug_capture(self, capture_name: str) -> None:
        safe_name = capture_session_name(capture_name or f"relay_{self.left_port}_{self.right_port}")
        for side, fmt in (("left", self.left.audio_format), ("right", self.right.audio_format)):
            path = DEBUG_CAPTURE_DIR / f"{safe_name}_{side}_rx.wav"
            self._capture_buffers[side] = bytearray()
            self._capture_paths[side] = path
            self._capture_formats[side] = fmt
            _LOGGER.info("SIP RTP debug capture prepared side=%s path=%s format=%s", side, path, fmt.wire_token())

    def _capture_pcm(self, side: str, pcm: bytes) -> None:
        buffer = self._capture_buffers.get(side)
        if buffer is None:
            return
        fmt = self.left.audio_format if side == "left" else self.right.audio_format
        max_frames = int(fmt.sample_rate * _DEBUG_CAPTURE_SECONDS)
        current = self._capture_frames.get(side, 0)
        if current >= max_frames:
            return
        samples = len(pcm) // max(1, fmt.container_bytes_per_sample * fmt.channels)
        if current + samples > max_frames:
            keep = (max_frames - current) * fmt.container_bytes_per_sample * fmt.channels
            pcm = pcm[:keep]
            samples = max_frames - current
        buffer.extend(pcm)
        self._capture_frames[side] = current + samples

    def _detach_debug_capture_snapshot(self) -> None:
        """Freeze live capture buffers before any asynchronous teardown await."""

        self._capture_snapshot = {
            side: (
                bytes(pcm),
                self._capture_formats[side],
                self._capture_paths[side],
            )
            for side, pcm in list(self._capture_buffers.items())
            if side in self._capture_formats and side in self._capture_paths
        }
        self._capture_buffers.clear()
        self._capture_frames.clear()

    def _write_debug_capture_files(self) -> None:
        snapshot = dict(self._capture_snapshot)
        if not snapshot:
            return
        with debug_capture_transaction():
            published: list[Path] = []
            try:
                for side, (pcm, fmt, path) in snapshot.items():
                    temporary = capture_temp_path(path)
                    try:
                        sample_width, wav_payload = wav_pcm_payload(fmt, pcm)
                        with wave.open(str(temporary), "wb") as wav:
                            wav.setnchannels(fmt.channels)
                            wav.setsampwidth(sample_width)
                            wav.setframerate(fmt.sample_rate)
                            wav.writeframes(wav_payload)
                        commit_capture_file(temporary, path)
                        published.append(path)
                    finally:
                        with contextlib.suppress(OSError):
                            temporary.unlink()
                    _LOGGER.info(
                        "SIP RTP debug capture wrote side=%s path=%s bytes=%d format=%s",
                        side,
                        path,
                        len(pcm),
                        fmt.wire_token(),
                    )
                prune_debug_captures()
            except BaseException:
                for destination in published:
                    with contextlib.suppress(OSError):
                        destination.unlink()
                raise

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.left_transport is not None and self.right_transport is not None:
                return
            if self._released or self._stop_requested:
                raise RuntimeError("SIP RTP relay has already been stopped")
            task = self._start_task
            if task is None:
                task = asyncio.create_task(
                    self._start_impl(),
                    name=f"voip-rtp-relay-start-{self.left_port}-{self.right_port}",
                )
                self._start_task = task
        try:
            await task
        finally:
            async with self._lifecycle_lock:
                if self._start_task is task and task.done():
                    self._start_task = None

    async def _start_impl(self) -> None:
        loop = asyncio.get_running_loop()
        left_sock: socket.socket | None = None
        right_sock: socket.socket | None = None
        try:
            left_sock = self._rtp_socket(self.left_port)
            right_sock = self._rtp_socket(self.right_port)
            left_transport, _ = await loop.create_datagram_endpoint(
                lambda: _RelayProtocol(self, "left"),
                sock=left_sock,
            )
            if self._stop_requested:
                left_transport.close()
                raise asyncio.CancelledError
            self.left_transport = left_transport
            left_sock = None
            right_transport, _ = await loop.create_datagram_endpoint(
                lambda: _RelayProtocol(self, "right"),
                sock=right_sock,
            )
            if self._stop_requested:
                right_transport.close()
                raise asyncio.CancelledError
            self.right_transport = right_transport
            right_sock = None
            if self.video_relay is not None:
                await self.video_relay.start()
            if self._stop_requested:
                raise asyncio.CancelledError
            self._start_audio_pacers()
        except BaseException:
            self._close_audio_resources()
            if left_sock is not None:
                left_sock.close()
            if right_sock is not None:
                right_sock.close()
            video_relay = self.video_relay
            self.video_relay = None
            if video_relay is not None:
                try:
                    await video_relay.stop()
                except BaseException:
                    _LOGGER.debug(
                        "SIP video relay cleanup failed during audio relay start",
                        exc_info=True,
                    )
            self._release_ports()
            raise
        _LOGGER.info(
            "SIP RTP relay listening left=%s right=%s left=%s->%s right=%s->%s",
            self.left_port,
            self.right_port,
            self.left.audio_format.wire_token(),
            self.right.outbound_audio_format.wire_token(),
            self.right.audio_format.wire_token(),
            self.left.outbound_audio_format.wire_token(),
        )

    @staticmethod
    def _rtp_socket(port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _RTP_IP_TOS)
            sock.bind(("0.0.0.0", int(port)))
            return sock
        except BaseException:
            sock.close()
            raise

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stop_requested = True
            task = self._stop_task
            if task is None:
                task = asyncio.create_task(
                    self._stop_impl(),
                    name=f"voip-rtp-relay-stop-{self.left_port}-{self.right_port}",
                )
                self._stop_task = task
        await async_wait_for_cleanup(task)

    async def _stop_impl(self) -> None:
        async with self._lifecycle_lock:
            start_task = self._start_task
            if start_task is not None and not start_task.done():
                start_task.cancel()
        if start_task is not None and start_task is not asyncio.current_task():
            await asyncio.gather(start_task, return_exceptions=True)
        _LOGGER.info(
            "SIP RTP relay stopped left=%s right=%s forwarded=%s dropped=%s left_rx=%s right_rx=%s left_tx=%s right_tx=%s",
            self.left_port,
            self.right_port,
            self.forwarded,
            self.dropped,
            self.left_rx_packets,
            self.right_rx_packets,
            self.left_tx_packets,
            self.right_tx_packets,
        )
        video_relay = self.video_relay
        self.video_relay = None
        pacer_tasks = self._stop_audio_pacers()
        dtmf_tasks = tuple(self._dtmf_tasks)
        for task in dtmf_tasks:
            task.cancel()
        self._close_audio_resources()
        self._detach_debug_capture_snapshot()
        # Port ownership is independent from optional diagnostics and video
        # cleanup. Release it before the first await so a slow filesystem or a
        # broken video relay cannot starve subsequent calls.
        self._release_ports()
        if pacer_tasks or dtmf_tasks:
            await asyncio.gather(
                *pacer_tasks,
                *dtmf_tasks,
                return_exceptions=True,
            )
        if video_relay is not None:
            try:
                await video_relay.stop()
            except asyncio.CancelledError:
                # This task is shielded from caller cancellation. A child
                # relay returning CancelledError is therefore a child cleanup
                # failure, not a request to abandon audio/debug teardown.
                _LOGGER.warning(
                    "SIP video relay cleanup was cancelled; audio teardown continues left=%s right=%s",
                    self.left_port,
                    self.right_port,
                )
            except Exception:
                _LOGGER.exception(
                    "SIP video relay cleanup failed; audio teardown continues left=%s right=%s",
                    self.left_port,
                    self.right_port,
                )
        if self._capture_snapshot:
            if not try_reserve_debug_capture_write():
                self.debug_capture_dropped_writes += 1
                _LOGGER.warning(
                    "VoIP debug capture writer pool full; dropping RTP relay capture left=%s right=%s",
                    self.left_port,
                    self.right_port,
                )
            else:
                try:
                    await asyncio.to_thread(self._write_debug_capture_files)
                except Exception:
                    _LOGGER.exception(
                        "SIP RTP debug capture write failed; relay teardown continues left=%s right=%s",
                        self.left_port,
                        self.right_port,
                    )
                finally:
                    release_debug_capture_write()
        self._capture_snapshot.clear()

    def _close_audio_resources(self) -> None:
        """Synchronously revoke both RTP transports."""

        if self.left_transport is not None:
            self.left_transport.close()
            self.left_transport = None
        if self.right_transport is not None:
            self.right_transport.close()
            self.right_transport = None

    def _start_audio_pacers(self) -> None:
        """Start one event-driven packet clock for each destination leg."""

        if self._audio_pacers:
            return
        for side in ("left", "right"):
            queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue(
                maxsize=_RTP_PACER_QUEUE_FRAMES
            )
            self._audio_queues[side] = queue
            self._audio_pacers[side] = asyncio.create_task(
                self._pace_audio_packets(side, queue),
                name=f"voip-rtp-pacer-{side}-{self.left_port}-{self.right_port}",
            )

    def _reconfigure_audio_pacers(self) -> None:
        self._audio_pacing_generation += 1
        dropped = sum(drain_queue(queue) for queue in self._audio_queues.values())
        self.pacing_dropped_packets += dropped
        self.dropped += dropped

    def _stop_audio_pacers(self) -> tuple[asyncio.Task[None], ...]:
        self._reconfigure_audio_pacers()
        tasks = tuple(self._audio_pacers.values())
        for task in tasks:
            task.cancel()
        self._audio_pacers.clear()
        self._audio_queues.clear()
        return tasks

    async def _pace_audio_packets(
        self,
        destination_side: str,
        queue: asyncio.Queue[tuple[int, bytes]],
    ) -> None:
        """Smooth converted frames on the destination RTP packet clock."""

        loop = asyncio.get_running_loop()
        generation = self._audio_pacing_generation
        next_send = 0.0
        while True:
            packet_generation, packet = await queue.get()
            if generation != self._audio_pacing_generation:
                generation = self._audio_pacing_generation
                next_send = 0.0
            if packet_generation != generation:
                self.pacing_dropped_packets += 1
                self.dropped += 1
                continue
            destination = (
                self.left if destination_side == "left" else self.right
            )
            frame_delay = max(
                0.001,
                destination.outbound_rtp_format.frame_ms / 1000,
            )
            now = loop.time()
            if next_send <= 0 or now > next_send + frame_delay:
                next_send = now + frame_delay
            await asyncio.sleep(max(0.0, next_send - now))
            if packet_generation != self._audio_pacing_generation:
                next_send = 0.0
                self.pacing_dropped_packets += 1
                self.dropped += 1
                continue
            self._send_audio_packet(destination_side, packet)
            next_send += frame_delay
            if next_send <= loop.time():
                next_send = loop.time() + frame_delay

    def _send_audio_packet(self, destination_side: str, packet: bytes) -> bool:
        destination = self.left if destination_side == "left" else self.right
        transport = (
            self.left_transport
            if destination_side == "left"
            else self.right_transport
        )
        if (
            transport is None
            or self._stop_requested
            or not destination.can_receive
            or destination.connection_held
        ):
            self.dropped += 1
            return False
        try:
            transport.sendto(packet, (destination.host, destination.port))
        except (OSError, RuntimeError) as err:
            self.dropped += 1
            _LOGGER.debug("RTP relay send drop: %s", err)
            return False
        if destination_side == "left":
            self.left_tx_packets += 1
            self.left_tx_bytes += len(packet)
        else:
            self.right_tx_packets += 1
            self.right_tx_bytes += len(packet)
        self.forwarded += 1
        return True

    def _queue_audio_packet(self, destination_side: str, packet: bytes) -> bool:
        queue = self._audio_queues.get(destination_side)
        if queue is None:
            return self._send_audio_packet(destination_side, packet)
        if put_drop_oldest(queue, (self._audio_pacing_generation, packet)):
            self.pacing_dropped_packets += 1
            self.dropped += 1
        return True

    def _release_ports(self) -> None:
        """Return the reserved RTP pair exactly once."""

        if self._on_release is not None and not self._released:
            self._released = True
            self._on_release((self.left_port, self.right_port))

    def attach_video_relay(self, relay: Any) -> None:
        """Attach one call-owned video relay to the audio relay lifecycle."""

        if self._released or self._stop_requested:
            raise RuntimeError("SIP RTP relay has already been stopped")
        if self.video_relay is not None and self.video_relay is not relay:
            raise RuntimeError("video relay already attached")
        self.video_relay = relay

    def relay_dtmf(
        self,
        source_side: str,
        digit: str,
        *,
        duration_ms: int = 160,
    ) -> bool:
        """Translate one detected digit to the opposite negotiated RTP leg."""

        if source_side not in {"left", "right"} or self._stop_requested:
            return False
        destination_side = "right" if source_side == "left" else "left"
        destination = self.right if destination_side == "right" else self.left
        event = telephone_event_code(digit)
        if (
            event is None
            or destination.outbound_dtmf_payload_type is None
            or event not in destination.outbound_dtmf_events
            or not destination.can_receive
            or destination.connection_held
        ):
            _LOGGER.info(
                "RTP DTMF not relayed source=%s destination=%s digit=%s negotiated=%s",
                source_side,
                destination_side,
                digit,
                destination.outbound_dtmf_payload_type is not None,
            )
            return False

        async def _run() -> None:
            try:
                await self._send_dtmf_event(
                    destination_side,
                    destination,
                    str(digit).upper(),
                    duration_ms=max(40, int(duration_ms)),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "RTP DTMF relay failed destination=%s digit=%s",
                    destination_side,
                    digit,
                )

        task = asyncio.create_task(
            _run(),
            name=f"voip-rtp-dtmf-{destination_side}-{digit}",
        )
        self._dtmf_tasks.add(task)
        task.add_done_callback(self._dtmf_tasks.discard)
        return True

    async def _send_dtmf_event(
        self,
        destination_side: str,
        destination: RtpPeer,
        digit: str,
        *,
        duration_ms: int,
    ) -> None:
        """Originate one RFC 4733 event with three final reports."""

        async with self._dtmf_locks[destination_side]:
            if self._stop_requested:
                return
            event_rate = max(1, destination.outbound_dtmf_clock_rate)
            audio_rate = max(
                1, int(destination.outbound_rtp_format.rtp_clock_rate)
            )
            shared_clock = event_rate == audio_rate
            event_timestamp = (
                destination.timestamp
                if shared_clock
                else destination.dtmf_timestamp
            )
            audio_timestamp_at_start = destination.timestamp

            def _send(*, elapsed_ms: int, marker: bool, end: bool) -> bool:
                current = self.right if destination_side == "right" else self.left
                if (
                    current is not destination
                    or self._stop_requested
                    or not destination.can_receive
                    or destination.connection_held
                ):
                    return False
                duration = max(1, round(elapsed_ms * event_rate / 1000))
                if shared_clock:
                    sequence = destination.sequence
                    ssrc = destination.ssrc
                    destination.sequence = rtp.next_sequence(destination.sequence)
                else:
                    sequence = destination.dtmf_sequence
                    ssrc = destination.dtmf_ssrc
                    destination.dtmf_sequence = rtp.next_sequence(
                        destination.dtmf_sequence
                    )
                packet = rtp.build_packet(
                    rtp.RtpPacket(
                        payload_type=int(destination.outbound_dtmf_payload_type),
                        sequence=sequence,
                        timestamp=event_timestamp,
                        ssrc=ssrc,
                        payload=build_telephone_event_payload(
                            digit,
                            duration=min(duration, 0xFFFF),
                            end=end,
                        ),
                        marker=marker,
                    )
                )
                return self._queue_audio_packet(destination_side, packet)

            if not _send(elapsed_ms=1, marker=True, end=False):
                return
            elapsed_ms = 0
            while elapsed_ms < duration_ms:
                step_ms = min(round(_DTMF_UPDATE_SECONDS * 1000), duration_ms - elapsed_ms)
                await asyncio.sleep(step_ms / 1000)
                elapsed_ms += step_ms
                if not _send(
                    elapsed_ms=elapsed_ms,
                    marker=False,
                    end=elapsed_ms >= duration_ms,
                ):
                    return
            for _repeat in range(2):
                await asyncio.sleep(_DTMF_UPDATE_SECONDS)
                if not _send(elapsed_ms=duration_ms, marker=False, end=True):
                    return

            if shared_clock:
                expected_audio_delta = round(duration_ms * audio_rate / 1000)
                actual_audio_delta = (
                    destination.timestamp - audio_timestamp_at_start
                ) & 0xFFFFFFFF
                if actual_audio_delta < expected_audio_delta:
                    destination.timestamp = rtp.next_timestamp(
                        audio_timestamp_at_start,
                        expected_audio_delta,
                    )
            else:
                destination.dtmf_timestamp = rtp.next_timestamp(
                    event_timestamp,
                    round((duration_ms + 50) * event_rate / 1000),
                )
            if destination_side == "left":
                self.left_dtmf_tx_events += 1
            else:
                self.right_dtmf_tx_events += 1
            _LOGGER.info(
                "RTP DTMF relayed destination=%s digit=%s payload=%s rate=%s",
                destination_side,
                digit,
                destination.outbound_dtmf_payload_type,
                event_rate,
            )

    def handle_packet(self, side: str, data: bytes, addr) -> None:
        source = self.left if side == "left" else self.right
        dest = self.right if side == "left" else self.left
        transport = self.right_transport if side == "left" else self.left_transport
        if transport is None:
            self.dropped += 1
            return
        if not source.accepts_source_host(str(addr[0])):
            self.dropped += 1
            _LOGGER.debug("RTP relay rejected packet from unexpected %s:%s", addr[0], addr[1])
            return
        if dest.connection_held:
            self.dropped += 1
            self.drop_connection_hold += 1
            _LOGGER.debug("RTP relay suppressed packet toward held connection")
            return
        if not source.can_send or not dest.can_receive:
            self.dropped += 1
            _LOGGER.debug("RTP relay rejected packet: media direction is not negotiated")
            return
        dtmf_decoder = self._dtmf_decoders[side]
        shared_dtmf_clock = (
            source.dtmf_clock_rate == source.inbound_rtp_format.rtp_clock_rate
        )
        if dtmf_decoder is not None and (
            digit := dtmf_decoder.decode(
                data,
                expected_ssrc=source.rx_ssrc if shared_dtmf_clock else None,
            )
        ):
            if source.rx_ssrc is None and shared_dtmf_clock:
                source.rx_ssrc = dtmf_decoder.ssrc
                source.host = str(addr[0])
                source.port = int(addr[1])
            elif (
                source.rx_ssrc is not None
                and shared_dtmf_clock
                and (str(addr[0]), int(addr[1]))
                != (source.host, int(source.port))
            ):
                source.host = str(addr[0])
                source.port = int(addr[1])
            if self.on_dtmf is not None:
                try:
                    self.on_dtmf(side, digit, "rtp_event")
                except Exception as err:  # noqa: BLE001 - event consumers cannot break RTP.
                    _LOGGER.warning("RTP relay DTMF callback failed: %s", err)
            self.relay_dtmf(side, digit)
            return
        try:
            packet = rtp.parse_packet(data)
            if packet.payload_type != source.payload_type:
                raise ValueError(f"payload type {packet.payload_type} != expected {source.payload_type}")
            if source.rx_ssrc is not None and packet.ssrc != source.rx_ssrc:
                raise ValueError(f"SSRC {packet.ssrc} != latched {source.rx_ssrc}")
            decoder = self.left_decoder if side == "left" else self.right_decoder
            pcm = decoder.decode(packet.payload)
            if not pcm:
                return
            if source.rx_ssrc is None:
                source.rx_ssrc = packet.ssrc
                source.host = str(addr[0])
                source.port = int(addr[1])
            elif (str(addr[0]), int(addr[1])) != (source.host, int(source.port)):
                # Symmetric RTP: follow a valid same-SSRC NAT tuple rebind.
                source.host = str(addr[0])
                source.port = int(addr[1])
            self._capture_pcm(side, pcm)
            converter = self.left_to_right if side == "left" else self.right_to_left
            converted_frames = converter.convert(pcm)
            encoder = self.right_encoder if side == "left" else self.left_encoder
            outgoing: list[bytes] = []
            sequence = dest.sequence
            timestamp = dest.timestamp
            for frame in converted_frames:
                outgoing.append(
                    rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=dest.outbound_payload_type,
                            sequence=sequence,
                            timestamp=timestamp,
                            ssrc=dest.ssrc,
                            payload=encoder.encode(frame),
                        )
                    )
                )
                sequence = rtp.next_sequence(sequence)
                timestamp = rtp.next_timestamp(
                    timestamp,
                    dest.outbound_rtp_format.rtp_timestamp_step,
                )
        except Exception as err:
            self.dropped += 1
            _LOGGER.debug("RTP relay drop: %s", err)
            return
        if side == "left":
            self.left_rx_packets += 1
            self.left_rx_bytes += len(data)
        else:
            self.right_rx_packets += 1
            self.right_rx_bytes += len(data)
        destination_side = "right" if side == "left" else "left"
        for out in outgoing:
            self._queue_audio_packet(destination_side, out)
        dest.sequence = sequence
        dest.timestamp = timestamp

    def snapshot(self) -> dict[str, Any]:
        snapshot = {
            "left_port": self.left_port,
            "right_port": self.right_port,
            "left_peer": f"{self.left.host}:{self.left.port}",
            "right_peer": f"{self.right.host}:{self.right.port}",
            "forwarded_packets": self.forwarded,
            "dropped_packets": self.dropped,
            "drop_connection_hold": self.drop_connection_hold,
            "debug_capture_dropped_writes": self.debug_capture_dropped_writes,
            "left_rx_packets": self.left_rx_packets,
            "left_rx_bytes": self.left_rx_bytes,
            "left_tx_packets": self.left_tx_packets,
            "left_tx_bytes": self.left_tx_bytes,
            "right_rx_packets": self.right_rx_packets,
            "right_rx_bytes": self.right_rx_bytes,
            "right_tx_packets": self.right_tx_packets,
            "right_tx_bytes": self.right_tx_bytes,
            "pacing_dropped_packets": self.pacing_dropped_packets,
            "left_pacer_queue_depth": (
                self._audio_queues["left"].qsize()
                if "left" in self._audio_queues
                else 0
            ),
            "right_pacer_queue_depth": (
                self._audio_queues["right"].qsize()
                if "right" in self._audio_queues
                else 0
            ),
            "left_dtmf_tx_events": self.left_dtmf_tx_events,
            "right_dtmf_tx_events": self.right_dtmf_tx_events,
            "left_rx_format": self.left.audio_format.wire_token(),
            "left_tx_format": self.left.outbound_audio_format.wire_token(),
            "right_rx_format": self.right.audio_format.wire_token(),
            "right_tx_format": self.right.outbound_audio_format.wire_token(),
            "left_direction": self._peer_direction(self.left),
            "right_direction": self._peer_direction(self.right),
            "left_connection_held": self.left.connection_held,
            "right_connection_held": self.right.connection_held,
        }
        if self.video_relay is not None:
            snapshot["video"] = self.video_relay.snapshot()
        return snapshot

    @staticmethod
    def _peer_direction(peer: RtpPeer) -> str:
        if peer.can_send and peer.can_receive:
            return "sendrecv"
        if peer.can_send:
            return "sendonly"
        if peer.can_receive:
            return "recvonly"
        return "inactive"
