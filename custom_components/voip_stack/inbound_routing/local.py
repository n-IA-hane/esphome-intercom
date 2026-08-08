"""Inbound routing to local Assist, ring group and conference owners."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Protocol

from homeassistant.core import HomeAssistant

from ..call_registry import TERMINAL_STATES
from ..conference import conference_manager
from ..endpoint_lifecycle import create_runtime_task
from ..endpoint_registry import EndpointBusyError
from ..endpoint_termination import EndpointTerminationHandler
from ..endpoint_session import TerminationInitiator
from ..fsm import CallState, TerminalReason
from ..groups import GROUP_TYPE_CONFERENCE, GROUP_TYPE_RING
from ..media_ports import RtpPortReservation, take_delayed_offer_ports
from ..phone_endpoint import EndpointKind
from ..router import RouteAction
from ..core.sdp import build_answer_directional
from ..sip_listener import SipInviteResult

if TYPE_CHECKING:
    from ..pbx_runtime import SipEndpointRuntime
    from ..phone_endpoint import PhoneEndpoint
    from ..router import RouteDecision
    from ..sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)


class LocalRouteRuntime(Protocol):
    """Dependencies required by local inbound route owners."""

    hass: HomeAssistant
    local_ip: str
    browser_leg_for_member: Callable[..., Any]
    on_conference_inbound_timeout: Callable[..., Any]
    ring_conference_members: Callable[..., Any]
    run_ring_group_call: Callable[..., Any]
    start_local_assist_bridge: Callable[..., Any]


def _busy_result() -> SipInviteResult:
    return SipInviteResult(
        486,
        "Busy Here",
        to_tag="",
        decline_reason=TerminalReason.BUSY.value,
    )


async def _claim_source(
    *,
    hass: HomeAssistant,
    registry: SipEndpointRuntime,
    invite: SipInvite,
    source_endpoint: PhoneEndpoint | None,
    state: str,
    route_kind: str,
) -> SipInviteResult | None:
    if source_endpoint is None or source_endpoint.kind is EndpointKind.BROWSER:
        return None
    registry.upsert(
        invite.call_id,
        state=state,
        owner="router",
        caller=invite.caller,
        callee=invite.target,
        route_kind=route_kind,
        source_endpoint_id=source_endpoint.endpoint_id,
    )
    try:
        registry.claim_endpoint(
            invite.call_id,
            source_endpoint.endpoint_id,
            role="source",
            adopt_transport=True,
        )
    except EndpointBusyError:
        await EndpointTerminationHandler(hass).terminate_reason(
            invite.call_id,
            TerminalReason.BUSY.value,
            TerminationInitiator.ROUTING,
        )
        return _busy_result()
    return None


async def route_local_assist(
    *,
    runtime: LocalRouteRuntime,
    invite: SipInvite,
    decision: RouteDecision,
    roster_entries: list[Any],
    source_endpoint: PhoneEndpoint | None,
    registry: SipEndpointRuntime,
    source: str,
    called_extension: str,
) -> SipInviteResult:
    """Start the one local Assist RTP owner and return its SIP answer."""

    if any(
        session.route_kind == RouteAction.ASSIST.value
        and session.state not in TERMINAL_STATES
        for session in registry.sessions.values()
    ):
        return _busy_result()
    if busy := await _claim_source(
        hass=runtime.hass,
        registry=registry,
        invite=invite,
        source_endpoint=source_endpoint,
        state=CallState.CONNECTING.value,
        route_kind=RouteAction.ASSIST.value,
    ):
        return busy
    try:
        assist_ports = take_delayed_offer_ports(
            registry, invite.call_id
        ) or RtpPortReservation.allocate(runtime.hass)
    except RuntimeError as err:
        _LOGGER.warning("Assist RTP port allocation failed: %s", err)
        await EndpointTerminationHandler(runtime.hass).terminate_reason(
            invite.call_id,
            TerminalReason.TRANSPORT_UNREACHABLE.value,
            TerminationInitiator.MEDIA,
        )
        return SipInviteResult(503, "Service Unavailable", to_tag="")
    assist_rtp_port = assist_ports.ports[0]
    try:
        await runtime.start_local_assist_bridge(
            invite,
            reservation=assist_ports,
            local_rtp_port=assist_rtp_port,
            roster_entries=roster_entries,
            source=source,
            called_extension=called_extension,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Assist bridge failed call_id=%s", invite.call_id)
        assist_ports.release()
        await EndpointTerminationHandler(runtime.hass).terminate_reason(
            invite.call_id,
            TerminalReason.PROTOCOL_ERROR.value,
            TerminationInitiator.INTERNAL,
        )
        return SipInviteResult(
            500,
            "Server Internal Error",
            to_tag="",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
    answer = build_answer_directional(
        runtime.local_ip,
        runtime.local_ip,
        assist_rtp_port,
        invite.send_format,
        invite.recv_format,
        remote_sdp=invite.remote_sdp,
    )
    return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")


async def route_local_group(
    *,
    runtime: LocalRouteRuntime,
    invite: SipInvite,
    decision: RouteDecision,
    peers: list[Any],
    roster_entries: list[Any],
    source_endpoint: PhoneEndpoint | None,
    source_endpoint_id: str,
    registry: SipEndpointRuntime,
) -> SipInviteResult:
    """Dispatch one inbound call to its ring group or conference owner."""

    if busy := await _claim_source(
        hass=runtime.hass,
        registry=registry,
        invite=invite,
        source_endpoint=source_endpoint,
        state=CallState.RINGING.value,
        route_kind=RouteAction.GROUP.value,
    ):
        return busy
    group_type = (
        str((decision.entry.metadata or {}).get("group_type") or "")
        if decision.entry is not None
        else ""
    )
    if group_type == GROUP_TYPE_CONFERENCE and decision.entry is not None:
        ring_members = [
            str(member).strip()
            for member in ((decision.entry.metadata or {}).get("ring_members") or [])
        ]
        ring_endpoint_ids = tuple(
            leg.endpoint_id
            for member in ring_members
            if (
                leg := runtime.browser_leg_for_member(
                    member,
                    peers,
                    roster_entries,
                )
            )
            is not None
            and leg.endpoint_id != source_endpoint_id
        )
        result = await conference_manager(
            runtime.hass,
            local_ip=runtime.local_ip,
            on_inbound_timeout=runtime.on_conference_inbound_timeout,
        ).join(
            invite,
            decision.entry,
            ring_endpoint_ids=ring_endpoint_ids,
        )
        if result.status == 200:
            registry.upsert(
                invite.call_id,
                state=CallState.IN_CALL.value,
                owner="bridge",
                caller=invite.caller,
                callee=invite.target,
                route_kind=GROUP_TYPE_CONFERENCE,
                source_endpoint_id=source_endpoint_id,
            )
            registry.add_leg(
                invite.call_id,
                invite.call_id,
                role="caller",
                state=CallState.IN_CALL.value,
            )
            create_runtime_task(
                runtime.hass,
                runtime.ring_conference_members(
                    room_name=str(
                        decision.entry.name or decision.entry.id or invite.target
                    ),
                    caller=invite.caller,
                    source_host=invite.source_host,
                    entry=decision.entry,
                    peers=peers,
                    roster_entries=roster_entries,
                    owner_call_id=invite.call_id,
                ),
            )
        else:
            terminal_reason = (
                result.decline_reason or TerminalReason.TRANSPORT_UNREACHABLE.value
            )
            await EndpointTerminationHandler(runtime.hass).terminate_reason(
                invite.call_id,
                terminal_reason,
                TerminationInitiator.ROUTING,
            )
        return result
    if group_type == GROUP_TYPE_RING and decision.entry is not None:
        registry.upsert(
            invite.call_id,
            state=CallState.RINGING.value,
            owner="router",
            caller=invite.caller,
            callee=invite.target,
            route_kind=GROUP_TYPE_RING,
            source_endpoint_id=source_endpoint_id,
        )
        registry.add_leg(
            invite.call_id,
            invite.call_id,
            role="caller",
            state=CallState.RINGING.value,
        )
        create_runtime_task(
            runtime.hass,
            runtime.run_ring_group_call(
                invite,
                decision.entry,
                peers,
                roster_entries,
            ),
        )
        return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
    await EndpointTerminationHandler(runtime.hass).terminate_reason(
        invite.call_id,
        TerminalReason.TRANSPORT_UNREACHABLE.value,
        TerminationInitiator.ROUTING,
    )
    return SipInviteResult(480, "Temporarily Unavailable", to_tag="")
