"""Bounded Home Assistant automation window for inbound SIP routing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING, Callable, Protocol

from homeassistant.core import HomeAssistant

from ..call_scope import set_pending_route, take_pending_route
from ..fsm import CallState, TerminalReason
from ..sip_listener import SipInviteResult
from ..websocket_api import _set_sip_bridge_call_state

if TYPE_CHECKING:
    from ..router import RouteDecision
    from ..sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5


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
        )


async def request_route_override(
    *,
    runtime: AutomationRouteRuntime,
    invite: SipInvite,
    decision: RouteDecision,
    registered_source: bool,
    caller_is_trusted_endpoint: bool,
    automation_routing_enabled: bool,
    trunk_invite: bool,
) -> AutomationRoute:
    """Open one bounded automation window or keep the dialplan default."""

    if (
        registered_source
        or not caller_is_trusted_endpoint
        or not automation_routing_enabled
    ):
        _LOGGER.debug(
            "SIP caller uses central dialplan without automation window caller=%s target=%s route=%s uri=%s",
            invite.caller or invite.source_host,
            invite.target,
            decision.action.value,
            decision.sip_uri or "-",
        )
        return AutomationRoute()

    hass = runtime.hass
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    expires_at = time.time() + SIP_ROUTE_DECISION_TIMEOUT
    set_pending_route(hass, invite.call_id, {
        "future": future,
        "invite": invite,
        "decision": decision,
        "created_at": time.time(),
        "expires_at": expires_at,
        "decision_deadline": expires_at,
        "fallback_destination": decision.target,
    })
    _set_sip_bridge_call_state(
        hass,
        CallState.CONNECTING.value,
        caller=invite.caller,
        callee=invite.target,
        peer_name=invite.caller,
        local_name=runtime.ha_peer_name(hass),
        call_id=invite.call_id,
        selected_tx_format=invite.send_format.audio_format.wire_token(),
        selected_rx_format=invite.recv_format.audio_format.wire_token(),
        selected_tx_rtp_format=invite.send_format.wire_token(),
        selected_rx_rtp_format=invite.recv_format.wire_token(),
        audio_mode="full_duplex",
        route_kind=decision.action.value,
        sip_uri=decision.sip_uri,
        sip_status_code=100,
        last_sip_event="INVITE",
        direction="incoming",
        ingress="trunk" if trunk_invite else "extension",
        origin="trunk" if trunk_invite else "extension",
        route_request=True,
        phase="route_decision",
        source_host=invite.source_host,
        caller_route=invite.routing_caller,
        target_route=invite.routing_target,
        target=decision.target,
        default_destination=decision.target,
        fallback_destination=decision.target,
        expires_at=expires_at,
        decision_deadline=expires_at,
        decision_timeout_ms=int(SIP_ROUTE_DECISION_TIMEOUT * 1000),
        rtp_format=(
            f"{invite.selected_format.encoding}/"
            f"{invite.selected_format.sample_rate}/"
            f"{invite.selected_format.channels}"
        ),
    )
    _LOGGER.info(
        "SIP route requested: caller=%s target=%s route=%s uri=%s media=%s/%s",
        invite.caller or invite.source_host,
        invite.target,
        decision.action.value,
        decision.sip_uri or "-",
        invite.selected_format.encoding,
        invite.selected_format.sample_rate,
    )
    try:
        payload = await asyncio.wait_for(
            future,
            timeout=SIP_ROUTE_DECISION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        payload = {}
    finally:
        take_pending_route(hass, invite.call_id)
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
    _set_sip_bridge_call_state(
        hass,
        (
            CallState.BUSY.value
            if app_reason == TerminalReason.BUSY.value
            else CallState.CANCELLED.value
            if status == 487
            else "declined"
        ),
        caller=invite.caller,
        callee=invite.target,
        peer_name=invite.caller,
        call_id=invite.call_id,
        reason=app_reason,
        origin="self",
        sip_status_code=status,
        last_sip_event="SIP_RESPONSE",
    )
    return SipInviteResult(
        status,
        reason,
        to_tag="",
        decline_reason=app_reason,
    )
