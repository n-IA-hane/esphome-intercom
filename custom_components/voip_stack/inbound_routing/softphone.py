"""Inbound SIP routing to a Home Assistant browser softphone."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from ..endpoint_registry import EndpointBusyError
from ..fsm import CallState, TerminalReason
from ..media_ports import (
    allocate_sip_rtp_port,
    reserve_sip_video_media,
    take_delayed_offer_ports,
)
from ..phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
    PhoneEndpoint,
)
from ..router import RouteReason
from ..core.sdp import build_answer_directional, constrained_video_direction
from ..sip_listener import SipInviteResult
from ..websocket_api import _set_ha_softphone_call_state

if TYPE_CHECKING:
    from ..pbx_runtime import SipEndpointRuntime
    from ..router import RouteDecision
    from ..sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BrowserRouteTarget:
    """Resolved browser endpoint identity for one inbound route."""

    endpoint: PhoneEndpoint | None
    endpoint_id: str
    device_id: str


def _resolve_browser_target(
    target_endpoint: PhoneEndpoint | None,
    *,
    require_browser_kind: bool,
) -> BrowserRouteTarget | None:
    endpoint = target_endpoint
    if require_browser_kind and (
        endpoint is None or endpoint.kind is not EndpointKind.BROWSER
    ):
        endpoint = None
    if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
        return None
    return BrowserRouteTarget(endpoint, endpoint.endpoint_id, endpoint.device_id)


def defer_browser_softphone_invite(
    *,
    registry: SipEndpointRuntime,
    invite: SipInvite,
    decision: RouteDecision,
    resolved_callee: str,
    source_endpoint: PhoneEndpoint | None,
    target_endpoint: PhoneEndpoint | None,
    defer_invite: Callable[..., None],
) -> SipInviteResult:
    """Publish ringing and leave the final answer to the owning browser."""

    target = _resolve_browser_target(
        target_endpoint,
        require_browser_kind=False,
    )
    if target is None:
        return SipInviteResult(
            480,
            "Temporarily Unavailable",
            to_tag="",
            decline_reason=RouteReason.TARGET_UNREACHABLE.value,
        )
    try:
        if (
            source_endpoint is not None
            and source_endpoint.kind is not EndpointKind.BROWSER
        ):
            registry.upsert(
                invite.call_id,
                state=CallState.RINGING.value,
                owner="router",
                caller=invite.caller,
                callee=resolved_callee,
                route_kind=decision.action.value,
                source_endpoint_id=source_endpoint.endpoint_id,
            )
            registry.claim_endpoint(
                invite.call_id,
                source_endpoint.endpoint_id,
                role="source",
                adopt_transport=True,
            )
        defer_invite(
            invite,
            route_kind=decision.action.value,
            endpoint_id=target.endpoint_id,
            endpoint_device_id=target.device_id,
            callee=resolved_callee,
            sip_uri=decision.sip_uri,
        )
    except EndpointBusyError:
        registry.terminate_call(
            invite.call_id,
            reason=TerminalReason.BUSY.value,
        )
        return SipInviteResult(
            486,
            "Busy Here",
            to_tag="",
            decline_reason=TerminalReason.BUSY.value,
        )
    return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)


def answer_inbound_ha_softphone(
    *,
    hass: HomeAssistant,
    local_ip: str,
    registry: SipEndpointRuntime,
    invite: SipInvite,
    decision: RouteDecision,
    resolved_callee: str,
    source_endpoint: PhoneEndpoint | None,
    target_endpoint: PhoneEndpoint | None,
    dtmf_format: Any,
) -> SipInviteResult:
    """Accept an inbound SIP call into the selected browser softphone."""

    target = _resolve_browser_target(
        target_endpoint,
        require_browser_kind=True,
    )
    if target is None:
        return SipInviteResult(
            480,
            "Temporarily Unavailable",
            to_tag="",
            decline_reason=RouteReason.TARGET_UNREACHABLE.value,
        )
    browser_endpoint = target.endpoint
    if browser_endpoint is not None:
        if browser_endpoint.dnd or (
            browser_endpoint.active_call_id
            and browser_endpoint.active_call_id != invite.call_id
        ):
            return SipInviteResult(
                486,
                "Busy Here",
                to_tag="",
                decline_reason=TerminalReason.BUSY.value,
            )
        if browser_endpoint.availability is EndpointAvailability.UNAVAILABLE:
            return SipInviteResult(
                480,
                "Temporarily Unavailable",
                to_tag="",
                decline_reason=RouteReason.TARGET_UNREACHABLE.value,
            )

    registry.upsert(
        invite.call_id,
        state=CallState.CONNECTING.value,
        owner="ha_softphone",
        caller=invite.caller,
        callee=resolved_callee,
        route_kind=decision.action.value,
        endpoint_id=target.endpoint_id,
        session_device_id=target.device_id,
        source_endpoint_id=(
            source_endpoint.endpoint_id
            if source_endpoint is not None
            and source_endpoint.kind is not EndpointKind.BROWSER
            else ""
        ),
    )
    try:
        if (
            source_endpoint is not None
            and source_endpoint.kind is not EndpointKind.BROWSER
        ):
            registry.claim_endpoint(
                invite.call_id,
                source_endpoint.endpoint_id,
                role="source",
                adopt_transport=True,
            )
        registry.claim_endpoint(
            invite.call_id,
            target.endpoint_id,
            role="destination",
        )
    except EndpointBusyError:
        registry.terminate_call(
            invite.call_id,
            reason=TerminalReason.BUSY.value,
        )
        return SipInviteResult(
            486,
            "Busy Here",
            to_tag="",
            decline_reason=TerminalReason.BUSY.value,
        )

    media_reservation = None
    local_video_rtp_port = 0
    video_rtp_socket = None
    video_rtcp_socket = None
    video_failure_reason = ""
    endpoint_video_enabled = (
        browser_endpoint is None or browser_endpoint.supports("video")
    )
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
                "SIP video socket unavailable, answering audio-only: %s", err
            )
            media_reservation = None
            video_failure_reason = "local_video_resources_unavailable"
            local_rtp_port = allocate_sip_rtp_port(hass)
            local_video_rtp_port = 0
    else:
        media_reservation = take_delayed_offer_ports(registry, invite.call_id)
        local_rtp_port = (
            media_reservation.ports[0]
            if media_reservation is not None
            else allocate_sip_rtp_port(hass)
        )

    video_direction = (
        constrained_video_direction(
            invite.video_format.direction,
            # Automation answers have no browser permission or card camera
            # choice. Only explicit answer actions may advertise camera video.
            allow_send=False,
        )
        if invite.video_format is not None and endpoint_video_enabled
        else "inactive"
    )
    answer = build_answer_directional(
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
    softphone_media = {
        "invite": invite,
        "local_rtp_port": local_rtp_port,
        "local_video_rtp_port": local_video_rtp_port,
        "video_direction": video_direction,
        "camera_send_authorized": False,
        "video_rtp_socket": video_rtp_socket,
        "video_rtcp_socket": video_rtcp_socket,
        "rtp_reservation": media_reservation,
        "endpoint_id": target.endpoint_id,
        "video_failure_reason": video_failure_reason,
    }
    registry.upsert(
        invite.call_id,
        state=CallState.IN_CALL.value,
        owner="ha_softphone",
        caller=invite.caller,
        callee=resolved_callee,
        route_kind=decision.action.value,
        endpoint_id=target.endpoint_id,
        session_device_id=target.device_id,
    )
    registry.attach_media(invite.call_id, softphone_media)
    registry.add_leg(
        invite.call_id,
        invite.call_id,
        role="ha_softphone",
        state=CallState.IN_CALL.value,
    )
    video_active = bool(
        invite.video_format is not None
        and local_video_rtp_port
        and video_direction != "inactive"
    )
    _set_ha_softphone_call_state(
        hass,
        CallState.IN_CALL.value,
        endpoint_id=target.endpoint_id,
        session_device_id=target.device_id,
        caller=invite.caller,
        callee=resolved_callee,
        peer_name=invite.caller,
        direction="incoming",
        call_id=invite.call_id,
        selected_tx_format=invite.send_format.audio_format.wire_token(),
        selected_rx_format=invite.recv_format.audio_format.wire_token(),
        selected_tx_rtp_format=invite.send_format.wire_token(),
        selected_rx_rtp_format=invite.recv_format.wire_token(),
        audio_direction=invite.local_audio_direction,
        audio_connection_held=invite.remote_audio_connection_held,
        video_active=video_active,
        video_requested=bool(invite.video_format is not None),
        video_negotiated=bool(
            invite.video_format is not None and local_video_rtp_port
        ),
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
        video_format=(
            invite.video_format.wire_token() if invite.video_format else ""
        ),
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
        audio_mode="full_duplex",
        route_kind=decision.action.value,
        sip_uri=decision.sip_uri,
        sip_status_code=200,
        last_sip_event="SIP_RESPONSE",
    )
    return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
