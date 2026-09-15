"""Explicit per-call automation deadlines.

Deadlines only emit an event. They never alter SIP routing by themselves, so
installations without automation rules keep the normal phonebook dial plan.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from collections.abc import Callable
from .endpoint_session import CleanupStage

from .automation_routing import deadline_is_current
from .call_registry import TERMINAL_STATES
from .endpoint_lifecycle import call_registry
from .service_errors import service_error as _service_error
from .websocket_api import _fire_call_event


class CallDeadline:
    """Use the session cleanup owner for timers and their cancellation hooks."""

    def __init__(self, registry, session, name: str, callback: Callable[[], None]):
        self.registry = registry
        self.token = session.token
        self.name = name
        self.callback = callback
        self.handle = None
        self.on_close: Callable[[], None] | None = None
        self.closed = False

    def arm(self, loop, delay: float) -> None:
        self.registry.own_resource(
            self.token.call_id,
            self.name,
            self,
            self.close,
            stage=CleanupStage.OBSERVER,
            generation=self.token.generation,
        )
        self.handle = loop.call_later(delay, self._expired)

    def _expired(self) -> None:
        current = self.registry.is_generation_current(
            self.token.call_id, self.token.generation
        )
        if self.closed:
            return
        self.cancel()
        if current:
            self.callback()

    def cancel(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None
        session = self.registry.get_session(self.token.call_id)
        # During teardown the session already owns and closes this resource.
        if session is not None and session.owns(self.token):
            self.registry.release_resource(
                self.token.call_id,
                self.name,
                value=self,
                generation=self.token.generation,
            )
        if self.on_close is not None:
            self.on_close()

    async def close(self, _reason: str) -> None:
        self.cancel()


def cancel_call_deadline(hass: HomeAssistant, call_id: str) -> None:
    """Cancel an armed deadline, if present."""
    deadline = call_registry(hass).resource_for(str(call_id or "").strip(), "deadline")
    if deadline is not None:
        deadline.cancel()


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
        or registry.artifact_for(call_id, "pending_invite") is not None
        or registry.resource_for(call_id, "preanswered") is not None
        or registry.bridge_link_for(call_id)
        or registry.resource_for(call_id, "softphone_media") is not None
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

    def expired() -> None:
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

    if session is None or not session.live:
        raise _service_error(
            f"call_id {call_id} is no longer active",
            "call_inactive",
            call_id=call_id,
        )
    deadline = CallDeadline(registry, session, f"deadline:{call_id}", expired)
    deadline.arm(hass.loop, timeout)
