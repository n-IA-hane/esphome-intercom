"""Shared local SIP audio transport for announcements and Assist."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
import contextlib
import logging
import secrets
from typing import Any, TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .core import rtp
from .core.audio_format import AudioFormat
from .core.audio_pcm import PcmFrameConverter
from .queue_utils import put_drop_oldest
from .sip_client import RtpPayloadDecoder, RtpPayloadEncoder
from .sip_listener import SipInvite

if TYPE_CHECKING:
    from .media_ports import RtpPortReservation

_LOGGER = logging.getLogger(__name__)

LOCAL_PCM_FORMAT = AudioFormat(16000, "s16le", 1, 20)
_RX_QUEUE_FRAMES = 50
_TX_QUEUE_FRAMES = 50


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
        invite: SipInvite,
        local_rtp_port: int,
        reservation: RtpPortReservation,
        on_complete: Callable[[str], Awaitable[None]],
    ) -> None:
        self.hass = hass
        self.invite = invite
        self.local_rtp_port = int(local_rtp_port)
        self.reservation = reservation
        self.release_reservation_on_stop = True
        self.on_complete = on_complete
        self.on_dtmf = None
        self._dtmf_sdp = None
        self._dtmf_decoder = None
        self._dtmf_events = frozenset()

        self.transport: asyncio.DatagramTransport | None = None
        self.closed = asyncio.Event()
        self.rx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_RX_QUEUE_FRAMES)
        self.tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_TX_QUEUE_FRAMES)
        self.decoder = RtpPayloadDecoder(invite.recv_format)
        self.encoder = RtpPayloadEncoder(invite.send_format)
        self.rx_converter = PcmFrameConverter(
            invite.recv_format.audio_format, LOCAL_PCM_FORMAT
        )
        self.tx_converter = PcmFrameConverter(
            LOCAL_PCM_FORMAT, invite.send_format.audio_format
        )
        self.sequence = secrets.randbelow(0x10000)
        self.timestamp = secrets.randbelow(0x100000000)
        self.ssrc = secrets.randbelow(0x100000000)
        self.remote_rtp_port = int(invite.remote_rtp_port)
        self.remote_ssrc: int | None = None
        self._consumer_task: asyncio.Task | None = None
        self._tx_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._cleanup_done = asyncio.Event()
        self._completed = False
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

    def prepare_media_update(self, updated: SipInvite) -> Callable[[], None]:
        """Prepare an atomic in-dialog audio update for the Local call RTP leg."""

        previous = self.invite
        if updated.call_id != previous.call_id:
            raise ValueError("Assist media update belongs to another call")
        if self.closed.is_set() or self._cleanup_done.is_set():
            raise RuntimeError("Local call media session is already closed")

        # Codec construction and PCM converter validation may fail.  Do all of
        # that work before returning the SIP 200 so the active RTP contract is
        # left untouched when the new offer cannot be supported.
        decoder = RtpPayloadDecoder(updated.recv_format)
        encoder = RtpPayloadEncoder(updated.send_format)
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

        return _commit

    async def start(self) -> None:
        """Bind RTP and start the persistent pipeline/media tasks."""
        async with self._start_lock:
            if self.transport is not None:
                return
            if self.closed.is_set() or self._cleanup_done.is_set():
                raise RuntimeError("Local call media session is already closed")
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _LocalRtpProtocol(self),
                local_addr=("0.0.0.0", self.local_rtp_port),
            )
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
                # These resources are synchronous to release.  Keep them in a
                # finally block so cancellation of a Home Assistant shutdown
                # cannot strand the reserved RTP port behind a closed flag.
                if self.transport is not None:
                    self.transport.close()
                    self.transport = None
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
            if packet.payload_type != self.invite.recv_format.payload_type:
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
            pcm = self.decoder.decode(packet.payload)
            if not pcm:
                return
            self.counters["rtp_rx"] += 1
            if not self._accepting_input:
                self.counters["rx_suppressed"] += 1
                return
            for frame in self.rx_converter.convert(pcm):
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
                        packet = rtp.build_packet(
                            rtp.RtpPacket(
                                payload_type=invite.send_format.payload_type,
                                sequence=self.sequence,
                                timestamp=self.timestamp,
                                ssrc=self.ssrc,
                                payload=payload,
                            )
                        )
                        if self.transport is not None:
                            self.transport.sendto(
                                packet,
                                (invite.remote_rtp_host, remote_rtp_port),
                            )
                            self.counters["rtp_tx"] += 1
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
                self.sequence = rtp.next_sequence(self.sequence)
                self.timestamp = rtp.next_timestamp(
                    self.timestamp,
                    invite.send_format.rtp_timestamp_step,
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

    async def play_pcm_stream(self, chunks: AsyncGenerator[bytes]) -> None:
        """Pace a provider PCM stream through the existing RTP output queue."""
        pending = bytearray()
        frame_bytes = LOCAL_PCM_FORMAT.nominal_frame_bytes
        async for chunk in chunks:
            if self.closed.is_set():
                return
            pending.extend(chunk)
            while len(pending) >= frame_bytes:
                source_frame = bytes(pending[:frame_bytes])
                del pending[:frame_bytes]
                for frame in self.tx_converter.convert(source_frame):
                    await self.tx_queue.put(frame)
        if pending:
            pending.extend(bytes(frame_bytes - len(pending)))
            for frame in self.tx_converter.convert(bytes(pending)):
                await self.tx_queue.put(frame)

    def start_consumer(self, consumer: Callable[[], Awaitable[None]]) -> None:
        """Attach one audio consumer without replacing the RTP transport."""
        if self.closed.is_set() or (
            self._consumer_task is not None and not self._consumer_task.done()
        ):
            raise RuntimeError("Local call media already has a consumer or is closed")
        self._consumer_task = self.hass.async_create_task(consumer())

    def _start_application(self) -> None:
        """Attach the initial consumer under the transport startup lock."""

    def discard_output(self) -> None:
        """Remove interrupted playback before another application takes over."""
        while not self.tx_queue.empty():
            self.tx_queue.get_nowait()
            self.tx_queue.task_done()
        self.tx_converter = PcmFrameConverter(
            LOCAL_PCM_FORMAT, self.invite.send_format.audio_format
        )
