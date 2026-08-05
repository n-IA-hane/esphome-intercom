"""Transactional inbound answering for Home Assistant browser phones."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .call_scope import endpoint_call_ids, pending_routes
from .config import transport_config
from .const import CONF_VIDEO_CAMERA_SEND, DOMAIN
from .fsm import CallState, TerminalReason
from .inbound_answer import AnswerTransaction
from .media_ports import (
    allocate_sip_rtp_port,
    release_media_reservation,
    reserve_sip_video_media,
)
from .peer_snapshot import async_advertise_host
from .route_decisions import set_pending_route_decision
from .sip_runtime import send_bye, send_final_response
from .softphone_commands import BrowserCallCommand, bind_service_call_controller
from .video_rtp import RtpSenderState
from .websocket_api import _set_ha_softphone_call_state


_LOGGER = logging.getLogger(__name__)


async def async_answer_browser_call(
    hass: HomeAssistant,
    call: ServiceCall,
    command: BrowserCallCommand,
) -> None:
    """Answer one inbound browser call using a single transactional owner."""

    endpoint_id = command.endpoint_id
    browser_endpoint = command.endpoint
    local_name = command.endpoint_name
    endpoint_device_id = command.device_id
    call_id = command.call_id
    registry = command.registry
    if call_id and registry.resolve_session_id(call_id) in registry.sessions:
        bind_service_call_controller(
            registry,
            call_id,
            call,
            endpoint_id=endpoint_id,
        )
    camera_send_requested = bool(
        transport_config(hass).get(CONF_VIDEO_CAMERA_SEND, False)
    ) and bool(call.data.get("send_video", False))

    from .local_softphone_bridge import LocalBridgeError
    from .local_softphone_runtime import local_softphone_bridge

    local_bridge = local_softphone_bridge(hass)
    if local_bridge is not None and local_bridge.get_call(call_id) is not None:
        try:
            local_bridge.answer(
                call_id,
                endpoint_id,
                str(call.data.get("media_client_id") or ""),
                enable_video_send=camera_send_requested,
            )
        except LocalBridgeError as err:
            raise ServiceValidationError(str(err)) from err
        return

    bucket = hass.data.setdefault(DOMAIN, {})
    # A browser ring-group member resolves its own pending candidate before
    # the generic forwarding guard. The fork controller then commits the only
    # winner and cancels every sibling B-leg.
    if call_id and call_id in pending_routes(hass):
        set_pending_route_decision(
            hass,
            {
                "call_id": call_id,
                "action": "answer_ha",
                "endpoint_id": endpoint_id,
                "media_client_id": str(call.data.get("media_client_id") or ""),
                "send_video": camera_send_requested,
            },
        )
        return
    forward_task = bucket.get("forward_tasks", {}).get(call_id)
    forward_claimed = call_id in bucket.get("forward_claims", set())
    group_answer_commit = call_id in bucket.get("ring_group_answer_commits", set())
    if not group_answer_commit and (
        forward_claimed or (forward_task is not None and not forward_task.done())
    ):
        raise ServiceValidationError(f"call_id {call_id} is being forwarded")

    if call_id.startswith("conference:"):
        manager = bucket.get("conference_manager")
        resolved = manager.resolve_ha_call(call_id) if manager is not None else None
        if resolved is None or resolved[1] != endpoint_id:
            raise ServiceValidationError(
                f"conference call {call_id} does not belong to phone {endpoint_id}"
            )
        room_name = resolved[0]
        joined = manager.join_ha_softphone(
            room_name,
            endpoint_id=endpoint_id,
            call_id=call_id,
        )
        if joined is None:
            _LOGGER.warning(
                "sip_answer: conference room not found or full for %s",
                call_id,
            )
            return
        _joined_call_id, queue = joined
        conference_media = {
            "conference_room": room_name,
            "conference_queue": queue,
            "endpoint_id": endpoint_id,
            "media_client_id": str(call.data.get("media_client_id") or ""),
        }
        registry.upsert(
            call_id,
            state=CallState.IN_CALL.value,
            owner="ha_softphone",
            caller=room_name,
            callee=local_name,
            route_kind="conference",
            endpoint_id=endpoint_id,
        )
        registry.attach_media(call_id, conference_media)
        bind_service_call_controller(registry, call_id, call)
        registry.add_leg(
            call_id,
            call_id,
            role="ha_softphone",
            state=CallState.IN_CALL.value,
        )
        _set_ha_softphone_call_state(
            hass,
            CallState.IN_CALL.value,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            caller=room_name,
            callee=local_name,
            peer_name=room_name,
            direction="incoming",
            call_id=call_id,
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            selected_tx_format="16000:s16le:1:20",
            selected_rx_format="16000:s16le:1:20",
            selected_tx_rtp_format="pt=96:L16/16000/1/20ms",
            selected_rx_rtp_format="pt=96:L16/16000/1/20ms",
        )
        return

    pending = registry.pending_invites
    endpoint_pending = endpoint_call_ids(registry, pending, endpoint_id)
    if not call_id and len(endpoint_pending) == 1:
        call_id = endpoint_pending[0]
        bind_service_call_controller(registry, call_id, call)
    invite = pending.get(call_id) if call_id else None
    if invite is None:
        raise ServiceValidationError(
            f"SIP call {call_id or '(current)'} was already answered or is no longer ringing"
        )

    session = registry.sessions.get(registry.resolve_session_id(call_id))
    pbx_runtime = bucket.get("pbx_runtime")
    authoritative_session = (
        pbx_runtime.get_session(
            registry.resolve_session_id(call_id),
            generation=session.generation if session is not None else None,
        )
        if pbx_runtime is not None
        else None
    )
    if authoritative_session is None:
        raise ServiceValidationError(
            f"PBX session for call_id {call_id} is no longer available"
        )

    preanswered = registry.take_media(call_id, provisional=True)
    from .sdp import (
        browser_video_send_supported,
        build_answer_directional,
        constrained_video_direction,
        offered_dtmf_formats,
    )

    local_rtp_port = int((preanswered or {}).get("local_rtp_port") or 0)
    local_video_rtp_port = int(
        (preanswered or {}).get("local_video_rtp_port") or 0
    )
    video_rtp_socket = (preanswered or {}).get("video_rtp_socket")
    video_rtcp_socket = (preanswered or {}).get("video_rtcp_socket")
    video_rtp_source = (preanswered or {}).get("video_rtp_source")
    media_reservation = (preanswered or {}).get("rtp_reservation")
    video_media_reservation = (preanswered or {}).get("video_rtp_reservation")
    video_failure_reason = str(
        (preanswered or {}).get("video_failure_reason") or ""
    )
    endpoint_video_enabled = (
        browser_endpoint is None or browser_endpoint.supports("video")
    )
    camera_send_enabled = (
        endpoint_video_enabled
        and bool(transport_config(hass).get(CONF_VIDEO_CAMERA_SEND, False))
        and bool(call.data.get("send_video", False))
    )
    video_direction = (
        constrained_video_direction(
            invite.video_format.direction,
            allow_send=(
                camera_send_enabled
                and browser_video_send_supported(invite.video_format)
                and not invite.remote_video_connection_held
            ),
        )
        if invite.video_format is not None and endpoint_video_enabled
        else "inactive"
    )
    if preanswered is not None and preanswered.get("final_response_sent", True):
        negotiated_video_direction = str(
            preanswered.get("video_direction") or "inactive"
        )
        video_direction = constrained_video_direction(
            negotiated_video_direction,
            allow_send=(
                camera_send_enabled
                and browser_video_send_supported(invite.video_format)
                and not invite.remote_video_connection_held
            ),
        )

    answer_sdp = ""
    dtmf_formats = offered_dtmf_formats(invite.remote_sdp)
    dtmf_format = dtmf_formats[0] if dtmf_formats else None
    response_already_sent = bool(
        preanswered is not None and preanswered.get("final_response_sent", True)
    )
    if local_rtp_port:
        if not bool((preanswered or {}).get("final_response_sent", True)):
            local_ip = await async_advertise_host(hass)
            answer_sdp = build_answer_directional(
                local_ip,
                local_ip,
                local_rtp_port,
                invite.send_format,
                invite.recv_format,
                dtmf=dtmf_format,
                remote_sdp=invite.remote_sdp,
                video_port=local_video_rtp_port,
                video_format=(
                    invite.answer_video_format
                    if local_video_rtp_port and endpoint_video_enabled
                    else None
                ),
                video_direction=video_direction,
            )
        _LOGGER.info("SIP answered early-media trunk call_id=%s", call_id)
    else:
        local_ip = await async_advertise_host(hass)
        if invite.video_format is not None and endpoint_video_enabled:
            try:
                (
                    media_reservation,
                    video_rtp_socket,
                    video_rtcp_socket,
                ) = reserve_sip_video_media(hass)
                local_rtp_port, local_video_rtp_port = media_reservation.ports
            except (OSError, RuntimeError) as err:
                _LOGGER.warning(
                    "SIP video socket unavailable, answering audio-only: %s",
                    err,
                )
                media_reservation = None
                video_failure_reason = "local_video_resources_unavailable"
                local_rtp_port = allocate_sip_rtp_port(hass)
                local_video_rtp_port = 0
        else:
            local_rtp_port = allocate_sip_rtp_port(hass)
        if local_video_rtp_port and invite.video_format is not None:
            video_rtp_source = RtpSenderState.create(
                clock_rate=int(invite.video_format.clock_rate),
                now=asyncio.get_running_loop().time(),
            )
        answer_sdp = build_answer_directional(
            local_ip,
            local_ip,
            local_rtp_port,
            invite.send_format,
            invite.recv_format,
            dtmf=dtmf_format,
            remote_sdp=invite.remote_sdp,
            video_port=local_video_rtp_port,
            video_format=(
                invite.answer_video_format if endpoint_video_enabled else None
            ),
            video_direction=video_direction,
        )

    resolved_callee = str(
        (session.callee if session is not None else "") or local_name
    )
    softphone_media = {
        "invite": invite,
        "local_rtp_port": local_rtp_port,
        "local_video_rtp_port": local_video_rtp_port,
        "video_direction": video_direction,
        # Per-call consent remains valid if a later standard re-INVITE adds
        # video to an initially audio-only dialog.
        "camera_send_authorized": bool(camera_send_enabled),
        "video_rtp_socket": video_rtp_socket,
        "video_rtcp_socket": video_rtcp_socket,
        "video_rtp_source": video_rtp_source,
        "rtp_reservation": media_reservation,
        "video_rtp_reservation": video_media_reservation,
        "endpoint_id": endpoint_id,
        "media_client_id": str(call.data.get("media_client_id") or ""),
        "video_failure_reason": video_failure_reason,
    }

    def _release_answer_media(_reason: str) -> None:
        if registry.softphone_media.get(call_id) is softphone_media:
            registry.softphone_media.pop(call_id, None)
        release_media_reservation(softphone_media)

    transaction = AnswerTransaction(
        authoritative_session,
        lambda status, reason, sdp: (
            True
            if response_already_sent
            else send_final_response(
                hass,
                call_id,
                status,
                reason,
                answer_sdp=sdp,
            )
        ),
    )
    transaction.add_resource(
        f"softphone_media:{call_id}",
        softphone_media,
        _release_answer_media,
    )

    def _claim_answer() -> bool:
        if pending.get(call_id) is not invite:
            return False
        claimed = registry.transition(
            call_id,
            state=CallState.IN_CALL.value,
            owner="ha_softphone",
            caller=invite.caller,
            callee=resolved_callee,
            route_kind="ha_softphone",
            endpoint_id=endpoint_id,
            media_client_id=str(call.data.get("media_client_id") or ""),
            expected_generation=authoritative_session.generation,
        )
        if claimed is None:
            return False
        registry.take_pending_invite(call_id)
        return True

    answer_result = await transaction.commit(answer_sdp, claim=_claim_answer)
    if not answer_result.committed:
        if response_already_sent:
            send_bye(hass, call_id)
        registry.finish_and_pop(
            call_id,
            reason=answer_result.reason or TerminalReason.PROTOCOL_ERROR.value,
            state=CallState.CANCELLED.value,
        )
        raise ServiceValidationError(
            f"SIP answer transaction failed for call_id {call_id}: "
            f"{answer_result.reason or 'unknown error'}"
        )

    registry.attach_media(call_id, softphone_media)
    registry.add_leg(
        call_id,
        call_id,
        role="ha_softphone",
        state=CallState.IN_CALL.value,
    )
    _LOGGER.info("SIP answered call_id=%s", call_id)
    video_active = bool(
        invite.video_format is not None
        and local_video_rtp_port
        and video_direction != "inactive"
    )
    _set_ha_softphone_call_state(
        hass,
        CallState.IN_CALL.value,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
        caller=invite.caller,
        callee=resolved_callee,
        peer_name=invite.caller,
        connected_party=invite.caller,
        answered_by=invite.caller,
        direction="incoming",
        call_id=call_id,
        dialed_target=invite.target,
        sip_status_code=200,
        last_sip_event="SIP_RESPONSE",
        selected_tx_format=invite.send_format.audio_format.wire_token(),
        selected_rx_format=invite.recv_format.audio_format.wire_token(),
        selected_tx_rtp_format=invite.send_format.wire_token(),
        selected_rx_rtp_format=invite.recv_format.wire_token(),
        audio_direction=invite.local_audio_direction,
        audio_connection_held=invite.remote_audio_connection_held,
        video_active=video_active,
        video_requested=bool(invite.video_format is not None),
        video_negotiated=bool(invite.video_format is not None and local_video_rtp_port),
        video_status=(
            "degraded"
            if video_failure_reason
            else "active"
            if video_active
            else "rejected"
            if invite.video_format is not None
            else "inactive"
        ),
        video_failure_reason=video_failure_reason,
        video_format=(invite.video_format.wire_token() if invite.video_format else ""),
        video_send_format=(
            invite.send_video_format.wire_token()
            if invite.send_video_format is not None
            else ""
        ),
        video_receive_format=(
            invite.recv_video_format.wire_token()
            if invite.recv_video_format is not None
            else ""
        ),
        video_direction=(
            video_direction
            if invite.video_format is not None and local_video_rtp_port
            else "inactive"
        ),
    )
