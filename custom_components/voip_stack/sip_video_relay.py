"""Direct or bounded cross-codec RTP/RTCP relay for HA-owned SIP bridges."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket
import struct
from typing import Any, Callable

from . import rtp
from .sdp import RtpVideoFormat, video_formats_passthrough_compatible
from .session_cleanup import async_wait_for_cleanup
from .video_transcoder import (
    FfmpegVideoTranscoder,
    _claim_transcoder_slot,
    _release_transcoder_slot,
    video_transcode_supported,
)
from .video_rtcp import RtcpError, parse_compound


_LOGGER = logging.getLogger(__name__)
_RTP_IP_TOS = 0x88
_TRANSCODE_STARTUP_MAX_PACKETS = 64
_TRANSCODE_STARTUP_MAX_BYTES = 256 * 1024


def remote_can_send(video_format: RtpVideoFormat | None) -> bool:
    """Return whether a remote endpoint may send media in its SDP direction."""

    return bool(
        video_format is not None and video_format.direction in {"sendonly", "sendrecv"}
    )


def remote_can_receive(
    video_format: RtpVideoFormat | None,
    *,
    connection_held: bool = False,
) -> bool:
    """Return whether a remote endpoint may receive media in its SDP direction."""

    return bool(
        video_format is not None
        and not connection_held
        and video_format.direction in {"recvonly", "sendrecv"}
    )


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

    def datagram_received(self, data: bytes, _addr) -> None:
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
        self._transcode_sockets: dict[tuple[str, bool], socket.socket | None] = {}
        self._transcode_transports: dict[
            tuple[str, bool], asyncio.DatagramTransport
        ] = {}
        self._transcoders: dict[str, FfmpegVideoTranscoder] = {}
        self._transcode_directions: set[str] = set()
        self._transcode_hass: Any | None = None
        self._transcode_call_id = ""
        self._transcode_slot_claimed = False
        self._transcode_startup_rtp: dict[str, list[bytes]] = {
            "left": [],
            "right": [],
        }
        self._transcode_startup_bytes = {"left": 0, "right": 0}
        self._on_release = on_release
        self._released = False
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_requested = False
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
                    and rtp_socket.getsockname()[1] + 1
                    == rtcp_socket.getsockname()[1]
                ):
                    rtp_socket.close()
                    rtcp_socket.close()
        raise OSError("unable to reserve FFmpeg loopback RTP pair")

    @property
    def transcoding(self) -> bool:
        return bool(self._transcode_directions)

    def transcodes_from(self, side: str) -> bool:
        """Return whether RTP sourced by this side uses its transcoder."""

        if side not in {"left", "right"}:
            raise ValueError(f"unknown video relay side: {side}")
        return side in self._transcode_directions

    @property
    def stopping(self) -> bool:
        """Return whether a subsequent call may await this relay's release."""

        return self._stop_requested or self._released

    def configure_transcoding(self, hass: Any, call_id: str) -> None:
        """Configure only the incompatible active directions for FFmpeg."""

        if self.started or self._start_task is not None:
            raise RuntimeError("cannot configure transcoding after relay start")
        directions: set[str] = set()
        for side in ("left", "right"):
            source, destination = self._peers(side)
            if not remote_can_send(source.video_format) or not remote_can_receive(
                destination.video_format,
                connection_held=destination.connection_held,
            ):
                continue
            if video_formats_passthrough_compatible(
                source.recv_format,
                destination.send_format,
            ):
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
        if not directions:
            return
        self._transcode_hass = hass
        self._transcode_call_id = str(call_id or "sip-video")
        self._transcode_directions = directions

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
            if self._transcode_directions:
                await self._start_transcoding(loop)
            self.started = True
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
        await _claim_transcoder_slot(hass, self)
        self._transcode_slot_claimed = True
        try:
            for source_side in sorted(self._transcode_directions):
                pair = self._loopback_socket_pair()
                rtp_port = int(pair[0].getsockname()[1])
                for rtcp, sock in ((False, pair[0]), (True, pair[1])):
                    key = (source_side, rtcp)
                    self._transcode_sockets[key] = sock
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda source_side=source_side, rtcp=rtcp: (
                            _TranscodedOutputProtocol(
                                self,
                                source_side,
                                rtcp=rtcp,
                            )
                        ),
                        sock=sock,
                    )
                    self._transcode_sockets[key] = None
                    self._transcode_transports[key] = transport
                source, destination = self._peers(source_side)
                self._transcoders[source_side] = FfmpegVideoTranscoder(
                    hass=hass,
                    call_id=f"{self._transcode_call_id}-{source_side}",
                    input_format=source.recv_format,
                    output_format=destination.send_format,
                    output_port=rtp_port,
                    slot_owner=self,
                    manage_slot=False,
                )
            await asyncio.gather(
                *(transcoder.async_start() for transcoder in self._transcoders.values())
            )
            self._flush_transcode_startup_rtp()
        except BaseException:
            await self._stop_transcoding()
            raise

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
            transcoder = self._transcoders.get(side)
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
        transcoders = tuple(self._transcoders.values())
        self._transcoders.clear()
        if transcoders:
            await asyncio.gather(
                *(transcoder.async_close() for transcoder in transcoders),
                return_exceptions=True,
            )
        for transport in self._transcode_transports.values():
            transport.close()
        self._transcode_transports.clear()
        for sock in self._transcode_sockets.values():
            if sock is not None:
                sock.close()
        self._transcode_sockets.clear()
        if self._transcode_slot_claimed and self._transcode_hass is not None:
            await _release_transcoder_slot(self._transcode_hass, self)
        self._transcode_slot_claimed = False

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stop_requested = True
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
        if start_task is not None and start_task is not asyncio.current_task():
            await asyncio.gather(start_task, return_exceptions=True)
        async with self._lifecycle_lock:
            await self._stop_transcoding()
            self._close_resources()
            self._release_ports()

    def _close_resources(self) -> None:
        """Synchronously detach every socket/transport from the relay."""

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
                "SIP video relay stopped forwarded=%d rtcp=%d dropped=%d",
                self.forwarded,
                self.rtcp_forwarded,
                self.dropped,
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
            peer.rtcp_source_port = None
            if side == "left":
                self.left = peer
            else:
                self.right = peer

        return _commit

    def reconfigure_peer(self, side: str, peer: VideoRtpPeer) -> None:
        """Atomically replace one negotiated video peer and reset its latch."""

        self.prepare_peer_reconfiguration(side, peer)()

    def handle_rtp(self, side: str, data: bytes, addr) -> None:
        if self._stop_requested:
            self.ignored_after_stop += 1
            return
        source, destination = self._peers(side)
        output = self._transports.get(("right" if side == "left" else "left", False))
        try:
            if destination.connection_held:
                self.drop_connection_hold += 1
                raise ValueError("destination connection is held")
            if not remote_can_send(source.video_format) or not remote_can_receive(
                destination.video_format,
                connection_held=destination.connection_held,
            ):
                raise ValueError("RTP direction is not negotiated")
            if side not in self._transcode_directions and not (
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
            if source.rx_ssrc is not None and packet.ssrc != source.rx_ssrc:
                raise ValueError("unexpected RTP SSRC")
            if source.rx_ssrc is None:
                source.rx_ssrc = packet.ssrc
            source.host = str(addr[0])
            source.port = int(addr[1])
            if side in self._transcode_directions:
                transcoder = self._transcoders.get(side)
                if transcoder is None or not getattr(transcoder, "ready", True):
                    if not self.started:
                        if self._buffer_transcode_startup_rtp(side, data):
                            self._account_received(side, len(data))
                        return
                    raise RuntimeError("directional FFmpeg transcoder is not ready")
                transcoder.send_rtp(data)
                self._account_received(side, len(data))
                self.transcoded_input_packets += 1
                return
            if output is None or destination.port <= 0:
                raise ValueError("destination RTP leg is not ready")
            payload_type = int(destination.send_format.payload_type)
            if not 0 <= payload_type <= 127:
                raise ValueError("invalid destination RTP payload type")
            outgoing = (
                data
                if payload_type == packet.payload_type
                else bytes((data[0], (data[1] & 0x80) | payload_type)) + data[2:]
            )
            output.sendto(outgoing, (destination.host, int(destination.port)))
        except (OSError, RuntimeError, ValueError) as err:
            self.dropped += 1
            _LOGGER.debug("SIP video RTP relay drop side=%s: %s", side, err)
            return
        self._account(side, len(data), len(outgoing))
        self.forwarded += 1

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
        if self._transcode_directions:
            self.transcode_rtcp_dropped += 1
            return
        source, destination = self._peers(side)
        output = self._transports.get(("right" if side == "left" else "left", True))
        try:
            if destination.connection_held:
                self.drop_connection_hold += 1
                raise ValueError("destination RTCP connection is held")
            if not source.accepts_rtcp_source_host(str(addr[0])):
                raise ValueError("unexpected RTCP source host")
            packets = parse_compound(data)
            if destination.rx_ssrc is not None:
                expected_ssrc = int(destination.rx_ssrc)
                for packet in packets:
                    if packet.packet_type != 206:
                        continue
                    if packet.fmt == 1:
                        feedback_targets = (struct.unpack_from("!I", packet.payload, 4)[0],)
                    elif packet.fmt == 4:
                        feedback_targets = tuple(
                            struct.unpack_from("!I", packet.payload, offset)[0]
                            for offset in range(8, len(packet.payload), 8)
                        )
                    else:
                        continue
                    if any(target != expected_ssrc for target in feedback_targets):
                        raise ValueError("RTCP feedback targets an unexpected media SSRC")
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
            "transcode_directions": sorted(self._transcode_directions),
            "transcoded_input_packets": self.transcoded_input_packets,
            "transcoded_output_packets": self.transcoded_output_packets,
            "transcoded_rtcp_packets": self.transcoded_rtcp_packets,
            "transcode_rtcp_dropped": self.transcode_rtcp_dropped,
            "transcode_startup_buffered": self.transcode_startup_buffered,
            "transcode_startup_dropped": self.transcode_startup_dropped,
            "ignored_after_stop": self.ignored_after_stop,
        }
