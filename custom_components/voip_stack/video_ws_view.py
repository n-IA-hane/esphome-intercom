"""Browser video WebSocket for the HA SIP softphone."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import json
import logging
import socket
import struct
import time

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from . import rtp, sdp
from .config import debug_mode, transport_config
from .const import CONF_VIDEO_TRANSCODING, DOMAIN
from .call_registry import CallRegistry
from .phone_endpoint import DEFAULT_ENDPOINT_ID
from .media_debug import merge_media_debug
from .media_call_lifetime import active_media_call, listen_for_media_call_end
from .media_ws_session import (
    async_claimed_media_websocket,
    async_prepare_media_websocket_request,
)
from .queue_utils import drain_queue
from .session_cleanup import async_wait_for_cleanup
from .sip_client import SipCallClient
from .video_rtcp import (
    RtcpError,
    build_fir,
    build_pli,
    build_receiver_compound,
    build_sender_compound,
    parse_compound,
)
from .video_transcoder import (
    FfmpegJpegNormalizer,
    FfmpegVideoTranscoder,
    VideoTranscoderError,
)
from .video_rtp import (
    H264Depacketizer,
    H264RtpError,
    JpegDepacketizer,
    RtpExtendedSequenceTracker,
    RtpReorderBuffer,
    RtpSenderState,
    VideoAccessUnit,
    Vp8Depacketizer,
    jpeg_dimensions,
    packetize_annex_b,
    packetize_jpeg,
    packetize_vp8,
    unknown_dynamic_payload_type,
)
from .websocket_api import (
    _ha_softphone_store,
    _publish_ha_softphone_state,
)
from .websocket_owner import (
    WebSocketOwnerBusyError,
)


_LOGGER = logging.getLogger(__name__)
_VIDEO_ACCESS_UNIT = 1
_VIDEO_HEADER = struct.Struct("!BBI")
_VIDEO_OWNER_HANDOFF_TIMEOUT = 5.0
_VIDEO_ACCESS_UNIT_QUEUE = 3
_VIDEO_ACCESS_UNIT_QUEUE_BYTES = 4 * 1024 * 1024
_VIDEO_RTP_PACKET_QUEUE = 256
_VIDEO_RTP_QUEUE_BYTES = 2 * 1024 * 1024
_VIDEO_RTCP_PACKET_QUEUE = 64
_VIDEO_RTCP_QUEUE_BYTES = 256 * 1024
_MAX_BROWSER_ACCESS_UNIT_BYTES = 1024 * 1024
_MAX_BROWSER_RECEIVE_ACCESS_UNIT_BYTES = 2 * 1024 * 1024
_MAX_BROWSER_JPEG_DIMENSION = 1280
_MAX_BROWSER_JPEG_PIXELS = 1280 * 800
_JPEG_BROWSER_MAX_FPS = 15
# Do not introduce a private RTP MTU contract. The queue byte budget below is
# the memory backstop; this ceiling only rejects datagrams that cannot be valid
# UDP payloads.
_MAX_VIDEO_DATAGRAM_BYTES = 65_507
_VIDEO_RTP_SEND_BURST_PACKETS = 32
_VIDEO_RTP_RECEIVE_BURST_PACKETS = 32
_RTCP_REPORT_INTERVAL = 5.0
_KEYFRAME_FEEDBACK_INTERVAL = 1.0
_SYMMETRIC_RTP_KEEPALIVE_INTERVAL = 0.25
_SYMMETRIC_RTP_KEEPALIVE_ATTEMPTS = 8
_SYMMETRIC_RTP_REFRESH_INTERVAL = 15.0
_VIDEO_PACKETIZE_SEMAPHORE = asyncio.Semaphore(1)


@dataclass(slots=True)
class _VideoMediaSession:
    call_id: str
    local_rtp_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    remote_rtcp_host: str
    remote_rtcp_port: int
    remote_rtcp_mux: bool
    video_format: sdp.RtpVideoFormat
    local_direction: str
    # ``video_format`` is the local-TX contract kept for compatibility;
    # local RX can retain a different offer/answer receiver contract.
    local_video_format: sdp.RtpVideoFormat | None = None
    signaling_host: str = ""
    remote_video_payload_types: tuple[int, ...] = ()
    remote_connection_held: bool = False
    camera_send_enabled: bool = False
    transcoding_enabled: bool = False
    debug_mode: bool = False
    rtp_source: RtpSenderState | None = None
    rtp_socket: socket.socket | None = None
    rtcp_socket: socket.socket | None = None
    media_generation: int = 0
    removed: bool = False
    update_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def requires_receive_transcoding(self) -> bool:
        """Return whether the browser's receive path needs transcoding."""

        return not sdp.browser_video_receive_supported(self.recv_video_format)

    @property
    def send_video_format(self) -> sdp.RtpVideoFormat:
        return self.video_format

    @property
    def recv_video_format(self) -> sdp.RtpVideoFormat:
        return self.local_video_format or self.video_format

    @property
    def browser_send_format(self) -> sdp.RtpVideoFormat:
        return self.send_video_format

    @property
    def browser_receive_format(self) -> sdp.RtpVideoFormat:
        if not self.requires_receive_transcoding:
            return self.recv_video_format
        return sdp.RtpVideoFormat(
            payload_type=103,
            encoding="VP8",
            clock_rate=90000,
            direction=self.recv_video_format.direction,
            rtcp_feedback=("nack pli", "ccm fir"),
        )

    @property
    def browser_format(self) -> sdp.RtpVideoFormat:
        """Compatibility alias for the browser's receive/decoder format."""

        return self.browser_receive_format

    @property
    def can_send(self) -> bool:
        return bool(
            self.camera_send_enabled
            and not self.remote_connection_held
            and self.local_direction in {"sendonly", "sendrecv"}
            and sdp.browser_video_send_supported(self.send_video_format)
        )

    @property
    def can_receive(self) -> bool:
        return bool(
            self.local_direction in {"recvonly", "sendrecv"}
            and (
                not self.requires_receive_transcoding
                or self.transcoding_enabled
            )
        )


def _transcoder_format_signature(
    video_format: sdp.RtpVideoFormat,
) -> tuple[object, ...]:
    """Return the immutable RTP/codec contract frozen into FFmpeg's SDP."""

    return (
        int(video_format.payload_type),
        str(video_format.encoding).upper(),
        int(video_format.clock_rate),
        str(video_format.transport_profile).upper(),
        str(video_format.profile_level_id).lower(),
        int(video_format.packetization_mode),
        bool(video_format.level_asymmetry_allowed),
        str(video_format.sprop_parameter_sets),
        str(video_format.fmtp),
    )


def _video_pipeline_signature(session: _VideoMediaSession) -> tuple[object, ...]:
    """Describe resources that cannot be replaced inside a live WebSocket.

    Direct browser codecs share the same RTP/access-unit pipeline and can be
    rebuilt in place.  FFmpeg, its loopback transport, and the selected input
    SDP are fixed at process startup, so entering/leaving transcoding or
    changing its input contract requires an ownership-safe WebSocket restart.
    """

    if not session.requires_receive_transcoding:
        return (
            "direct",
            int(session.send_video_format.clock_rate),
            int(session.recv_video_format.clock_rate),
        )
    return (
        "transcode",
        _transcoder_format_signature(session.recv_video_format),
        _transcoder_format_signature(session.browser_receive_format),
    )


class _ByteBudgetQueue(asyncio.Queue):
    """An asyncio queue bounded by both item count and retained payload bytes."""

    def __init__(
        self,
        *,
        maxsize: int,
        max_bytes: int,
        item_size: Callable[[object], int],
    ) -> None:
        super().__init__(maxsize=maxsize)
        self.max_bytes = int(max_bytes)
        self.queued_bytes = 0
        self._item_size = item_size

    def _put(self, item) -> None:
        self.queued_bytes += max(0, int(self._item_size(item)))
        super()._put(item)

    def _get(self):
        item = super()._get()
        self.queued_bytes = max(
            0,
            self.queued_bytes - max(0, int(self._item_size(item))),
        )
        return item

    def can_fit(self, item) -> bool:
        size = max(0, int(self._item_size(item)))
        return size <= self.max_bytes and self.queued_bytes + size <= self.max_bytes

    def put_nowait(self, item) -> None:
        """Enforce the byte invariant even when a future caller forgets can_fit."""

        if not self.can_fit(item):
            raise asyncio.QueueFull
        super().put_nowait(item)


class _RtpVideoProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        queue: _ByteBudgetQueue,
        *,
        source_allowed: Callable[[str], bool],
        min_datagram_bytes: int,
        max_datagram_bytes: int = _MAX_VIDEO_DATAGRAM_BYTES,
    ) -> None:
        self.queue = queue
        self.source_allowed = source_allowed
        self.min_datagram_bytes = int(min_datagram_bytes)
        self.max_datagram_bytes = int(max_datagram_bytes)
        self.dropped_packets = 0
        self._queue_drops = 0
        self._source_drops = 0
        self._size_drops = 0

    def _record_drop(self, reason: str) -> None:
        self.dropped_packets += 1
        if reason == "queue":
            self._queue_drops += 1
        elif reason == "source":
            self._source_drops += 1
        else:
            self._size_drops += 1

    def take_drop_counts(self) -> tuple[int, int, int]:
        """Return new queue/source/size drops since the preceding snapshot."""

        counts = (self._queue_drops, self._source_drops, self._size_drops)
        self._queue_drops = 0
        self._source_drops = 0
        self._size_drops = 0
        return counts

    def datagram_received(self, data: bytes, addr) -> None:
        if not self.min_datagram_bytes <= len(data) <= self.max_datagram_bytes:
            self._record_drop("size")
            return
        if not self.source_allowed(str(addr[0])):
            self._record_drop("source")
            return
        item = (data, addr)
        while self.queue.full() or not self.queue.can_fit(item):
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                self._record_drop("size")
                return
            self._record_drop("queue")
        self.queue.put_nowait(item)


