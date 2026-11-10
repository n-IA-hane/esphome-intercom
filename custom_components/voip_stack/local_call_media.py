"""Shared local SIP audio transport for announcements and Assist."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import AsyncGenerator, Awaitable, Callable
import contextlib
import logging
import socket
from typing import Any, TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .core import rtp
from .core.audio_format import AudioFormat
from .core.audio_pcm import PcmFrameConverter
from .queue_utils import put_drop_oldest
from .session_cleanup import async_wait_for_cleanup
from .sip_client import RtpPayloadEncoder
from .rtp_audio_receiver import RtpAudioReceiver
from .sip_listener import SipInvite

if TYPE_CHECKING:
    from .media_ports import RtpPortReservation

_LOGGER = logging.getLogger(__name__)

LOCAL_PCM_FORMAT = AudioFormat(16000, "s16le", 1, 20)
_RX_QUEUE_FRAMES = 50
_TX_QUEUE_FRAMES = 50
# SIP has no playout-complete acknowledgement. Keep the existing paced RTP
# silence flowing briefly after outgoing announcements so a normal peer jitter
# buffer can render the final speech before BYE. Baresip defaults to 100-200 ms.
_OUTGOING_PLAYOUT_TAIL_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class LocalAudioContract:
    """Negotiated media for a local application, independent of SIP direction."""

    call_id: str
    remote_rtp_host: str
    remote_rtp_port: int
    send_format: Any
    recv_format: Any
    recv_formats: tuple = ()
    send_formats: tuple = ()
    match_received_codec: bool = False
    local_audio_direction: str = "sendrecv"
    remote_audio_connection_held: bool = False
    remote_sdp: str = ""
    dtmf_payload_type: int | None = None
    dtmf_clock_rate: int = 8000
    dtmf_events: frozenset[int] = frozenset()

    @classmethod
    def from_dialog(cls, dialog):
        return cls(**{name: getattr(dialog, name) for name in cls.__dataclass_fields__ if name != "remote_sdp"})


@dataclass(frozen=True, slots=True)
class ApplicationCallSource(LocalAudioContract):
    """An already established UAC peer retained by a PBX application.

    These are media and routing fields from the actual dialog, not an incoming
    SIP request. The existing client remains responsible for signaling.
    """

    caller: str = ""
    routing_caller: str = ""
    target: str = ""
    source_host: str = ""
    source_port: int = 0
    remote_audio_direction: str = "sendrecv"
    video_format: Any = None
    send_video_format: Any = None
    recv_video_format: Any = None
    answer_video_format: Any = None
    remote_video_rtp_host: str = ""
    remote_video_rtp_port: int = 0
    remote_video_rtcp_host: str = ""
    remote_video_rtcp_port: int = 0
    remote_video_rtcp_mux: bool = False
    remote_video_payload_types: tuple[int, ...] = ()
    remote_video_connection_held: bool = False
    peer_profile: str = ""
    received_via_trunk: bool = False
    trunk_id: str = ""
    send_dtmf_payload_type: int | None = None
    send_dtmf_clock_rate: int | None = None
    send_dtmf_events: frozenset[int] | None = None

    @classmethod
    def from_client(cls, client, *, call_id, caller, target="", trunk_id="", allow_video=False):
        return cls.from_peer_dialog(client.dialog, call_id=call_id, caller=caller, target=target,
                                    trunk_id=trunk_id, allow_video=allow_video)

    @classmethod
    def from_peer_dialog(cls, dialog, *, call_id, caller, target="", trunk_id="", allow_video=False):
        from .core.sip import parse_sip_uri

        if dialog is None or (dialog.video_format is not None and not allow_video):
            raise ValueError("Retaining an application caller requires an established audio dialog")
        base = LocalAudioContract.from_dialog(dialog)
        fields = {name: getattr(base, name) for name in LocalAudioContract.__dataclass_fields__}
        fields["call_id"] = call_id
        return cls(**fields, caller=caller, routing_caller=parse_sip_uri(dialog.remote_uri).user,
            target=target, source_host=dialog.remote_host, source_port=dialog.remote_sip_port,
            received_via_trunk=bool(trunk_id), trunk_id=trunk_id,
            remote_audio_direction=dialog.remote_audio_direction,
            video_format=dialog.video_format, send_video_format=dialog.send_video_format,
            recv_video_format=dialog.recv_video_format, answer_video_format=dialog.recv_video_format,
            remote_video_rtp_host=dialog.remote_video_rtp_host, remote_video_rtp_port=dialog.remote_video_rtp_port,
            remote_video_rtcp_host=dialog.remote_video_rtcp_host, remote_video_rtcp_port=dialog.remote_video_rtcp_port,
            remote_video_rtcp_mux=dialog.remote_video_rtcp_mux, remote_video_payload_types=dialog.remote_video_payload_types,
            remote_video_connection_held=dialog.remote_video_connection_held,
            send_dtmf_payload_type=dialog.send_dtmf_payload_type,
            send_dtmf_clock_rate=dialog.send_dtmf_clock_rate,
            send_dtmf_events=dialog.send_dtmf_events)


def source_dtmf_formats(source):
    """Read negotiated events from a dialog contract or an actual SIP offer."""
    from .core.sdp import RtpDtmfFormat, offered_dtmf_formats

    if isinstance(source, LocalAudioContract):
        return (RtpDtmfFormat(source.dtmf_payload_type, source.dtmf_clock_rate, source.dtmf_events),) if source.dtmf_payload_type is not None else ()
    return tuple(offered_dtmf_formats(source.remote_sdp))


class _LocalRtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, session: "LocalCallMedia") -> None:
        self.session = session

    def datagram_received(self, data: bytes, addr) -> None:
        self.session.handle_rtp(data, addr)


class LocalCallMedia:
    """Own one local SIP audio transport independently of its application."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        invite: SipInvite | LocalAudioContract,
        local_rtp_port: int,
        reservation: RtpPortReservation,
        on_complete: Callable[[str], Awaitable[None]],
        rtp_source: rtp.AudioRtpSenderState | None = None,
        rtp_socket: socket.socket | None = None,
    ) -> None:
        self.hass = hass
        self.invite = invite
        self.local_rtp_port = int(local_rtp_port)
        self.reservation = reservation
        self.video_relay = None
        self.release_reservation_on_stop = True
        self.on_complete = on_complete
        self.on_dtmf = None
        self._dtmf_sdp = None
        self._dtmf_decoder = None
        self._dtmf_events = frozenset()

        self.transport: asyncio.DatagramTransport | None = None
        self._rtp_socket = rtp_socket
        self.closed = asyncio.Event()
        self.rx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_RX_QUEUE_FRAMES)
        self.tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_TX_QUEUE_FRAMES)
        self.decoder = RtpAudioReceiver(invite.recv_format, tuple(getattr(invite, "recv_formats", ())))
        self.encoder = RtpPayloadEncoder(invite.send_format, send_formats=getattr(invite, "send_formats", ()), match_received_codec=getattr(invite, "match_received_codec", False))
        self.rx_converter = PcmFrameConverter(
            invite.recv_format.audio_format, LOCAL_PCM_FORMAT
        )
        self.tx_converter = PcmFrameConverter(
            LOCAL_PCM_FORMAT, invite.send_format.audio_format
        )
        self.rtp_source = rtp_source or rtp.AudioRtpSenderState.create()
        self.remote_rtp_port = int(invite.remote_rtp_port)
        self.remote_ssrc: int | None = None
        self._consumer_task: asyncio.Task | None = None
        self._tx_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._cleanup_done = asyncio.Event()
        self._completed = False
        self._last_output_deadline = 0.0
        self._accepting_input = False
        self.can_receive = invite.local_audio_direction in {"recvonly", "sendrecv"}
        self.can_send = (
            invite.local_audio_direction in {"sendonly", "sendrecv"}
            and not invite.remote_audio_connection_held
        )
        self.counters = {
            "rtp_rx": 0,
            "rtp_tx": 0,
            "drop_addr": 0,
            "drop_payload_type": 0,
            "drop_ssrc": 0,
            "drop_decode": 0,
            "drop_rx_queue": 0,
            "rx_suppressed": 0,
            "tx_error": 0,
            "tx_silence": 0,
            "tx_suppressed": 0,
            "drop_direction_rx": 0,
            "drop_connection_hold": 0,
            "pipeline_runs": 0,
            "speech_gate_opens": 0,
        }

    def prepare_media_update(self, updated: SipInvite | LocalAudioContract) -> Callable[[], None]:
        """Prepare an atomic in-dialog audio update for the Local call RTP leg."""

        previous = self.invite
        if updated.call_id != previous.call_id:
            raise ValueError("Assist media update belongs to another call")
        if self.closed.is_set() or self._cleanup_done.is_set():
            raise RuntimeError("Local call media session is already closed")

        # Codec construction and PCM converter validation may fail.  Do all of
        # that work before returning the SIP 200 so the active RTP contract is
        # left untouched when the new offer cannot be supported.
        decoder = RtpAudioReceiver(
            updated.recv_format, tuple(getattr(updated, "recv_formats", ())),
            previous=self.decoder,
        )
        encoder = RtpPayloadEncoder(updated.send_format, send_formats=getattr(updated, "send_formats", ()), match_received_codec=getattr(updated, "match_received_codec", False))
        rx_converter = PcmFrameConverter(
            updated.recv_format.audio_format, LOCAL_PCM_FORMAT
        )
        tx_converter = PcmFrameConverter(
            LOCAL_PCM_FORMAT, updated.send_format.audio_format
        )
        can_receive = updated.local_audio_direction in {"recvonly", "sendrecv"}
        can_send = (
            updated.local_audio_direction in {"sendonly", "sendrecv"}
            and not updated.remote_audio_connection_held
        )
        reset_remote_source = (
            updated.remote_rtp_host != previous.remote_rtp_host
            or int(updated.remote_rtp_port) != int(previous.remote_rtp_port)
            or updated.recv_format != previous.recv_format
        )

        def _commit() -> None:
            if self.closed.is_set() or self._cleanup_done.is_set():
                raise RuntimeError("Local call media session ended before media commit")
            if self.invite is not previous:
                raise RuntimeError("Assist media contract changed before commit")
            self.decoder = decoder
            self.encoder = encoder
            self.rx_converter = rx_converter
            self.tx_converter = tx_converter
            self.remote_rtp_port = int(updated.remote_rtp_port)
            if reset_remote_source:
                self.remote_ssrc = None
            self.can_receive = can_receive
            self.can_send = can_send
            self.invite = updated
            self._dtmf_sdp = None

        return _commit

    async def start(self) -> None:
        """Bind RTP and start the persistent pipeline/media tasks."""
        async with self._start_lock:
            if self.transport is not None:
                return
            if self.closed.is_set() or self._cleanup_done.is_set():
                raise RuntimeError("Local call media session is already closed")
            loop = asyncio.get_running_loop()
            bound_socket, self._rtp_socket = self._rtp_socket, None
            if bound_socket is None:
                bound_socket = self.reservation.take_audio_socket(self.local_rtp_port)
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _LocalRtpProtocol(self),
                    **({"sock": bound_socket} if bound_socket is not None else
                       {"local_addr": ("0.0.0.0", self.local_rtp_port)}),
                )
            except BaseException:
                if bound_socket is not None:
                    bound_socket.close()
                raise
            # stop() deliberately does not wait behind a potentially blocked
            # socket bind. If shutdown won the race, close the acquired
            # transport before it can publish tasks behind cleanup_done.
            if self.closed.is_set() or self._cleanup_done.is_set():
                transport.close()
                raise RuntimeError("Local call media session closed while starting")
            self.transport = transport  # type: ignore[assignment]
            self._tx_task = self.hass.async_create_task(self._send_loop())
            self._start_application()
        _LOGGER.info(
            "Local call media session started call_id=%s local_rtp=%s remote=%s:%s tx=%s rx=%s",
            self.invite.call_id,
            self.local_rtp_port,
            self.invite.remote_rtp_host,
            self.invite.remote_rtp_port,
            self.invite.send_format.wire_token(),
            self.invite.recv_format.wire_token(),
        )

    def detach_transport(self):
        """Give the bound RTP socket to a prepared successor without rebinding."""
        if self.closed.is_set() or self.transport is None or self.transport.is_closing():
            raise RuntimeError("Local media transport is no longer available")
        transport = self.transport
        self.closed.set()
        self.transport = None
        self.release_reservation_on_stop = False
        return transport

    def adopt_transport(self, transport):
        """Start application playback on an already owned, bound RTP socket."""
        if self.transport is not None or self.closed.is_set() or transport.is_closing():
            raise RuntimeError("Local media cannot adopt this transport")
        previous = transport.get_protocol()
        try:
            transport.set_protocol(_LocalRtpProtocol(self))
            self.transport = transport
            self._tx_task = self.hass.async_create_task(self._send_loop())
            self._start_application()
        except BaseException:
            self.transport = None
            transport.set_protocol(previous)
            if self._tx_task is not None:
                self._tx_task.cancel()
            raise

    async def stop(self) -> None:
        """Stop pipeline and RTP exactly once."""
        async with self._stop_lock:
            if self._cleanup_done.is_set():
                return
            self.closed.set()
            current = asyncio.current_task()
            tasks = [self._consumer_task, self._tts_task, self._tx_task]
            for task in tasks:
                if task is not None and task is not current and not task.done():
                    task.cancel()
            try:
                await asyncio.gather(
                    *(
                        task
                        for task in tasks
                        if task is not None and task is not current
                    ),
                    return_exceptions=True,
                )
            finally:
                video, self.video_relay = self.video_relay, None
                try:
                    if video is not None:
                        await async_wait_for_cleanup(asyncio.create_task(video.stop(), name="voip-application-video-stop"))
                finally:
                    # Cancellation must not strand either owned media child.
                    if self.transport is not None:
                        self.transport.close()
                        self.transport = None
                    if self._rtp_socket is not None:
                        self._rtp_socket.close()
                        self._rtp_socket = None
                    if self.release_reservation_on_stop:
                        self.reservation.release()
                    self._cleanup_done.set()
                    _LOGGER.info(
                        "Local call media session stopped call_id=%s counters=%s",
                        self.invite.call_id,
                        self.counters,
                    )

    def handle_rtp(self, data: bytes, addr) -> None:
        """Decode one negotiated RTP packet and enqueue pipeline PCM."""
        if self.closed.is_set():
            return
        if not self.can_receive:
            self.counters["drop_direction_rx"] += 1
            return
        if str(addr[0]) != self.invite.remote_rtp_host:
            self.counters["drop_addr"] += 1
            return
        try:
            packet = rtp.parse_packet(data)
            if self.on_dtmf is not None:
                if self._dtmf_sdp != self.invite.remote_sdp:
                    from .core.sdp import offered_dtmf_formats
                    from .dtmf import RtpDtmfDecoder

                    if isinstance(self.invite, LocalAudioContract):
                        from .core.sdp import RtpDtmfFormat
                        formats = [RtpDtmfFormat(
                            self.invite.dtmf_payload_type, self.invite.dtmf_clock_rate,
                            self.invite.dtmf_events,
                        )] if self.invite.dtmf_payload_type is not None else []
                    else:
                        formats = offered_dtmf_formats(self.invite.remote_sdp)
                    self._dtmf_sdp = self.invite.remote_sdp
                    self._dtmf_decoder = RtpDtmfDecoder(formats[0].payload_type) if formats else None
                    self._dtmf_events = formats[0].events if formats else frozenset()
                if self._dtmf_decoder is not None and packet.payload_type == self._dtmf_decoder.payload_type:
                    from .dtmf import telephone_event_code

                    digit = self._dtmf_decoder.decode(data)
                    if digit and telephone_event_code(digit) in self._dtmf_events:
                        self.on_dtmf("left", digit, "rtp_event")
                    return
            if self.decoder.format_for(packet.payload_type) is None:
                self.counters["drop_payload_type"] += 1
                return
            if self.remote_ssrc is None:
                self.remote_ssrc = packet.ssrc
                self.remote_rtp_port = int(addr[1])
            elif packet.ssrc != self.remote_ssrc:
                self.counters["drop_ssrc"] += 1
                return
            elif int(addr[1]) != self.remote_rtp_port:
                self.remote_rtp_port = int(addr[1])
            pcm_frames = self.decoder.decode(packet.payload_type, packet.payload)
            self.encoder.follow_received(self.decoder.format_for(packet.payload_type))
            if not pcm_frames:
                return
            self.counters["rtp_rx"] += 1
            if not self._accepting_input:
                self.counters["rx_suppressed"] += 1
                return
            for frame in (frame for pcm in pcm_frames for frame in self.rx_converter.convert(pcm)):
                if put_drop_oldest(self.rx_queue, frame):
                    self.counters["drop_rx_queue"] += 1
        except Exception as err:  # noqa: BLE001 - malformed media cannot end the call.
            self.counters["drop_decode"] += 1
            _LOGGER.debug(
                "Local call RTP RX drop call_id=%s: %s", self.invite.call_id, err
            )

    async def _send_loop(self) -> None:
        """Maintain the RTP clock and stream TTS frames as soon as they arrive."""
        loop = asyncio.get_running_loop()
        next_send = loop.time()
        active_format = None
        silence = b""
        try:
            while not self.closed.is_set():
                delay = next_send - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if self.closed.is_set():
                    break
                # Snapshot the committed contract only after the pacing await.
                # A re-INVITE may commit while this task sleeps; pairing the
                # current encoder, payload type and destination prevents one
                # mixed old/new RTP packet at that boundary.
                invite = self.invite
                encoder = self.encoder
                remote_rtp_port = self.remote_rtp_port
                frame_format = invite.send_format.audio_format
                if frame_format != active_format:
                    active_format = frame_format
                    silence = bytes(frame_format.nominal_frame_bytes)
                    next_send = max(next_send, loop.time())
                frame_delay = max(0.001, frame_format.frame_ms / 1000.0)
                queued = True
                try:
                    pcm = self.tx_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pcm = silence
                    queued = False
                    self.counters["tx_silence"] += 1
                try:
                    if not self.can_send:
                        self.counters["tx_suppressed"] += 1
                        if self.invite.remote_audio_connection_held:
                            self.counters["drop_connection_hold"] += 1
                    else:
                        payload = encoder.encode(pcm)
                        wire_format = encoder.fmt
                        self.rtp_source.use_clock_rate(wire_format.rtp_clock_rate)
                        packet = rtp.build_packet(
                            rtp.RtpPacket(
                                payload_type=wire_format.payload_type,
                                sequence=self.rtp_source.sequence,
                                timestamp=self.rtp_source.timestamp,
                                ssrc=self.rtp_source.ssrc,
                                payload=payload,
                            )
                        )
                        if self.transport is not None:
                            self.transport.sendto(
                                packet,
                                (invite.remote_rtp_host, remote_rtp_port),
                            )
                            self.counters["rtp_tx"] += 1
                            if queued:
                                self._last_output_deadline = loop.time() + frame_delay
                except Exception as err:  # noqa: BLE001 - keep the media clock alive.
                    self.counters["tx_error"] += 1
                    _LOGGER.debug(
                        "Local call RTP TX drop call_id=%s: %s",
                        self.invite.call_id,
                        err,
                    )
                finally:
                    if queued:
                        self.tx_queue.task_done()
                self.rtp_source.sequence = rtp.next_sequence(self.rtp_source.sequence)
                self.rtp_source.timestamp = rtp.next_timestamp(
                    self.rtp_source.timestamp,
                    encoder.fmt.rtp_timestamp_step,
                )
                next_send += frame_delay
                if next_send <= loop.time():
                    next_send = loop.time() + frame_delay
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _tts_audio_output() -> dict[str, Any]:
        from homeassistant.components import tts

        return {
            tts.ATTR_PREFERRED_FORMAT: "s16le",
            tts.ATTR_PREFERRED_SAMPLE_RATE: 16000,
            tts.ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
            tts.ATTR_PREFERRED_SAMPLE_BYTES: 2,
        }

    def _drain_rx(self) -> None:
        while not self.rx_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.rx_queue.get_nowait()

    async def input_pcm_stream(self):
        """Let one application consume the existing bounded decoded input."""
        if self._accepting_input:
            raise RuntimeError("Local call audio already has an input consumer")
        self._drain_rx()
        self._accepting_input = True
        try:
            while not self.closed.is_set():
                yield await self.rx_queue.get()
        finally:
            self._accepting_input = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "call_id": self.invite.call_id,
            "local_rtp_port": self.local_rtp_port,
            "remote_rtp_host": self.invite.remote_rtp_host,
            "remote_rtp_port": self.remote_rtp_port,
            "local_audio_direction": self.invite.local_audio_direction,
            "remote_connection_held": self.invite.remote_audio_connection_held,
            **self.counters,
        }

    async def play_pcm_stream(self, chunks: AsyncGenerator[bytes], *, source_format=LOCAL_PCM_FORMAT) -> None:
        """Pace PCM through the call's negotiated output, including native WAV."""
        pending = bytearray()
        frame_bytes = source_format.nominal_frame_bytes
        converter = None

        def convert(frame):
            nonlocal converter
            destination = self.invite.send_format.audio_format
            if converter is None or converter.dst != destination:
                converter = PcmFrameConverter(source_format, destination)
            return converter.convert(frame)

        async for chunk in chunks:
            if self.closed.is_set():
                return
            pending.extend(chunk)
            while len(pending) >= frame_bytes:
                source_frame = bytes(pending[:frame_bytes])
                del pending[:frame_bytes]
                for frame in convert(source_frame):
                    await self.tx_queue.put(frame)
        if pending:
            pending.extend(bytes(frame_bytes - len(pending)))
            for frame in convert(bytes(pending)):
                await self.tx_queue.put(frame)

    def start_consumer(self, consumer: Callable[[], Awaitable[None]]) -> None:
        """Attach one audio consumer without replacing the RTP transport."""
        if self.closed.is_set() or (
            self._consumer_task is not None and not self._consumer_task.done()
        ):
            raise RuntimeError("Local call media already has a consumer or is closed")
        self._consumer_task = self.hass.async_create_task(consumer())

    async def stop_consumer(self):
        """Detach application work while retaining the same RTP transport."""
        tasks = (self._consumer_task, self._tts_task)
        self._consumer_task = self._tts_task = None
        for task in tasks:
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None and task is not asyncio.current_task()),
                             return_exceptions=True)
        self._accepting_input = False

    def _start_application(self) -> None:
        """Attach the initial consumer under the transport startup lock."""

    async def drain_output(self) -> None:
        """Drain the last frame and a bounded silent RTP tail for remote playout."""
        await self.tx_queue.join()
        remaining = self._last_output_deadline + _OUTGOING_PLAYOUT_TAIL_SECONDS - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)
        if self.closed.is_set() or not self.can_send or self.transport is None:
            raise RuntimeError("The call ended or stopped accepting announcement audio")

    def discard_output(self) -> None:
        """Remove interrupted playback before another application takes over."""
        while not self.tx_queue.empty():
            self.tx_queue.get_nowait()
            self.tx_queue.task_done()
        self.tx_converter = PcmFrameConverter(
            LOCAL_PCM_FORMAT, self.invite.send_format.audio_format
        )
