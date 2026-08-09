from __future__ import annotations

from collections.abc import Callable, Sequence
import contextlib
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

from .call_scope import take_pending_route
from .endpoint_session import TerminationInitiator, TerminationIntent
from .endpoint_termination import EndpointTerminationHandler
from .fsm import sip_public_state
from .media_ports import release_media_reservation
from .outbound_attempts import OutboundLeg, async_cleanup_outbound_attempts
from .runtime_data import call_runtime_artifacts

_ROUTE_OWNERS = {"router", "bridge", "assist"}


@dataclass(frozen=True, slots=True)
class RouteAbortIntent:
    reason: str
    action: str = "terminate"
    sip_status: int = 0


@dataclass(slots=True)
class RouteAbortContext:
    hass: HomeAssistant
    registry: Any
    call_id: str
    settle: Callable[..., None] | None = None
    attempts: Sequence[OutboundLeg] = ()
    consume_source: bool = False
    resume_callee: str = ""
    resume_route_kind: str = ""
    transition_resume: bool = False
    publish_resume: Callable[[], None] | None = None


async def async_abort_route(
    context: RouteAbortContext,
    intent: RouteAbortIntent,
) -> bool:
    hass, registry, call_id = context.hass, context.registry, context.call_id
    bridge_client = registry.sip_client_for(registry.bridge_for(call_id)[1])
    attempts = [
        attempt for attempt in context.attempts if attempt.client is not bridge_client
    ]
    try:
        take_pending_route(hass, call_id)
        if context.settle is not None:
            context.settle(sip_public_state(intent.reason), intent.reason)

        from .local_softphone_runtime import local_softphone_bridge

        bridge = local_softphone_bridge(hass)
        if bridge is not None and (local_call := bridge.get_call(call_id)) is not None:
            with contextlib.suppress(Exception):
                bridge.hangup(call_id, local_call.caller_endpoint_id)

        action = intent.action
        artifacts = call_runtime_artifacts(hass).artifacts_for(call_id)
        if action == "resume" and artifacts is not None and artifacts.trunk_closed:
            action = "terminate"
        if action == "resume":
            current = registry.sessions.get(registry.resolve_session_id(call_id))
            if context.transition_resume:
                if current is None or current.owner not in _ROUTE_OWNERS:
                    return False
                current = registry.transition(
                    call_id,
                    state="ringing",
                    owner="ha_softphone",
                    callee=context.resume_callee,
                    route_kind=context.resume_route_kind,
                    expected_revision=current.revision,
                    expected_owner=current.owner,
                )
                if current is None:
                    return False
            if context.publish_resume is not None:
                context.publish_resume()
            return True
        if action == "cleanup":
            return False

        status = intent.sip_status or (486 if action == "busy" else 480)
        if context.consume_source:
            registry.take_pending_invite(call_id)
            media = registry.take_media(call_id, provisional=True)
            release_media_reservation(media)
            terminal = (
                TerminationIntent.bye(
                    intent.reason,
                    TerminationInitiator.ROUTING,
                    response_status=status,
                )
                if media is not None and bool(media.get("final_response_sent", True))
                else TerminationIntent.final_response(intent.reason, status)
            )
        else:
            terminal = (
                TerminationIntent.final_response(intent.reason, intent.sip_status)
                if intent.sip_status
                else TerminationIntent(intent.reason)
            )
        await EndpointTerminationHandler(hass).terminate(call_id, terminal)
        return False
    finally:
        await async_cleanup_outbound_attempts([], attempts)