def _packetize_browser_access_unit(
    access_unit: bytes,
    *,
    encoding: str,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
) -> list[tuple[bytes, int]]:
    """Packetize and serialize one browser AU outside Home Assistant's loop."""

    if encoding == "H264":
        packets = packetize_annex_b(
            access_unit,
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
        )
    elif encoding == "VP8":
        packets = packetize_vp8(
            access_unit,
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
        )
    elif encoding == "JPEG":
        packets = packetize_jpeg(
            access_unit,
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
        )
    else:
        raise ValueError(f"browser TX does not support {encoding}")
    return [(rtp.build_packet(packet), len(packet.payload)) for packet in packets]


async def _async_packetize_browser_access_unit(
    access_unit: bytes,
    *,
    encoding: str,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
    should_continue: Callable[[], bool] | None = None,
) -> list[tuple[bytes, int]] | None:
    """Keep access-unit parsing and RTP serialization off the HA event loop."""

    # Packetizers are Python CPU work and therefore still contend for the GIL
    # when moved to a thread. Serialize them globally: one bounded AU cannot
    # monopolise HA, and stale queued frames are discarded before they ever
    # enter the executor.
    async with _VIDEO_PACKETIZE_SEMAPHORE:
        if should_continue is not None and not should_continue():
            return None
        result = await asyncio.to_thread(
            _packetize_browser_access_unit,
            access_unit,
            encoding=encoding,
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
        )
        if should_continue is not None and not should_continue():
            return None
        return result


async def _async_validate_browser_jpeg(access_unit: bytes) -> tuple[int, int]:
    """Validate JPEG geometry off-loop before FFmpeg may allocate a frame."""

    async with _VIDEO_PACKETIZE_SEMAPHORE:
        width, height = await asyncio.to_thread(jpeg_dimensions, access_unit)
    if (
        width > _MAX_BROWSER_JPEG_DIMENSION
        or height > _MAX_BROWSER_JPEG_DIMENSION
        or width * height > _MAX_BROWSER_JPEG_PIXELS
    ):
        raise ValueError("browser JPEG exceeds the normalization budget")
    return width, height


async def _send_packetized_datagrams(
    transport: asyncio.DatagramTransport,
    datagrams: list[tuple[bytes, int]],
    target: tuple[str, int],
    *,
    should_continue: Callable[[], bool],
    on_sent: Callable[[int, int], None],
) -> bool:
    """Send one AU in bounded bursts while call control retains loop fairness."""

    if not should_continue():
        return False
    for index, (raw, payload_bytes) in enumerate(datagrams):
        if index and index % _VIDEO_RTP_SEND_BURST_PACKETS == 0:
            await asyncio.sleep(0)
            if not should_continue():
                return False
        transport.sendto(raw, target)
        on_sent(len(raw), payload_bytes)
    return True


class VoipVideoWebSocketView(HomeAssistantView):
    """Expose the negotiated SIP video media to one authenticated HA card."""

    url = "/api/voip_stack/video_ws"
    name = "api:voip_stack:video_ws"
    requires_auth = True

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        prepared = await async_prepare_media_websocket_request(
            request,
            active_session_resolver=_active_video_session,
            missing_dialog_text="HA softphone has no matching video dialog",
            require_local_video=True,
        )
        context = prepared.context
        hass: HomeAssistant = context.hass
        endpoint_id = context.endpoint_id
        local_bridge = context.local_bridge

        try:
            async with async_claimed_media_websocket(
                request,
                context,
                prepared.registry,
                channel="video",
                max_msg_size=(
                    _MAX_BROWSER_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size
                ),
                timeout=_VIDEO_OWNER_HANDOFF_TIMEOUT,
                local_call=prepared.local_call,
                publish_state=lambda: _publish_ha_softphone_state(
                    hass, endpoint_id=endpoint_id
                ),
            ) as claimed:
                ws = claimed.websocket
                media_owner = claimed.media_session
                local_call, session = prepared.resolve_current()
                if local_call is not None:
                    lease = prepared.acquire_local_lease(media_owner)
                    await ws.prepare(request)
                    await _run_local_video_session(
                        hass,
                        ws,
                        local_bridge,
                        lease,
                    )
                else:
                    assert session is not None
                    await ws.prepare(request)
                    _detach_video_socket(hass, session)
                    await _run_video_session(
                        hass,
                        ws,
                        session,
                        request.transport,
                        endpoint_id=endpoint_id,
                    )
        except WebSocketOwnerBusyError as err:
            raise web.HTTPConflict(text="HA softphone video is already attached") from err
        return claimed.websocket


def async_register_video_ws_view(hass: HomeAssistant) -> None:
    bucket = hass.data.setdefault(DOMAIN, {})
    if bucket.get("video_ws_view_registered"):
        return
    hass.http.register_view(VoipVideoWebSocketView)
    bucket["video_ws_view_registered"] = True
    _LOGGER.info("SIP video websocket ready on %s", VoipVideoWebSocketView.url)


def _active_video_session(
    hass: HomeAssistant,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
) -> _VideoMediaSession | None:
    active = active_media_call(hass, endpoint_id)
    if active is None:
        return None
    store = active.store
    call_id = active.call_id
    registry = active.registry
    config = transport_config(hass)
    transcode = bool(config.get(CONF_VIDEO_TRANSCODING, False))
    debug = debug_mode(hass)

    item = registry.softphone_media.get(call_id)
    if item is not None:
        invite = item.get("invite")
        video_format = getattr(invite, "video_format", None)
        local_port = int(item.get("local_video_rtp_port") or 0)
        if invite is not None and video_format is not None and local_port:
            rtp_source = item.get("video_rtp_source")
            if not isinstance(rtp_source, RtpSenderState):
                rtp_source = RtpSenderState.create(
                    clock_rate=int(video_format.clock_rate),
                    now=asyncio.get_running_loop().time(),
                )
                item["video_rtp_source"] = rtp_source
            return _VideoMediaSession(
                call_id=call_id,
                local_rtp_port=local_port,
                remote_rtp_host=str(invite.remote_video_rtp_host),
                remote_rtp_port=int(invite.remote_video_rtp_port),
                remote_rtcp_host=str(
                    invite.remote_video_rtcp_host or invite.remote_video_rtp_host
                ),
                remote_rtcp_port=int(
                    invite.remote_video_rtcp_port or int(invite.remote_video_rtp_port) + 1
                ),
                remote_rtcp_mux=bool(invite.remote_video_rtcp_mux),
                video_format=video_format,
                local_video_format=getattr(invite, "recv_video_format", video_format),
                local_direction=str(
                    item.get("video_direction")
                    or store.get("video_direction")
                    or "inactive"
                ),
                signaling_host=str(invite.source_host),
                remote_video_payload_types=tuple(invite.remote_video_payload_types),
                remote_connection_held=bool(
                    invite.remote_video_connection_held
                ),
                camera_send_enabled=bool(
                    item.get("camera_send_authorized", False)
                ),
                transcoding_enabled=transcode,
                debug_mode=debug,
                rtp_source=rtp_source,
                rtp_socket=item.get("video_rtp_socket"),
                rtcp_socket=item.get("video_rtcp_socket"),
            )
    clients: dict[str, SipCallClient] = registry.sip_clients
    client = clients.get(call_id)
    dialog = client.dialog if client is not None else None
    if dialog is None or dialog.video_format is None or not dialog.local_video_rtp_port:
        return None
    rtp_source = getattr(client, "video_rtp_source", None)
    if not isinstance(rtp_source, RtpSenderState):
        rtp_source = RtpSenderState.create(
            clock_rate=int(dialog.video_format.clock_rate),
            now=asyncio.get_running_loop().time(),
        )
        client.video_rtp_source = rtp_source
    return _VideoMediaSession(
        call_id=call_id,
        local_rtp_port=int(dialog.local_video_rtp_port),
        remote_rtp_host=str(dialog.remote_video_rtp_host),
        remote_rtp_port=int(dialog.remote_video_rtp_port),
        remote_rtcp_host=str(
            dialog.remote_video_rtcp_host or dialog.remote_video_rtp_host
        ),
        remote_rtcp_port=int(
            dialog.remote_video_rtcp_port or int(dialog.remote_video_rtp_port) + 1
        ),
        remote_rtcp_mux=bool(dialog.remote_video_rtcp_mux),
        video_format=dialog.video_format,
        local_video_format=dialog.recv_video_format,
        local_direction=str(dialog.local_video_direction or "inactive"),
        signaling_host=str(dialog.remote_host),
        remote_video_payload_types=tuple(dialog.remote_video_payload_types),
        remote_connection_held=bool(dialog.remote_video_connection_held),
        camera_send_enabled=client.video_direction in {"sendonly", "sendrecv"},
        transcoding_enabled=transcode,
        debug_mode=debug,
        rtp_source=rtp_source,
        rtp_socket=client.video_rtp_socket,
        rtcp_socket=client.video_rtcp_socket,
    )


def _detach_video_socket(hass: HomeAssistant, session: _VideoMediaSession) -> None:
    """Transfer pre-bound RTP/RTCP sockets from call to media lifetime."""

    registry = hass.data.get(DOMAIN, {}).get("call_registry")
    if not isinstance(registry, CallRegistry):
        return
    item = registry.softphone_media.get(session.call_id)
    if isinstance(item, dict) and (
        item.get("video_rtp_socket") is session.rtp_socket
        or item.get("video_rtcp_socket") is session.rtcp_socket
    ):
        if item.get("video_rtp_socket") is session.rtp_socket:
            item.pop("video_rtp_socket", None)
        if item.get("video_rtcp_socket") is session.rtcp_socket:
            item.pop("video_rtcp_socket", None)
        return
    client = registry.sip_clients.get(session.call_id)
    if client is not None and client.video_rtp_socket is session.rtp_socket:
        client.video_rtp_socket = None
    if client is not None and client.video_rtcp_socket is session.rtcp_socket:
        client.video_rtcp_socket = None


