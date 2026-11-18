"""An automation execution refers to its triggering call, never the latest call."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
from typing import Any
from uuid import uuid4

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .endpoint_lifecycle import call_registry
from .endpoint_session import CallToken
from .pbx_trace import trace_call


_LOGGER = logging.getLogger(__name__)


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


def call_action_actor(hass, call_id: str, *, logger: logging.Logger) -> str:
    """Name only the current automation of this still-current triggering call."""
    fallback = "Home Assistant action"
    if not logger.isEnabledFor(logging.DEBUG):
        return fallback
    execution = current_execution.get()
    if execution is None or not execution.active or execution.token.call_id != call_id:
        return fallback
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.trace import trace_id_get

    trace = trace_id_get()
    if trace is None:
        return fallback
    domain, separator, unique_id = trace[0].partition(".")
    if domain != "automation" or not separator or unique_id in {"", "None"}:
        return fallback
    if not call_registry(hass).is_generation_current(call_id, execution.token.generation):
        return fallback
    entity_id = er.async_get(hass).async_get_entity_id("automation", "automation", unique_id)
    state = hass.states.get(entity_id) if entity_id is not None else None
    return f'Automation "{state.name}"' if state is not None else fallback


CALL_ACTIONS = frozenset(
    {
        "answer",
        "decline",
        "hangup",
        "forward",
        "transfer",
        "park",
        "pickup",
        "start_recording",
        "stop_recording",
        "tts_say",
        "play_media",
        "route",
        "select_inbound_destination",
        "set_deadline",
        "cancel_deadline",
        "wait_for_dtmf",
        "wait_unanswered",
        "send_to_voicemail",
    }
)


def _bind_outgoing_action(call: ServiceCall) -> ServiceCall:
    """Resolve from existing session owners, never a global preferred phone."""
    if call.service not in {"tts_say", "play_media", "hangup", "forward", "wait_for_dtmf", "send_to_voicemail"}:
        return call
    if call.data.get("call_id") or call.data.get("device_id"):
        return call
    registry = call_registry(call.hass)
    matches = [
        application
        for _, application in registry.resource_items("automation")
        if application.outgoing and application.controller == call.context.id
        and application.session.live
    ]
    if not matches:
        raise ServiceValidationError(
            "This execution has no outgoing Automation call. Call first, or select a phone or call explicitly"
        )
    if len(matches) != 1:
        raise ServiceValidationError(
            "This execution has multiple calls. Select call_id and expected_generation explicitly"
        )
    application = matches[0]
    generation = application.session.generation
    expected = call.data.get("expected_generation")
    application.require_current(generation if expected is None else expected)
    return ServiceCall(
        call.hass, call.domain, call.service,
        {**call.data, "call_id": application.session.call_id, "expected_generation": generation},
        context=call.context, return_response=call.return_response,
    )


def bind_call_action(call: ServiceCall) -> ServiceCall:
    """Resolve implicit actions before dispatch; retain explicit legacy requests."""
    execution = current_execution.get()
    if call.service not in CALL_ACTIONS:
        return call
    if execution is None:
        return _bind_outgoing_action(call)
    if execution.snapshot.get("automation_control") == "observed" and call.service in {
        "forward",
        "transfer",
        "park",
        "start_recording",
        "stop_recording",
        "tts_say",
        "play_media",
        "wait_for_dtmf",
        "wait_unanswered",
        "send_to_voicemail",
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
    if call.service == "pickup":
        if not data.get("target_device_id") and not data.get("slot"):
            data["target_device_id"] = str(execution.snapshot.get("device_id") or execution.snapshot.get("endpoint_id") or "")
        if execution.snapshot.get("leg_id"):
            data.setdefault("leg_id", execution.snapshot["leg_id"])
    if execution.ringing_guard is not None and call.service == "forward":
        data.setdefault("expected_sequence", execution.ringing_guard[0])
    if call.service in {
        "answer",
        "decline",
        "hangup",
        "forward",
        "transfer",
        "park",
        "start_recording",
        "stop_recording",
    } and not data.get("device_id"):
        device_id = execution.snapshot.get("device_id")
        if device_id:
            data["device_id"] = device_id
    if call.service in {"tts_say", "play_media", "forward", "hangup", "wait_for_dtmf", "wait_unanswered", "send_to_voicemail", "park", "pickup", "start_recording", "stop_recording"}:
        expected = data.get("expected_generation")
        if expected is not None and expected != execution.token.generation:
            raise ServiceValidationError(
                "The selected call generation is no longer current"
            )
        data["expected_generation"] = execution.token.generation
    if (call.service == "forward" and _LOGGER.isEnabledFor(logging.DEBUG)
            and registry.artifact_for(execution.token.call_id, "pending_route") is None):
        trace_call(
            _LOGGER, execution.token.call_id,
            "%s requests forwarding to %s; context=%s",
            call_action_actor(call.hass, execution.token.call_id, logger=_LOGGER),
            data.get("destination") or "not specified", call.context.id,
        )
    return ServiceCall(
        call.hass,
        call.domain,
        call.service,
        data,
        context=call.context,
        return_response=call.return_response,
    )
