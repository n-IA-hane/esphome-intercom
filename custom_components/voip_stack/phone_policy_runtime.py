"""Session-owned no-answer defaults applied to committed call state."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

from .automation_routing import automation_event_type, matches_call
from .call_deadlines import CallDeadline
from .call_state import CallPrecondition
from .call_scope import has_ringing_destination
from .endpoint_session import TerminationIntent
from .inbound_routing.automation import SIP_ROUTE_DECISION_TIMEOUT
from .phone_policy import DestinationKind, PhonePolicy
from .ringing_policy import resolve_ringing_timeout
from .runtime_data import registration_data, runtime_data
from .const import CALL_EVENT


_LOGGER = logging.getLogger(__name__)
_TIMER = "deadline:phone_policy"
_DECISION = "deadline:phone_policy_decision"


def apply_phone_policy_state(hass, payload: dict) -> None:
    """Arm one deadline from the committed call state, before HA delivery."""
    runtime = runtime_data(hass)
    registry = runtime.sip if runtime is not None else None
    if registry is None:
        return
    session = registry.get_session(str(payload.get("call_id") or ""))
    if session is None or not session.live or session.generation != payload.get("generation"):
        return
    if payload.get("scope") == "phone_policy" or session.metadata.get("application_ringing_owned"):
        return
    if not has_ringing_destination(session) or automation_event_type(payload) == "forwarding":
        for name in (_TIMER, _DECISION):
            deadline = registry.resource_for(session.call_id, name)
            if deadline is not None:
                deadline.cancel()
        session.metadata.pop("phone_policy_ringing_target", None)
        return
    if session.route_kind in {"group", "ring", "ring_group", "conference"}:
        return
    if (session.route_kind == "transfer"
            and (forward := registry.task_for(session.call_id, "forward")) is not None
            and not forward.done()):
        # The forward task owns the target's configured answer deadline.
        return
    endpoint_id = str(session.metadata.get("dest_endpoint_id")
                      or session.metadata.get("target_endpoint_id") or "")
    endpoint = runtime.endpoints.get(endpoint_id) if endpoint_id else runtime.endpoints.resolve(session.callee)
    if endpoint is None and session.owner not in {"router", "ha_softphone", "automation"}:
        return
    target_id = endpoint.endpoint_id if endpoint is not None else ""
    target = (target_id, session.callee)
    if session.metadata.get("phone_policy_ringing_target") == target:
        return
    for name in (_TIMER, _DECISION):
        deadline = registry.resource_for(session.call_id, name)
        if deadline is not None:
            deadline.cancel()
    # Snapshot even an unset rule, so a later settings edit cannot affect a
    # call that was already ringing before that edit.
    session.metadata["phone_policy_ringing_target"] = target
    policy = endpoint.policy if endpoint is not None else PhonePolicy()
    timeout = resolve_ringing_timeout(runtime.transport_config, account=endpoint,
        override=session.metadata.get("route_ringing_timeout"))
    if timeout is None:
        return
    token, owner, callee = session.token, session.owner, session.callee

    def current():
        candidate = registry.get_session(token.call_id, generation=token.generation)
        if (candidate is not session or not session.live or session.owner != owner
                or not has_ringing_destination(session, target_id) or session.callee != callee):
            return False
        destination_id = registry.bridge_link_for(token.call_id)
        client = registry.sip_client_for(destination_id or token.call_id)
        if client is not None and client.dialog is not None:
            # A final SIP answer wins even while the bridge prepares media.
            # Provisional SDP lives in early_dialogs, not this confirmed dialog.
            return False
        return True

    async def execute(sequence, precondition):
        context = registry.event_context(token.call_id)
        if not current() or not precondition.matches(session) or context is None or context.sequence != sequence:
            return
        destination = policy.no_answer
        if destination is None or destination.kind is DestinationKind.HANGUP:
            registry.request_termination(
                token.call_id, TerminationIntent("no_answer", public_state="idle", response_status=480),
                generation=token.generation,
            )
            return
        target_endpoint = runtime.endpoints.get(destination.target)
        target_name = target_endpoint.username if target_endpoint is not None else destination.target
        if destination.kind is DestinationKind.VOICEMAIL:
            target_name = f"voicemail.{destination.target}"
        try:
            if registry.forward_call is None:
                raise RuntimeError("SIP forwarding is unavailable")
            await registry.forward_call(
                call_id=token.call_id, destination=target_name, on_failure="terminate",
                precondition=precondition,
            )
        except Exception:
            _LOGGER.exception("Phone no-answer route failed call_id=%s", token.call_id)
            context = registry.event_context(token.call_id)
            if current() and context is not None and context.sequence == sequence:
                registry.request_termination(token.call_id, TerminationIntent("route_failed"), generation=token.generation)

    def commit(sequence, precondition):
        if current():
            task = session.create_task(execute(sequence, precondition), name="phone-policy-fallback")
            # Session.create_task is the existing task/cleanup owner.
            return task
        return None

    def expired():
        if not current():
            return
        context = registry.event_context(token.call_id)
        if context is None:
            return
        sequence = context.sequence
        precondition = CallPrecondition.capture(session)
        event = {**payload, "scope": "phone_policy", "event_type": "ringing_timeout_requested",
                 "reason": "no_answer", "sequence": sequence, "timeout": timeout}
        interested = any(
            matches_call(event, options, hass=hass)
            and (options.get("for") is None or options["for"].total_seconds() <= timeout)
            for options in registration_data(hass).unanswered_trigger_filters.values()
        )
        hass.bus.async_fire(CALL_EVENT, event, context=registry.ha_context(token.call_id))
        if interested:
            decision = CallDeadline(registry, session, f"{_DECISION}:{session.call_id}", lambda: commit(sequence, precondition))
            decision.arm(hass.loop, SIP_ROUTE_DECISION_TIMEOUT)
        else:
            commit(sequence, precondition)

    deadline = CallDeadline(registry, session, f"{_TIMER}:{session.call_id}", expired)
    deadline.arm(hass.loop, timeout)


def phone_failure_destination(endpoint, status):
    """Select a phone default from a final SIP result, not from media errors."""
    if endpoint is None:
        return None
    if status == 486:
        return endpoint.policy.busy
    if status in {408, 480, 502, 503, 504}:
        return endpoint.policy.unavailable
    return None


def dispatch_phone_failure(hass, session, endpoint, destination, *, on_failure="terminate",
                           failed_forward: asyncio.Task | None = None,
                           restore_source: Callable[[], Awaitable[bool]] | None = None):
    """Continue the retained caller after one failed destination leg.

    The SIP watcher returns before the shared forwarder retires that watcher.
    The new work remains a task of the same session and generation.
    """
    if (destination is None or destination.kind is DestinationKind.HANGUP
            or session is None or not session.live
            or session.route_kind in {'group', 'ring', 'ring_group', 'conference'}):
        return False
    if session.state == 'in_call' and failed_forward is None:
        return False
    registry = runtime_data(hass).sip
    if session.state == 'in_call' and not (
        session.owner == 'router' and session.route_kind == 'transfer'
        and registry.task_for(session.call_id, 'forward') is failed_forward
        and failed_forward is asyncio.current_task() and not failed_forward.done()
        and endpoint is not None
        and session.metadata.get('dest_endpoint_id') == endpoint.endpoint_id
    ):
        return False
    key = (endpoint.endpoint_id, destination.kind.value, destination.target)
    attempted = tuple(session.metadata.get('phone_failure_routes', ()))
    if key in attempted or len(attempted) >= 8:
        return False
    session.metadata['phone_failure_routes'] = (*attempted, key)
    token, owner, state = session.token, session.owner, session.state
    async def forward():
        if not registry.is_generation_current(token.call_id, token.generation) or session.owner != owner or session.state != state:
            return
        current_owner, current_state = owner, state
        target_endpoint = runtime_data(hass).endpoints.get(destination.target)
        target = target_endpoint.username if target_endpoint is not None else destination.target
        if destination.kind is DestinationKind.VOICEMAIL:
            target = f'voicemail.{destination.target}'
        try:
            if restore_source is not None:
                if not await restore_source():
                    return
                if not session.live or not registry.is_generation_current(token.call_id, token.generation):
                    return
                current_owner, current_state = session.owner, session.state
            context = registry.event_context(token.call_id)
            task = await registry.forward_call(call_id=token.call_id, destination=target,
                                        expected_state=context.state if context else '',
                                        expected_sequence=context.sequence if context else 0,
                                        on_failure=on_failure)
            if task is not None:
                from .forward_plan import await_forward_result
                return await await_forward_result(task)
        except Exception:
            _LOGGER.exception('Phone failure route failed call_id=%s', token.call_id)
            if session.live and session.owner == current_owner and session.state == current_state:
                registry.request_termination(token.call_id, TerminationIntent('route_failed'), generation=token.generation)
    return session.create_task(forward(), name=f'phone-failure-route:{len(attempted)}')
