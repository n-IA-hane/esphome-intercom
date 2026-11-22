"""Adapt inbound SIP and logical origination to the shared PBX route window."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Callable, Protocol

from homeassistant.core import HomeAssistant

from ..routing_automation import request_route_window
from ..routing_automation import SIP_ROUTE_DECISION_TIMEOUT as SIP_ROUTE_DECISION_TIMEOUT
from ..endpoint_lifecycle import call_registry
from ..fsm import CallState, TerminalReason
from ..pbx_trace import trace_call
from ..sip_listener import SipInviteResult

if TYPE_CHECKING:
    from ..router import RouteDecision
    from ..sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)


class AutomationRouteRuntime(Protocol):
    """Dependencies used by the route decision window."""

    hass: HomeAssistant
    ha_peer_name: Callable[..., str]


@dataclass(frozen=True, slots=True)
class AutomationRoute:
    """Normalized result of one optional HA routing decision."""

    action: str = "default"
    destination: str = ""
    status: int = 0
    reason: str = ""
    decline_reason: str = ""
    endpoint_id: str = ""
    trunk_id: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> AutomationRoute:
        if not isinstance(payload, dict):
            return cls()
        return cls(
            action=str(payload.get("action") or "default").strip().lower(),
            destination=str(payload.get("destination") or "").strip(),
            status=int(payload.get("status") or 0),
            reason=str(payload.get("reason") or "").strip(),
            decline_reason=str(payload.get("decline_reason") or "").strip(),
            endpoint_id=str(payload.get("endpoint_id") or "").strip(),
            trunk_id=str(payload.get("trunk_id") or "").strip(),
        )


async def request_route_override(
    *,
    runtime: AutomationRouteRuntime,
    invite: SipInvite,
    decision: RouteDecision,
    authenticated_account: bool,
    caller_is_trusted_endpoint: bool,
    automation_routing_enabled: bool,
    trunk_invite: bool,
    native_override: bool = False,
) -> AutomationRoute:
    """Open one bounded automation window or keep the dialplan default."""

    if (
        (authenticated_account and not native_override)
        or not caller_is_trusted_endpoint
        or not automation_routing_enabled
    ):
        trace_call(
            _LOGGER, invite.call_id,
            "Using the central dialplan without a routing-action window: %s",
            "authenticated SIP account" if authenticated_account and not native_override
            else "caller is not trusted for routing actions" if not caller_is_trusted_endpoint
            else "routing actions are disabled",
        )
        return AutomationRoute()

    hass = runtime.hass
    registry = call_registry(hass)
    session = registry.upsert(
        invite.call_id, state=CallState.CONNECTING.value,
        caller=invite.caller, callee=decision.target or invite.target,
        route_kind=decision.action.value,
    )
    if session is None:
        return AutomationRoute()
    from ..automation_routing import RouteRequest, route_window_snapshot

    logical = isinstance(invite, RouteRequest)
    presentation = route_window_snapshot(invite, decision, trunk_invite=trunk_invite)
    presentation.update(peer_name=invite.caller, local_name=runtime.ha_peer_name(hass),
        route_kind=decision.action.value, sip_uri=decision.sip_uri,
        direction="outgoing" if logical else "incoming",
        origin=presentation["ingress"], source_host=invite.source_host,
        caller_route=invite.routing_caller, target_route=invite.routing_target,
        dialed_target=invite.routing_target, target=decision.target,
        default_destination=decision.target, fallback_destination=decision.target)
    if logical:
        from ..counterpart_display import counterpart_display

        presentation["peer_name"] = counterpart_display(fallback=invite.target, entry=decision.entry)
    else:
        presentation.update(sip_status_code=100, last_sip_event="INVITE")
    if getattr(invite, "send_format", None) is not None:
        presentation.update(
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(), audio_mode="full_duplex",
            rtp_format=f"{invite.selected_format.encoding}/{invite.selected_format.sample_rate}/{invite.selected_format.channels}")
    payload = await request_route_window(hass, session, invite=invite, decision=decision, **presentation)
    return AutomationRoute.from_payload(payload)


def automation_rejection(
    *,
    hass: HomeAssistant,
    invite: SipInvite,
    route: AutomationRoute,
) -> SipInviteResult | None:
    """Convert an automation decline, busy or cancel into one SIP result."""

    if route.action not in {"decline", "busy", "cancel"}:
        return None
    if route.action == "busy":
        status = route.status or 486
        reason = route.reason or "Busy Here"
        app_reason = TerminalReason.BUSY.value
    elif route.action == "cancel":
        status = route.status or 487
        reason = route.reason or "Request Terminated"
        app_reason = TerminalReason.CANCELLED.value
    else:
        status = route.status or 603
        reason = route.reason or "Decline"
        app_reason = route.decline_reason or TerminalReason.DECLINED.value
    return SipInviteResult(
        status,
        reason,
        to_tag="",
        decline_reason=app_reason,
    )
