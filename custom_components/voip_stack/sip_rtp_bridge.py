"""Session-owned RTP relay/resampler for SIP PCM calls."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
import logging
from pathlib import Path
import secrets
import socket
from typing import Any, Awaitable, Callable
import wave

from .core import rtp
from .core.sip_stun import is_stun_message
from .core.audio_format import AudioFormat
from .core.audio_pcm import PcmFrameConverter
from .core.media.bridge import payload_relay_compatible, plan_audio_flow
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
    telephone_event_segments,
)
from .media_ports import bind_sip_audio_socket
from .core.sdp import RtpDtmfFormat, RtpPcmFormat, audio_format_to_rtp
from .session_cleanup import async_wait_for_cleanup
from .core.rtp_payload import RtpPayloadEncoder
from .rtp_audio_receiver import RtpAudioReceiver

_LOGGER = logging.getLogger(__name__)
_DEBUG_CAPTURE_SECONDS = 8
_DTMF_UPDATE_SECONDS = 0.05


@dataclass(slots=True)
class _PayloadRelayClock:
    source_base: int | None = None
    destination_base: int = 0
    source_format: RtpPcmFormat | None = None
    source_sequence: int | None = None
    destination_sequence: int = 0
    source_ssrc: int | None = None
    highest_sequence: int | None = None
    insertions: list = field(default_factory=list)

    def reset(self):
        self.source_base = self.source_sequence = None
        self.highest_sequence = None
        self.insertions.clear()

    def observe_format(self, fmt: RtpPcmFormat) -> None:
        """Preserve the source timeline while its RTP clock remains unchanged."""
        if self.source_format is None or fmt.rtp_clock_rate != self.source_format.rtp_clock_rate:
            self.source_base = None
        self.source_format = fmt

    def map(self, source_timestamp: int, destination_timestamp: int) -> int:
        if self.source_base is None:
            self.source_base = int(source_timestamp)
            self.destination_base = int(destination_timestamp)
        return (
            self.destination_base + (int(source_timestamp) - self.source_base)
        ) & 0xFFFFFFFF

    def map_sequence(self, packet, destination_sequence):
        """Preserve loss and reordering with one ingress-to-egress offset."""
        if self.source_ssrc != packet.ssrc or self.source_sequence is None:
            self.source_ssrc = packet.ssrc
            self.source_sequence = packet.sequence
            self.destination_sequence = destination_sequence
            self.source_base = None
            self.insertions.clear()
            self.highest_sequence = packet.sequence
        extended = self.highest_sequence + (
            (packet.sequence - self.highest_sequence + 32768) % 65536 - 32768)
        self.highest_sequence = max(extended, self.highest_sequence)
        while self.insertions and self.insertions[0][0] <= self.highest_sequence - 128:
            _boundary, count = self.insertions.pop(0)
            self.destination_sequence += count
        added = sum(count for boundary, count in self.insertions
            if extended >= boundary)
        return (self.destination_sequence + extended - self.source_sequence + added) & 65535

    def inserted_after(self, sequence, count=1):
        """Keep audio mapping stable when this source also carries local events."""
        if self.source_sequence is None or not count:
            return
        boundary = self.highest_sequence + (
            (sequence - self.highest_sequence + 32768) % 65536 - 32768) + 1
        if self.insertions and self.insertions[-1][0] == boundary:
            previous = self.insertions[-1]
            self.insertions[-1] = (boundary, previous[1] + count)
        else:
            self.insertions.append((boundary, count))
        if len(self.insertions) > 128:
            _boundary, count = self.insertions.pop(0)
            self.destination_sequence = (self.destination_sequence + count) & 65535

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
    inbound_dtmf_formats: tuple = ()
    outbound_dtmf_formats: tuple = ()
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
    inbound_rtp_formats: tuple[RtpPcmFormat, ...] = ()
    outbound_rtp_formats: tuple[RtpPcmFormat, ...] = ()
    match_received_codec: bool = False
    clock_rate: int = 0

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
    def __init__(self, relay: "SipRtpRelay", side: str, *, paused: bool = False) -> None:
        self.relay = relay
        self.side = side
        self.paused = paused
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        if self.paused:
            transport.pause_reading()

    def datagram_received(self, data: bytes, addr) -> None:
        self.relay.handle_packet(self.side, data, addr)


@asynccontextmanager
async def audio_reconfiguration_guard(*owners, deadline: float | None = None):
    """Finish current digits before a media owner commits or splices a leg.

    Ordering is global relay identity, then left/right. Cancellation and an
    optional absolute deadline belong to the caller; this guard never renews
    a SIP deadline. A relay's existing stop owner wakes pending acquisitions.
    """
    acquired = []
    relays = sorted({id(owner): owner for owner in owners if isinstance(owner, SipRtpRelay)}.values(), key=id)
    try:
        async with asyncio.timeout_at(deadline):
            for relay in relays:
                if relay._stop_requested or relay._released:
                    raise RuntimeError("RTP relay closed before media commit")
                for side in ("left", "right"):
                    lock = relay._dtmf_locks[side]
                    taking = asyncio.create_task(lock.acquire(), name="voip-media-dtmf-barrier")
                    stopping = asyncio.create_task(relay._stop_signal.wait(), name="voip-media-relay-stop")
                    retained = False
                    try:
                        await asyncio.wait((taking, stopping), return_when=asyncio.FIRST_COMPLETED)
                        if relay._stop_requested or relay._released or stopping.done():
                            raise RuntimeError("RTP relay closed while waiting for media commit")
                        await taking
                        acquired.append(lock)
                        retained = True
                    finally:
                        for task in (taking, stopping):
                            if not task.done():
                                task.cancel()
                        try:
                            await async_wait_for_cleanup(asyncio.gather(taking, stopping, return_exceptions=True))
                        finally:
                            if not retained and taking.done() and not taking.cancelled() and taking.result():
                                lock.release()
            yield
    finally:
        for lock in reversed(acquired):
            lock.release()


class SipRtpRelay:
    """Bidirectional RTP relay with explicit peer ownership.

    Each SIP leg keeps its negotiated PCM shape. HA converts between the two
    negotiated RTP formats when needed, including sample-rate and frame-size
    changes that are exact for the supported PCM profile.
    """

    @classmethod
    def splice_audio(cls, source, source_side: str, destination, destination_side: str, *, merge_reservations=None, commit_owner=None):
        """Move two established audio legs into one relay without rebinding UDP.

        Call ownership must be committed by the caller in the same event-loop
        turn. The old relays retain only the transferrer's sockets. Their port
        reservations move together and are released when this relay stops.
        A pool-backed caller may merge its reservations after transport
        preparation, returning the release callback for the retained pair.
        An optional synchronous owner commit runs before reservation transfer;
        callers must protect registry mutations with their session handoff.
        """
        if source is destination or source_side not in {"left", "right"} or destination_side not in {"left", "right"}:
            raise ValueError("splice requires two distinct relay legs")
        for relay in (source, destination):
            if relay._stop_requested or relay._start_task is not None or relay.video_relay is not None:
                raise ValueError("splice requires established audio-only relays")
        left = getattr(source, f"{source_side}_transport")
        right = getattr(destination, f"{destination_side}_transport")
        if left is None or right is None or left.is_closing() or right.is_closing():
            raise ValueError("splice transport is unavailable")
        previous_left, previous_right = left.get_protocol(), right.get_protocol()
        result = cls(
            left=replace(getattr(source, source_side)), right=replace(getattr(destination, destination_side)),
            left_port=getattr(source, f"{source_side}_port"),
            right_port=getattr(destination, f"{destination_side}_port"),
            debug=source.debug or destination.debug,
            debug_capture=source.debug_capture or destination.debug_capture,
            capture_name=f"splice-{source.left_port}-{destination.right_port}",
        )
        for previous, previous_side, next_side in ((source, source_side, "left"), (destination, destination_side, "right")):
            old_encoder = getattr(previous, f"{previous_side}_encoder")
            new_encoder = getattr(result, f"{next_side}_encoder")
            if old_encoder is not None and new_encoder is not None:
                new_encoder.follow_received(old_encoder.fmt)
        result.left_transport, result.right_transport = left, right
        try:
            left_protocol, right_protocol = _RelayProtocol(result, "left"), _RelayProtocol(result, "right")
            left_protocol.connection_made(left)
            right_protocol.connection_made(right)
            left.set_protocol(left_protocol)
            right.set_protocol(right_protocol)
            if commit_owner is not None:
                commit_owner(result)
            merged_release = merge_reservations(
                ((source.left_port, source.right_port), (destination.left_port, destination.right_port)),
                (result.left_port, result.right_port),
            ) if merge_reservations is not None else None
        except BaseException:
            left.set_protocol(previous_left)
            right.set_protocol(previous_right)
            result.left_transport = result.right_transport = None
            raise
        source_release, destination_release = source._on_release, destination._on_release
        source_ports = (source.left_port, source.right_port)
        destination_ports = (destination.left_port, destination.right_port)
        def release(_ports):
            try:
                if source_release is not None:
                    source_release(source_ports)
            finally:
                if destination_release is not None:
                    destination_release(destination_ports)
        result._on_release = merged_release if merge_reservations is not None else release
        source._on_release = destination._on_release = None
        setattr(source, f"{source_side}_transport", None)
        setattr(destination, f"{destination_side}_transport", None)
        source._request_stop()
        destination._request_stop()
        for relay in (source, destination):
            for side in ("left", "right"):
                relay._cancel_comfort_noise(side)
        return result

    def __init__(
        self,
        *,
        left: RtpPeer,
        right: RtpPeer,
        left_port: int,
        right_port: int,
        debug: bool = False,
        debug_capture: bool = False,
        capture_name: str = "",
        on_release: Callable[[tuple[int, int]], None] | None = None,
        on_dtmf: Callable[[str, str, str], None] | None = None,
        on_dtmf_fallback: Callable[[str, str], Awaitable[bool]] | None = None,
        on_media_route_changed: Callable[[str], None] | None = None,
    ) -> None:
        self.left = left
        self.right = right
        self.left_port = int(left_port)
        self.right_port = int(right_port)
        self.debug = bool(debug)
        self.debug_capture = bool(debug_capture)
        self.left_transport: asyncio.DatagramTransport | None = None
        self.right_transport: asyncio.DatagramTransport | None = None
        self._audio_sockets: tuple[socket.socket, ...] | None = None
        self._on_release = on_release
        self.on_dtmf = on_dtmf
        self.on_dtmf_fallback = on_dtmf_fallback
        self.on_media_route_changed = on_media_route_changed
        self._released = False
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._cn_timers: dict[str, asyncio.TimerHandle] = {}
        self._cn_generations = {"left": 0, "right": 0}
        self._stop_signal = asyncio.Event()
        self._dtmf_tasks: set[asyncio.Task[None]] = set()
        self._dtmf_locks = {"left": asyncio.Lock(), "right": asyncio.Lock()}
        self._receive_states = {"left": rtp.RtpReceiveState(), "right": rtp.RtpReceiveState()}
        self._dtmf_report_maps = {}
        self._audio_observer_roles = []
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

    def observe_audio(self, callback, on_error=None):
        """Tap native decoded samples while preserving RTP passthrough."""
        detach = []
        roles = {"left": "left", "right": "right"}
        self._audio_observer_roles.append(roles)
        for side in ("left", "right"):
            decoder = getattr(self, f"{side}_decoder")
            if decoder is None:
                peer = getattr(self, side)
                decoder = RtpAudioReceiver(peer.inbound_rtp_format, peer.inbound_rtp_formats,
                    receive_state=self._receive_states[side])
                setattr(self, f"{side}_decoder", decoder)
            detach.append(decoder.add_pcm_observer(
                lambda pcm, fmt, side=side: callback(roles[side], pcm, fmt)))
        self._recording_error = on_error
        self._bind_receive_callbacks()
        def remove():
            self._audio_observer_roles[:] = [item for item in self._audio_observer_roles if item is not roles]
            for unsubscribe in detach:
                unsubscribe()
            if getattr(self, "_recording_error", None) is on_error:
                self._recording_error = None
        return remove

    def reverse_held_sides(self):
        """Retain the opposite caller leg without replacing its media owners.

        The caller holds media_reconfiguration_guard and commits session roles
        in the same event-loop turn. Attached video retains its active workers.
        """
        if (self._stop_requested or self._released or self._start_task is not None
                or not all(lock.locked() for lock in self._dtmf_locks.values())
                or any(not task.done() for task in self._dtmf_tasks)):
            raise RuntimeError("Audio relay cannot change orientation during an active media operation")
        for side in ("left", "right"):
            transport = getattr(self, f"{side}_transport")
            if (transport is None or transport.is_closing()
                    or not isinstance(transport.get_protocol(), _RelayProtocol)
                    or transport.get_protocol().relay is not self
                    or transport.get_protocol().side != side):
                raise RuntimeError("The audio transport owner changed before handoff")
        if self.video_relay is not None:
            self.video_relay.reverse_held_sides()
        for handle in self._cn_timers.values():
            handle.cancel()
        self._cn_timers.clear()
        for side in ("left", "right"):
            self._cn_generations[side] += 1
        self.left, self.right = self.right, self.left
        for suffix in ("port", "transport", "decoder", "encoder", "rx_packets", "rx_bytes",
                       "tx_packets", "tx_bytes", "dtmf_tx_events"):
            left, right = f"left_{suffix}", f"right_{suffix}"
            previous = getattr(self, left)
            setattr(self, left, getattr(self, right))
            setattr(self, right, previous)
        for suffix in ("", "_passthrough", "_clock"):
            left, right = f"left_to_right{suffix}", f"right_to_left{suffix}"
            previous = getattr(self, left)
            setattr(self, left, getattr(self, right))
            setattr(self, right, previous)
        for mapping in (self._receive_states, self._dtmf_decoders, self._dtmf_locks,
                        self._cn_generations, self._capture_buffers, self._capture_paths,
                        self._capture_formats, self._capture_frames, self._capture_snapshot):
            has_left, has_right = "left" in mapping, "right" in mapping
            left, right = mapping.pop("left", None), mapping.pop("right", None)
            if has_right:
                mapping["left"] = right
            if has_left:
                mapping["right"] = left
        self._dtmf_report_maps = {
            ("right" if key[0] == "left" else "left", *key[1:]): value
            for key, value in self._dtmf_report_maps.items()}
        for roles in self._audio_observer_roles:
            roles["left"], roles["right"] = roles["right"], roles["left"]
        self._bind_receive_callbacks()
        for side in ("left", "right"):
            getattr(self, f"{side}_transport").get_protocol().side = side
            self._arm_comfort_noise(side)

    @property
    def media_route(self) -> str:
        """Describe the active audio processing, including capture conversion."""
        video_transcoding = self.video_relay is not None and self.video_relay.transcoding
        return "direct" if self.left_to_right_passthrough and self.right_to_left_passthrough and not video_transcoding else "ha_transcoding"

    def _build_media_state(self, left: RtpPeer, right: RtpPeer) -> dict[str, Any]:
        """Prepare converters without changing the live relay."""

        def receiver(side, peer, target):
            previous = getattr(self, f"{side}_decoder", None)
            if previous is None and hasattr(self, "left_decoder"):
                old_peer = getattr(self, side)
                previous = RtpAudioReceiver(old_peer.inbound_rtp_format, old_peer.inbound_rtp_formats)
            return RtpAudioReceiver(peer.inbound_rtp_format, peer.inbound_rtp_formats,
                previous=previous, receive_state=self._receive_states[side],
                output_pcm=target.outbound_rtp_format.audio_format)

        left_receiver = receiver("left", left, right)
        right_receiver = receiver("right", right, left)
        left_to_right_passthrough = bool(
            not self.debug_capture
            and not right.match_received_codec
            and len(left_receiver.accepted_formats) == 1
            and plan_audio_flow(
                left.inbound_rtp_format,
                right.outbound_rtp_format,
            ).payload_relay
        )
        right_to_left_passthrough = bool(
            not self.debug_capture
            and not left.match_received_codec
            and len(right_receiver.accepted_formats) == 1
            and plan_audio_flow(
                right.inbound_rtp_format,
                left.outbound_rtp_format,
            ).payload_relay
        )
        return {
            "left_to_right_passthrough": left_to_right_passthrough,
            "right_to_left_passthrough": right_to_left_passthrough,
            "left_to_right_clock": _PayloadRelayClock(),
            "right_to_left_clock": _PayloadRelayClock(),
            "left_to_right": None
            if left_to_right_passthrough
            else PcmFrameConverter(left_receiver.output_pcm, right.outbound_rtp_format.audio_format),
            "right_to_left": None
            if right_to_left_passthrough
            else PcmFrameConverter(right_receiver.output_pcm, left.outbound_rtp_format.audio_format),
            "left_decoder": None
            if left_to_right_passthrough and not left_receiver.has_pcm_observers
            else left_receiver,
            "right_decoder": None
            if right_to_left_passthrough and not right_receiver.has_pcm_observers
            else right_receiver,
            "left_encoder": None
            if right_to_left_passthrough
            else RtpPayloadEncoder(left.outbound_rtp_format, send_formats=left.outbound_rtp_formats, match_received_codec=left.match_received_codec),
            "right_encoder": None
            if left_to_right_passthrough
            else RtpPayloadEncoder(right.outbound_rtp_format, send_formats=right.outbound_rtp_formats, match_received_codec=right.match_received_codec),
            "dtmf_decoders": {
                "left": RtpDtmfDecoder(left.inbound_dtmf_formats or
                    (RtpDtmfFormat(left.dtmf_payload_type, left.dtmf_clock_rate, left.dtmf_events),),
                    audio_receiver=left_receiver)
                    if left.inbound_dtmf_formats or left.dtmf_payload_type is not None else None,
                "right": RtpDtmfDecoder(right.inbound_dtmf_formats or
                    (RtpDtmfFormat(right.dtmf_payload_type, right.dtmf_clock_rate, right.dtmf_events),),
                    audio_receiver=right_receiver)
                    if right.inbound_dtmf_formats or right.dtmf_payload_type is not None else None,
            },
        }

    @staticmethod
    def _media_codec_contract(left: RtpPeer, right: RtpPeer) -> tuple[Any, ...]:
        return tuple(
            (
                peer.inbound_rtp_format,
                peer.inbound_rtp_formats,
                peer.outbound_rtp_formats,
                peer.match_received_codec,
                peer.outbound_rtp_format,
                peer.inbound_dtmf_formats, peer.outbound_dtmf_formats,
                peer.dtmf_payload_type,
                peer.dtmf_clock_rate,
                peer.dtmf_events,
                peer.outbound_dtmf_payload_type,
                peer.outbound_dtmf_clock_rate,
                peer.outbound_dtmf_events,
            )
            for peer in (left, right)
        )

    def _apply_media_state(
        self,
        left: RtpPeer,
        right: RtpPeer,
        state: dict[str, Any],
    ) -> None:
        """Publish one fully prepared media state."""
        for side in ("left", "right"):
            self._cancel_comfort_noise(side)
            for suffix in ("decoder", "encoder"):
                previous = getattr(self, f"{side}_{suffix}", None)
                close = getattr(previous, "close", None)
                if close is not None:
                    close()
        self.left = left
        self.right = right
        self.left_to_right_passthrough = state["left_to_right_passthrough"]
        self.right_to_left_passthrough = state["right_to_left_passthrough"]
        self.left_to_right_clock = state["left_to_right_clock"]
        self.right_to_left_clock = state["right_to_left_clock"]
        self.left_to_right = state["left_to_right"]
        self.right_to_left = state["right_to_left"]
        self.left_decoder = state["left_decoder"]
        self.right_decoder = state["right_decoder"]
        self.left_encoder = state["left_encoder"]
        self.right_encoder = state["right_encoder"]
        self._dtmf_decoders = state["dtmf_decoders"]
        self._bind_receive_callbacks()

    def _bind_receive_callbacks(self):
        for side in ("left", "right"):
            receiver = getattr(self, f"{side}_decoder")
            if receiver is not None:
                receiver.set_reorder_callback(
                    lambda frames, side=side, receiver=receiver: self._receive_deferred_pcm(side, receiver, frames),
                    lambda error, side=side, receiver=receiver: self._receive_deferred_error(side, receiver, error))

    def adopt_audio_state(self, side, state):
        """Move one live source's codecs, admission and clocks into this relay."""
        if side not in {"left", "right"} or self._stop_requested:
            raise RuntimeError("The RTP source cannot be adopted by this relay")
        opposite = "right" if side == "left" else "left"
        peer = getattr(self, side)
        for suffix, replacement in (("decoder", state.receiver), ("encoder", state.encoder)):
            old = getattr(self, f"{side}_{suffix}")
            setattr(self, f"{side}_{suffix}", replacement)
            if old is not None:
                old.close()
        other_receiver = getattr(self, f"{opposite}_decoder")
        if other_receiver is None:
            other_peer = getattr(self, opposite)
            other_receiver = RtpAudioReceiver(other_peer.inbound_rtp_format,
                other_peer.inbound_rtp_formats, receive_state=self._receive_states[opposite])
        other_encoder = getattr(self, f"{opposite}_encoder")
        if other_encoder is None:
            other_peer = getattr(self, opposite)
            other_encoder = RtpPayloadEncoder(other_peer.outbound_rtp_format,
                send_formats=other_peer.outbound_rtp_formats,
                match_received_codec=other_peer.match_received_codec)
        pending_rx = state.retarget_receiver(other_encoder.input_format)
        incoming = PcmFrameConverter(state.receiver.output_pcm, other_encoder.input_format)
        pending_return = other_receiver.retarget_output(state.encoder.fmt.audio_format)
        outgoing = state.converter_for(other_receiver.output_pcm)
        setattr(self, f"{opposite}_decoder", other_receiver)
        setattr(self, f"{opposite}_encoder", other_encoder)
        self._receive_states[side] = state.receiver.receive_state
        self._dtmf_decoders[side] = state.dtmf_decoder
        setattr(self, "left_to_right" if side == "left" else "right_to_left", incoming)
        setattr(self, "right_to_left" if side == "left" else "left_to_right", outgoing)
        self.left_to_right_passthrough = self.right_to_left_passthrough = False
        self._adopted_audio_state = True
        peer.sequence, peer.timestamp, peer.ssrc = (state.sender.sequence,
            state.sender.timestamp, state.sender.ssrc)
        peer.clock_rate = state.sender.clock_rate or state.encoder.fmt.rtp_clock_rate
        peer.host, peer.port, peer.rx_ssrc = state.remote_host, state.remote_port, state.receiver.source_ssrc
        self._bind_receive_callbacks()
        for frame in state.ready_tx:
            fmt = state.encoder.fmt
            rtp.use_audio_clock(peer, fmt.rtp_clock_rate)
            self._send_audio_packet(side, rtp.build_packet(rtp.RtpPacket(payload_type=fmt.payload_type,
                sequence=peer.sequence, timestamp=peer.timestamp, ssrc=peer.ssrc,
                payload=state.encoder.encode(frame))))
            peer.sequence = rtp.next_sequence(peer.sequence)
            peer.timestamp = rtp.next_timestamp(peer.timestamp, fmt.rtp_timestamp_step)
        state.sender.sequence, state.sender.timestamp, state.sender.ssrc, state.sender.clock_rate = (
            peer.sequence, peer.timestamp, peer.ssrc, peer.clock_rate)
        if pending_return:
            self._receive_deferred_pcm(opposite, other_receiver, pending_return)
        return pending_rx

    def _receive_deferred_error(self, side, receiver, error):
        if getattr(self, f"{side}_decoder") is receiver and not self._stop_requested:
            self.dropped += 1
            _LOGGER.debug("Deferred RTP relay audio drop side=%s: %s", side, error)
            recording_error = getattr(self, "_recording_error", None)
            if recording_error is not None:
                recording_error("decode_error")

    def _receive_deferred_pcm(self, side, receiver, frames):
        """Finish one expired receive gap in this relay's existing media generation."""
        if self._stop_requested or getattr(self, f"{side}_decoder") is not receiver:
            return
        converter = self.left_to_right if side == "left" else self.right_to_left
        source = self.left if side == "left" else self.right
        dest = self.right if side == "left" else self.left
        encoder = self.right_encoder if side == "left" else self.left_encoder
        if (converter is None or encoder is None or not source.can_send
                or not dest.can_receive or dest.connection_held):
            return
        if rtp.use_audio_clock(dest, encoder.fmt.rtp_clock_rate):
            (self.left_to_right_clock if side == "left" else self.right_to_left_clock).reset()
        if receiver.last_gap_ms:
            converter = PcmFrameConverter(converter.src, converter.dst)
            setattr(self, "left_to_right" if side == "left" else "right_to_left", converter)
            dest.timestamp = rtp.next_timestamp(dest.timestamp,
                round(receiver.last_gap_ms * encoder.fmt.rtp_clock_rate / 1000))
        packets, sequence, timestamp = self._encode_received_pcm(side, frames)
        for packet in packets:
            self._send_audio_packet("right" if side == "left" else "left", packet)
        dest.sequence, dest.timestamp = sequence, timestamp

    def _configure_media(self, left: RtpPeer, right: RtpPeer) -> None:
        """Build all codec state first, then publish one coherent peer pair."""

        self._apply_media_state(left, right, self._build_media_state(left, right))

    def prepare_peer_reconfiguration(
        self,
        side: str,
        peer: RtpPeer,
    ) -> Callable[[], None]:
        """Prepare a negotiated peer update whose commit checks the current owner."""

        if side not in {"left", "right"}:
            raise ValueError(f"unknown RTP relay side: {side}")
        previous = self.left if side == "left" else self.right
        left = peer if side == "left" else self.left
        right = peer if side == "right" else self.right
        prepared_contract = self._media_codec_contract(left, right)
        state = (
            self._build_media_state(left, right)
            if prepared_contract != self._media_codec_contract(self.left, self.right)
            else None
        )

        def _commit() -> None:
            current = self.left if side == "left" else self.right
            if current is not previous:
                raise RuntimeError("stale RTP relay reconfiguration")
            commit_left = peer if side == "left" else self.left
            commit_right = peer if side == "right" else self.right
            # Preserve the sender identity at commit time so packets forwarded
            # while the SIP response was in flight cannot rewind the RTP clock.
            peer.sequence = current.sequence
            peer.timestamp = current.timestamp
            peer.ssrc = current.ssrc
            peer.clock_rate = current.clock_rate
            peer.rx_ssrc = None
            peer.dtmf_sequence = current.dtmf_sequence
            peer.dtmf_timestamp = current.dtmf_timestamp
            peer.dtmf_ssrc = current.dtmf_ssrc
            if prepared_contract == self._media_codec_contract(self.left, self.right):
                receiver = getattr(self, f"{side}_decoder")
                if receiver is not None:
                    receiver.reset_source()
                self.left = commit_left
                self.right = commit_right
            else:
                commit_state = (
                    state
                    if state is not None
                    and commit_left is left
                    and commit_right is right
                    else self._build_media_state(commit_left, commit_right)
                )
                self._apply_media_state(commit_left, commit_right, commit_state)
            if side in self._capture_buffers and peer.audio_format != previous.audio_format:
                self._capture_buffers[side].clear()
                self._capture_frames[side] = 0
                self._capture_formats[side] = peer.audio_format

        return _commit

    def media_reconfiguration_guard(self, *, deadline: float | None = None):
        """Serialize a negotiated commit with this owner's active digits."""
        return audio_reconfiguration_guard(self, deadline=deadline)

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

    def adopt_audio_sockets(self, sockets: tuple[socket.socket, ...]) -> None:
        """Accept an existing reservation without opening a second media owner."""
        if (self._released or self._stop_requested or self._start_task is not None
                or self.left_transport is not None or self.right_transport is not None):
            raise RuntimeError("SIP RTP relay cannot adopt sockets after startup")
        combined = sockets if self._audio_sockets is None else self._audio_sockets + sockets
        if any(sock.fileno() < 0 for sock in combined):
            raise ValueError("RTP sockets do not match the relay reservation")
        ports = [int(sock.getsockname()[1]) for sock in combined]
        if (not sockets or len(combined) > 2 or len(set(ports)) != len(ports)
                or len({sock.fileno() for sock in combined}) != len(combined)
                or any(sock.fileno() < 0 or port not in (self.left_port, self.right_port)
                       or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_DGRAM
                       for sock, port in zip(combined, ports, strict=True))):
            raise ValueError("RTP sockets do not match the relay reservation")
        self._audio_sockets = combined

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
            sockets, self._audio_sockets = self._audio_sockets or (), None
            by_port = {int(sock.getsockname()[1]): sock for sock in sockets}
            left_sock = by_port.get(self.left_port)
            right_sock = by_port.get(self.right_port)
            if left_sock is None:
                left_sock = self._rtp_socket(self.left_port)
            if right_sock is None:
                right_sock = self._rtp_socket(self.right_port)
            left_transport, _ = await loop.create_datagram_endpoint(
                lambda: _RelayProtocol(self, "left", paused=True),
                sock=left_sock,
            )
            if self._stop_requested:
                left_transport.close()
                raise asyncio.CancelledError
            self.left_transport = left_transport
            left_sock = None
            right_transport, _ = await loop.create_datagram_endpoint(
                lambda: _RelayProtocol(self, "right", paused=True),
                sock=right_sock,
            )
            if self._stop_requested:
                right_transport.close()
                raise asyncio.CancelledError
            self.right_transport = right_transport
            right_sock = None
            # Both destinations exist before either queued RTP packet is read.
            self.left_transport.resume_reading()
            self.right_transport.resume_reading()
            if self.video_relay is not None:
                await self.video_relay.start()
            if self._stop_requested:
                raise asyncio.CancelledError
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
        return bind_sip_audio_socket(port)

    def _request_stop(self) -> None:
        self._stop_requested = True
        for side in ("left", "right"):
            receiver = getattr(self, f"{side}_decoder", None)
            if receiver is not None:
                receiver.close()
        self.on_media_route_changed = None
        self._stop_signal.set()

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._request_stop()
            task = self._stop_task
            if task is None:
                task = asyncio.create_task(
                    self._stop_impl(),
                    name=f"voip-rtp-relay-stop-{self.left_port}-{self.right_port}",
                )
                self._stop_task = task
        await async_wait_for_cleanup(task)

    async def _stop_impl(self) -> None:
        on_end = getattr(self, "_recording_error", None)
        self._recording_error = None
        if on_end is not None:
            on_end("media_ended")
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
        dtmf_tasks = tuple(self._dtmf_tasks)
        for task in dtmf_tasks:
            task.cancel()
        self._close_audio_resources()
        for side in ("left", "right"):
            for suffix in ("decoder", "encoder"):
                resource = getattr(self, f"{side}_{suffix}", None)
                close = getattr(resource, "close", None)
                if close is not None:
                    close()
        self._detach_debug_capture_snapshot()
        # Port ownership is independent from optional diagnostics and video
        # cleanup. Release it before the first await so a slow filesystem or a
        # broken video relay cannot starve subsequent calls.
        self._release_ports()
        if dtmf_tasks:
            await asyncio.gather(
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
        for side in ("left", "right"):
            self._cancel_comfort_noise(side)
        sockets, self._audio_sockets = self._audio_sockets, None
        for sock in sockets or ():
            with contextlib.suppress(OSError):
                sock.close()
        if self.left_transport is not None:
            self.left_transport.close()
            self.left_transport = None
        if self.right_transport is not None:
            self.right_transport.close()
            self.right_transport = None

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

    def prime_silence(self, side: str) -> bool:
        """Open the negotiated RTP path once, using its own encoder and clock."""
        if side not in {"left", "right"}:
            raise ValueError("invalid RTP relay side")
        peer = self.left if side == "left" else self.right
        encoder = self.left_encoder if side == "left" else self.right_encoder
        if not peer.can_receive or peer.connection_held:
            return False
        if encoder is None:
            encoder = RtpPayloadEncoder(peer.outbound_rtp_format,
                send_formats=peer.outbound_rtp_formats, match_received_codec=peer.match_received_codec)
            setattr(self, f"{side}_encoder", encoder)
        payload = encoder.encode(bytes(encoder.input_format.nominal_frame_bytes))
        fmt = encoder.fmt
        rtp.use_audio_clock(peer, fmt.rtp_clock_rate)
        packet = rtp.build_packet(rtp.RtpPacket(fmt.payload_type, peer.sequence, peer.timestamp, peer.ssrc, payload))
        if not self._send_audio_packet(side, packet):
            return False
        peer.sequence = rtp.next_sequence(peer.sequence)
        peer.timestamp = rtp.next_timestamp(peer.timestamp, fmt.rtp_timestamp_step)
        return True

    def _release_ports(self) -> None:
        """Return the reserved RTP pair exactly once."""

        if self._on_release is not None and not self._released:
            self._released = True
            self._stop_signal.set()
            self._on_release((self.left_port, self.right_port))

    def attach_video_relay(self, relay: Any) -> None:
        """Attach one call-owned video relay to the audio relay lifecycle."""

        if self._released or self._stop_requested:
            raise RuntimeError("SIP RTP relay has already been stopped")
        if self.video_relay is not None and self.video_relay is not relay:
            raise RuntimeError("video relay already attached")
        self.video_relay = relay

    def _relay_dtmf_report(self, side, source, destination, report):
        """Translate progress and long-event segments without shortening a press."""
        destination_side = "right" if side == "left" else "left"
        event = telephone_event_code(report.digit)
        key = (side, report.ssrc, report.timestamp, report.digit)
        mapping = self._dtmf_report_maps.get(key)
        if mapping is None and (destination.outbound_dtmf_payload_type is None
                or event not in destination.outbound_dtmf_events):
            if report.first:
                self._schedule_dtmf_fallback(side, report.digit)
            return
        rate = max(1, destination.outbound_dtmf_clock_rate)
        encoder = self.right_encoder if destination_side == "right" else self.left_encoder
        audio_rate = (encoder.fmt if encoder is not None else destination.outbound_rtp_format).rtp_clock_rate
        shared = rate == audio_rate
        if mapping is None:
            mapping = (destination, destination.timestamp if shared else destination.dtmf_timestamp,
                0, rate, destination.outbound_dtmf_payload_type, shared,
                destination.ssrc if shared else destination.dtmf_ssrc)
            self._dtmf_report_maps[key] = mapping
            if len(self._dtmf_report_maps) > 256:
                del self._dtmf_report_maps[next(iter(self._dtmf_report_maps))]
        if mapping[0] is not destination:
            return
        timestamp, previous_segment, rate, payload_type, shared, ssrc = mapping[1:]
        duration = max(1, round(report.duration * rate / max(1, report.sample_rate)))
        sent = 0
        for offset, segment_duration, ended in telephone_event_segments(duration,
                previous_segment=previous_segment, end=report.end):
            sequence = destination.sequence if shared else destination.dtmf_sequence
            packet = rtp.build_packet(rtp.RtpPacket(
                payload_type=payload_type,
                sequence=sequence, timestamp=rtp.next_timestamp(timestamp, offset),
                ssrc=ssrc,
                payload=build_telephone_event_payload(report.digit, duration=segment_duration,
                    end=ended, volume=report.volume), marker=report.marker and offset == 0))
            if not self._send_audio_packet(destination_side, packet):
                return
            sent += 1
            if shared:
                destination.sequence = rtp.next_sequence(sequence)
            else:
                destination.dtmf_sequence = rtp.next_sequence(sequence)
        if not sent:
            return
        self._dtmf_report_maps[key] = (destination, timestamp, (duration - 1) // 65535,
            rate, payload_type, shared, ssrc)
        if shared:
            highest = self._receive_states[side].highest
            if highest is not None:
                clock = self.left_to_right_clock if side == "left" else self.right_to_left_clock
                source_shared = report.shared_audio
                clock.inserted_after(highest, sent - int(source_shared))
            if report.end and ((destination.timestamp - timestamp) & 0xFFFFFFFF) < duration:
                destination.timestamp = rtp.next_timestamp(timestamp, duration)
        elif report.end:
            destination.dtmf_timestamp = rtp.next_timestamp(timestamp, duration + rate // 20)
        if report.first:
            if destination_side == "left":
                self.left_dtmf_tx_events += 1
            else:
                self.right_dtmf_tx_events += 1

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
        if event is None or not destination.can_receive or destination.connection_held:
            _LOGGER.info(
                "RTP DTMF not relayed source=%s destination=%s digit=%s negotiated=%s",
                source_side,
                destination_side,
                digit,
                destination.outbound_dtmf_payload_type is not None,
            )
            return False
        if (
            destination.outbound_dtmf_payload_type is None
            or event not in destination.outbound_dtmf_events
        ):
            self._schedule_dtmf_fallback(source_side, digit, duration_ms=duration_ms)
            _LOGGER.info(
                "RTP DTMF unavailable; signaling fallback requested source=%s destination=%s digit=%s",
                source_side,
                destination_side,
                digit,
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

    def _schedule_dtmf_fallback(self, source_side: str, digit: str, *, duration_ms=160) -> None:
        """Run one dialog-level fallback under the relay lifecycle owner."""

        callback = self.on_dtmf_fallback
        event_callback = getattr(self, "on_dtmf_fallback_event", None)
        if (callback is None and event_callback is None) or self._stop_requested:
            return

        async def _run() -> None:
            try:
                result = (await event_callback(source_side, digit, duration_ms)
                          if event_callback is not None else await callback(source_side, digit))
                if not result:
                    _LOGGER.info(
                        "SIP INFO DTMF fallback not accepted source=%s digit=%s",
                        source_side,
                        digit,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "SIP INFO DTMF fallback failed source=%s digit=%s",
                    source_side,
                    digit,
                )

        task = asyncio.create_task(
            _run(),
            name=f"voip-sip-info-dtmf-{source_side}-{digit}",
        )
        self._dtmf_tasks.add(task)
        task.add_done_callback(self._dtmf_tasks.discard)

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
            event_payload_type = destination.outbound_dtmf_payload_type
            event_set = frozenset(destination.outbound_dtmf_events)
            if event_payload_type is None or telephone_event_code(digit) not in event_set:
                return
            event_rate = max(1, destination.outbound_dtmf_clock_rate)
            encoder = self.right_encoder if destination_side == "right" else self.left_encoder
            wire_format = encoder.fmt if encoder is not None else destination.outbound_rtp_format
            audio_rate = max(1, int(wire_format.rtp_clock_rate))
            # A codec switch can be selected by RX before the next audio send.
            # Commit that source clock before taking this event's RTP identity.
            if rtp.use_audio_clock(destination, audio_rate):
                clock = self.left_to_right_clock if destination_side == "right" else self.right_to_left_clock
                clock.reset()
            shared_clock = event_rate == audio_rate
            event_timestamp = (
                destination.timestamp
                if shared_clock
                else destination.dtmf_timestamp
            )
            event_ssrc = destination.ssrc if shared_clock else destination.dtmf_ssrc
            audio_timestamp_at_start = destination.timestamp

            previous_segment = 0

            def _send(*, elapsed_ms: int, marker: bool, end: bool) -> bool:
                nonlocal previous_segment
                current = self.right if destination_side == "right" else self.left
                if (current is not destination or self._stop_requested
                        or not destination.can_receive or destination.connection_held):
                    return False
                duration = max(1, round(elapsed_ms * event_rate / 1000))
                for offset, segment_duration, ended in telephone_event_segments(duration,
                        previous_segment=previous_segment, end=end):
                    sequence = destination.sequence if shared_clock else destination.dtmf_sequence
                    packet = rtp.build_packet(rtp.RtpPacket(
                        payload_type=int(event_payload_type), sequence=sequence,
                        timestamp=rtp.next_timestamp(event_timestamp, offset),
                        ssrc=event_ssrc,
                        payload=build_telephone_event_payload(digit,
                            duration=segment_duration, end=ended), marker=marker and offset == 0))
                    if not self._send_audio_packet(destination_side, packet):
                        return False
                    if shared_clock:
                        destination.sequence = rtp.next_sequence(sequence)
                        source_side = "left" if destination_side == "right" else "right"
                        highest = self._receive_states[source_side].highest
                        if highest is not None:
                            clock = self.left_to_right_clock if source_side == "left" else self.right_to_left_clock
                            clock.inserted_after(highest)
                    else:
                        destination.dtmf_sequence = rtp.next_sequence(sequence)
                previous_segment = (duration - 1) // 65535
                return True

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
                event_payload_type,
                event_rate,
            )

    def handle_packet(self, side: str, data: bytes, addr) -> None:
        if self._stop_requested or is_stun_message(data):
            return
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
        report = (dtmf_decoder.decode_report(data)
            if dtmf_decoder is not None else None)
        if report is not None:
            if report.shared_audio:
                audio_receiver = self.left_decoder if side == "left" else self.right_decoder
                if audio_receiver is not None:
                    if report.source_changed:
                        audio_receiver.reset_source()
                    audio_receiver.observe_non_audio_sequence(rtp.parse_packet(data).sequence)
                source.rx_ssrc = report.ssrc
                source.host, source.port = str(addr[0]), int(addr[1])
                if report.source_changed:
                    (self.left_to_right_clock if side == "left" else self.right_to_left_clock).reset()
            if report.first and self.on_dtmf is not None:
                try:
                    self.on_dtmf(side, report.digit, "rtp_event")
                except Exception as err:
                    _LOGGER.warning("RTP relay DTMF callback failed: %s", err)
            self._relay_dtmf_report(side, source, dest, report)
            return
        previous_route = self.media_route if self.on_media_route_changed is not None else None
        try:
            packet = rtp.parse_packet(data)
            decoder = self.left_decoder if side == "left" else self.right_decoder
            received_format = (
                decoder.format_for(packet.payload_type) if decoder is not None
                else source.inbound_rtp_format if packet.payload_type == source.payload_type else None
            )
            if received_format is None:
                raise ValueError(f"payload type {packet.payload_type} != expected {source.payload_type}")
            rtp.validate_audio_payload_size(packet.payload, received_format)
            receive_state = self._receive_states[side]
            if receive_state.ssrc is None and source.rx_ssrc is not None:
                receive_state.ssrc = source.rx_ssrc
            if not receive_state.admit(packet):
                self.dropped += 1
                return
            if receive_state.changed:
                if decoder is not None:
                    decoder.reset_source()
                clock = self.left_to_right_clock if side == "left" else self.right_to_left_clock
                clock.source_base = clock.source_sequence = None
            source.rx_ssrc = packet.ssrc
            output_encoder = self.right_encoder if side == "left" else self.left_encoder
            wire_format = output_encoder.fmt if output_encoder is not None else dest.outbound_rtp_format
            if rtp.use_audio_clock(dest, wire_format.rtp_clock_rate):
                (self.left_to_right_clock if side == "left" else self.right_to_left_clock).reset()
            converter = self.left_to_right if side == "left" else self.right_to_left
            pending_pcm = ((decoder is not None and decoder.has_pending_pcm)
                           or (converter is not None and converter.has_pending_pcm))
            packet_size = len(packet.payload) if not pending_pcm else None
            passthrough = not self.debug_capture and not getattr(self, "_adopted_audio_state", False) and payload_relay_compatible(
                received_format, wire_format, payload_size=packet_size
            )
            if pending_pcm or (decoder is not None and decoder.has_pending_media):
                passthrough = False
            clock = self.left_to_right_clock if side == "left" else self.right_to_left_clock
            clock.observe_format(received_format)
            if passthrough:
                previous_passthrough = self.left_to_right_passthrough if side == "left" else self.right_to_left_passthrough
                if not previous_passthrough:
                    if decoder is not None:
                        dest.timestamp = rtp.next_timestamp(dest.timestamp,
                            round(decoder.unrendered_gap_ms(packet) * wire_format.rtp_clock_rate / 1000))
                    clock.reset()
                rtp.validate_audio_payload_size(
                    packet.payload,
                    received_format,
                )
                rtp.validate_audio_payload_size(packet.payload, wire_format)
                if decoder is not None:
                    if decoder.has_pcm_observers:
                        try:
                            decoder.decode_rtp(packet, admitted=True, deliver_frames=False)
                        except Exception:
                            error = getattr(self, "_recording_error", None)
                            if error is not None:
                                error("decode_error")
                    else:
                        decoder.observe_rtp(packet)
                if source.rx_ssrc is None:
                    source.rx_ssrc = packet.ssrc
                    source.host = str(addr[0])
                    source.port = int(addr[1])
                elif (str(addr[0]), int(addr[1])) != (
                    source.host,
                    int(source.port),
                ):
                    source.host = str(addr[0])
                    source.port = int(addr[1])
                mapped_sequence = clock.map_sequence(packet, dest.sequence)
                outgoing_timestamp = clock.map(packet.timestamp, dest.timestamp)
                outgoing = [
                    rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=wire_format.payload_type,
                            sequence=mapped_sequence,
                            timestamp=outgoing_timestamp,
                            ssrc=dest.ssrc,
                            payload=packet.payload,
                            marker=packet.marker,
                        )
                    )
                ]
                advanced = (mapped_sequence - dest.sequence) & 65535
                sequence = rtp.next_sequence(mapped_sequence) if advanced < 32768 else dest.sequence
                timestamp = rtp.next_timestamp(
                    outgoing_timestamp,
                    round(rtp.audio_payload_duration_ms(packet.payload, received_format)
                          * wire_format.rtp_clock_rate / 1000),
                ) if advanced < 32768 else dest.timestamp
            else:
                decoder = self.left_decoder if side == "left" else self.right_decoder
                if decoder is None:
                    raise RuntimeError("RTP decoder missing for transcoding path")
                converter = self.left_to_right if side == "left" else self.right_to_left
                if converter is None:
                    raise RuntimeError("PCM converter missing for transcoding path")
                previous_passthrough = self.left_to_right_passthrough if side == "left" else self.right_to_left_passthrough
                if previous_passthrough:
                    decoder.resume_pcm()
                # Same-geometry reframers accept arbitrary sample-aligned input.
                # Resamplers require nominal input quanta from this receiver.
                pcm_frames = decoder.decode_rtp(packet, admitted=True,
                    normalize_frames=converter.requires_fixed_input)
                self._arm_comfort_noise(side)
                if not pcm_frames:
                    return
                if decoder.last_gap_ms:
                    converter = PcmFrameConverter(converter.src, converter.dst)
                    if side == "left":
                        self.left_to_right = converter
                    else:
                        self.right_to_left = converter
                    dest.timestamp = rtp.next_timestamp(dest.timestamp,
                        round(decoder.last_gap_ms * wire_format.rtp_clock_rate / 1000))
                if source.rx_ssrc is None:
                    source.rx_ssrc = packet.ssrc
                    source.host = str(addr[0])
                    source.port = int(addr[1])
                elif (str(addr[0]), int(addr[1])) != (source.host, int(source.port)):
                    # Symmetric RTP: follow a valid same-SSRC NAT tuple rebind.
                    source.host = str(addr[0])
                    source.port = int(addr[1])
                outgoing, sequence, timestamp = self._encode_received_pcm(side, pcm_frames)
        except Exception as err:
            self.dropped += 1
            _LOGGER.debug("RTP relay drop: %s", err)
            return
        return_encoder = self.left_encoder if side == "left" else self.right_encoder
        if source.match_received_codec and return_encoder is not None and not self._dtmf_locks[side].locked():
            return_encoder.follow_received(received_format)
        if side == "left":
            self.left_to_right_passthrough = passthrough
            self.left_rx_packets += 1
            self.left_rx_bytes += len(data)
        else:
            self.right_to_left_passthrough = passthrough
            self.right_rx_packets += 1
            self.right_rx_bytes += len(data)
        destination_side = "right" if side == "left" else "left"
        for out in outgoing:
            self._send_audio_packet(destination_side, out)
        dest.sequence = sequence
        dest.timestamp = timestamp
        self._arm_comfort_noise(side)
        callback = self.on_media_route_changed
        if callback is not None and not self._stop_requested:
            route = self.media_route
            if route != previous_route:
                try:
                    callback(route)
                except Exception:
                    _LOGGER.debug("RTP media route observer failed", exc_info=True)

    def _encode_received_pcm(self, side, pcm_frames):
        """Use the same converter, encoder and RTP source for packets and CN."""
        dest = self.right if side == "left" else self.left
        converter = self.left_to_right if side == "left" else self.right_to_left
        encoder = self.right_encoder if side == "left" else self.left_encoder
        if converter is None or encoder is None:
            raise RuntimeError("RTP media conversion resources are unavailable")
        wire_format = encoder.fmt
        outgoing = []
        sequence, timestamp = dest.sequence, dest.timestamp
        for pcm in pcm_frames:
            self._capture_pcm(side, pcm)
            for frame in converter.convert(pcm):
                outgoing.append(rtp.build_packet(rtp.RtpPacket(
                    payload_type=wire_format.payload_type, sequence=sequence,
                    timestamp=timestamp, ssrc=dest.ssrc, payload=encoder.encode(frame))))
                sequence = rtp.next_sequence(sequence)
                timestamp = rtp.next_timestamp(timestamp, wire_format.rtp_timestamp_step)
        return outgoing, sequence, timestamp

    def _cancel_comfort_noise(self, side):
        handle = self._cn_timers.pop(side, None)
        if handle is not None:
            handle.cancel()
        self._cn_generations[side] += 1
        receiver = getattr(self, f"{side}_decoder", None)
        end = getattr(receiver, "end_comfort_noise", None)
        if end is not None:
            end()

    def _arm_comfort_noise(self, side):
        receiver = getattr(self, f"{side}_decoder", None)
        if receiver is None or not receiver.comfort_noise_active or self._stop_requested:
            if side in self._cn_timers:
                self._cancel_comfort_noise(side)
            return
        if side in self._cn_timers:
            return
        generation = self._cn_generations[side]
        delay = receiver.comfort_noise_packet_ms / 1000
        self._cn_timers[side] = asyncio.get_running_loop().call_later(
            delay, self._comfort_noise_tick, side, receiver, generation)

    def _comfort_noise_tick(self, side, receiver, generation):
        if (generation != self._cn_generations[side]
                or getattr(self, f"{side}_decoder", None) is not receiver):
            return
        self._cn_timers.pop(side, None)
        source = self.left if side == "left" else self.right
        dest = self.right if side == "left" else self.left
        if (self._stop_requested or generation != self._cn_generations[side]
                or getattr(self, f"{side}_decoder", None) is not receiver
                or not receiver.comfort_noise_active or not source.can_send
                or not dest.can_receive or dest.connection_held):
            return
        converter = self.left_to_right if side == "left" else self.right_to_left
        try:
            frames = receiver.take_comfort_noise(normalize_frames=converter.requires_fixed_input)
            packets, sequence, timestamp = self._encode_received_pcm(side, frames)
            destination_side = "right" if side == "left" else "left"
            for packet in packets:
                if not self._send_audio_packet(destination_side, packet):
                    self._cancel_comfort_noise(side)
                    return
            dest.sequence, dest.timestamp = sequence, timestamp
        except Exception as error:
            self._cancel_comfort_noise(side)
            _LOGGER.debug("Comfort-noise RTP period ended (%s)", type(error).__name__)
            return
        self._arm_comfort_noise(side)

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
            "left_to_right_audio_path": (
                "payload_relay" if self.left_to_right_passthrough else "transcode"
            ),
            "right_to_left_audio_path": (
                "payload_relay" if self.right_to_left_passthrough else "transcode"
            ),
        }
        if self.video_relay is not None:
            snapshot["video"] = self.video_relay.snapshot()
        if self.debug:
            needed = []
            for source_name, source, destination in (
                ("left_to_right", self.left, self.right),
                ("right_to_left", self.right, self.left),
            ):
                passthrough = (
                    self.left_to_right_passthrough
                    if source_name == "left_to_right"
                    else self.right_to_left_passthrough
                )
                if not passthrough:
                    incoming = source.inbound_rtp_format
                    outgoing = destination.outbound_rtp_format
                    needed.append(
                        f"{source_name}: {incoming.encoding.upper()}/"
                        f"{incoming.sample_rate}/{incoming.channels}/"
                        f"{incoming.frame_ms} -> {outgoing.encoding.upper()}/"
                        f"{outgoing.sample_rate}/{outgoing.channels}/"
                        f"{outgoing.frame_ms}"
                    )
            if needed:
                snapshot["audio_transcoding"] = needed
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