async def _run_local_video_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    bridge,
    lease,
) -> None:
    """Relay browser VP8 access units between two local logical phones."""
    snapshot = bridge.require_call(lease.call_id)
    direction = snapshot.video_direction_for(lease.endpoint_id)
    can_send = direction in {"sendonly", "sendrecv"}
    can_receive = direction in {"recvonly", "sendrecv"}
    format_payload = {
        "codec": "vp8",
        "encoding": "VP8",
        "clock_rate": 90000,
        "payload_type": 103,
        "fmtp": "",
        "profile_level_id": "",
        "packetization_mode": 0,
        "format": "pt=103:VP8/90000",
    }
    await ws.send_json(
        {
            "state": "in_call",
            "call_id": lease.call_id,
            **format_payload,
            "send": dict(format_payload),
            "receive": dict(format_payload),
            "source_format": "local/VP8/90000",
            "sip_send_format": "local/VP8/90000",
            "sip_receive_format": "local/VP8/90000",
            "direction": direction,
            "can_send": can_send,
            "can_receive": can_receive,
            "remote_connection_held": False,
            "camera_send_enabled": can_send,
            "transcoding_enabled": False,
            "debug": debug_mode(hass),
            "media_generation": 0,
            "media_transport": "local_websocket",
        }
    )
    counters = {
        "video_access_units_tx": 0,
        "video_access_units_rx": 0,
        "video_drop_error": 0,
        "video_drop_direction": 0,
        "video_access_unit_queue_drops": 0,
        "video_rate_limited_access_units": 0,
        "video_browser_keyframe_requests": 0,
        "video_keyframe_requests_to_browser": 0,
    }
    _LOGGER.info(
        "HA local softphone video websocket attached call_id=%s endpoint=%s "
        "direction=%s can_send=%s can_receive=%s",
        lease.call_id,
        lease.endpoint_id,
        direction,
        can_send,
        can_receive,
    )
    ws_send_lock = asyncio.Lock()

    async def peer_to_browser() -> None:
        if not can_receive:
            await bridge.wait_closed(lease.call_id)
            return
        while True:
            frame = await bridge.receive_video(
                lease.call_id,
                lease.endpoint_id,
                lease.token,
            )
            async with ws_send_lock:
                await ws.send_bytes(frame)
            counters["video_access_units_rx"] += 1

    async def controls_to_browser() -> None:
        while True:
            control = await bridge.receive_video_control(
                lease.call_id,
                lease.endpoint_id,
                lease.token,
            )
            if control == "force_key_frame":
                async with ws_send_lock:
                    await ws.send_json(
                        {"type": "force_key_frame", "feedback": "local"}
                    )
                counters["video_keyframe_requests_to_browser"] += 1

    async def browser_to_peer() -> None:
        received_in_burst = 0
        drop_logs = 0
        async for msg in ws:
            received_in_burst += 1
            if received_in_burst >= _VIDEO_RTP_RECEIVE_BURST_PACKETS:
                received_in_burst = 0
                await asyncio.sleep(0)
            if msg.type == WSMsgType.BINARY:
                if not can_send:
                    counters["video_drop_direction"] += 1
                    continue
                frame = bytes(msg.data)
                if (
                    len(frame) <= _VIDEO_HEADER.size
                    or len(frame)
                    > _MAX_BROWSER_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size
                ):
                    counters["video_drop_error"] += 1
                    continue
                frame_type, _flags, _timestamp = _VIDEO_HEADER.unpack_from(frame)
                if frame_type != _VIDEO_ACCESS_UNIT:
                    counters["video_drop_error"] += 1
                    continue
                try:
                    if bridge.send_video(
                        lease.call_id,
                        lease.endpoint_id,
                        lease.token,
                        frame,
                    ):
                        counters["video_access_unit_queue_drops"] += 1
                    counters["video_access_units_tx"] += 1
                except Exception as err:  # noqa: BLE001 - video must not stop audio.
                    counters["video_drop_error"] += 1
                    drop_logs += 1
                    if drop_logs & (drop_logs - 1) == 0:
                        _LOGGER.debug(
                            "Local softphone browser video drops=%s "
                            "call_id=%s endpoint=%s latest=%s",
                            drop_logs,
                            lease.call_id,
                            lease.endpoint_id,
                            err,
                        )
            elif msg.type == WSMsgType.TEXT:
                try:
                    control = (
                        json.loads(msg.data)
                        if len(str(msg.data)) <= 256
                        else {}
                    )
                except (TypeError, ValueError):
                    control = {}
                if control.get("type") == "request_key_frame":
                    counters["video_browser_keyframe_requests"] += 1
                    bridge.send_video_control(
                        lease.call_id,
                        lease.endpoint_id,
                        lease.token,
                        "force_key_frame",
                    )
            elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                return

    peer_task = asyncio.create_task(peer_to_browser())
    control_task = asyncio.create_task(controls_to_browser())
    browser_task = asyncio.create_task(browser_to_peer())
    lifetime_task = asyncio.create_task(bridge.wait_closed(lease.call_id))
    tasks = (peer_task, control_task, browser_task, lifetime_task)
    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lifetime_task in done:
            ws.force_close()
        for task in done:
            if task is lifetime_task or task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                _LOGGER.debug(
                    "Local softphone video session ended call_id=%s endpoint=%s: %s",
                    lease.call_id,
                    lease.endpoint_id,
                    error,
                )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store = _ha_softphone_store(hass, lease.endpoint_id)
        current_call_id = str(
            store.get("call_id") or store.get("last_terminal_call_id") or ""
        )
        if current_call_id == lease.call_id:
            store.update(counters)
            store["last_sip_event"] = "local_video_detached"
            _publish_ha_softphone_state(hass, endpoint_id=lease.endpoint_id)
        _LOGGER.info(
            "HA local softphone video websocket detached call_id=%s endpoint=%s "
            "direction=%s counters=%s",
            lease.call_id,
            lease.endpoint_id,
            direction,
            counters,
        )


