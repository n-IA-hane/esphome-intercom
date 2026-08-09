"""Explicit per-call automation deadlines.

Deadlines only emit an event. They never alter SIP routing by themselves, so
installations without automation rules keep the normal phonebook dial plan.
"""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant

from .automation_routing import deadline_is_current
from .call_registry import TERMINAL_STATES
from .endpoint_lifecycle import call_registry, create_runtime_task
from .runtime_data import call_runtime_artifacts
from .service_errors import service_error as _service_error
from .websocket_api import _fire_call_event


def cancel_call_deadline(hass: HomeAssistant, call_id: str) -> None:
    """Cancel an armed deadline, if present."""
    call_runtime_artifacts(hass).cancel_task(
        str(call_id or "").strip(), "deadline"
    )


async def async_set_call_deadline(hass: HomeAssistant, data: dict) -> None:
    """Arm a state-guarded calling/ringing timeout event."""
    call_id = str(data.get("call_id") or "").strip()
    phase = str(data.get("phase") or "").strip().lower()
    timeout = float(data.get("timeout") or 0)
    expected_state = str(data.get("expected_state") or "").strip().lower()
    expected_sequence = int(data.get("expected_sequence") or 0)
    registry = call_registry(hass)
    context = registry.event_context(call_id)
    if context is None:
        raise _service_error(
            f"unknown or ended call_id {call_id}",
            "call_unknown_or_ended",
            call_id=call_id,
        )
    allowed_states = {
        "calling": {"calling", "connecting"},
        "ringing": {"ringing", "remote_ringing"},
    }[phase]
    if context.state not in allowed_states:
        raise _service_error(
            f"call_id {call_id} is {context.state}, not in the {phase} phase",
            "call_phase_mismatch",
            call_id=call_id,
            state=context.state,
            phase=phase,
        )
    session = registry.sessions.get(registry.resolve_session_id(call_id))
    owned = bool(
        (session is not None and session.state not in TERMINAL_STATES)
        or call_id in registry.pending_invites
        or call_id in registry.preanswered
        or call_id in registry.bridge_clients
        or call_id in registry.softphone_media
    )
    if not owned:
        raise _service_error(
            f"call_id {call_id} is no longer active",
            "call_inactive",
            call_id=call_id,
        )
    if expected_state and context.state != expected_state:
        raise _service_error(
            f"call_id {call_id} is {context.state}, expected {expected_state}",
            "call_state_mismatch",
            call_id=call_id,
            actual=context.state,
            expected=expected_state,
        )
    if expected_sequence and context.sequence != expected_sequence:
        raise _service_error(
            f"call_id {call_id} sequence is {context.sequence}, expected {expected_sequence}",
            "call_sequence_mismatch",
            call_id=call_id,
            actual=context.sequence,
            expected=expected_sequence,
        )

    cancel_call_deadline(hass, call_id)
    armed_state = context.state
    armed_sequence = context.sequence

    async def _wait() -> None:
        await asyncio.sleep(timeout)
        current = registry.event_context(call_id)
        if current is None:
            return
        if not deadline_is_current(
            current.state,
            current.sequence,
            armed_state=armed_state,
            armed_sequence=armed_sequence,
        ):
            return
        _fire_call_event(
            hass,
            {
                "event_type": f"{phase}_timeout_requested",
                "state": current.state,
                "scope": "automation_deadline",
                "call_id": call_id,
                "phase": phase,
                "timeout": timeout,
                "armed_state": armed_state,
                "armed_sequence": armed_sequence,
            },
            "sip",
        )

    task = create_runtime_task(hass, _wait())
    if not call_runtime_artifacts(hass).replace_task(
        call_id, task, name="deadline"
    ):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise _service_error(
            f"call_id {call_id} is no longer active",
            "call_inactive",
            call_id=call_id,
        )
