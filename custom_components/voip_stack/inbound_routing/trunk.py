"""Inbound trunk early media and DTMF route preparation."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Protocol

from homeassistant.core import HomeAssistant

from ..core import sdp as sip_sdp
from ..const import (
    CONF_SIP_VIDEO,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_INBOUND_MODE,
    TRUNK_INBOUND_MODE_DTMF,
)
from ..endpoint_lifecycle import create_runtime_task
from ..fsm import CallState
from ..media_ports import RtpPortReservation, reserve_sip_video_media
from ..runtime_data import call_runtime_artifacts
from ..core.sdp import build_answer_directional, constrained_video_direction
from ..sip_listener import SipInviteResult
from ..websocket_api import _set_sip_bridge_call_state

if TYPE_CHECKING:
    from ..pbx_runtime import SipEndpointRuntime
    from ..sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)
MAX_TRUNK_INFO_DIGITS = 16


class TrunkRouteRuntime(Protocol):
    """Dependencies used while preparing an inbound trunk route."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    run_trunk_inbound_route_guarded: Callable[..., Any]


def prepare_trunk_preanswer(
    *,
    runtime: TrunkRouteRuntime,
    invite: SipInvite,
    trunk_config: dict[str, Any],
    direct_route_preprocessed: bool,
    registry: SipEndpointRuntime,
) -> SipInviteResult | None:
    """Prepare bounded early media when DTMF selects the destination."""

    dtmf_timeout_ms = max(
        0,
        int(trunk_config.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0),
    )
    dtmf_preanswer = bool(
        trunk_config.get(CONF_TRUNK_INBOUND_MODE) == TRUNK_INBOUND_MODE_DTMF
        and trunk_config.get(CONF_TRUNK_DTMF_ENABLED)
        and dtmf_timeout_ms > 0
    )
    if not dtmf_preanswer:
        if not direct_route_preprocessed:
            raise RuntimeError("direct trunk route was not preprocessed")
        return None

    hass = runtime.hass
    cfg = runtime.config
    local_ip = runtime.local_ip
    artifacts = call_runtime_artifacts(hass)
    artifacts.trunk_closed_calls.discard(invite.call_id)
    artifacts.trunk_info_queues[invite.call_id] = asyncio.Queue(
        maxsize=MAX_TRUNK_INFO_DIGITS
    )
    try:
        bridge_ports = RtpPortReservation.allocate(hass)
    except RuntimeError as err:
        _LOGGER.warning("SIP trunk RTP bridge port allocation failed: %s", err)
        return SipInviteResult(503, "Service Unavailable", to_tag="")
    source_relay_port, _dest_relay_port = bridge_ports.ports
    video_media_reservation = None
    video_rtp_socket = None
    video_rtcp_socket = None
    source_video_port = 0
    video_failure_reason = ""
    if invite.video_format is not None and cfg.get(CONF_SIP_VIDEO, False):
        try:
            (
                video_media_reservation,
                video_rtp_socket,
                video_rtcp_socket,
            ) = reserve_sip_video_media(hass)
            _unused_audio_port, source_video_port = video_media_reservation.ports
        except (OSError, RuntimeError) as err:
            video_failure_reason = "local_video_resources_unavailable"
            _LOGGER.warning(
                "SIP trunk DTMF video socket unavailable; collecting digits audio-only: %s",
                err,
            )
    preanswered_media = {
        # Early media is provisional. The winning endpoint owns the final
        # answer and may narrow media according to its capabilities.
        "final_response_sent": False,
        "local_rtp_port": source_relay_port,
        "local_video_rtp_port": source_video_port,
        "video_direction": "recvonly" if source_video_port else "inactive",
        "rtp_reservation": bridge_ports,
        "video_rtp_reservation": video_media_reservation,
        "video_rtp_socket": video_rtp_socket,
        "video_rtcp_socket": video_rtcp_socket,
        "video_failure_reason": video_failure_reason,
    }
    callee = str(trunk_config.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA")
    registry.upsert(
        invite.call_id,
        state=CallState.CONNECTING.value,
        owner="router",
        caller=invite.caller,
        callee=callee,
        route_kind="trunk",
        ingress="trunk",
        origin="trunk",
    )
    registry.set_pending_invite(invite.call_id, invite)
    registry.attach_media(
        invite.call_id,
        preanswered_media,
        provisional=True,
    )
    expires_at = time.time() + (float(dtmf_timeout_ms) / 1000.0)
    dtmf_formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
    dtmf_format = dtmf_formats[0] if dtmf_formats else None
    # RFC 4733 works in provisional early media. SIP INFO is an in-dialog
    # compatibility transport, so only its branch receives an immediate 2xx.
    confirm_for_sip_info = dtmf_format is None
    preanswered_media["final_response_sent"] = confirm_for_sip_info
    preanswer_video_direction = (
        constrained_video_direction(
            invite.video_format.direction,
            allow_send=True,
        )
        if source_video_port and invite.video_format is not None
        else "inactive"
    )
    answer = build_answer_directional(
        local_ip,
        local_ip,
        source_relay_port,
        invite.send_format,
        invite.recv_format,
        dtmf=dtmf_format,
        remote_sdp=invite.remote_sdp,
        video_port=source_video_port,
        video_format=(invite.answer_video_format if source_video_port else None),
        # SDP advertises capability. Browser camera access remains gated by
        # the explicit answer action.
        video_direction=preanswer_video_direction,
    )
    if not registry.update_media(
        invite.call_id,
        provisional=True,
        video_direction=preanswer_video_direction,
        early_answer_sdp=answer,
    ):
        raise RuntimeError("preanswered media owner disappeared during trunk setup")
    _set_sip_bridge_call_state(
        hass,
        CallState.CONNECTING.value,
        caller=invite.caller,
        callee=callee,
        peer_name=invite.caller,
        call_id=invite.call_id,
        selected_tx_format=invite.send_format.audio_format.wire_token(),
        selected_rx_format=invite.recv_format.audio_format.wire_token(),
        selected_tx_rtp_format=invite.send_format.wire_token(),
        selected_rx_rtp_format=invite.recv_format.wire_token(),
        audio_mode="full_duplex",
        route_kind="trunk",
        sip_status_code=200 if confirm_for_sip_info else 183,
        last_sip_event="INVITE",
        direction="incoming",
        scope="sip_trunk",
        phase="dtmf_route",
        source_host=invite.source_host,
        expires_at=expires_at,
        decision_timeout_ms=dtmf_timeout_ms,
        video_requested=bool(invite.video_format is not None),
        video_negotiated=bool(source_video_port),
        video_status=(
            "degraded"
            if video_failure_reason
            else "active"
            if source_video_port
            else "rejected"
            if invite.video_format is not None
            else "inactive"
        ),
        video_failure_reason=video_failure_reason,
    )
    create_runtime_task(
        hass,
        runtime.run_trunk_inbound_route_guarded(
            invite,
            bridge_ports=bridge_ports,
        ),
    )
    if confirm_for_sip_info:
        return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
    return SipInviteResult(
        183,
        "Session Progress",
        answer_sdp=answer,
        to_tag="",
        defer_final=True,
    )