def _sdp_parameter_sets(video_format: sdp.RtpVideoFormat) -> list[bytes]:
    out: list[bytes] = []
    for value in str(video_format.sprop_parameter_sets or "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            continue
        if decoded and len(decoded) <= 65536:
            out.append(decoded)
    return out


def _video_format_payload(
    video_format: sdp.RtpVideoFormat,
) -> dict[str, object]:
    return {
        "codec": video_format.browser_codec,
        "encoding": video_format.encoding,
        "clock_rate": video_format.clock_rate,
        "payload_type": video_format.payload_type,
        "fmtp": video_format.fmtp,
        "profile_level_id": video_format.profile_level_id,
        "packetization_mode": video_format.packetization_mode,
        "max_framerate": video_format.max_framerate,
        "format": video_format.wire_token(),
    }


def _video_negotiation_payload(
    session: _VideoMediaSession,
    *,
    message_type: str = "",
) -> dict[str, object]:
    current_browser_receive = session.browser_receive_format
    current_browser_send = session.browser_send_format
    payload: dict[str, object] = {
        "state": "in_call",
        "call_id": session.call_id,
        # Flat keys remain an RX/decoder compatibility alias for older cards.
        # New cards must use the directional objects.
        **_video_format_payload(current_browser_receive),
        "send": _video_format_payload(current_browser_send),
        "receive": _video_format_payload(current_browser_receive),
        "source_format": session.recv_video_format.wire_token(),
        "sip_send_format": session.send_video_format.wire_token(),
        "sip_receive_format": session.recv_video_format.wire_token(),
        "direction": session.local_direction,
        "can_send": session.can_send,
        "can_receive": session.can_receive,
        "remote_connection_held": session.remote_connection_held,
        "camera_send_enabled": session.camera_send_enabled,
        "transcoding_enabled": session.transcoding_enabled,
        "debug": session.debug_mode,
        "media_generation": session.media_generation,
    }
    if message_type:
        payload["type"] = message_type
    return payload


def _make_video_depacketizer(
    session: _VideoMediaSession,
    browser_format: sdp.RtpVideoFormat,
    registry: object,
    cached_parameter_sets: tuple[bytes, ...],
):
    if browser_format.encoding == "H264":
        parameter_sets = cached_parameter_sets
        if isinstance(registry, CallRegistry):
            parameter_sets = registry.video_parameter_sets.get(
                session.call_id, parameter_sets
            )
        # A current SDP sprop is authoritative over parameter sets learned
        # from the preceding RTP generation.
        return H264Depacketizer(
            [*parameter_sets, *_sdp_parameter_sets(browser_format)]
        )
    if browser_format.encoding == "VP8":
        return Vp8Depacketizer()
    if browser_format.encoding == "JPEG":
        return JpegDepacketizer(
            max_access_unit_bytes=_MAX_BROWSER_RECEIVE_ACCESS_UNIT_BYTES
        )
    return None


def _sync_video_protocol_drop_counters(
    counters: dict[str, int],
    protocol: _RtpVideoProtocol,
    rtcp_protocol: _RtpVideoProtocol,
    transcode_protocol: _RtpVideoProtocol | None,
) -> None:
    """Project pre-queue filtering without double-counting snapshots."""

    rtp_protocols = [protocol]
    if transcode_protocol is not None:
        rtp_protocols.append(transcode_protocol)
    for candidate in rtp_protocols:
        queue_drops, source_drops, size_drops = candidate.take_drop_counts()
        counters["video_rtp_drop_queue"] += queue_drops
        counters["video_drop_addr"] += source_drops
        counters["video_drop_error"] += size_drops
    queue_drops, source_drops, size_drops = rtcp_protocol.take_drop_counts()
    counters["video_rtcp_drop_queue"] += queue_drops
    counters["video_rtcp_drop_addr"] += source_drops
    counters["video_rtcp_drop_error"] += size_drops


def _sync_video_reorder_counters(
    counters: dict[str, int],
    session: _VideoMediaSession,
    reorder: RtpReorderBuffer,
    input_reorder: RtpReorderBuffer,
) -> None:
    """Publish loss state even when a timeout is the final media event."""

    active_reorder = input_reorder if session.requires_receive_transcoding else reorder
    counters["video_reordered_packets"] = active_reorder.reordered
    counters["video_lost_packets"] = active_reorder.lost
    counters["video_duplicate_packets"] = active_reorder.duplicates


async def _video_access_units_to_ws(
    ws: web.WebSocketResponse,
    closed: asyncio.Event,
    access_units: _ByteBudgetQueue,
    ws_send_lock: asyncio.Lock,
) -> None:
    last_jpeg_sent = 0.0
    loop = asyncio.get_running_loop()
    while not closed.is_set():
        access_unit = await access_units.get()
        if access_unit.encoding == "JPEG":
            due = last_jpeg_sent + (1.0 / _JPEG_BROWSER_MAX_FPS)
            wait_seconds = max(0.0, due - loop.time())
            if wait_seconds:
                try:
                    await asyncio.wait_for(closed.wait(), timeout=wait_seconds)
                    return
                except TimeoutError:
                    pass
            # A newer independent JPEG may have arrived while pacing.
            while True:
                try:
                    access_unit = access_units.get_nowait()
                except asyncio.QueueEmpty:
                    break
        flags = 1 if access_unit.key_frame else 0
        async with ws_send_lock:
            await ws.send_bytes(
                _VIDEO_HEADER.pack(
                    _VIDEO_ACCESS_UNIT, flags, access_unit.timestamp
                )
                + access_unit.data
            )
        if access_unit.encoding == "JPEG":
            last_jpeg_sent = loop.time()


class _BrowserJpegNormalizer:
    """Own the optional persistent browser JPEG normalization process."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: _VideoMediaSession,
        counters: dict[str, int],
    ) -> None:
        self.hass = hass
        self.session = session
        self.counters = counters
        self.normalizer: FfmpegJpegNormalizer | None = None
        self.disabled = False

    async def normalize(
        self,
        access_unit: bytes,
        *,
        media_current: Callable[[], bool],
    ) -> bytes | None:
        if self.disabled:
            raise VideoTranscoderError("JPEG normalization is unavailable")
        await _async_validate_browser_jpeg(access_unit)
        if not media_current():
            return None
        if self.normalizer is None:
            self.normalizer = FfmpegJpegNormalizer(
                hass=self.hass,
                call_id=self.session.call_id,
            )
        try:
            normalized = await self.normalizer.async_normalize(access_unit)
        except (OSError, RuntimeError, VideoTranscoderError) as err:
            self.disabled = True
            self.counters["video_jpeg_normalizer_errors"] += 1
            raise VideoTranscoderError(
                f"browser JPEG normalization unavailable: {err}"
            ) from err
        if not media_current():
            return None
        self.counters["video_jpeg_normalized"] += 1
        return normalized

    async def async_close(self) -> None:
        if self.normalizer is not None:
            await self.normalizer.async_close()


def _observe_nonfatal_video_task(
    task: asyncio.Task,
    *,
    counter: str,
    counters: dict[str, int],
    session: _VideoMediaSession,
    store_counters: Callable[[], None],
) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    counters[counter] += 1
    _LOGGER.warning(
        "HA softphone non-fatal video task stopped call_id=%s task=%s error=%s",
        session.call_id,
        task.get_name(),
        error,
    )
    store_counters()


async def _run_video_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    session: _VideoMediaSession,
    websocket_transport: asyncio.BaseTransport | None = None,
    *,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
) -> None:
    remote_host = str(session.remote_rtp_host)
    remote_port = int(session.remote_rtp_port)
    remote_rtcp_host = str(session.remote_rtcp_host or remote_host)
    remote_rtcp_port = int(session.remote_rtcp_port)
    remote_rtcp_host_explicit = bool(
        session.remote_rtcp_host
        and session.remote_rtcp_host != session.remote_rtp_host
    )
    remote_rtcp_offset = (
        0
        if session.remote_rtcp_mux
        else int(session.remote_rtcp_port) - int(session.remote_rtp_port)
    )

    def rtp_source_allowed(host: str) -> bool:
        return host in {
            str(session.remote_rtp_host),
            str(session.signaling_host),
            remote_host,
        }

    queue = _ByteBudgetQueue(
        maxsize=_VIDEO_RTP_PACKET_QUEUE,
        max_bytes=_VIDEO_RTP_QUEUE_BYTES,
        item_size=lambda item: len(item[0]),
    )
    protocol = _RtpVideoProtocol(
        queue,
        source_allowed=rtp_source_allowed,
        min_datagram_bytes=rtp.RTP_HEADER_SIZE,
    )
    loop = asyncio.get_running_loop()
    transport: asyncio.DatagramTransport | None = None

    def close_detached_sockets() -> None:
        if session.rtp_socket is not None:
            session.rtp_socket.close()
            session.rtp_socket = None
        if session.rtcp_socket is not None:
            session.rtcp_socket.close()
            session.rtcp_socket = None

    try:
        if session.rtp_socket is not None:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                sock=session.rtp_socket,
            )
        else:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("0.0.0.0", int(session.local_rtp_port)),
            )
    except asyncio.CancelledError:
        if transport is not None:
            transport.close()
        close_detached_sockets()
        raise
    except (OSError, RuntimeError, ValueError) as err:
        if transport is not None:
            transport.close()
        close_detached_sockets()
        _LOGGER.warning(
            "HA softphone video websocket rejected call_id=%s local_rtp=%s: %s",
            session.call_id,
            session.local_rtp_port,
            err,
        )
        await ws.close(code=1013, message=b"video RTP port already in use")
        return
    except BaseException:
        if transport is not None:
            transport.close()
        close_detached_sockets()
        raise

    assert transport is not None

    browser_format = session.browser_receive_format
    media_queue = queue
    transcode_transport: asyncio.DatagramTransport | None = None
    transcode_protocol: _RtpVideoProtocol | None = None
    transcoder: FfmpegVideoTranscoder | None = None
    jpeg_normalizer: _BrowserJpegNormalizer | None = None

    async def close_setup_resources() -> None:
        """Release media acquired before the long-lived task guard exists."""

        transport.close()
        if session.rtcp_socket is not None:
            session.rtcp_socket.close()
            session.rtcp_socket = None
        if transcode_transport is not None:
            transcode_transport.close()
        if transcoder is not None:
            await transcoder.async_close()
        if jpeg_normalizer is not None:
            await jpeg_normalizer.async_close()

    if session.requires_receive_transcoding:
        try:
            transcode_queue = _ByteBudgetQueue(
                maxsize=_VIDEO_RTP_PACKET_QUEUE,
                max_bytes=_VIDEO_RTP_QUEUE_BYTES,
                item_size=lambda item: len(item[0]),
            )
            transcode_protocol = _RtpVideoProtocol(
                transcode_queue,
                source_allowed=lambda host: host in {"127.0.0.1", "::1"},
                min_datagram_bytes=rtp.RTP_HEADER_SIZE,
            )
            transcode_transport, _ = await loop.create_datagram_endpoint(
                lambda: transcode_protocol,
                local_addr=("127.0.0.1", 0),
            )
            output_port = int(transcode_transport.get_extra_info("sockname")[1])
            transcoder = FfmpegVideoTranscoder(
                hass=hass,
                call_id=session.call_id,
                input_format=session.recv_video_format,
                output_port=output_port,
            )
            await transcoder.async_start()
        except asyncio.CancelledError:
            await close_setup_resources()
            raise
        except Exception as err:  # noqa: BLE001 - video failure must leave audio/call control alive.
            await close_setup_resources()
            with contextlib.suppress(ConnectionError, RuntimeError):
                await ws.send_json({"error": str(err)})
            await ws.close(code=1013, message=b"video transcode unavailable")
            return
        media_queue = transcode_queue

    closed = asyncio.Event()
    ws_send_lock = asyncio.Lock()
    # Browser AUs and RFC 6263 keepalives share one RTP source.  Serialize the
    # sequence/timestamp mutation while leaving JPEG normalization outside this
    # lock so call control and teardown never wait on FFmpeg.
    rtp_send_lock = asyncio.Lock()
    rtp_source = session.rtp_source or RtpSenderState.create(
        clock_rate=int(session.send_video_format.clock_rate),
        now=loop.time(),
    )
    session.rtp_source = rtp_source
    sequence = int(rtp_source.sequence)
    ssrc = int(rtp_source.ssrc)
    outbound_clock = rtp_source.clock
    outbound_clock.reset_browser()
    applied_media_generation = int(session.media_generation)
    active_pipeline_signature = _video_pipeline_signature(session)
    restart_notified_generation = -1
    media_reset_lock = asyncio.Lock()
    last_regular_rtp_tx_at = 0.0
    latched_source: tuple[str, int] | None = None
    latched_ssrc: int | None = None
    latched_rtcp_source: tuple[str, int] | None = None
    registry = hass.data.get(DOMAIN, {}).get("call_registry")
    cached_parameter_sets: tuple[bytes, ...] = ()
    if isinstance(registry, CallRegistry):
        cached_parameter_sets = registry.video_parameter_sets.get(session.call_id, ())
    depacketizer = _make_video_depacketizer(
        session,
        browser_format,
        registry,
        cached_parameter_sets,
    )
    reorder: RtpReorderBuffer[rtp.RtpPacket] = RtpReorderBuffer()
    input_reorder: RtpReorderBuffer[tuple[bytes, rtp.RtpPacket]] = RtpReorderBuffer()
    access_units = _ByteBudgetQueue(
        maxsize=_VIDEO_ACCESS_UNIT_QUEUE,
        max_bytes=_VIDEO_ACCESS_UNIT_QUEUE_BYTES,
        item_size=lambda item: len(item.data),
    )
    needs_key_frame = True
    extended_sequence = RtpExtendedSequenceTracker()
    highest_sequence = 0
    last_keyframe_feedback = 0.0
    rtcp_transport: asyncio.DatagramTransport | None = None
    rtcp_queue = _ByteBudgetQueue(
        maxsize=_VIDEO_RTCP_PACKET_QUEUE,
        max_bytes=_VIDEO_RTCP_QUEUE_BYTES,
        item_size=lambda item: len(item[0]),
    )
    rtcp_protocol = _RtpVideoProtocol(
        rtcp_queue,
        source_allowed=lambda host: host
        in {
            str(session.remote_rtp_host),
            str(session.remote_rtcp_host),
            str(session.signaling_host),
            remote_host,
            remote_rtcp_host,
        },
        min_datagram_bytes=4,
    )
    last_browser_keyframe_feedback = 0.0
    counters = {
        "video_rtp_rx_packets": 0,
        "video_rtp_tx_packets": 0,
        "video_rtp_rx_bytes": 0,
        "video_rtp_tx_bytes": 0,
        "video_rtp_tx_payload_bytes": 0,
        "video_rtp_drop_queue": 0,
        "video_access_units_rx": 0,
        "video_access_units_tx": 0,
        "video_drop_addr": 0,
        "video_drop_payload_type": 0,
        "video_drop_error": 0,
        "video_drop_direction": 0,
        "video_drop_connection_hold": 0,
        "video_reordered_packets": 0,
        "video_lost_packets": 0,
        "video_duplicate_packets": 0,
        "video_keyframe_requests": 0,
        "video_symmetric_rtp_keepalives": int(rtp_source.keepalives),
        "video_symmetric_rtp_keepalive_payload_type": 0,
        "video_access_unit_queue_max": 0,
        "video_access_unit_queue_drops": 0,
        "video_rate_limited_access_units": 0,
        "video_browser_keyframe_requests": 0,
        "video_rtcp_rx_packets": 0,
        "video_rtcp_rx_bytes": 0,
        "video_rtcp_tx_packets": 0,
        "video_rtcp_tx_bytes": 0,
        "video_rtcp_drop_addr": 0,
        "video_rtcp_drop_error": 0,
        "video_rtcp_drop_queue": 0,
        "video_rtcp_send_errors": 0,
        "video_rtcp_task_errors": 0,
        "video_keepalive_task_errors": 0,
        "video_rtcp_pli_rx": 0,
        "video_rtcp_fir_rx": 0,
        "video_rtcp_keyframe_requests_to_browser": 0,
        "video_pipeline_restarts": 0,
        "video_jpeg_normalized": 0,
        "video_jpeg_normalizer_errors": 0,
    }
    jpeg_normalizer = _BrowserJpegNormalizer(hass, session, counters)

    async def refresh_media_state(generation: int) -> bool:
        """Atomically discard packets and codec state from an older SDP generation."""

        nonlocal applied_media_generation
        nonlocal restart_notified_generation
        nonlocal browser_format, depacketizer, reorder, input_reorder
        nonlocal latched_source, latched_ssrc, latched_rtcp_source
        nonlocal needs_key_frame, highest_sequence
        nonlocal remote_host, remote_port, remote_rtcp_host, remote_rtcp_port
        nonlocal remote_rtcp_offset, remote_rtcp_host_explicit
        nonlocal last_keyframe_feedback
        if generation == applied_media_generation:
            return True
        async with media_reset_lock:
            if generation == applied_media_generation:
                return True
            if _video_pipeline_signature(session) != active_pipeline_signature:
                if restart_notified_generation != generation:
                    payload = _video_negotiation_payload(
                        session, message_type="media_update"
                    )
                    payload["restart_required"] = True
                    payload["restart_reason"] = "video_pipeline_changed"
                    async with ws_send_lock:
                        await ws.send_json(payload)
                    restart_notified_generation = generation
                    counters["video_pipeline_restarts"] += 1
                    store_counters()
                return False
            browser_format = session.browser_receive_format
            remote_host = str(session.remote_rtp_host)
            remote_port = int(session.remote_rtp_port)
            remote_rtcp_host = str(session.remote_rtcp_host or remote_host)
            remote_rtcp_port = int(session.remote_rtcp_port)
            remote_rtcp_host_explicit = bool(
                session.remote_rtcp_host
                and session.remote_rtcp_host != session.remote_rtp_host
            )
            remote_rtcp_offset = (
                0
                if session.remote_rtcp_mux
                else remote_rtcp_port - remote_port
            )
            latched_source = None
            latched_ssrc = None
            latched_rtcp_source = None
            depacketizer = _make_video_depacketizer(
                session,
                browser_format,
                registry,
                cached_parameter_sets,
            )
            reorder = RtpReorderBuffer()
            input_reorder = RtpReorderBuffer()
            drain_queue(queue)
            if media_queue is not queue:
                drain_queue(media_queue)
            drain_queue(access_units)
            needs_key_frame = True
            extended_sequence.reset()
            highest_sequence = 0
            last_keyframe_feedback = 0.0
            async with rtp_send_lock:
                outbound_clock.reset_browser()
            applied_media_generation = generation
            return True

    try:
        await ws.send_json(_video_negotiation_payload(session))
    except asyncio.CancelledError:
        await close_setup_resources()
        raise
    except (ConnectionError, RuntimeError):
        await close_setup_resources()
        return
    except BaseException:
        await close_setup_resources()
        raise
    active_sessions = hass.data.setdefault(DOMAIN, {}).setdefault("active_video_sessions", {})
    active_sessions[session.call_id] = session
    _LOGGER.info(
        "HA softphone video websocket attached call_id=%s local_rtp=%s remote=%s:%s format=%s direction=%s",
        session.call_id,
        session.local_rtp_port,
        session.remote_rtp_host,
        session.remote_rtp_port,
        (
            f"tx={session.send_video_format.wire_token()} "
            f"rx={session.recv_video_format.wire_token()}"
        ),
        session.local_direction,
    )

    last_counter_event = 0.0

    def store_counters(*, force: bool = False) -> None:
        """Persist diagnostics without emitting a call lifecycle occurrence."""

        nonlocal last_counter_event
        now = loop.time()
        interval = 0.5 if session.debug_mode else 5.0
        if not force and now - last_counter_event < interval:
            return
        last_counter_event = now
        _sync_video_protocol_drop_counters(
            counters,
            protocol,
            rtcp_protocol,
            transcode_protocol,
        )
        store = _ha_softphone_store(hass, endpoint_id)
        current_call_id = str(store.get("call_id") or "")
        if current_call_id:
            if current_call_id != session.call_id:
                return
        elif str(store.get("last_terminal_call_id") or "") != session.call_id:
            return
        store.update(counters)
        store["video_rtp_dropped_packets"] = protocol.dropped_packets + int(
            transcode_protocol.dropped_packets if transcode_protocol is not None else 0
        )
        if debug_mode(hass):
            merge_media_debug(
                store,
                call_id=session.call_id,
                channel="video",
                values={
                    "local_rtp_port": session.local_rtp_port,
                    "remote_rtp_host": remote_host,
                    "remote_rtp_port": remote_port,
                    "remote_rtcp_host": remote_rtcp_host,
                    "remote_rtcp_port": remote_rtcp_port,
                    "format": session.recv_video_format.wire_token(),
                    "send_format": session.send_video_format.wire_token(),
                    "receive_format": session.recv_video_format.wire_token(),
                    "direction": session.local_direction,
                    "remote_connection_held": session.remote_connection_held,
                    **counters,
                    "video_rtp_dropped_packets": store[
                        "video_rtp_dropped_packets"
                    ],
                },
            )
        _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)

    def record_rtcp_send_error(context: str, err: BaseException) -> None:
        """Count persistent RTCP send failures without flooding HA logs."""

        counters["video_rtcp_send_errors"] += 1
        failures = counters["video_rtcp_send_errors"]
        # Log the first failure and powers of two thereafter. Diagnostics retain
        # the exact count while a broken route cannot emit a warning every RTP
        # loss or five-second report interval indefinitely.
        if failures & (failures - 1) == 0:
            _LOGGER.warning(
                "HA softphone video RTCP %s failed call_id=%s failures=%d error=%s",
                context,
                session.call_id,
                failures,
                err,
            )
        store_counters()

    def queue_access_unit(access_unit: VideoAccessUnit, now: float) -> None:
        nonlocal needs_key_frame
        if len(access_unit.data) > _MAX_BROWSER_RECEIVE_ACCESS_UNIT_BYTES:
            counters["video_access_unit_queue_drops"] += 1
            counters["video_drop_error"] += 1
            needs_key_frame = browser_format.encoding != "JPEG"
            request_key_frame(now)
            return
        if browser_format.encoding == "JPEG":
            # Every RTP/JPEG frame is independently decodable. Retain only the
            # newest complete image instead of allowing a slow browser to
            # accumulate stale full-frame work.
            dropped = 0
            while True:
                try:
                    access_units.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
            counters["video_access_unit_queue_drops"] += dropped
            counters["video_drop_error"] += dropped
        if access_units.full() or not access_units.can_fit(access_unit):
            dropped = 0
            while True:
                try:
                    access_units.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
            counters["video_access_unit_queue_drops"] += dropped
            counters["video_drop_error"] += dropped
            if not access_unit.key_frame:
                # Dropping one encoded delta invalidates the rest of its GOP.
                # Resume only from a key frame instead of forwarding a broken
                # dependency chain to the browser decoder.
                needs_key_frame = True
                counters["video_access_unit_queue_drops"] += 1
                counters["video_drop_error"] += 1
                request_key_frame(now)
                return
            needs_key_frame = False
        if not access_units.can_fit(access_unit):
            counters["video_access_unit_queue_drops"] += 1
            counters["video_drop_error"] += 1
            needs_key_frame = True
            request_key_frame(now)
            return
        access_units.put_nowait(access_unit)
        counters["video_access_unit_queue_max"] = max(
            counters["video_access_unit_queue_max"], access_units.qsize()
        )

    def request_key_frame(now: float) -> None:
        nonlocal last_keyframe_feedback
        if (
            session.remote_connection_held
            or rtcp_transport is None
            or latched_ssrc is None
            or now - last_keyframe_feedback < _KEYFRAME_FEEDBACK_INTERVAL
        ):
            return
        feedback = set(session.send_video_format.rtcp_feedback)
        feedback_packet = None
        if "nack pli" in feedback:
            feedback_packet = build_pli(ssrc, latched_ssrc)
        elif "ccm fir" in feedback:
            feedback_packet = build_fir(
                ssrc,
                latched_ssrc,
                counters["video_keyframe_requests"] + 1,
            )
        if feedback_packet is None:
            return
        raw = build_receiver_compound(
            ssrc,
            latched_ssrc,
            cumulative_lost=(
                input_reorder.lost
                if session.requires_receive_transcoding
                else reorder.lost
            ),
            highest_sequence=highest_sequence,
            feedback=feedback_packet,
        )
        try:
            rtcp_transport.sendto(raw, (remote_rtcp_host, remote_rtcp_port))
        except (OSError, RuntimeError, ValueError) as err:
            # Feedback is advisory. A closed/unreachable RTCP transport must
            # not tear down the video receive task or the audio SIP dialog.
            last_keyframe_feedback = now
            record_rtcp_send_error("keyframe feedback", err)
            return
        counters["video_rtcp_tx_packets"] += 1
        counters["video_rtcp_tx_bytes"] += len(raw)
        last_keyframe_feedback = now
        counters["video_keyframe_requests"] += 1

    def consume_ordered(packet: rtp.RtpPacket, now: float) -> None:
        nonlocal needs_key_frame, highest_sequence
        if not session.requires_receive_transcoding:
            highest_sequence = extended_sequence.observe(packet.sequence)
        if depacketizer is None:
            counters["video_drop_error"] += 1
            return
        before = int(getattr(depacketizer, "dropped_access_units", 0))
        result = depacketizer.push(packet)
        if int(getattr(depacketizer, "dropped_access_units", 0)) > before:
            needs_key_frame = True
            request_key_frame(now)
        if result is None:
            return
        access_unit = (
            VideoAccessUnit(result.data, result.timestamp, result.key_frame, "H264")
            if browser_format.encoding == "H264"
            else result
        )
        if browser_format.encoding == "H264" and isinstance(depacketizer, H264Depacketizer):
            parameter_sets = depacketizer.parameter_sets
            if len(parameter_sets) == 2 and isinstance(registry, CallRegistry):
                registry.video_parameter_sets[session.call_id] = parameter_sets
        if needs_key_frame and not access_unit.key_frame:
            counters["video_drop_error"] += 1
            request_key_frame(now)
            return
        if access_unit.key_frame:
            needs_key_frame = False
        queue_access_unit(access_unit, now)
        counters["video_access_units_rx"] += 1
        if counters["video_access_units_rx"] % 30 == 0:
            store_counters()

    def forward_ordered_to_transcoder(item: tuple[bytes, rtp.RtpPacket]) -> None:
        nonlocal highest_sequence
        if transcoder is None:
            return
        raw, packet = item
        highest_sequence = extended_sequence.observe(packet.sequence)
        transcoder.send_rtp(raw)

    async def rtp_to_transcoder() -> None:
        nonlocal latched_source, latched_ssrc, remote_host, remote_port, needs_key_frame
        nonlocal remote_rtcp_host, remote_rtcp_port
        assert transcoder is not None
        local_loop = asyncio.get_running_loop()
        observed_generation = int(session.media_generation)
        dequeued_in_burst = 0
        drop_logs = 0
        while not closed.is_set():
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                if not await refresh_media_state(observed_generation):
                    return
            deadline = input_reorder.next_deadline
            timeout = None if deadline is None else max(0.0, deadline - local_loop.time())
            try:
                data, addr = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                lost_before = input_reorder.lost
                for ordered in input_reorder.flush(local_loop.time()):
                    forward_ordered_to_transcoder(ordered)
                if input_reorder.lost > lost_before:
                    needs_key_frame = True
                    request_key_frame(local_loop.time())
                _sync_video_reorder_counters(
                    counters, session, reorder, input_reorder
                )
                continue
            dequeued_in_burst += 1
            if dequeued_in_burst >= _VIDEO_RTP_RECEIVE_BURST_PACKETS:
                dequeued_in_burst = 0
                await asyncio.sleep(0)
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                if not await refresh_media_state(observed_generation):
                    return
            try:
                if str(addr[0]) not in {
                    session.remote_rtp_host,
                    session.signaling_host,
                }:
                    counters["video_drop_addr"] += 1
                    continue
                packet = rtp.parse_packet(data)
                if packet.payload_type != session.recv_video_format.payload_type:
                    counters["video_drop_payload_type"] += 1
                    continue
                source = (str(addr[0]), int(addr[1]))
                if latched_ssrc is not None and packet.ssrc != latched_ssrc:
                    counters["video_drop_addr"] += 1
                    continue
                if latched_source is None:
                    latched_source = source
                    latched_ssrc = packet.ssrc
                    remote_host, remote_port = source
                    if latched_rtcp_source is None and not remote_rtcp_host_explicit:
                        remote_rtcp_host = source[0]
                        remote_rtcp_port = source[1] + remote_rtcp_offset
                    request_key_frame(local_loop.time())
                elif source[0] != latched_source[0]:
                    counters["video_drop_addr"] += 1
                    continue
                elif source[1] != latched_source[1]:
                    latched_source = source
                    remote_port = source[1]
                    if latched_rtcp_source is None and not remote_rtcp_host_explicit:
                        remote_rtcp_port = source[1] + remote_rtcp_offset
                counters["video_rtp_rx_packets"] += 1
                counters["video_rtp_rx_bytes"] += len(data)
                lost_before = input_reorder.lost
                for ordered in input_reorder.push(
                    packet.sequence, (data, packet), local_loop.time()
                ):
                    forward_ordered_to_transcoder(ordered)
                if input_reorder.lost > lost_before:
                    needs_key_frame = True
                    request_key_frame(local_loop.time())
                _sync_video_reorder_counters(
                    counters, session, reorder, input_reorder
                )
            except (OSError, RuntimeError, ValueError) as err:
                counters["video_drop_error"] += 1
                drop_logs += 1
                if drop_logs & (drop_logs - 1) == 0:
                    _LOGGER.debug(
                        "HA softphone video transcode input drops=%s latest=%s",
                        drop_logs,
                        err,
                    )

    async def rtp_to_access_units() -> None:
        nonlocal latched_source, latched_ssrc, remote_host, remote_port, needs_key_frame
        nonlocal remote_rtcp_host, remote_rtcp_port
        loop = asyncio.get_running_loop()
        observed_generation = int(session.media_generation)
        dequeued_in_burst = 0
        drop_logs = 0
        while not closed.is_set():
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                if not await refresh_media_state(observed_generation):
                    return
            deadline = reorder.next_deadline
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            try:
                data, addr = await asyncio.wait_for(media_queue.get(), timeout=timeout)
            except TimeoutError:
                lost_before = reorder.lost
                for ordered in reorder.flush(loop.time()):
                    consume_ordered(ordered, loop.time())
                if reorder.lost > lost_before:
                    needs_key_frame = True
                    request_key_frame(loop.time())
                _sync_video_reorder_counters(
                    counters, session, reorder, input_reorder
                )
                continue
            dequeued_in_burst += 1
            if dequeued_in_burst >= _VIDEO_RTP_RECEIVE_BURST_PACKETS:
                dequeued_in_burst = 0
                await asyncio.sleep(0)
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                if not await refresh_media_state(observed_generation):
                    return
            if not session.can_receive:
                counters["video_drop_direction"] += 1
                continue
            try:
                packet = rtp.parse_packet(data)
                if packet.payload_type != browser_format.payload_type:
                    counters["video_drop_payload_type"] += 1
                    continue
                if session.requires_receive_transcoding:
                    lost_before = reorder.lost
                    for ordered in reorder.push(packet.sequence, packet, loop.time()):
                        consume_ordered(ordered, loop.time())
                    if reorder.lost > lost_before:
                        needs_key_frame = True
                    continue
                if str(addr[0]) not in {
                    session.remote_rtp_host,
                    session.signaling_host,
                }:
                    counters["video_drop_addr"] += 1
                    continue
                source = (str(addr[0]), int(addr[1]))
                if latched_ssrc is not None and packet.ssrc != latched_ssrc:
                    counters["video_drop_addr"] += 1
                    continue
                if latched_source is None:
                    latched_source = source
                    latched_ssrc = packet.ssrc
                    # RFC 3264 does not require an RTP packet's source tuple
                    # to equal the c=/m= destination advertised in SDP.  Latch
                    # the first valid negotiated payload/SSRC and send media
                    # back to that symmetric RTP tuple, which is essential for
                    # ordinary SIP phones behind NAT.
                    remote_host = source[0]
                    remote_port = source[1]
                    if latched_rtcp_source is None and not remote_rtcp_host_explicit:
                        remote_rtcp_host = source[0]
                        remote_rtcp_port = source[1] + remote_rtcp_offset
                    request_key_frame(loop.time())
                elif source[0] != latched_source[0]:
                    counters["video_drop_addr"] += 1
                    continue
                elif source[1] != latched_source[1]:
                    latched_source = source
                    remote_port = source[1]
                    if latched_rtcp_source is None and not remote_rtcp_host_explicit:
                        remote_rtcp_port = source[1] + remote_rtcp_offset
                counters["video_rtp_rx_packets"] += 1
                counters["video_rtp_rx_bytes"] += len(data)
                lost_before = reorder.lost
                for ordered in reorder.push(packet.sequence, packet, loop.time()):
                    consume_ordered(ordered, loop.time())
                if reorder.lost > lost_before:
                    needs_key_frame = True
                    request_key_frame(loop.time())
                _sync_video_reorder_counters(
                    counters, session, reorder, input_reorder
                )
            except Exception as err:  # noqa: BLE001 - a bad frame must not stop audio/call control.
                counters["video_drop_error"] += 1
                drop_logs += 1
                if drop_logs & (drop_logs - 1) == 0:
                    _LOGGER.debug(
                        "HA softphone video RTP RX drops=%s latest=%s",
                        drop_logs,
                        err,
                    )

    async def rtcp_reports() -> None:
        while not closed.is_set():
            try:
                await asyncio.wait_for(closed.wait(), timeout=_RTCP_REPORT_INTERVAL)
            except TimeoutError:
                pass
            if (
                closed.is_set()
                or session.remote_connection_held
                or rtcp_transport is None
            ):
                continue
            report_kwargs = {
                "cumulative_lost": (
                    input_reorder.lost
                    if session.requires_receive_transcoding
                    else reorder.lost
                ),
                "highest_sequence": highest_sequence,
            }
            if counters["video_rtp_tx_packets"]:
                # A send-only source has no inbound SSRC to report on. RFC
                # 3550 still requires periodic SR/SDES, with RC=0 in that case.
                monotonic_now = loop.time()
                unix_now = time.time()
                unix_seconds = int(unix_now)
                report = build_sender_compound(
                    ssrc,
                    latched_ssrc,
                    ntp_seconds=unix_seconds + 2_208_988_800,
                    ntp_fraction=int(
                        (unix_now - unix_seconds) * (1 << 32)
                    ),
                    rtp_timestamp=outbound_clock.current(monotonic_now),
                    packet_count=counters["video_rtp_tx_packets"],
                    octet_count=counters["video_rtp_tx_payload_bytes"],
                    **report_kwargs,
                )
            elif latched_ssrc is not None:
                report = build_receiver_compound(
                    ssrc,
                    latched_ssrc,
                    **report_kwargs,
                )
            else:
                continue
            try:
                rtcp_transport.sendto(
                    report, (remote_rtcp_host, remote_rtcp_port)
                )
            except (OSError, RuntimeError, ValueError) as err:
                record_rtcp_send_error("report", err)
                continue
            counters["video_rtcp_tx_packets"] += 1
            counters["video_rtcp_tx_bytes"] += len(report)

    async def rtcp_to_browser_feedback() -> None:
        """Forward valid remote PLI/FIR feedback to the browser encoder."""

        nonlocal latched_rtcp_source, remote_rtcp_host, remote_rtcp_port
        nonlocal last_browser_keyframe_feedback
        dequeued_in_burst = 0
        while not closed.is_set():
            data, addr = await rtcp_queue.get()
            dequeued_in_burst += 1
            if dequeued_in_burst >= _VIDEO_RTP_RECEIVE_BURST_PACKETS:
                dequeued_in_burst = 0
                await asyncio.sleep(0)
            source = (str(addr[0]), int(addr[1]))
            allowed_hosts = {
                str(session.remote_rtp_host),
                str(session.remote_rtcp_host),
                str(session.signaling_host),
                str(remote_host),
                str(remote_rtcp_host),
            }
            if source[0] not in allowed_hosts:
                counters["video_rtcp_drop_addr"] += 1
                continue
            if latched_rtcp_source is not None and source[0] != latched_rtcp_source[0]:
                counters["video_rtcp_drop_addr"] += 1
                continue
            try:
                packets = parse_compound(data)
            except RtcpError:
                counters["video_rtcp_drop_error"] += 1
                continue

            feedback = ""
            invalid_target = False
            for packet in packets:
                if packet.packet_type != 206:
                    continue
                if packet.fmt == 1:
                    if len(packet.payload) != 8:
                        invalid_target = True
                        continue
                    _sender_ssrc, media_ssrc = struct.unpack_from("!II", packet.payload)
                    if media_ssrc != ssrc:
                        invalid_target = True
                        continue
                    counters["video_rtcp_pli_rx"] += 1
                    feedback = "pli"
                elif packet.fmt == 4:
                    if len(packet.payload) < 16 or (len(packet.payload) - 8) % 8:
                        invalid_target = True
                        continue
                    fir_targets = {
                        struct.unpack_from("!I", packet.payload, offset)[0]
                        for offset in range(8, len(packet.payload) - 7, 8)
                    }
                    if ssrc not in fir_targets:
                        invalid_target = True
                        continue
                    counters["video_rtcp_fir_rx"] += 1
                    feedback = "fir"
            if invalid_target and not feedback:
                counters["video_rtcp_drop_error"] += 1
                continue

            latched_rtcp_source = source
            remote_rtcp_host, remote_rtcp_port = source
            counters["video_rtcp_rx_packets"] += 1
            counters["video_rtcp_rx_bytes"] += len(data)
            now = loop.time()
            if feedback and now - last_browser_keyframe_feedback >= 0.2:
                async with ws_send_lock:
                    await ws.send_json(
                        {"type": "force_key_frame", "feedback": feedback}
                    )
                last_browser_keyframe_feedback = now
                counters["video_rtcp_keyframe_requests_to_browser"] += 1
                store_counters()

    try:
        if session.rtcp_socket is not None:
            rtcp_transport, _ = await loop.create_datagram_endpoint(
                lambda: rtcp_protocol,
                sock=session.rtcp_socket,
            )
        else:
            rtcp_transport, _ = await loop.create_datagram_endpoint(
                lambda: rtcp_protocol,
                local_addr=("0.0.0.0", int(session.local_rtp_port) + 1),
            )
    except asyncio.CancelledError:
        if active_sessions.get(session.call_id) is session:
            active_sessions.pop(session.call_id, None)
        await close_setup_resources()
        raise
    except (OSError, RuntimeError, ValueError) as err:
        if session.rtcp_socket is not None:
            session.rtcp_socket.close()
            session.rtcp_socket = None
        _LOGGER.debug("SIP video RTCP disabled call_id=%s: %s", session.call_id, err)
    except BaseException:
        if active_sessions.get(session.call_id) is session:
            active_sessions.pop(session.call_id, None)
        await close_setup_resources()
        raise

    call_ended, remove_call_listener = listen_for_media_call_end(
        hass, session.call_id, endpoint_id
    )

    async def session_updates_to_ws() -> None:
        """Apply committed re-INVITEs and notify the attached browser."""

        observed_generation = int(session.media_generation)
        while not closed.is_set():
            await session.update_event.wait()
            session.update_event.clear()
            generation = int(session.media_generation)
            if generation == observed_generation:
                continue
            if session.removed:
                return
            if not await refresh_media_state(generation):
                return
            observed_generation = generation
            async with ws_send_lock:
                await ws.send_json(
                    _video_negotiation_payload(
                        session, message_type="media_update"
                    )
                )

    jpeg_normalization_required = False
    browser_tx_drop_logs = 0

    async def browser_to_rtp() -> None:
        nonlocal sequence, remote_host, remote_port, latched_source, latched_ssrc
        nonlocal last_regular_rtp_tx_at
        nonlocal jpeg_normalization_required, browser_tx_drop_logs
        observed_generation = int(session.media_generation)
        received_in_burst = 0
        last_access_unit_at = 0.0
        async for msg in ws:
            received_in_burst += 1
            if received_in_burst >= _VIDEO_RTP_RECEIVE_BURST_PACKETS:
                received_in_burst = 0
                await asyncio.sleep(0)
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                last_access_unit_at = 0.0
                if not await refresh_media_state(observed_generation):
                    return
            if msg.type == WSMsgType.BINARY:
                if session.remote_connection_held:
                    counters["video_drop_connection_hold"] += 1
                    continue
                if not session.can_send:
                    counters["video_drop_direction"] += 1
                    continue
                data = bytes(msg.data)
                if (
                    len(data) <= _VIDEO_HEADER.size
                    or len(data)
                    > _MAX_BROWSER_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size
                ):
                    counters["video_drop_error"] += 1
                    continue
                frame_type, _flags, source_timestamp = _VIDEO_HEADER.unpack_from(data)
                if frame_type != _VIDEO_ACCESS_UNIT:
                    counters["video_drop_error"] += 1
                    continue
                try:
                    packet_generation = observed_generation
                    send_format = session.send_video_format
                    max_framerate = send_format.max_framerate
                    if max_framerate is not None and max_framerate > 0:
                        now = loop.time()
                        due = last_access_unit_at + (1.0 / max_framerate)
                        if last_access_unit_at > 0.0 and now < due:
                            counters["video_rate_limited_access_units"] += 1
                            if send_format.encoding == "JPEG":
                                # JPEG pictures are independent, so dropping
                                # an early frame cannot damage a later one.
                                continue
                            # H.264/VP8 access units form a dependency chain.
                            # Dropping one delta frame here leaves an RTP-clean
                            # but undecodable GOP at the SIP receiver. Apply
                            # backpressure for the short cadence remainder
                            # instead; the browser's bounded WebSocket queue
                            # remains the overload gate.
                            try:
                                await asyncio.wait_for(
                                    closed.wait(), timeout=due - now
                                )
                                return
                            except TimeoutError:
                                pass
                            now = loop.time()
                        last_access_unit_at = now

                    def media_current(generation: int = packet_generation) -> bool:
                        return bool(
                            not closed.is_set()
                            and not call_ended.is_set()
                            and not session.removed
                            and generation == session.media_generation
                            and session.can_send
                            and not session.remote_connection_held
                        )

                    access_unit = data[_VIDEO_HEADER.size :]
                    if (
                        send_format.encoding == "JPEG"
                        and jpeg_normalization_required
                    ):
                        access_unit = await jpeg_normalizer.normalize(
                            access_unit,
                            media_current=media_current,
                        )
                        if access_unit is None:
                            continue
                    while True:
                        retry_with_normalized_jpeg = False
                        async with rtp_send_lock:
                            start_sequence = sequence
                            packet_target = (remote_host, remote_port)
                            try:
                                datagrams = await _async_packetize_browser_access_unit(
                                    access_unit,
                                    encoding=send_format.encoding,
                                    payload_type=send_format.payload_type,
                                    sequence=start_sequence,
                                    timestamp=outbound_clock.map_browser(
                                        source_timestamp, loop.time()
                                    ),
                                    ssrc=ssrc,
                                    should_continue=media_current,
                                )
                            except ValueError:
                                if (
                                    send_format.encoding != "JPEG"
                                    or jpeg_normalization_required
                                ):
                                    raise
                                retry_with_normalized_jpeg = True
                                datagrams = None
                            if datagrams is not None:
                                sent_packets = 0

                                def record_sent(
                                    raw_bytes: int, payload_bytes: int
                                ) -> None:
                                    nonlocal sent_packets
                                    sent_packets += 1
                                    counters["video_rtp_tx_packets"] += 1
                                    counters["video_rtp_tx_bytes"] += raw_bytes
                                    counters["video_rtp_tx_payload_bytes"] += (
                                        payload_bytes
                                    )

                                complete = False
                                try:
                                    complete = await _send_packetized_datagrams(
                                        transport,
                                        datagrams,
                                        packet_target,
                                        should_continue=media_current,
                                        on_sent=record_sent,
                                    )
                                finally:
                                    if sent_packets:
                                        sequence = (
                                            start_sequence + sent_packets
                                        ) & 0xFFFF
                                        rtp_source.sequence = sequence
                                        last_regular_rtp_tx_at = loop.time()
                        if not retry_with_normalized_jpeg:
                            break
                        access_unit = await jpeg_normalizer.normalize(
                            access_unit,
                            media_current=media_current,
                        )
                        if access_unit is None:
                            break
                        jpeg_normalization_required = True
                    if datagrams is None:
                        continue
                    if not complete:
                        if sent_packets:
                            counters["video_drop_error"] += 1
                        continue
                    counters["video_access_units_tx"] += 1
                    if counters["video_access_units_tx"] % 30 == 0:
                        store_counters()
                except (H264RtpError, OSError, RuntimeError, ValueError) as err:
                    counters["video_drop_error"] += 1
                    browser_tx_drop_logs += 1
                    if browser_tx_drop_logs & (browser_tx_drop_logs - 1) == 0:
                        _LOGGER.debug(
                            "HA softphone browser video TX drops=%s latest=%s",
                            browser_tx_drop_logs,
                            err,
                        )
            elif msg.type == WSMsgType.TEXT:
                try:
                    control = (
                        json.loads(msg.data)
                        if len(str(msg.data)) <= 256
                        else {}
                    )
                except (TypeError, ValueError):
                    control = {}
                if control.get("type") == "request_key_frame":
                    counters["video_browser_keyframe_requests"] += 1
                    request_key_frame(loop.time())
                elif control.get("type") == "tx_epoch":
                    async with rtp_send_lock:
                        outbound_clock.reset_browser()
            elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break

    async def symmetric_rtp_keepalive() -> None:
        """Open and refresh the RTP mapping for the whole video session.

        SIP media relays commonly use symmetric RTP when an endpoint is behind
        NAT. A recvonly answer, delayed camera permission, or a browser with
        Send Camera disabled can otherwise leave the advertised private RTP
        port unreachable. RFC 6263 section 4.6 defines a zero-payload RTP
        packet on an unnegotiated dynamic payload type for this purpose; it
        carries no video media but lets the relay learn the symmetric RTP
        source tuple.
        """

        nonlocal sequence
        attempt = 0
        last_warning_at = 0.0
        observed_generation = -1
        keepalive_payload_type: int | None = None
        generation_rx_packets = 0
        generation_tx_packets = 0
        while not closed.is_set():
            generation = int(session.media_generation)
            if generation != observed_generation:
                negotiated_payload_types = {
                    *session.remote_video_payload_types,
                    session.send_video_format.payload_type,
                    session.recv_video_format.payload_type,
                }
                keepalive_payload_type = unknown_dynamic_payload_type(
                    negotiated_payload_types
                )
                counters["video_symmetric_rtp_keepalive_payload_type"] = int(
                    keepalive_payload_type or 0
                )
                observed_generation = generation
                attempt = 0
                generation_rx_packets = counters["video_rtp_rx_packets"]
                generation_tx_packets = counters["video_rtp_tx_packets"]
                if keepalive_payload_type is None:
                    _LOGGER.warning(
                        "SIP video symmetric RTP keepalive unavailable call_id=%s: all dynamic payload types negotiated",
                        session.call_id,
                    )
            if session.remote_connection_held:
                try:
                    await asyncio.wait_for(
                        closed.wait(), timeout=_SYMMETRIC_RTP_REFRESH_INTERVAL
                    )
                except TimeoutError:
                    pass
                continue
            if keepalive_payload_type is None:
                try:
                    await asyncio.wait_for(
                        closed.wait(), timeout=_SYMMETRIC_RTP_REFRESH_INTERVAL
                    )
                except TimeoutError:
                    pass
                continue
            probing = bool(
                attempt < _SYMMETRIC_RTP_KEEPALIVE_ATTEMPTS
                and counters["video_rtp_rx_packets"] == generation_rx_packets
                and counters["video_rtp_tx_packets"] == generation_tx_packets
            )
            interval = (
                _SYMMETRIC_RTP_KEEPALIVE_INTERVAL
                if probing
                else _SYMMETRIC_RTP_REFRESH_INTERVAL
            )
            if not probing:
                try:
                    await asyncio.wait_for(closed.wait(), timeout=interval)
                except TimeoutError:
                    pass
                if closed.is_set():
                    return
                if (
                    last_regular_rtp_tx_at
                    and loop.time() - last_regular_rtp_tx_at < interval
                ):
                    continue
            try:
                async with rtp_send_lock:
                    keepalive = rtp_source.build_keepalive(
                        keepalive_payload_type,
                        now=loop.time(),
                    )
                    # build_keepalive advances the shared source even if the
                    # datagram send fails; keep the browser sender aligned.
                    sequence = int(rtp_source.sequence)
                    transport.sendto(keepalive, (remote_host, remote_port))
            except (OSError, RuntimeError, ValueError) as err:
                counters["video_keepalive_task_errors"] += 1
                attempt += 1
                now = loop.time()
                if now - last_warning_at >= _SYMMETRIC_RTP_REFRESH_INTERVAL:
                    _LOGGER.warning(
                        "HA softphone video RTP keepalive failed call_id=%s error=%s",
                        session.call_id,
                        err,
                    )
                    last_warning_at = now
                store_counters()
                retry_delay = min(
                    _SYMMETRIC_RTP_REFRESH_INTERVAL,
                    _SYMMETRIC_RTP_KEEPALIVE_INTERVAL * (2 ** min(attempt, 6)),
                )
                try:
                    await asyncio.wait_for(closed.wait(), timeout=retry_delay)
                except TimeoutError:
                    pass
                continue
            counters["video_symmetric_rtp_keepalives"] += 1
            attempt += 1
            if probing:
                await asyncio.sleep(_SYMMETRIC_RTP_KEEPALIVE_INTERVAL)

    rx_task = asyncio.create_task(rtp_to_access_units())
    ws_task = asyncio.create_task(
        _video_access_units_to_ws(ws, closed, access_units, ws_send_lock)
    )
    rtcp_task = asyncio.create_task(
        rtcp_reports(), name=f"voip-video-rtcp-{session.call_id}"
    )
    rtcp_input_task = asyncio.create_task(rtcp_to_browser_feedback())
    lifetime_task = asyncio.create_task(call_ended.wait())
    update_task = asyncio.create_task(session_updates_to_ws())
    browser_task = asyncio.create_task(browser_to_rtp())
    keepalive_task = asyncio.create_task(
        symmetric_rtp_keepalive(), name=f"voip-video-keepalive-{session.call_id}"
    )

    rtcp_task.add_done_callback(
        lambda task: _observe_nonfatal_video_task(
            task,
            counter="video_rtcp_task_errors",
            counters=counters,
            session=session,
            store_counters=store_counters,
        )
    )
    keepalive_task.add_done_callback(
        lambda task: _observe_nonfatal_video_task(
            task,
            counter="video_keepalive_task_errors",
            counters=counters,
            session=session,
            store_counters=store_counters,
        )
    )
    transcode_input_task = (
        asyncio.create_task(rtp_to_transcoder()) if transcoder is not None else None
    )
    try:
        critical_tasks = {
            browser_task,
            lifetime_task,
            rx_task,
            ws_task,
            update_task,
            rtcp_input_task,
        }
        if transcode_input_task is not None:
            critical_tasks.add(transcode_input_task)
        done, _pending = await asyncio.wait(
            critical_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        media_task_ended = bool(done - {browser_task, lifetime_task})
        if media_task_ended:
            for task in done - {browser_task, lifetime_task}:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    _LOGGER.warning(
                        "HA softphone video media task failed call_id=%s task=%s error=%s",
                        session.call_id,
                        task.get_name(),
                        error,
                    )
        if lifetime_task in done or media_task_ended:
            # Do not let a browser that vanished without a close handshake
            # retain the SIP RTP/RTCP ports after the dialog is gone.
            # This is a dedicated media WebSocket, so closing its HTTP
            # transport is the deterministic way to wake aiohttp's pending
            # receive before releasing the RTP resources.
            ws.force_close()
            if websocket_transport is not None:
                websocket_transport.close()
            browser_task.cancel()
            await asyncio.gather(browser_task, return_exceptions=True)
        if browser_task in done and not browser_task.cancelled():
            browser_task.result()
    finally:
        closed.set()
        if active_sessions.get(session.call_id) is session:
            active_sessions.pop(session.call_id, None)
        remove_call_listener()
        tasks = [
            rx_task,
            ws_task,
            rtcp_task,
            rtcp_input_task,
            lifetime_task,
            update_task,
            browser_task,
            keepalive_task,
        ]
        if transcode_input_task is not None:
            tasks.append(transcode_input_task)
        for task in tasks:
            task.cancel()
        transport.close()
        if transcode_transport is not None:
            transcode_transport.close()
        if rtcp_transport is not None:
            rtcp_transport.close()
        caller_cancelled = False
        try:
            await async_wait_for_cleanup(
                asyncio.gather(*tasks, return_exceptions=True)
            )
        except asyncio.CancelledError:
            caller_cancelled = True
        try:
            if transcoder is not None:
                await transcoder.async_close()
            if jpeg_normalizer is not None:
                await jpeg_normalizer.async_close()
        except asyncio.CancelledError:
            # FFmpeg helpers finish shielded cleanup before re-raising
            # cancellation. Final counters still belong to this media owner.
            caller_cancelled = True
        except Exception:
            _LOGGER.exception(
                "HA softphone video transcoder cleanup failed call_id=%s",
                session.call_id,
            )
        finally:
            counters["video_rtp_dropped_packets"] = protocol.dropped_packets + int(
                transcode_protocol.dropped_packets if transcode_protocol is not None else 0
            )
            _sync_video_reorder_counters(
                counters, session, reorder, input_reorder
            )
            store_counters(force=True)
            _LOGGER.info(
                "HA softphone video websocket detached call_id=%s counters=%s",
                session.call_id,
                counters,
            )
        if caller_cancelled:
            raise asyncio.CancelledError
