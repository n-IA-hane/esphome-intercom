"""An automation execution refers to its triggering call, never the latest call."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .endpoint_lifecycle import call_registry
from .endpoint_session import CallToken


@dataclass(slots=True)
class CallExecution:
    token: CallToken
    snapshot: dict[str, Any]
    controller: str = field(default_factory=lambda: uuid4().hex)
    active: bool = True
    dtmf_result: dict[str, Any] | None = None
    ringing_guard: tuple[int, str] | None = None


current_execution: ContextVar[CallExecution | None] = ContextVar(
    "voip_call_execution", default=None
)

CALL_ACTIONS = frozenset(
    {
        "answer",
        "decline",
        "hangup",
        "forward",
        "transfer",
        "tts_say",
        "route",
        "select_inbound_destination",
        "set_deadline",
        "cancel_deadline",
        "wait_for_dtmf",
    }
)


def bind_call_action(call: ServiceCall) -> ServiceCall:
    """Resolve implicit actions before dispatch; retain explicit legacy requests."""
    execution = current_execution.get()
    if execution is None or call.service not in CALL_ACTIONS:
        return call
    if execution.snapshot.get("automation_control") == "observed" and call.service in {
        "forward",
        "transfer",
        "tts_say",
        "wait_for_dtmf",
        "route",
        "select_inbound_destination",
    }:
        raise ServiceValidationError(
            "This direct call does not pass through Home Assistant"
        )
    requested = str(call.data.get("call_id") or "")
    if requested and requested != execution.token.call_id:
        raise ServiceValidationError(
            "This action selects a different call than its trigger"
        )
    registry = call_registry(call.hass)
    if not execution.active or not registry.is_generation_current(
        execution.token.call_id, execution.token.generation
    ):
        raise ServiceValidationError(
            "The call that triggered this automation has ended"
        )
    if execution.ringing_guard is not None:
        sequence, destination = execution.ringing_guard
        session = registry.get_session(execution.token.call_id)
        context = registry.event_context(execution.token.call_id)
        if (
            session is None
            or session.state not in {"ringing", "remote_ringing"}
            or session.callee != destination
            or context is None
            or context.sequence != sequence
        ):
            raise ServiceValidationError(
                "The unanswered call has changed or has already been answered"
            )
    data = dict(call.data)
    data["call_id"] = execution.token.call_id
    if execution.ringing_guard is not None and call.service == "forward":
        data.setdefault("expected_sequence", execution.ringing_guard[0])
    if call.service in {
        "answer",
        "decline",
        "hangup",
        "forward",
        "transfer",
    } and not data.get("device_id"):
        device_id = execution.snapshot.get("device_id")
        if device_id:
            data["device_id"] = device_id
    if call.service in {"tts_say", "forward", "wait_for_dtmf"}:
        expected = data.get("expected_generation")
        if expected is not None and expected != execution.token.generation:
            raise ServiceValidationError(
                "The selected call generation is no longer current"
            )
        data["expected_generation"] = execution.token.generation
    return ServiceCall(
        call.hass,
        call.domain,
        call.service,
        data,
        context=call.context,
        return_response=call.return_response,
    )
