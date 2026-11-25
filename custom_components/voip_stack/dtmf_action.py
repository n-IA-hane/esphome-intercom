"""Native HA keypad commands dispatched to the active call's media owner."""

from __future__ import annotations

import asyncio

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .runtime_data import require_runtime_data
from .dtmf import send_dtmf_sequence


async def _send_application_digits(call: ServiceCall) -> dict:
    """Share the Automation action's authorization, generation and cleanup."""
    from .authorization import async_require_service_admin
    from .automation_call import resolve_application_action

    await async_require_service_admin(call.hass, call)
    application, call = await resolve_application_action(call)
    try:
        return await application.send_dtmf(call)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        raise ServiceValidationError("The call ended before keypad input completed") from None


async def async_send_dtmf(call: ServiceCall) -> dict:
    """Send from the existing application or one authorized phone's media."""
    runtime = require_runtime_data(call.hass)
    registry = runtime.sip
    if registry is None:
        raise ServiceValidationError("VoIP Stack is not running")
    call_id = str(call.data.get("call_id") or "").strip()
    if call_id and not call.data.get("device_id"):
        from .automation_context import current_execution

        execution = current_execution.get()
        selected = registry.get_session(registry.resolve_session_id(call_id))
        if (registry.resource_for(call_id, "automation") is not None
                or (execution is not None and selected is not None
                    and selected.state in {"ringing", "remote_ringing"})):
            return await _send_application_digits(call)
        if execution is not None and execution.snapshot.get("device_id"):
            call = ServiceCall(call.hass, call.domain, call.service,
                {**call.data, "device_id": execution.snapshot["device_id"]},
                context=call.context, return_response=call.return_response)
    phone = await runtime.phones.resolve_source(call)
    endpoint_id = phone.endpoint_id
    if call_id:
        session = registry.get_session(registry.resolve_session_id(call_id))
    else:
        matches = [item for item in registry.sessions.values()
                   if item.live and endpoint_id in item.endpoint_claims]
        if len(matches) != 1:
            raise ServiceValidationError("Select one active call using call_id")
        session = matches[0]
    if session is None or not session.live:
        raise ServiceValidationError("Sending keypad input requires an established call")
    role = session.endpoint_claims.get(endpoint_id)
    if role not in {"source", "destination"}:
        raise ServiceValidationError("The selected call does not belong to this phone")
    if registry.resource_for(session.call_id, "automation") is not None:
        # A manually selected phone may have an Automation contact on its
        # other end. Use that application's actual peer, not a fabricated leg.
        return await _send_application_digits(call)
    if session.state != "in_call":
        raise ServiceValidationError("Sending keypad input requires an established call")
    expected = call.data.get("expected_generation")
    if expected is not None and session.generation != int(expected):
        raise ServiceValidationError("The selected call has ended or changed generation")
    token = session.token
    active_audio = runtime.media.sessions_for("audio")
    browser_owners = [value for (identity, endpoint), value in active_audio.items()
                      if endpoint == endpoint_id and registry.resolve_session_id(identity) == session.call_id
                      and callable(getattr(value, "schedule_dtmf", None))]
    if len(browser_owners) > 1:
        raise ServiceValidationError("The selected phone has more than one active media owner")
    owner = browser_owners[0] if browser_owners else registry.resource_for(session.call_id, "relay")
    schedule = getattr(owner, "schedule_dtmf", None)
    from .local_call_media import LocalCallMedia

    if isinstance(owner, LocalCallMedia):
        raise ServiceValidationError("This local application is not controlled by an Automation contact")
    if not callable(schedule):
        raise ServiceValidationError("This call has no active media owner that can send keypad input")
    destination = "right" if role == "source" else "left"

    def require_current() -> None:
        current = registry.get_session(session.call_id, generation=token.generation)
        owned = (any(value is owner for value in active_audio.values()) if browser_owners
                 else registry.resource_for(session.call_id, "relay") is owner)
        if (current is not session or not session.live or session.state != "in_call"
                or session.endpoint_claims.get(endpoint_id) != role or not owned):
            raise ServiceValidationError("The call or its media owner changed while sending keypad input")

    name = f"send_dtmf:{endpoint_id}"
    previous = registry.task_for(session.call_id, name)
    if previous is not None and not previous.done():
        raise ServiceValidationError("Keypad input is already being sent from this phone")
    digits = str(call.data["digits"]).upper()
    duration_ms = int(call.data.get("duration_ms", 160))
    gap_ms = int(call.data.get("gap_ms", 100))

    async def send_sequence() -> dict:
        def enqueue(digit, *, duration_ms):
            return (schedule(digit, duration_ms=duration_ms) if browser_owners
                    else schedule(destination, digit, duration_ms=duration_ms))

        if not await send_dtmf_sequence(digits, duration_ms=duration_ms, gap_ms=gap_ms,
                schedule=enqueue, require_current=require_current):
            raise ServiceValidationError("The negotiated media owner could not send keypad input")
        return {"call_id": session.call_id, "generation": token.generation,
                "digits": digits, "sent": len(digits)}

    operation = session.create_task(send_sequence(), name=name)
    try:
        return await operation
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        raise ServiceValidationError("The call ended before keypad input completed") from None
