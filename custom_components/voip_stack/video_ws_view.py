"""Browser video WebSocket for the HA SIP softphone."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
import json
import logging
import socket
import struct as struct
import time as time

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .core import rtp, sdp
from .config import debug_mode, transport_config
from .const import CONF_VIDEO_TRANSCODING
from .media_debug import merge_media_debug
from .media_call_lifetime import active_media_call, listen_for_media_call_end
from .media_ws_session import (
    async_claimed_media_websocket,
    async_wait_for_media_workers, async_stop_media_workers,
    async_prepare_media_websocket_request,
)
from .video_ws_io import (
    _ByteBudgetQueue as _ByteBudgetQueue,
    _RtpVideoProtocol as _RtpVideoProtocol,
    async_open_video_receiver, async_open_receive_transcoder,
)
from .video_rtp_keepalive import run_symmetric_video_keepalive
from .video_receive_state import VideoReceiveRoute, VideoReceiveState, video_receive_counters
from .video_receive import VideoReceiveHooks, VideoReceiveWorkers
from .video_receive_generation import VideoGenerationHooks, VideoGenerationUpdates
from .video_receive_diagnostics import VideoDiagnosticHooks, VideoReceiveDiagnostics
from .video_rtcp_worker import VideoRtcpHooks, VideoRtcpWorkers
from .queue_utils import drain_queue as drain_queue
from .runtime_data import (
    call_projection,
    registration_data,
    require_runtime_data,
    sip_endpoint_manager,
)
from .core.video_rtcp import (
    RtcpError as RtcpError,
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
from .core.video_rtp import (
    RtpExtendedSequenceTracker as RtpExtendedSequenceTracker,
    RtpReorderBuffer,
    RtpSenderState,
    VideoAccessUnit as VideoAccessUnit,
    split_annex_b as split_annex_b,
)
from .core.imageattr import constraints_payload
from .browser_phone_data import _ha_softphone_store
from .browser_phone_runtime import _publish_ha_softphone_state
from .video_ws_frames import (
    BrowserVideoSender, _MAX_BROWSER_ACCESS_UNIT_BYTES, _MAX_BROWSER_RECEIVE_ACCESS_UNIT_BYTES, _VIDEO_ACCESS_UNIT, _VIDEO_HEADER,
    _VIDEO_RTP_RECEIVE_BURST_PACKETS, _async_validate_browser_jpeg,
    _make_video_depacketizer, _video_access_units_to_ws, decode_browser_video_packet,
)
from .websocket_owner import (
    WebSocketOwnerBusyError,
)


_LOGGER = logging.getLogger(__name__)
_VIDEO_OWNER_HANDOFF_TIMEOUT = 5.0
_VIDEO_ACCESS_UNIT_QUEUE = 3
_VIDEO_ACCESS_UNIT_QUEUE_BYTES = 4 * 1024 * 1024
_VIDEO_RTP_PACKET_QUEUE = 256
_VIDEO_RTP_QUEUE_BYTES = 2 * 1024 * 1024
_VIDEO_RTCP_PACKET_QUEUE = 64
_VIDEO_RTCP_QUEUE_BYTES = 256 * 1024
# Do not introduce a private RTP MTU contract. The queue byte budget below is
# the memory backstop; this ceiling only rejects datagrams that cannot be valid
# UDP payloads.
_MAX_VIDEO_DATAGRAM_BYTES = 65_507
_RTCP_REPORT_INTERVAL = 5.0
_KEYFRAME_FEEDBACK_INTERVAL = 1.0
_SYMMETRIC_RTP_KEEPALIVE_INTERVAL = 0.25
_SYMMETRIC_RTP_KEEPALIVE_ATTEMPTS = 8
_SYMMETRIC_RTP_REFRESH_INTERVAL = 15.0


def _video_loss_requires_key_frame(
    encoding: str, *, receive_transcoding: bool
) -> bool:
    """Return whether loss invalidates every following decoder input."""

    return encoding.upper() == "H264" or receive_transcoding


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
    request_remote_keyframe: Callable[[], Awaitable[bool]] | None = None
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
            and (not self.requires_receive_transcoding or self.transcoding_enabled)
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
                max_msg_size=(_MAX_BROWSER_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size),
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
                    if not await claimed.prepare(request):
                        return ws
                    await _run_local_video_session(
                        hass,
                        ws,
                        local_bridge,
                        lease,
                    )
                else:
                    assert session is not None
                    call = prepared.registry.get_session(session.call_id)
                    generation = call.generation if call is not None else None
                    if not await claimed.prepare(request):
                        return ws
                    if (call is None or not call.live
                            or call_projection(hass) is not prepared.registry
                            or prepared.registry.get_session(session.call_id) is not call
                            or call.generation != generation
                            or media_owner.owners.get(media_owner.owner_key) is not claimed.owner
                            or claimed.owner.handoff_requested.is_set()
                            or media_owner.media.shutdown.is_set()):
                        await ws.close(code=1008, message=b"video call changed before attachment")
                        return ws
                    try:
                        _, session = prepared.resolve_current()
                    except web.HTTPException:
                        await ws.close(code=1008, message=b"video media changed before attachment")
                        return ws
                    assert session is not None
                    _detach_video_socket(hass, session)
                    await _run_video_session(
                        hass,
                        ws,
                        session,
                        request.transport,
                        endpoint_id=endpoint_id,
                    )
        except WebSocketOwnerBusyError as err:
            raise web.HTTPConflict(
                text="HA softphone video is already attached"
            ) from err
        return claimed.websocket


def async_register_video_ws_view(hass: HomeAssistant) -> None:
    registration = registration_data(hass)
    if registration.video_view:
        return
    hass.http.register_view(VoipVideoWebSocketView)
    registration.video_view = True
    _LOGGER.info("SIP video websocket ready on %s", VoipVideoWebSocketView.url)


def _active_video_session(
    hass: HomeAssistant,
    endpoint_id: str,
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
    endpoint = sip_endpoint_manager(hass)

    item = registry.resource_for(call_id, "softphone_media")
    if item is not None:
        invite = item.get("invite")
        video_format = getattr(invite, "video_format", None)
        local_port = int(item.get("local_video_rtp_port") or 0)
        if invite is not None and video_format is not None and local_port:
            source_client = registry.source_sip_client_for(call_id)
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
                    invite.remote_video_rtcp_port
                    or int(invite.remote_video_rtp_port) + 1
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
                remote_connection_held=bool(invite.remote_video_connection_held),
                camera_send_enabled=bool(item.get("camera_send_authorized", False)),
                transcoding_enabled=transcode,
                debug_mode=debug,
                rtp_source=rtp_source,
                rtp_socket=item.get("video_rtp_socket"),
                rtcp_socket=item.get("video_rtcp_socket"),
                request_remote_keyframe=(
                    source_client.request_video_keyframe if source_client is not None
                    else (lambda: endpoint.async_request_video_keyframe(call_id))
                    if endpoint is not None
                    else None
                ),
            )
    client = registry.sip_client_for(call_id)
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
        request_remote_keyframe=client.request_video_keyframe,
    )


def _detach_video_socket(hass: HomeAssistant, session: _VideoMediaSession) -> None:
    """Lend bound descriptors to this WebSocket, retaining the SIP receive queue."""

    registry = call_projection(hass)
    if registry is None:
        return
    item = registry.resource_for(session.call_id, "softphone_media")
    # A preanswered resource may exist without owning this video's contract;
    # _active_video_session then resolves the outbound client's descriptors.
    media_owns_video = isinstance(item, dict) and (
        (getattr(item.get("invite"), "video_format", None) is not None
         and item.get("local_video_rtp_port"))
        or any(descriptor is not None and item.get(name) is descriptor for name, descriptor in (
            ("video_rtp_socket", session.rtp_socket), ("video_rtcp_socket", session.rtcp_socket)))
    )
    if media_owns_video:
        owned = (item.get("video_rtp_socket"), item.get("video_rtcp_socket"))
    else:
        client = registry.sip_client_for(session.call_id)
        if client is None:
            return
        owned = (client.video_rtp_socket, client.video_rtcp_socket)
    if any(expected is not current for expected, current in zip(
            (session.rtp_socket, session.rtcp_socket), owned, strict=True)):
        raise RuntimeError("video socket owner changed before attachment")
    borrowed = []
    try:
        for original in owned:
            borrowed.append(original.dup() if original is not None else None)
    except BaseException:
        for descriptor in borrowed:
            if descriptor is not None:
                descriptor.close()
        raise
    # The call retains the originals and closes them through its existing
    # cleanup. Closing this WS's duplicates cannot unbind the SIP media port
    # or lose SPS/PPS arriving before the replacement WS attaches.
    session.rtp_socket, session.rtcp_socket = borrowed


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
    negotiated = {
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
    await ws.send_json(negotiated)
    counters = {
        "video_access_units_tx": 0,
        "video_access_units_rx": 0,
        "video_drop_error": 0,
        "video_drop_wait_keyframe": 0,
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
        while True:
            frame = await bridge.receive_video(
                lease.call_id,
                lease.endpoint_id,
                lease.token,
            )
            if not can_receive:
                counters["video_drop_direction"] += 1
                continue
            async with ws_send_lock:
                await ws.send_bytes(frame)
            counters["video_access_units_rx"] += 1

    async def controls_to_browser() -> None:
        nonlocal direction, can_send, can_receive
        while True:
            control = await bridge.receive_video_control(
                lease.call_id,
                lease.endpoint_id,
                lease.token,
            )
            current = bridge.require_call(lease.call_id)
            next_direction = current.video_direction_for(lease.endpoint_id)
            if next_direction != direction:
                direction = next_direction
                can_send = direction in {"sendonly", "sendrecv"}
                can_receive = direction in {"recvonly", "sendrecv"}
                negotiated.update(
                    type="media_update",
                    direction=direction,
                    can_send=can_send,
                    can_receive=can_receive,
                    camera_send_enabled=can_send,
                    media_generation=negotiated["media_generation"] + 1,
                )
                async with ws_send_lock:
                    await ws.send_json(dict(negotiated))
                if can_receive:
                    bridge.send_video_control(
                        lease.call_id, lease.endpoint_id, lease.token, "force_key_frame"
                    )
            if control == "force_key_frame":
                async with ws_send_lock:
                    await ws.send_json({"type": "force_key_frame", "feedback": "local"})
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
                    or len(frame) > _MAX_BROWSER_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size
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
                    control = json.loads(msg.data) if len(str(msg.data)) <= 256 else {}
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


def _video_format_payload(
    video_format: sdp.RtpVideoFormat,
) -> dict[str, object]:
    payload = {
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
    if video_format.encoding == "JPEG" and video_format.image_attributes is not None:
        payload["image_attributes"] = constraints_payload(video_format.image_attributes)
    return payload


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
    *,
    admission_duplicates: int = 0,
) -> None:
    """Publish loss state even when a timeout is the final media event."""

    active_reorder = input_reorder if session.requires_receive_transcoding else reorder
    counters["video_reordered_packets"] = active_reorder.reordered
    counters["video_lost_packets"] = active_reorder.lost
    counters["video_duplicate_packets"] = active_reorder.duplicates + admission_duplicates


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
    endpoint_id: str,
) -> None:
    route = VideoReceiveRoute.from_session(session)

    def rtp_source_allowed(host: str) -> bool:
        return host in {
            str(session.remote_rtp_host),
            str(session.signaling_host),
            route.remote_host,
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

    try:
        transport = await async_open_video_receiver(session, protocol)
    except asyncio.CancelledError:
        if transport is not None:
            transport.close()
        raise
    except (OSError, RuntimeError, ValueError) as err:
        if transport is not None:
            transport.close()
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
        raise

    assert transport is not None

    browser_format = session.browser_receive_format
    media_queue = queue
    transcode_transport: asyncio.DatagramTransport | None = None
    transcode_protocol: _RtpVideoProtocol | None = None
    transcoder: FfmpegVideoTranscoder | None = None
    jpeg_normalizer: _BrowserJpegNormalizer | None = None

    def close_setup_io() -> None:
        """Release I/O before awaiting potentially slow codec shutdown."""
        transport.close()
        if session.rtcp_socket is not None:
            session.rtcp_socket.close()
            session.rtcp_socket = None
        if transcode_transport is not None:
            transcode_transport.close()

    async def close_setup_resources() -> None:
        """Release media acquired before the long-lived task guard exists."""
        close_setup_io()
        if transcoder is not None:
            await transcoder.async_close()
        if jpeg_normalizer is not None:
            await jpeg_normalizer.async_close()

    if session.requires_receive_transcoding:
        try:
            pipeline = await async_open_receive_transcoder(
                hass, session, transcoder_factory=FfmpegVideoTranscoder,
                max_packets=_VIDEO_RTP_PACKET_QUEUE, max_bytes=_VIDEO_RTP_QUEUE_BYTES,
                close_parent_io=close_setup_io,
            )
            transcode_transport = pipeline.transport
            transcode_protocol = pipeline.protocol
            transcoder = pipeline.transcoder
            transcode_queue = pipeline.queue
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
    keepalive_updated = asyncio.Event()
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
    sender = BrowserVideoSender(rtp_source, transport, rtp_send_lock, route.rtp_destination, loop)
    ssrc = sender.ssrc
    outbound_clock = rtp_source.clock
    outbound_clock.reset_browser()
    registry = call_projection(hass)
    cached_parameter_sets: tuple[bytes, ...] = ()
    if registry is not None:
        cached_parameter_sets = (
            registry.artifact_for(session.call_id, "video_parameter_sets") or ()
        )
    depacketizer = _make_video_depacketizer(
        session.call_id,
        browser_format,
        registry,
        cached_parameter_sets,
    )
    access_units = _ByteBudgetQueue(
        maxsize=_VIDEO_ACCESS_UNIT_QUEUE,
        max_bytes=_VIDEO_ACCESS_UNIT_QUEUE_BYTES,
        item_size=lambda item: len(item.data),
    )
    rtcp_transport: asyncio.DatagramTransport | None = None
    rtcp_queue = _ByteBudgetQueue(
        maxsize=_VIDEO_RTCP_PACKET_QUEUE,
        max_bytes=_VIDEO_RTCP_QUEUE_BYTES,
        item_size=lambda item: len(item[0]),
    )
    rtcp_protocol = _RtpVideoProtocol(
        rtcp_queue,
        source_allowed=lambda host: (
            host
            in {
                str(session.remote_rtp_host),
                str(session.remote_rtcp_host),
                str(session.signaling_host),
                route.remote_host,
                route.remote_rtcp_host,
            }
        ),
        min_datagram_bytes=4,
    )
    counters = video_receive_counters(int(rtp_source.keepalives))
    state = VideoReceiveState(route, browser_format, depacketizer,
        int(session.media_generation), _video_pipeline_signature(session), counters)
    diagnostics = VideoReceiveDiagnostics(hass, endpoint_id, session, state,
        protocol, rtcp_protocol, transcode_protocol, loop,
        VideoDiagnosticHooks(_ha_softphone_store, debug_mode, merge_media_debug,
            _publish_ha_softphone_state, _sync_video_protocol_drop_counters))
    store_counters = diagnostics.store_counters
    updates = VideoGenerationUpdates(session, state, registry, cached_parameter_sets,
        queue, media_queue, access_units, ws, ws_send_lock, rtp_send_lock,
        outbound_clock, closed, keepalive_updated, store_counters,
        VideoGenerationHooks(_video_pipeline_signature, _video_negotiation_payload, _make_video_depacketizer))
    feedback = VideoRtcpWorkers(session, state, ssrc, outbound_clock, ws, ws_send_lock,
        closed, rtcp_queue, store_counters,
        VideoRtcpHooks(build_pli, build_fir, build_receiver_compound, build_sender_compound, parse_compound),
        loop, _RTCP_REPORT_INTERVAL, _KEYFRAME_FEEDBACK_INTERVAL, _VIDEO_RTP_RECEIVE_BURST_PACKETS)
    receiver = VideoReceiveWorkers(session, state, queue, media_queue, access_units,
        closed, registry, transcoder, updates.refresh, feedback.request_key_frame, store_counters,
        VideoReceiveHooks(decode_browser_video_packet, _video_loss_requires_key_frame, _sync_video_reorder_counters),
        _MAX_BROWSER_RECEIVE_ACCESS_UNIT_BYTES, _VIDEO_RTP_RECEIVE_BURST_PACKETS)
    jpeg_normalizer = _BrowserJpegNormalizer(hass, session, counters)

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
    active_sessions = require_runtime_data(hass).media.sessions_for("video")
    active_key = (session.call_id, endpoint_id)
    active_sessions[active_key] = session
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
        if active_sessions.get(active_key) is session:
            active_sessions.pop(active_key, None)
        await close_setup_resources()
        raise
    except (OSError, RuntimeError, ValueError) as err:
        if session.rtcp_socket is not None:
            session.rtcp_socket.close()
            session.rtcp_socket = None
        _LOGGER.debug("SIP video RTCP disabled call_id=%s: %s", session.call_id, err)
    except BaseException:
        if active_sessions.get(active_key) is session:
            active_sessions.pop(active_key, None)
        await close_setup_resources()
        raise

    feedback.transport = rtcp_transport

    call_ended, remove_call_listener = listen_for_media_call_end(
        hass, session.call_id, endpoint_id
    )


    rx_task = asyncio.create_task(receiver.rtp_to_access_units())
    ws_task = asyncio.create_task(
        _video_access_units_to_ws(ws, closed, access_units, ws_send_lock)
    )
    rtcp_task = asyncio.create_task(
        feedback.rtcp_reports(), name=f"voip-video-rtcp-{session.call_id}"
    )
    rtcp_input_task = asyncio.create_task(feedback.rtcp_to_browser_feedback())
    lifetime_task = asyncio.create_task(call_ended.wait())
    update_task = asyncio.create_task(updates.run())
    browser_task = asyncio.create_task(sender.run(
        ws, session, closed=closed, call_ended=call_ended, counters=counters,
        refresh_media_state=updates.refresh, request_key_frame=feedback.request_key_frame,
        store_counters=store_counters, jpeg_normalizer=jpeg_normalizer,
    ))
    keepalive_task = asyncio.create_task(
        run_symmetric_video_keepalive(
            session, closed=closed, keepalive_updated=keepalive_updated,
            counters=counters, sender=sender, rtp_send_lock=rtp_send_lock,
            rtp_source=rtp_source, transport=transport,
            destination=route.rtp_destination, store_counters=store_counters,
            probe_interval=_SYMMETRIC_RTP_KEEPALIVE_INTERVAL,
            probe_attempts=_SYMMETRIC_RTP_KEEPALIVE_ATTEMPTS,
            refresh_interval=_SYMMETRIC_RTP_REFRESH_INTERVAL,
        ), name=f"voip-video-keepalive-{session.call_id}"
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
        asyncio.create_task(receiver.rtp_to_transcoder()) if transcoder is not None else None
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
        await async_wait_for_media_workers(
            critical_tasks=critical_tasks, browser_task=browser_task,
            lifetime_task=lifetime_task, websocket=ws,
            websocket_transport=websocket_transport,
            call_id=session.call_id, channel="video",
        )
    finally:
        closed.set()
        if active_sessions.get(active_key) is session:
            active_sessions.pop(active_key, None)
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
        if feedback.keyframe_task is not None:
            tasks.append(feedback.keyframe_task)
        caller_cancelled = await async_stop_media_workers(
            tasks, (transport, transcode_transport, rtcp_transport)
        )
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
                transcode_protocol.dropped_packets
                if transcode_protocol is not None
                else 0
            )
            _sync_video_reorder_counters(
                counters, session, state.reorder, state.input_reorder,
                admission_duplicates=state.admission_duplicates,
            )
            store_counters(force=True)
            _LOGGER.info(
                "HA softphone video websocket detached call_id=%s counters=%s",
                session.call_id,
                counters,
            )
        if caller_cancelled:
            raise asyncio.CancelledError
