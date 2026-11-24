"""Direct or bounded cross-codec RTP/RTCP relay for HA-owned SIP bridges."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import logging
import secrets
import socket
import struct
from time import monotonic
from typing import Any, Awaitable, Callable

from .core import rtp
from .core.video_rtp import JpegDepacketizer, parse_jpeg_rtp_header
from .core.sip_stun import is_stun_message
from .core.sdp import (
    RtpVideoFormat,
    remote_can_receive,
    remote_can_send,
    video_formats_passthrough_compatible,
)
from .session_cleanup import async_wait_for_cleanup
from .video_transcoder import (
    FfmpegVideoTranscoder,
    _register_transcoder,
    _unregister_transcoder,
    video_transcode_supported,
    jpeg_size_transcode_required,
)
from .core.video_rtcp import (
    RtcpError,
    build_fir,
    build_pli,
    build_receiver_compound,
    parse_compound,
)


_LOGGER = logging.getLogger(__name__)
_RTP_IP_TOS = 0x88
_TRANSCODE_STARTUP_MAX_PACKETS = 64
_TRANSCODE_STARTUP_MAX_BYTES = 256 * 1024
_REOFFER_STAGE_MAX_PACKETS = 256
_REOFFER_STAGE_MAX_BYTES = 1024 * 1024


@dataclass(slots=True)
class VideoRtpPeer:
    """One negotiated remote video leg."""

    host: str
    port: int
    rtcp_port: int
    video_format: RtpVideoFormat
    # Backward compatibility keeps ``video_format`` as the contract for RTP
    # sent by the relay toward this peer.  RTP received from the peer may have
    # a distinct receiver contract after directional offer/answer.
    local_video_format: RtpVideoFormat | None = None
    rtcp_host: str = ""
    signaling_host: str = ""
    advertised_host: str = ""
    rx_ssrc: int | None = None
    rtcp_source_port: int | None = None
    connection_held: bool = False
    receive_state: Any = field(default_factory=rtp.RtpReceiveState, repr=False)
    jpeg_geometry: tuple[int, int] | None = field(default=None, init=False, repr=False, compare=False)
    jpeg_timestamp: int | None = field(default=None, init=False, repr=False, compare=False)
    jpeg_frame_contract: tuple[int, ...] | None = field(default=None, init=False, repr=False, compare=False)
    jpeg_frame_blocked: bool = field(default=False, init=False, repr=False, compare=False)
    jpeg_resize_key: tuple | None = field(default=None, init=False, repr=False, compare=False)
    jpeg_resize_required: bool | None = field(default=None, init=False, repr=False, compare=False)
    jpeg_parser: JpegDepacketizer | None = field(default=None, init=False, repr=False, compare=False)
    jpeg_ssrc: int | None = field(default=None, init=False, repr=False, compare=False)
    jpeg_raw_bootstrap: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.advertised_host:
            self.advertised_host = self.host
        if not self.rtcp_host:
            self.rtcp_host = self.host

    @property
    def send_format(self) -> RtpVideoFormat:
        """RTP format sent locally toward the remote peer."""

        return self.video_format

    @property
    def recv_format(self) -> RtpVideoFormat:
        """RTP format received locally from the remote peer."""

        return self.local_video_format or self.video_format

    def accepts_rtp_source_host(self, source_host: str) -> bool:
        """Allow RTP only from its media or authenticated signaling host."""

        return str(source_host) in {
            self.host,
            self.advertised_host,
            self.signaling_host,
        }

    def accepts_rtcp_source_host(self, source_host: str) -> bool:
        """Allow RTCP from its explicit or symmetric media source host."""

        return str(source_host) in {
            self.rtcp_host,
            self.host,
            self.advertised_host,
            self.signaling_host,
        }


@dataclass(slots=True)
class _StagedVideoPeer:
    peer: VideoRtpPeer
    packets: list[tuple[bytes, tuple[Any, ...]]]
    byte_count: int = 0


@dataclass(slots=True)
class _TranscodeGeneration:
    directions: set[str]
    transcoders: dict[str, FfmpegVideoTranscoder]
    transports: dict[tuple[str, bool], asyncio.DatagramTransport]
    sockets: dict[tuple[str, bool], socket.socket | None]
    _cleanup_task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)


class VideoTranscodeRestoreError(RuntimeError):
    """A failed replacement also lost the previous conversion contract."""


class _VideoRelayProtocol(asyncio.DatagramProtocol):
    def __init__(
        self, relay: "SipVideoRtpRelay", side: str, *, rtcp: bool = False
    ) -> None:
        self.relay = relay
        self.side = side
        self.rtcp = rtcp

    def datagram_received(self, data: bytes, addr) -> None:
        if self.rtcp:
            self.relay.handle_rtcp(self.side, data, addr)
        else:
            self.relay.handle_rtp(self.side, data, addr)


class _TranscodedOutputProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        relay: "SipVideoRtpRelay",
        source_side: str,
        *,
        rtcp: bool = False,
    ) -> None:
        self.relay = relay
        self.source_side = source_side
        self.rtcp = rtcp
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, _addr) -> None:
        if (self.relay._stop_requested or self.transport is None
                or self.relay._transcode.transports.get((self.source_side, self.rtcp)) is not self.transport
                or self.transport.is_closing()):
            return
        if self.rtcp:
            self.relay.handle_transcoded_rtcp(self.source_side, data)
        else:
            self.relay.handle_transcoded_rtp(self.source_side, data)


class SipVideoRtpRelay:
    """Relay direct video or transcode only incompatible directions.

    On a compatible direct path, the payload type is the only RTP header field
    rewritten because it is negotiated independently on each SIP leg. RTP
    extensions, CSRCs, marker, sequence, timestamp and encoded payload remain
    byte-for-byte intact. A configured FFmpeg fallback owns separate loopback
    RTP pairs and never changes the direct direction.
    """

    def __init__(
        self,
        *,
        left: VideoRtpPeer,
        right: VideoRtpPeer,
        left_port: int,
        right_port: int,
        left_socket: socket.socket | None = None,
        right_socket: socket.socket | None = None,
        left_rtcp_socket: socket.socket | None = None,
        right_rtcp_socket: socket.socket | None = None,
        on_release: Callable[[tuple[int, int]], None] | None = None,
        on_media_processing_changed: Callable[[], None] | None = None,
    ) -> None:
        self.left = left
        self.right = right
        self.left_port = int(left_port)
        self.right_port = int(right_port)
        self._sockets = {
            ("left", False): left_socket,
            ("right", False): right_socket,
            ("left", True): left_rtcp_socket,
            ("right", True): right_rtcp_socket,
        }
        self._transports: dict[tuple[str, bool], asyncio.DatagramTransport] = {}
        self._transcode = _TranscodeGeneration(set(), {}, {}, {})
        self._transcode_hass: Any | None = None
        self._transcode_call_id = ""
        self._transcode_registered = False
        self._preparation_tasks: set[asyncio.Task[Any]] = set()
        self._provisional_transcodes: dict[int, _TranscodeGeneration] = {}
        self._transcode_startup_rtp: dict[str, list[bytes]] = {
            "left": [],
            "right": [],
        }
        self._transcode_startup_bytes = {"left": 0, "right": 0}
        self._keyframe_requests: dict[str, int | None] = {}
        self._keyframe_attempts: dict[str, tuple[int, int, float]] = {}
        self._transcoded_ssrc: dict[str, int] = {}
        self._staged_peers: dict[str, _StagedVideoPeer] = {}
        self._rtcp_ssrc = secrets.randbelow(0xFFFFFFFF) + 1
        self._fir_sequence = 0
        self._on_release = on_release
        self.on_media_processing_changed = on_media_processing_changed
        self._processing_snapshot = None
        self._jpeg_geometry_task: asyncio.Task | None = None
        self._jpeg_pending_directions: set[str] = set()
        self._jpeg_failed_key = None
        self._jpeg_failed_directions: set[str] = set()
        self._released = False
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._nat_timer: asyncio.TimerHandle | None = None
        self.started = False
        self.forwarded = 0
        self.rtcp_forwarded = 0
        self.dropped = 0
        self.drop_connection_hold = 0
        self.left_rx_packets = 0
        self.left_rx_bytes = 0
        self.left_tx_packets = 0
        self.left_tx_bytes = 0
        self.right_rx_packets = 0
        self.right_rx_bytes = 0
        self.right_tx_packets = 0
        self.right_tx_bytes = 0
        self.transcoded_input_packets = 0
        self.transcoded_output_packets = 0
        self.transcoded_rtcp_packets = 0
        self.transcode_rtcp_dropped = 0
        self.transcode_startup_buffered = 0
        self.transcode_startup_dropped = 0
        self.ignored_after_stop = 0
        self.rtp_drop_reasons: dict[str, int] = {}

    def _record_rtp_drop(self, error: BaseException) -> None:
        reason = str(error) or type(error).__name__
        self.rtp_drop_reasons[reason] = self.rtp_drop_reasons.get(reason, 0) + 1

    def pending_media_sockets(self, side: str) -> dict:
        """Borrow pre-bound descriptors for discovery before media starts."""
        if self.started or self._start_task is not None or self._stop_requested:
            raise RuntimeError("Media discovery must precede relay startup")
        return {name: self._sockets[(side, rtcp)] for name, rtcp in (
            ("video", False), ("video_rtcp", True)
        )}

    @staticmethod
    def _socket(port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _RTP_IP_TOS)
            sock.bind(("0.0.0.0", int(port)))
            return sock
        except BaseException:
            sock.close()
            raise

    def reverse_held_sides(self) -> None:
        """Reverse a settled handoff atomically, retaining active stream owners.

        The audio owner's media guard protects the synchronous session cutover.
        Repeating this operation restores the prior orientation for rollback.
        """
        if (not self.started or self._start_task is not None or self._stop_requested
                or self._staged_peers
                or (self._jpeg_geometry_task is not None and not self._jpeg_geometry_task.done())):
            raise RuntimeError("Only a settled video relay can change orientation")
        keys = {(side, rtcp) for side in ("left", "right") for rtcp in (False, True)}
        if keys != set(self._transports) or keys != set(self._sockets) or any(
                not isinstance(transport.get_protocol(), _VideoRelayProtocol)
                or transport.get_protocol().relay is not self
                or transport.get_protocol().side != side
                or transport.get_protocol().rtcp != rtcp
                or transport.is_closing() for (side, rtcp), transport in self._transports.items()):
            raise RuntimeError("The video transport owner changed before handoff")
        generation = self._transcode
        output_keys = {(side, rtcp) for side in generation.directions for rtcp in (False, True)}
        if (not generation.directions <= {"left", "right"}
                or (generation.directions and not self._transcode_registered)
                or set(generation.transcoders) != generation.directions
                or set(generation.transports) != output_keys or set(generation.sockets) != output_keys
                or any(not getattr(worker, "ready", True) or getattr(worker, "stopping", False)
                       for worker in generation.transcoders.values())
                or any(not isinstance(transport.get_protocol(), _TranscodedOutputProtocol)
                       or transport.get_protocol().relay is not self
                       or transport.get_protocol().source_side != side
                       or transport.get_protocol().rtcp != rtcp or transport.is_closing()
                       for (side, rtcp), transport in generation.transports.items())):
            raise RuntimeError("The video conversion generation changed before handoff")

        def opposite(side):
            return "right" if side == "left" else "left"

        self.left, self.right = self.right, self.left
        self.left_port, self.right_port = self.right_port, self.left_port
        for mapping in (self._sockets, self._transports):
            for rtcp in (False, True):
                mapping[("left", rtcp)], mapping[("right", rtcp)] = (
                    mapping[("right", rtcp)], mapping[("left", rtcp)])
        for (side, _rtcp), transport in self._transports.items():
            transport.get_protocol().side = side
        generation.directions = {opposite(side) for side in generation.directions}
        generation.transcoders = {opposite(side): worker for side, worker in generation.transcoders.items()}
        generation.sockets = {(opposite(side), rtcp): sock for (side, rtcp), sock in generation.sockets.items()}
        generation.transports = {(opposite(side), rtcp): transport
            for (side, rtcp), transport in generation.transports.items()}
        for (side, _rtcp), transport in generation.transports.items():
            transport.get_protocol().source_side = side
        for suffix in ("rx_packets", "rx_bytes", "tx_packets", "tx_bytes"):
            left, right = f"left_{suffix}", f"right_{suffix}"
            old_left = getattr(self, left)
            setattr(self, left, getattr(self, right))
            setattr(self, right, old_left)
        for mapping in (self._transcode_startup_rtp, self._transcode_startup_bytes):
            mapping["left"], mapping["right"] = mapping["right"], mapping["left"]
        self._keyframe_requests = {
            "right" if side == "left" else "left": value for side, value in self._keyframe_requests.items()
        }
        self._keyframe_attempts = {
            "right" if side == "left" else "left": value for side, value in self._keyframe_attempts.items()
        }
        self._transcoded_ssrc = {
            "right" if side == "left" else "left": value for side, value in self._transcoded_ssrc.items()
        }

    @staticmethod
    def _loopback_socket_pair() -> tuple[socket.socket, socket.socket]:
        for _attempt in range(64):
            rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rtcp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                rtp_socket.setblocking(False)
                rtcp_socket.setblocking(False)
                rtp_socket.bind(("127.0.0.1", 0))
                rtp_port = int(rtp_socket.getsockname()[1])
                if rtp_port & 1 or rtp_port >= 65535:
                    continue
                rtcp_socket.bind(("127.0.0.1", rtp_port + 1))
                return rtp_socket, rtcp_socket
            except OSError:
                pass
            finally:
                if not (
                    rtp_socket.fileno() >= 0
                    and rtcp_socket.fileno() >= 0
                    and rtp_socket.getsockname()[1] + 1 == rtcp_socket.getsockname()[1]
                ):
                    rtp_socket.close()
                    rtcp_socket.close()
        raise OSError("unable to reserve FFmpeg loopback RTP pair")

    @property
    def transcoding(self) -> bool:
        return bool(self._transcode.directions)

    def transcodes_from(self, side: str) -> bool:
        """Return whether RTP sourced by this side uses its transcoder."""

        if side not in {"left", "right"}:
            raise ValueError(f"unknown video relay side: {side}")
        return side in self._transcode.directions

    def _bounded_jpeg(self, side):
        source, destination = self._peers(side)
        attributes = destination.send_format.image_attributes
        return (source.recv_format.encoding == destination.send_format.encoding == "JPEG"
                and attributes is not None and attributes.recv is not None
                and not attributes.recv.wildcard)

    def processing_known_from(self, side: str) -> bool:
        """Report only settled processing, independently of SDP geometry hints."""
        if side not in {"left", "right"}:
            raise ValueError(f"unknown video relay side: {side}")
        if side in self._jpeg_pending_directions:
            return False
        if (side in self._jpeg_failed_directions
                and self._jpeg_failed_key == self._jpeg_processing_key()):
            return False
        if self._bounded_jpeg(side) and self._peers(side)[0].jpeg_frame_blocked:
            return False
        if side in self._transcode.directions:
            worker = self._transcode.transcoders.get(side)
            return worker is not None and bool(getattr(worker, "ready", False))
        if self._bounded_jpeg(side):
            source, destination = self._peers(side)
            if source.jpeg_geometry is None or source.jpeg_frame_blocked:
                return False
            try:
                return not self._jpeg_needs_resize(source, destination)
            except (RuntimeError, ValueError):
                return False
        return True

    @staticmethod
    def _jpeg_needs_resize(source, destination, dimensions=None):
        # Cache the plan on the existing stream owner. Repeated fragments and
        # equal-size frames only compare their small header/contract facts.
        dimensions = source.jpeg_geometry if dimensions is None else dimensions
        key = (source.recv_format, destination.send_format, dimensions)
        if key != source.jpeg_resize_key:
            source.jpeg_resize_key = key
            source.jpeg_resize_required = None
            source.jpeg_resize_required = jpeg_size_transcode_required(
                source.recv_format, destination.send_format, dimensions=dimensions)
        if source.jpeg_resize_required is None:
            raise ValueError("JPEG receiver has no usable image size")
        return source.jpeg_resize_required

    def _notify_media_processing(self):
        snapshot = tuple((self.processing_known_from(side), self.transcodes_from(side))
                         for side in ("left", "right"))
        if snapshot == self._processing_snapshot:
            return
        self._processing_snapshot = snapshot
        callback = self.on_media_processing_changed
        if callback is not None and not self._stop_requested:
            try:
                callback()
            except Exception:
                _LOGGER.debug("Video media processing observer failed", exc_info=True)

    def _jpeg_context_key(self):
        return (id(self.left), id(self.right), id(self._transcode),
                tuple((peer.recv_format, peer.send_format)
                      for peer in (self.left, self.right)))

    def _jpeg_processing_key(self):
        # Startup errors are latched for this operation/source, not retried
        # for every equal-operation geometry in an alternating JPEG stream.
        return (self._jpeg_context_key(), self.left.jpeg_ssrc, self.right.jpeg_ssrc,
                frozenset(self.transcode_directions_for(self.left, self.right)))

    @staticmethod
    def _reset_jpeg_observation(peer):
        peer.jpeg_geometry = peer.jpeg_timestamp = peer.jpeg_frame_contract = None
        peer.jpeg_frame_blocked = False
        peer.jpeg_resize_key = peer.jpeg_resize_required = None
        peer.jpeg_parser = None
        peer.jpeg_ssrc = None
        peer.jpeg_raw_bootstrap = False

    @classmethod
    def _retain_jpeg_observation(cls, previous, candidate):
        """Carry codec facts for the same source; validate its SSRC on use."""
        if candidate is previous:
            return
        cls._reset_jpeg_observation(candidate)
        if (previous.advertised_host == candidate.advertised_host
                and previous.signaling_host == candidate.signaling_host
                and replace(previous.recv_format, direction="sendrecv")
                == replace(candidate.recv_format, direction="sendrecv")):
            for name in ("jpeg_geometry", "jpeg_timestamp", "jpeg_frame_contract", "jpeg_frame_blocked",
                         "jpeg_resize_key", "jpeg_resize_required", "jpeg_parser", "jpeg_ssrc",
                         "jpeg_raw_bootstrap"):
                setattr(candidate, name, getattr(previous, name))

    def _observe_jpeg_packet(self, side, source, destination, packet):
        facts = parse_jpeg_rtp_header(packet.payload)
        if source.jpeg_ssrc != packet.ssrc:
            self._reset_jpeg_observation(source)
            source.jpeg_ssrc = packet.ssrc
        if source.jpeg_parser is None:
            source.jpeg_parser = JpegDepacketizer()
        dimensions = (facts.width, facts.height)
        resize = self._jpeg_needs_resize(source, destination, dimensions)
        latest = (source.jpeg_timestamp is None
                  or ((packet.timestamp - source.jpeg_timestamp) & 0xFFFFFFFF) < 0x80000000)
        if facts.fragment_offset == 0 and latest:
            source.jpeg_timestamp = packet.timestamp
            source.jpeg_frame_contract = facts.frame_contract
            source.jpeg_geometry = dimensions
            source.jpeg_frame_blocked = False
        elif source.jpeg_geometry is None:
            source.jpeg_geometry = dimensions
        if source.jpeg_frame_blocked and packet.timestamp == source.jpeg_timestamp:
            raise ValueError("JPEG frame was discarded during preparation")
        try:
            source.jpeg_parser.read_header(packet.payload)
        except ValueError:
            # An opaque relay never needed local decoder state. Require the
            # cache only when this packet must cross a decoder/cutover path.
            if resize or side in self._transcode.directions or side in self._jpeg_pending_directions:
                if latest:
                    source.jpeg_frame_blocked = True
                raise
        return facts, resize

    def _discard_jpeg_startup(self, side):
        count = len(self._transcode_startup_rtp[side])
        self._transcode_startup_rtp[side].clear()
        self._transcode_startup_bytes[side] = 0
        self.dropped += count
        self.transcode_startup_dropped += count

    def _buffer_jpeg_processing(self, side, data, facts):
        # Only the latest JPEG is useful while a worker starts. These packets
        # have already passed RTP admission; never replay them through admit().
        if facts.fragment_offset == 0:
            self._discard_jpeg_startup(side)
        if self._buffer_transcode_startup_rtp(side, data):
            self._account_received(side, len(data))
            return
        self._discard_jpeg_startup(side)
        self._peers(side)[0].jpeg_frame_blocked = True

    def _flush_jpeg_processing(self):
        for side in ("left", "right"):
            pending = self._transcode_startup_rtp[side]
            self._transcode_startup_rtp[side] = []
            self._transcode_startup_bytes[side] = 0
            source, destination = self._peers(side)
            for data in pending:
                try:
                    packet = rtp.parse_packet(data)
                    if (self._stop_requested or not remote_can_send(source.video_format)
                            or not remote_can_receive(destination.video_format,
                                                      connection_held=destination.connection_held)
                            or packet.payload_type != int(source.recv_format.payload_type)):
                        raise ValueError("queued JPEG direction or mapping changed")
                    if self._bounded_jpeg(side):
                        facts = parse_jpeg_rtp_header(packet.payload)
                        if (source.jpeg_frame_blocked or source.jpeg_timestamp != packet.timestamp
                                or source.jpeg_frame_contract != facts.frame_contract):
                            raise ValueError("queued JPEG frame is no longer current")
                        if (side not in self._transcode.directions
                                and self._jpeg_needs_resize(source, destination)):
                            raise ValueError("queued JPEG exceeds the receiver contract")
                    self._deliver_admitted_rtp(side, data, packet, received=False)
                except (OSError, RuntimeError, ValueError) as err:
                    self.dropped += 1
                    self.transcode_startup_dropped += 1
                    self._record_rtp_drop(err)

    def _queue_jpeg_processing(self, side, data, facts):
        wanted = self.transcode_directions_for(self.left, self.right)
        task = self._jpeg_geometry_task
        running = task is not None and not task.done()
        changing = wanted ^ self._transcode.directions
        if not (side in changing or side in self._jpeg_pending_directions
                or self._transcode_startup_rtp[side]):
            return False
        if not running:
            if (not self.started or self._transcode_hass is None or self._staged_peers
                    or self._preparation_tasks
                    or facts.fragment_offset != 0
                    or self._peers(side)[0].jpeg_timestamp is None
                    or self._jpeg_failed_key == self._jpeg_processing_key()):
                return False
            self._jpeg_pending_directions = changing
            task = asyncio.create_task(self._run_jpeg_processing(), name="voip-jpeg-processing-prepare")
            self._jpeg_geometry_task = task
            self._preparation_tasks.add(task)
            task.add_done_callback(self._preparation_tasks.discard)
        self._jpeg_pending_directions |= changing | {side}
        self._buffer_jpeg_processing(side, data, facts)
        self._notify_media_processing()
        return True

    async def _run_jpeg_processing(self):
        left, right = self.left, self.right
        try:
            while (not self._stop_requested and not self._staged_peers
                   and self.left is left and self.right is right):
                key = self._jpeg_processing_key()
                wanted = self.transcode_directions_for(self.left, self.right)
                changing = wanted ^ self._transcode.directions
                if not changing:
                    break
                self._jpeg_pending_directions = changing | {
                    side for side in ("left", "right") if self._transcode_startup_rtp[side]}
                rollback = None
                try:
                    commit, rollback = await self._prepare_peer_generation(
                        left=self.left, right=self.right,
                        processing_directions=wanted, processing_key=self._jpeg_context_key())
                    await commit()
                    self._jpeg_failed_key = None
                    self._jpeg_failed_directions.clear()
                except BaseException as error:
                    if rollback is not None:
                        await async_wait_for_cleanup(asyncio.create_task(rollback()))
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    if self._stop_requested or self._jpeg_processing_key() == key:
                        self._jpeg_failed_key = key
                        self._jpeg_failed_directions = changing
                        _LOGGER.debug("JPEG processing preparation failed", exc_info=True)
                        break
                    # A newer admitted geometry supersedes this preparation.
        finally:
            self._jpeg_pending_directions.clear()
            if self._jpeg_geometry_task is asyncio.current_task():
                self._jpeg_geometry_task = None
            if not self._stop_requested:
                if self.left is left and self.right is right:
                    self._flush_jpeg_processing()
                else:
                    for side in ("left", "right"):
                        self._discard_jpeg_startup(side)
                self._notify_media_processing()

    def transcode_directions_for(
        self,
        left: VideoRtpPeer,
        right: VideoRtpPeer,
    ) -> set[str]:
        directions: set[str] = set()
        for side, source, destination in (
            ("left", left, right),
            ("right", right, left),
        ):
            if not remote_can_send(source.video_format) or not remote_can_receive(
                destination.video_format, connection_held=destination.connection_held
            ):
                continue
            if video_formats_passthrough_compatible(
                source.recv_format,
                destination.send_format,
            ) and not (self._transcode_hass is not None and self._jpeg_needs_resize(source, destination)):
                continue
            if not video_transcode_supported(
                source.recv_format,
                destination.send_format,
            ):
                raise ValueError(
                    "unsupported directional SIP video transcode "
                    f"{source.recv_format.encoding}->{destination.send_format.encoding}"
                )
            directions.add(side)
        return directions

    def arm_keyframe_request(self, side: str) -> bool:
        """Arm recovery only for the decoder this relay actually owns."""

        if side not in self._transcode.directions:
            return False
        source, _destination = self._peers(side)
        feedback = set(source.recv_format.rtcp_feedback)
        if (
            not feedback.intersection({"ccm fir", "nack pli"})
        ):
            return False
        self._keyframe_requests.setdefault(side, None)
        return True

    def request_keyframe(self, side: str) -> bool:
        """Use the same validated RTCP request for feedback and legacy SIP INFO.

        This requests recovery from the remote source. FFmpeg output continues
        to use its configured periodic keyframes; no dynamic encoder API exists.
        """
        if side not in {"left", "right"} or self._stop_requested:
            return False
        source, _destination = self._peers(side)
        if not set(source.recv_format.rtcp_feedback).intersection({"ccm fir", "nack pli"}):
            return False
        previous = self._keyframe_attempts.get(side)
        if previous is not None and (side not in self._keyframe_requests or previous[1] >= 3):
            self._keyframe_attempts[side] = (previous[0], 0, previous[2])
        self._keyframe_requests[side] = None
        self._send_armed_keyframe_request(side, source)
        return True

    def _send_armed_keyframe_request(self, side: str, source: VideoRtpPeer) -> None:
        if (
            side not in self._keyframe_requests
            or source.rx_ssrc is None
        ):
            return
        feedback = set(source.recv_format.rtcp_feedback)
        if not feedback.intersection({"ccm fir", "nack pli"}):
            return
        now = monotonic()
        ssrc, count, previous = self._keyframe_attempts.get(side, (source.rx_ssrc, 0, 0.0))
        if ssrc != source.rx_ssrc:
            count, previous = 0, 0.0
        if count >= 3 or now - previous < 0.5:
            return
        if "ccm fir" in feedback:
            self._fir_sequence = (self._fir_sequence + 1) & 0xFF
            packet = build_fir(self._rtcp_ssrc, source.rx_ssrc, self._fir_sequence)
        else:
            packet = build_pli(self._rtcp_ssrc, source.rx_ssrc)
        output = self._transports.get((side, True))
        if output is None or source.rtcp_port <= 0:
            return
        raw = build_receiver_compound(
            self._rtcp_ssrc,
            source.rx_ssrc,
            feedback=packet,
        )
        output.sendto(raw, (source.rtcp_host, int(source.rtcp_port)))
        self._keyframe_requests[side] = source.rx_ssrc
        self._keyframe_attempts[side] = (source.rx_ssrc, count + 1, now)
        self.rtcp_forwarded += 1

    @property
    def stopping(self) -> bool:
        """Return whether a subsequent call may await this relay's release."""

        return self._stop_requested or self._released

    def configure_transcoding(self, hass: Any, call_id: str) -> None:
        """Configure every incompatible negotiated direction for FFmpeg.

        A later RFC 3264 offer may change ``recvonly`` or ``sendonly`` to
        ``sendrecv`` without changing codecs.  Provision both codec paths at
        bridge creation so that such a direction change is an atomic peer
        update, not a second media lifecycle.
        """

        if self.started or self._start_task is not None:
            raise RuntimeError("cannot configure transcoding after relay start")
        self._transcode_hass = hass
        self._transcode_call_id = str(call_id or "sip-video")
        self._transcode.directions = self.transcode_directions_for(self.left, self.right)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.started:
                return
            if self._released or self._stop_requested or self._stop_task is not None:
                raise RuntimeError("SIP video relay has already been stopped")
            task = self._start_task
            if task is None:
                task = asyncio.create_task(
                    self._start(),
                    name=f"voip-video-relay-start-{self.left_port}-{self.right_port}",
                )
                self._start_task = task
        try:
            await task
        finally:
            async with self._lifecycle_lock:
                if self._start_task is task and task.done():
                    self._start_task = None

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            for side, port in (("left", self.left_port), ("right", self.right_port)):
                for rtcp in (False, True):
                    key = (side, rtcp)
                    sock = self._sockets.get(key)
                    if sock is None:
                        sock = self._socket(port + (1 if rtcp else 0))
                        # Keep ownership visible before the first cancellation
                        # point.  create_datagram_endpoint() only takes over
                        # the socket when it succeeds; a failed or cancelled
                        # await must leave stop() able to close it.
                        self._sockets[key] = sock
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda side=side, rtcp=rtcp: _VideoRelayProtocol(
                            self, side, rtcp=rtcp
                        ),
                        sock=sock,
                    )
                    if self._stop_requested:
                        transport.close()
                        raise asyncio.CancelledError
                    # Ownership moves to the transport only after endpoint
                    # creation succeeds. On failure stop() must still see and
                    # close the pre-bound socket supplied by the port owner.
                    self._sockets[key] = None
                    self._transports[key] = transport
            if self._transcode.directions:
                await self._start_transcoding(loop)
            for side in ("left", "right"):
                self.arm_keyframe_request(side)
            self.started = True
            self._maintain_nat()
        except BaseException:
            await self._stop_transcoding()
            self._close_resources()
            self._release_ports()
            raise
        _LOGGER.info(
            "SIP video relay ready left=%s/%s right=%s/%s codec=%s",
            self.left_port,
            self.left_port + 1,
            self.right_port,
            self.right_port + 1,
            self.left.video_format.encoding,
        )

    async def _start_transcoding(self, loop: asyncio.AbstractEventLoop) -> None:
        hass = self._transcode_hass
        if hass is None:
            raise RuntimeError("SIP video transcode has no Home Assistant owner")
        await _register_transcoder(hass, self, self.stop)
        self._transcode_registered = True
        try:
            generation = await self._build_transcode_generation(
                loop,
                self.left,
                self.right,
            )
            self._install_transcode_generation(generation)
            self._flush_transcode_startup_rtp()
        except BaseException:
            await self._stop_transcoding()
            raise

    def _maintain_nat(self) -> None:
        """Keep receive-only video reachable without producing RTP media.

        RFC 6263 section 4.4: a STUN Binding Indication opens the actual RTP
        tuple and is ignored by peers that do not implement STUN. It does not
        introduce another RTP source or require negotiated RTCP multiplexing.
        """
        if self._nat_timer is not None:
            self._nat_timer.cancel()
            self._nat_timer = None
        if not self.started or self._stop_requested:
            return
        for side, peer in (("left", self.left), ("right", self.right)):
            if peer.connection_held:
                continue
            for rtcp, host, port in ((False, peer.host, peer.port),
                                     (True, peer.rtcp_host, peer.rtcp_port)):
                transport = self._transports.get((side, rtcp))
                if transport is None or port <= 0:
                    continue
                indication = struct.pack("!HHI", 0x0011, 0, 0x2112A442) + secrets.token_bytes(12)
                try:
                    transport.sendto(indication, (host, port))
                except OSError as err:
                    _LOGGER.debug("Video NAT indication failed side=%s rtcp=%s: %s", side, rtcp, err)
        # A peer may install its RTP tuple only after the SDP transaction ACK.
        # Probe promptly until the negotiated sender is observed, then maintain
        # the mapping at the ordinary interval. The peer's RTP latch owns this
        # readiness state, including after a committed address change.
        awaiting_source = any(
            remote_can_send(peer.video_format) and peer.rx_ssrc is None
            for peer in (self.left, self.right)
        )
        self._nat_timer = asyncio.get_running_loop().call_later(
            1 if awaiting_source else 15, self._maintain_nat
        )

    async def _build_transcode_generation(
        self,
        loop: asyncio.AbstractEventLoop,
        left: VideoRtpPeer,
        right: VideoRtpPeer,
        *,
        suffix: str = "",
        directions: set[str] | None = None,
    ) -> _TranscodeGeneration:
        hass = self._transcode_hass
        if hass is None:
            raise RuntimeError("SIP video transcode has no Home Assistant owner")
        generation = _TranscodeGeneration(
            self.transcode_directions_for(left, right) if directions is None else set(directions),
            {},
            {},
            {},
        )
        try:
            for source_side in sorted(generation.directions):
                source, destination = (
                    (left, right) if source_side == "left" else (right, left)
                )
                pair = self._loopback_socket_pair()
                rtp_port = int(pair[0].getsockname()[1])
                for rtcp, sock in ((False, pair[0]), (True, pair[1])):
                    key = (source_side, rtcp)
                    generation.sockets[key] = sock
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda source_side=source_side, rtcp=rtcp: (
                            _TranscodedOutputProtocol(self, source_side, rtcp=rtcp)
                        ),
                        sock=sock,
                    )
                    generation.sockets[key] = None
                    generation.transports[key] = transport
                generation.transcoders[source_side] = FfmpegVideoTranscoder(
                    hass=hass,
                    call_id=(f"{self._transcode_call_id}-{source_side}{suffix}"),
                    input_format=source.recv_format,
                    output_format=destination.send_format,
                    output_port=rtp_port,
                    resource_owner=self,
                    manage_registration=False,
                )
            await asyncio.gather(
                *(item.async_start() for item in generation.transcoders.values())
            )
        except BaseException:
            await self._close_transcode_generation(generation)
            raise
        return generation

    @staticmethod
    def _take_transcode_resources(generation, directions):
        """Move resources for selected directions without sharing their ownership."""
        return _TranscodeGeneration(set(directions),
            {side: generation.transcoders.pop(side) for side in directions if side in generation.transcoders},
            {key: generation.transports.pop(key) for key in tuple(generation.transports) if key[0] in directions},
            {key: generation.sockets.pop(key) for key in tuple(generation.sockets) if key[0] in directions})

    @staticmethod
    def _adopt_transcode_resources(target, source):
        for name in ("transcoders", "transports", "sockets"):
            getattr(target, name).update(getattr(source, name))
            getattr(source, name).clear()

    def _install_transcode_generation(
        self,
        generation: _TranscodeGeneration,
    ) -> None:
        self._transcode = generation
        self._keyframe_requests.clear()
        self._keyframe_attempts.clear()
        self._transcoded_ssrc.clear()
        for side in ("left", "right"):
            self.arm_keyframe_request(side)

    def _buffer_transcode_startup_rtp(self, side: str, data: bytes) -> bool:
        pending = self._transcode_startup_rtp[side]
        pending_bytes = self._transcode_startup_bytes[side]
        if (
            len(pending) >= _TRANSCODE_STARTUP_MAX_PACKETS
            or pending_bytes + len(data) > _TRANSCODE_STARTUP_MAX_BYTES
        ):
            self.dropped += 1
            self.transcode_startup_dropped += 1
            return False
        pending.append(data)
        self._transcode_startup_bytes[side] = pending_bytes + len(data)
        self.transcode_startup_buffered += 1
        return True

    def _flush_transcode_startup_rtp(self) -> None:
        for side in ("left", "right"):
            pending = self._transcode_startup_rtp[side]
            self._transcode_startup_rtp[side] = []
            self._transcode_startup_bytes[side] = 0
            transcoder = self._transcode.transcoders.get(side)
            if transcoder is None or not transcoder.ready:
                self.dropped += len(pending)
                self.transcode_startup_dropped += len(pending)
                continue
            for data in pending:
                try:
                    transcoder.send_rtp(data)
                except (OSError, RuntimeError, ValueError):
                    self.dropped += 1
                    self.transcode_startup_dropped += 1
                    continue
                self.transcoded_input_packets += 1

    async def _stop_transcoding(self) -> None:
        for side in ("left", "right"):
            self._transcode_startup_rtp[side].clear()
            self._transcode_startup_bytes[side] = 0
        generation = self._transcode
        self._transcode = _TranscodeGeneration(set(), {}, {}, {})
        await self._close_transcode_generation(generation)
        if self._transcode_registered and self._transcode_hass is not None:
            await _unregister_transcoder(self._transcode_hass, self)
        self._transcode_registered = False

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stop_requested = True
            self.on_media_processing_changed = None
            task = self._stop_task
            if task is None:
                task = asyncio.create_task(
                    self._stop(),
                    name=(f"voip-video-relay-stop-{self.left_port}-{self.right_port}"),
                )
                self._stop_task = task
        await async_wait_for_cleanup(task)

    async def _stop(self) -> None:
        """Finish one idempotent shutdown independently of caller lifetime."""

        async with self._lifecycle_lock:
            start_task = self._start_task
            if start_task is not None and not start_task.done():
                start_task.cancel()
            preparations = tuple(self._preparation_tasks)
            for preparation in preparations:
                preparation.cancel()
        if start_task is not None and start_task is not asyncio.current_task():
            await asyncio.gather(start_task, return_exceptions=True)
        await asyncio.gather(*preparations, return_exceptions=True)
        self._preparation_tasks.difference_update(preparations)
        async with self._lifecycle_lock:
            try:
                for generation in tuple(self._provisional_transcodes.values()):
                    await self._close_transcode_generation(generation)
                    self._provisional_transcodes.pop(id(generation), None)
                await self._stop_transcoding()
            finally:
                try:
                    self._close_resources()
                finally:
                    self._release_ports()

    def _close_resources(self) -> None:
        """Synchronously detach every socket/transport from the relay."""

        if self._nat_timer is not None:
            self._nat_timer.cancel()
            self._nat_timer = None
        self._staged_peers.clear()
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()
        for sock in self._sockets.values():
            if sock is not None:
                sock.close()
        self._sockets.clear()
        was_started = self.started
        self.started = False
        if was_started:
            _LOGGER.info(
                "SIP video relay stopped forwarded=%d rtcp=%d dropped=%d "
                "transcoded_in=%d transcoded_out=%d",
                self.forwarded,
                self.rtcp_forwarded,
                self.dropped,
                self.transcoded_input_packets,
                self.transcoded_output_packets,
            )

    def _release_ports(self) -> None:
        """Return the reserved RTP pairs exactly once."""

        if self._on_release is not None and not self._released:
            self._released = True
            self._on_release((self.left_port, self.right_port))

    def _peers(self, side: str) -> tuple[VideoRtpPeer, VideoRtpPeer]:
        return (self.left, self.right) if side == "left" else (self.right, self.left)

    def prepare_peer_reconfiguration(
        self, side: str, peer: VideoRtpPeer
    ) -> Callable[[], None]:
        """Stage one video peer update and reject a stale commit."""

        if side not in {"left", "right"}:
            raise ValueError(f"unknown video relay side: {side}")
        previous = self.left if side == "left" else self.right

        def _commit() -> None:
            current = self.left if side == "left" else self.right
            if current is not previous:
                raise RuntimeError(f"video relay peer changed before {side} commit")
            peer.rx_ssrc = None
            peer.receive_state = rtp.RtpReceiveState()
            peer.rtcp_source_port = None
            self._retain_jpeg_observation(previous, peer)
            if side == "left":
                self.left = peer
            else:
                self.right = peer
            for source_side in ("left", "right"):
                self._keyframe_requests.pop(source_side, None)
                self._keyframe_attempts.pop(source_side, None)
                self.arm_keyframe_request(source_side)

        return _commit

    def stage_peer_reconfiguration(
        self, side: str, peer: VideoRtpPeer
    ) -> tuple[Callable[[], None], Callable[[], None]]:
        """Buffer bounded RTP for a pending SDP peer contract.

        An offerer may switch to a newly offered receive path before the
        answer transaction completes.  Preserve only packets matching the
        pending authenticated peer, then replay them after the same atomic
        peer commit so in-band H.264 SPS/PPS are not lost.
        """

        if side not in {"left", "right"}:
            raise ValueError(f"unknown video relay side: {side}")
        if side in self._staged_peers:
            raise RuntimeError(f"video relay already stages {side} peer")
        staged = _StagedVideoPeer(peer, [])
        self._staged_peers[side] = staged
        commit_peer = self.prepare_peer_reconfiguration(side, peer)

        def rollback() -> None:
            if self._staged_peers.get(side) is staged:
                self._staged_peers.pop(side, None)

        def commit() -> None:
            if self._staged_peers.get(side) is not staged:
                raise RuntimeError(f"video relay staged {side} peer expired")
            self._staged_peers.pop(side, None)
            commit_peer()
            _LOGGER.debug(
                "SIP video peer stage committed side=%s packets=%d bytes=%d",
                side,
                len(staged.packets),
                staged.byte_count,
            )
            for data, addr in staged.packets:
                self.handle_rtp(side, data, addr)

        return commit, rollback

    def update_staged_peer(self, side: str, expected: VideoRtpPeer, candidate: VideoRtpPeer) -> None:
        """Complete an authenticated pending offer without losing its early RTP."""
        staged = self._staged_peers.get(side)
        if staged is None or staged.peer is not expected:
            raise RuntimeError("video peer stage changed before its answer")
        staged.peer = candidate
        staged.packets[:] = [(data, addr) for data, addr in staged.packets
                             if self._matches_staged_rtp(candidate, data, addr)]
        staged.byte_count = sum(len(data) for data, _addr in staged.packets)

    async def async_prepare_peer_generation(
        self,
        *,
        left: VideoRtpPeer,
        right: VideoRtpPeer,
    ) -> tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]]:
        """Join preparation through a child owned by this relay, not its SIP caller."""

        if self._stop_requested:
            raise RuntimeError("video relay is stopping")
        geometry_task = self._jpeg_geometry_task
        if geometry_task is not None and not geometry_task.done():
            geometry_task.cancel()
            await asyncio.gather(geometry_task, return_exceptions=True)
        task = asyncio.create_task(
            self._prepare_peer_generation(left=left, right=right),
            name=f"voip-video-generation-prepare-{self.left_port}-{self.right_port}",
        )
        self._preparation_tasks.add(task)

        async def abort() -> None:
            if not task.done():
                task.cancel()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            if not isinstance(result, BaseException):
                _commit, rollback = result
                await rollback()

        try:
            return await task
        except BaseException:
            await async_wait_for_cleanup(asyncio.create_task(
                abort(), name="voip-video-generation-abort",
            ))
            raise
        finally:
            self._preparation_tasks.discard(task)

    async def _prepare_peer_generation(
        self,
        *,
        left: VideoRtpPeer,
        right: VideoRtpPeer,
        processing_directions: set[str] | None = None,
        processing_key: tuple | None = None,
    ) -> tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]]:
        """Share one atomic generation cutover for SDP and admitted RTP facts."""

        if not self.started:
            raise RuntimeError("video relay is not active")
        previous_left = self.left
        previous_right = self.right
        staged_left = self._staged_peers.get("left")
        processing_only = processing_directions is not None
        if processing_only and (left is not previous_left or right is not previous_right or self._staged_peers):
            raise RuntimeError("RTP processing cannot replace an SDP peer")
        if not processing_only and left is not previous_left and (
            staged_left is None or staged_left.peer is not left
        ):
            raise RuntimeError("left video peer is not staged")
        staged_right = self._staged_peers.get("right")
        if not processing_only and (staged_right is None or staged_right.peer is not right):
            raise RuntimeError("right video peer is not staged")
        directions = (self.transcode_directions_for(left, right) if processing_directions is None
                      else set(processing_directions))

        # A direction/address change does not invalidate codec reference frames.
        # Retain the existing conversion processes when their contracts match.
        same_codecs = directions == self._transcode.directions and all(
            replace(before, direction="sendrecv") == replace(after, direction="sendrecv")
            for old, new in ((previous_left, left), (previous_right, right))
            for before, after in (
                (old.recv_format, new.recv_format),
                (old.send_format, new.send_format),
            )
        )
        if (not self._transcode.directions and not directions) or same_codecs:
            settled = False

            async def rollback_passthrough() -> None:
                nonlocal settled
                if settled:
                    return
                settled = True
                if processing_only:
                    return
                if self._staged_peers.get("left") is staged_left:
                    self._staged_peers.pop("left", None)
                if self._staged_peers.get("right") is staged_right:
                    self._staged_peers.pop("right", None)

            async def commit_passthrough() -> None:
                nonlocal settled
                if settled:
                    raise RuntimeError("video peer generation already settled")
                if (
                    self._stop_requested
                    or self.left is not previous_left
                    or self.right is not previous_right
                    or (
                        staged_left is not None
                        and self._staged_peers.get("left") is not staged_left
                    )
                    or self._staged_peers.get("right") is not staged_right
                    or (processing_only and (
                        self._jpeg_context_key() != processing_key
                        or self.transcode_directions_for(self.left, self.right) != directions))
                ):
                    await rollback_passthrough()
                    raise RuntimeError(
                        "video relay peer changed before generation commit"
                    )
                settled = True
                if processing_only:
                    self._flush_jpeg_processing()
                    return
                if staged_left is not None:
                    self._staged_peers.pop("left", None)
                self._staged_peers.pop("right", None)
                left.rx_ssrc = None
                left.rtcp_source_port = None
                right.rx_ssrc = None
                right.rtcp_source_port = None
                self._retain_jpeg_observation(previous_left, left)
                self._retain_jpeg_observation(previous_right, right)
                self.left = left
                self.right = right
                self._maintain_nat()
                for staged_side, staged_peer in (
                    ("left", staged_left),
                    ("right", staged_right),
                ):
                    if staged_peer is None:
                        continue
                    for data, addr in staged_peer.packets:
                        self.handle_rtp(staged_side, data, addr)

            return commit_passthrough, rollback_passthrough

        old = self._transcode
        retained = set()
        for side in old.directions & directions:
            old_source, old_destination = ((previous_left, previous_right) if side == "left"
                                            else (previous_right, previous_left))
            source, destination = (left, right) if side == "left" else (right, left)
            if all(replace(before, direction="sendrecv") == replace(after, direction="sendrecv")
                    for before, after in ((old_source.recv_format, source.recv_format),
                                          (old_destination.send_format, destination.send_format))):
                retained.add(side)
        new_directions = directions - retained
        replaced = old.directions & new_directions
        removed = old.directions - directions
        if new_directions and self._transcode_hass is None:
            raise RuntimeError("video relay transcoding is not configured")
        loop = asyncio.get_running_loop()
        new_owner_registration = bool(new_directions and not self._transcode_registered)
        generation = _TranscodeGeneration(set(), {}, {}, {})

        def current():
            return (not self._stop_requested and self._transcode is old
                and self.left is previous_left and self.right is previous_right
                and (staged_left is None or self._staged_peers.get("left") is staged_left)
                and self._staged_peers.get("right") is staged_right
                and (not processing_only or (
                    self._jpeg_context_key() == processing_key
                    and self.transcode_directions_for(self.left, self.right) == directions)))

        def clear_stages():
            for side, staged in (("left", staged_left), ("right", staged_right)):
                if self._staged_peers.get(side) is staged:
                    self._staged_peers.pop(side, None)

        async def restore_replaced():
            # Another leg may commit an RTP tuple while this offer is pending.
            # Restore this generation's workers without reverting that peer.
            if not replaced or self._stop_requested or self._transcode is not old:
                return
            try:
                restored = await self._build_transcode_generation(loop, previous_left, previous_right,
                    suffix="-restore", directions=replaced)
            except BaseException as err:
                if self._stop_requested or self._transcode is not old:
                    return
                raise VideoTranscodeRestoreError("Previous video converters could not be restored") from err
            if self._stop_requested or self._transcode is not old:
                await self._close_transcode_generation(restored)
                return
            self._adopt_transcode_resources(old, restored)
            for side in replaced:
                self._keyframe_requests.pop(side, None)
                self._keyframe_attempts.pop(side, None)
                self._transcoded_ssrc.pop(side, None)
                self.arm_keyframe_request(side)

        async def recover():
            try:
                await self._close_transcode_generation(generation)
                self._provisional_transcodes.pop(id(generation), None)
                await restore_replaced()
            finally:
                clear_stages()
                if new_owner_registration:
                    await _unregister_transcoder(self._transcode_hass, self)
                    self._transcode_registered = False

        try:
            if new_owner_registration:
                await _register_transcoder(self._transcode_hass, self, self.stop)
                self._transcode_registered = True
            if not current():
                raise RuntimeError("video relay owner changed before converter preparation")
            # A changed codec cannot share a worker with the old contract.
            # Stop only those directions before starting their replacements.
            if replaced:
                await async_wait_for_cleanup(asyncio.create_task(self._close_transcode_generation(
                    self._take_transcode_resources(old, replaced)), name="voip-video-replace-close"))
            if not current():
                raise RuntimeError("video relay owner changed before replacement start")
            if new_directions:
                generation = await self._build_transcode_generation(loop, left, right,
                    suffix="-reoffer", directions=new_directions)
                self._provisional_transcodes[id(generation)] = generation
            if not current():
                raise RuntimeError("video relay owner changed during generation preparation")
        except BaseException:
            await async_wait_for_cleanup(asyncio.create_task(recover(), name="voip-video-prepare-recovery"))
            raise

        settled = False

        async def rollback() -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            task = asyncio.create_task(recover(), name="voip-video-generation-rollback")
            self._preparation_tasks.add(task)
            try:
                await async_wait_for_cleanup(task)
            finally:
                self._preparation_tasks.discard(task)

        async def settle_commit() -> None:
            if not current():
                await recover()
                raise RuntimeError("video relay owner changed before commit settlement")
            for candidate, previous in ((left, previous_left), (right, previous_right)):
                if candidate is not previous:
                    candidate.rx_ssrc = None
                    candidate.receive_state = rtp.RtpReceiveState()
                    candidate.rtcp_source_port = None
                    self._retain_jpeg_observation(previous, candidate)
            retired = self._take_transcode_resources(old, removed)
            self._adopt_transcode_resources(old, generation)
            self._provisional_transcodes.pop(id(generation), None)
            old.directions = directions
            self.left = left
            self.right = right
            for side in removed:
                source, destination = self._peers(side)
                if source.recv_format.encoding == destination.send_format.encoding == "JPEG":
                    source.jpeg_raw_bootstrap = True
            self._maintain_nat()
            for side in ("left", "right"):
                self._keyframe_requests.pop(side, None)
                self._keyframe_attempts.pop(side, None)
                if side in removed | new_directions:
                    self._transcoded_ssrc.pop(side, None)
                self.arm_keyframe_request(side)
            await self._close_transcode_generation(retired)
            clear_stages()
            if processing_only:
                self._flush_jpeg_processing()
            else:
                for side in ("left", "right"):
                    self._transcode_startup_rtp[side].clear()
                    self._transcode_startup_bytes[side] = 0
            for staged_side, staged_peer in (
                ("left", staged_left),
                ("right", staged_right),
            ):
                if staged_peer is None:
                    continue
                for data, addr in staged_peer.packets:
                    self.handle_rtp(staged_side, data, addr)
            if not directions and self._transcode_registered:
                await _unregister_transcoder(self._transcode_hass, self)
                self._transcode_registered = False

        async def commit() -> None:
            nonlocal settled
            if settled:
                raise RuntimeError("video peer generation already settled")
            if not current():
                await rollback()
                raise RuntimeError("video relay peer changed before generation commit")
            settled = True
            task = asyncio.create_task(settle_commit(), name="voip-video-generation-commit")
            self._preparation_tasks.add(task)
            try:
                await async_wait_for_cleanup(task)
            finally:
                self._preparation_tasks.discard(task)

        return commit, rollback

    @staticmethod
    async def _close_transcode_generation(
        generation: _TranscodeGeneration,
    ) -> None:
        if generation._cleanup_task is None:
            generation._cleanup_task = asyncio.create_task(
                SipVideoRtpRelay._dispose_transcode_generation(generation),
                name="voip-video-generation-close",
            )
        await async_wait_for_cleanup(generation._cleanup_task)

    @staticmethod
    async def _dispose_transcode_generation(
        generation: _TranscodeGeneration,
    ) -> None:
        await asyncio.gather(
            *(item.async_close() for item in generation.transcoders.values()),
            return_exceptions=True,
        )
        for transport in generation.transports.values():
            transport.close()
        for sock in generation.sockets.values():
            if sock is not None:
                sock.close()

    def reconfigure_peer(self, side: str, peer: VideoRtpPeer) -> None:
        """Atomically replace one negotiated video peer and reset its latch."""

        self.prepare_peer_reconfiguration(side, peer)()

    def handle_rtp(self, side: str, data: bytes, addr) -> None:
        if is_stun_message(data):
            return
        if self._stop_requested:
            self.ignored_after_stop += 1
            return
        source, destination = self._peers(side)
        staged = self._staged_peers.get(side)
        if staged is not None and self._matches_staged_rtp(staged.peer, data, addr):
            if (
                len(staged.packets) >= _REOFFER_STAGE_MAX_PACKETS
                or staged.byte_count + len(data) > _REOFFER_STAGE_MAX_BYTES
            ):
                self.dropped += 1
                return
            staged.packets.append((data, tuple(addr)))
            staged.byte_count += len(data)
            return
        try:
            if destination.connection_held:
                self.drop_connection_hold += 1
                raise ValueError("destination connection is held")
            if not remote_can_send(source.video_format) or not remote_can_receive(
                destination.video_format,
                connection_held=destination.connection_held,
            ):
                raise ValueError("RTP direction is not negotiated")
            if side not in self._transcode.directions and not (
                video_formats_passthrough_compatible(
                    source.recv_format,
                    destination.send_format,
                )
            ):
                raise ValueError("directional RTP codec contracts are incompatible")
            if not source.accepts_rtp_source_host(str(addr[0])):
                raise ValueError("unexpected RTP source host")
            packet = rtp.parse_packet(data)
            if packet.payload_type != int(source.recv_format.payload_type):
                raise ValueError("unexpected RTP payload type")
            if source.receive_state.ssrc is None and source.rx_ssrc is not None:
                source.receive_state.ssrc = source.rx_ssrc
            if not source.receive_state.admit(packet):
                raise ValueError("duplicate RTP or unconfirmed source transition")
            if source.receive_state.changed:
                waiting = side in self._keyframe_requests
                self._keyframe_requests.pop(side, None)
                self._keyframe_attempts.pop(side, None)
                if not self.arm_keyframe_request(side) and waiting:
                    # Retain an explicit receiver request across a validated
                    # SSRC change; this does not arm an idle pure relay.
                    self._keyframe_requests[side] = None
            source.rx_ssrc = packet.ssrc
            self._send_armed_keyframe_request(side, source)
            source.host = str(addr[0])
            source.port = int(addr[1])
            if self._bounded_jpeg(side):
                facts, resize = self._observe_jpeg_packet(side, source, destination, packet)
                current_frame = packet.timestamp == source.jpeg_timestamp
                if current_frame and self._queue_jpeg_processing(side, data, facts):
                    return
                if side not in self._transcode.directions and resize:
                    self._notify_media_processing()
                    raise ValueError("actual JPEG exceeds the receiver contract")
            self._deliver_admitted_rtp(side, data, packet)
            self._notify_media_processing()
        except (OSError, RuntimeError, ValueError) as err:
            self.dropped += 1
            self._record_rtp_drop(err)
            self._notify_media_processing()
            _LOGGER.debug("SIP video RTP relay drop side=%s: %s", side, err)
            return

    def _deliver_admitted_rtp(self, side, data, packet, *, received=True):
        """Deliver an already admitted packet, counting its input at most once."""
        source, destination = self._peers(side)
        destination_side = "right" if side == "left" else "left"
        if side in self._transcode.directions:
            transcoder = self._transcode.transcoders.get(side)
            if transcoder is None or not getattr(transcoder, "ready", True):
                if not self.started and received:
                    if self._buffer_transcode_startup_rtp(side, data):
                        self._account_received(side, len(data))
                    return
                raise RuntimeError("directional FFmpeg transcoder is not ready")
            internal = (source.jpeg_parser.bootstrap_rtp(data, packet)
                        if source.jpeg_parser is not None else data)
            transcoder.send_rtp(internal)
            if received:
                self._account_received(side, len(data))
            self.transcoded_input_packets += 1
            return
        output = self._transports.get((destination_side, False))
        if output is None or destination.port <= 0 or destination.connection_held:
            raise ValueError("destination RTP leg is not ready")
        payload_type = int(destination.send_format.payload_type)
        if not 0 <= payload_type <= 127:
            raise ValueError("invalid destination RTP payload type")
        outgoing = data
        if source.jpeg_raw_bootstrap and source.jpeg_parser is not None:
            if parse_jpeg_rtp_header(packet.payload).fragment_offset:
                raise ValueError("JPEG passthrough restart needs a first fragment")
            outgoing = source.jpeg_parser.bootstrap_rtp(data, packet)
        if payload_type != packet.payload_type:
            outgoing = bytes((outgoing[0], (outgoing[1] & 0x80) | payload_type)) + outgoing[2:]
        output.sendto(outgoing, (destination.host, int(destination.port)))
        source.jpeg_raw_bootstrap = False
        if received:
            self._account_received(side, len(data))
        self._account_sent(destination_side, len(outgoing))
        self.forwarded += 1

    @staticmethod
    def _matches_staged_rtp(
        candidate: VideoRtpPeer,
        data: bytes,
        addr,
    ) -> bool:
        """Return whether RTP belongs only to the pending peer contract."""

        if not remote_can_send(candidate.video_format):
            return False
        if not candidate.accepts_rtp_source_host(str(addr[0])):
            return False
        try:
            packet = rtp.parse_packet(data)
        except ValueError:
            return False
        return packet.payload_type == int(candidate.recv_format.payload_type)

    def handle_transcoded_rtp(self, source_side: str, data: bytes) -> None:
        _source, destination = self._peers(source_side)
        destination_side = "right" if source_side == "left" else "left"
        output = self._transports.get((destination_side, False))
        try:
            if destination.connection_held or destination.port <= 0:
                raise ValueError("transcoded destination RTP leg is not ready")
            packet = rtp.parse_packet(data)
            if packet.payload_type != int(destination.send_format.payload_type):
                raise ValueError("unexpected transcoded RTP payload type")
            self._transcoded_ssrc[source_side] = packet.ssrc
            if output is None:
                raise ValueError("transcoded destination RTP transport is not ready")
            output.sendto(data, (destination.host, int(destination.port)))
        except (OSError, RuntimeError, ValueError) as err:
            self.dropped += 1
            _LOGGER.debug(
                "SIP transcoded video RTP drop side=%s: %s",
                source_side,
                err,
            )
            return
        if packet.marker:
            # A complete output frame from the existing FFmpeg consumer proves
            # its upstream decoder has recovered. A relay-only leg has no
            # decoder, buffer or implicit recovery train of its own.
            self._keyframe_requests.pop(source_side, None)
        self._account_sent(destination_side, len(data))
        self.transcoded_output_packets += 1
        self.forwarded += 1

    def handle_transcoded_rtcp(self, source_side: str, data: bytes) -> None:
        _source, destination = self._peers(source_side)
        destination_side = "right" if source_side == "left" else "left"
        output = self._transports.get((destination_side, True))
        try:
            parse_compound(data)
            if output is None or destination.rtcp_port <= 0:
                raise ValueError("transcoded destination RTCP leg is not ready")
            output.sendto(data, (destination.rtcp_host, int(destination.rtcp_port)))
        except (OSError, RuntimeError, RtcpError, ValueError) as err:
            self.dropped += 1
            _LOGGER.debug(
                "SIP transcoded video RTCP drop side=%s: %s",
                source_side,
                err,
            )
            return
        self.transcoded_rtcp_packets += 1
        self.rtcp_forwarded += 1

    def handle_rtcp(self, side: str, data: bytes, addr) -> None:
        if is_stun_message(data):
            return
        source, destination = self._peers(side)
        destination_side = "right" if side == "left" else "left"
        output = self._transports.get((destination_side, True))
        try:
            if destination.connection_held:
                self.drop_connection_hold += 1
                raise ValueError("destination RTCP connection is held")
            if not source.accepts_rtcp_source_host(str(addr[0])):
                raise ValueError("unexpected RTCP source host")
            packets = parse_compound(data)
            selected = []
            source_transcoded = self.transcodes_from(side)
            destination_transcoded = self.transcodes_from(destination_side)
            expected = (self._transcoded_ssrc.get(destination_side)
                        if destination_transcoded else destination.rx_ssrc)
            for packet in packets:
                payload, count = packet.payload, packet.fmt
                if packet.packet_type in {205, 206}:
                    if len(payload) < 8:
                        self.dropped += 1
                        continue
                    if packet.packet_type != 206 or count not in {1, 4}:
                        # Unsupported feedback does not invalidate reports or
                        # supported feedback in the same compound datagram.
                        self.transcode_rtcp_dropped += 1
                        continue
                    if ((count == 1 and len(payload) != 8)
                            or (count == 4 and (len(payload) < 16 or (len(payload) - 8) % 8))):
                        self.dropped += 1
                        continue
                    targets = (tuple(struct.unpack_from("!I", payload, offset)[0]
                        for offset in range(8, len(payload), 8))
                        if packet.packet_type == 206 and count == 4 else
                        (struct.unpack_from("!I", payload, 4)[0],))
                    if expected is not None and any(target != expected for target in targets):
                        self.dropped += 1
                        continue
                    if destination_transcoded:
                        # The encoder's output stream has a distinct SSRC and
                        # counters. It has periodic recovery, no live FFmpeg
                        # keyframe control. Only request upstream decoder repair.
                        if packet.packet_type == 206 and count in {1, 4}:
                            self.request_keyframe(destination_side)
                        self.transcode_rtcp_dropped += 1
                        continue
                elif packet.packet_type in {200, 201}:
                    base = 24 if packet.packet_type == 200 else 4
                    if len(payload) != base + count * 24:
                        self.dropped += 1
                        continue
                    blocks = []
                    for offset in range(base, len(payload), 24):
                        target = struct.unpack_from("!I", payload, offset)[0]
                        if expected is not None and target != expected:
                            self.dropped += 1
                            continue
                        if not destination_transcoded:
                            blocks.append(payload[offset:offset + 24])
                    if packet.packet_type == 200 and source_transcoded:
                        # Preserve any reports about the independent relayed
                        # direction without forwarding the input sender counts.
                        payload, count = payload[:4] + b"".join(blocks), len(blocks)
                        selected.append(struct.pack("!BBH", 0x80 | count, 201, len(payload) // 4) + payload)
                        continue
                    payload = payload[:base] + b"".join(blocks)
                    count = len(blocks)
                elif source_transcoded and packet.packet_type not in {202, 203}:
                    # Opaque sender-specific extensions describe the input
                    # stream. SDES/BYE still identify the preserved RR sender.
                    continue
                selected.append(struct.pack("!BBH", 0x80 | count, packet.packet_type,
                                             len(payload) // 4) + payload)
            if not selected:
                return
            data = b"".join(selected)
            source_port = int(addr[1])
            if (
                source.rtcp_source_port is not None
                and source_port != source.rtcp_source_port
            ):
                raise ValueError("unexpected RTCP source port")
            if source.rtcp_source_port is None:
                source.rtcp_source_port = source_port
            source.rtcp_host = str(addr[0])
            source.rtcp_port = source_port
            if output is None or destination.rtcp_port <= 0:
                raise ValueError("destination RTCP leg is not ready")
            output.sendto(data, (destination.rtcp_host, int(destination.rtcp_port)))
        except (OSError, RuntimeError, RtcpError, ValueError) as err:
            self.dropped += 1
            _LOGGER.debug("SIP video RTCP relay drop side=%s: %s", side, err)
            return
        self.rtcp_forwarded += 1

    def _account(self, side: str, received: int, sent: int) -> None:
        self._account_received(side, received)
        self._account_sent("right" if side == "left" else "left", sent)

    def _account_received(self, side: str, received: int) -> None:
        if side == "left":
            self.left_rx_packets += 1
            self.left_rx_bytes += int(received)
        else:
            self.right_rx_packets += 1
            self.right_rx_bytes += int(received)

    def _account_sent(self, side: str, sent: int) -> None:
        if side == "left":
            self.left_tx_packets += 1
            self.left_tx_bytes += int(sent)
        else:
            self.right_tx_packets += 1
            self.right_tx_bytes += int(sent)

    def snapshot(self) -> dict[str, Any]:
        return {
            "codec": self.left.video_format.encoding,
            "left_port": self.left_port,
            "right_port": self.right_port,
            "left_peer": f"{self.left.host}:{self.left.port}",
            "right_peer": f"{self.right.host}:{self.right.port}",
            "forwarded_packets": self.forwarded,
            "forwarded_rtcp_packets": self.rtcp_forwarded,
            "dropped_packets": self.dropped,
            "drop_connection_hold": self.drop_connection_hold,
            "left_connection_held": self.left.connection_held,
            "right_connection_held": self.right.connection_held,
            "left_send_format": self.left.send_format.wire_token(),
            "left_recv_format": self.left.recv_format.wire_token(),
            "right_send_format": self.right.send_format.wire_token(),
            "right_recv_format": self.right.recv_format.wire_token(),
            "left_rx_packets": self.left_rx_packets,
            "left_rx_bytes": self.left_rx_bytes,
            "left_tx_packets": self.left_tx_packets,
            "left_tx_bytes": self.left_tx_bytes,
            "right_rx_packets": self.right_rx_packets,
            "right_rx_bytes": self.right_rx_bytes,
            "right_tx_packets": self.right_tx_packets,
            "right_tx_bytes": self.right_tx_bytes,
            "transcoding": self.transcoding,
            "transcode_directions": sorted(self._transcode.directions),
            "transcoded_input_packets": self.transcoded_input_packets,
            "transcoded_output_packets": self.transcoded_output_packets,
            "transcoded_rtcp_packets": self.transcoded_rtcp_packets,
            "transcode_rtcp_dropped": self.transcode_rtcp_dropped,
            "transcode_startup_buffered": self.transcode_startup_buffered,
            "transcode_startup_dropped": self.transcode_startup_dropped,
            "rtp_drop_reasons": dict(sorted(self.rtp_drop_reasons.items())),
            "transcoder_status": {
                side: {
                    "ready": transcoder.ready,
                    "returncode": (
                        transcoder.process.returncode
                        if transcoder.process is not None
                        else None
                    ),
                    "stderr_tail": list(transcoder.stderr_tail),
                }
                for side, transcoder in sorted(self._transcode.transcoders.items())
            },
            "ignored_after_stop": self.ignored_after_stop,
        }
