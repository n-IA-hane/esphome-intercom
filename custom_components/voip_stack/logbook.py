"""Human-readable Logbook entries for completed VoIP calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, SIP_CALL_ENDED_EVENT, QUEUE_EVENT

from .entity_identity import shared_entity_ids


def _party(data: Mapping[str, Any], *keys: str) -> str:
    """Return the first useful human-facing party label."""

    for key in keys:
        value = str(data.get(key) or "").strip()
        if not value:
            continue
        lower = value.lower()
        if lower.startswith(("sip:", "sips:")):
            value = value.split(":", 1)[1].split(";", 1)[0]
            # A Logbook party is a human-facing identity, not a routable URI.
            # Keeping the user part is both clearer and less topology-revealing.
            value = value.split("@", 1)[0]
        return value
    return ""


def _format_duration(value: Any) -> str:
    """Format a recorded call duration without locale-dependent ambiguity."""

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(numeric) or numeric < 0:
        return ""
    seconds = round(numeric)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min {seconds:02d} s"
    if minutes:
        return f"{minutes} min {seconds:02d} s"
    return f"{seconds} s"


def _route_destination(data: Mapping[str, Any]) -> str:
    """Return the last explicit PBX destination selected for the call."""

    history = data.get("route_history")
    if not isinstance(history, list):
        return ""
    for item in reversed(history):
        if not isinstance(item, Mapping):
            continue
        destination = str(item.get("destination") or "").strip()
        if destination:
            return destination
    return ""


def _describe_call(data: Mapping[str, Any], entity_id: str | None = None) -> dict[str, str]:
    """Build one durable summary from the terminal event payload."""

    caller = _party(data, "caller", "peer_name") or "Unknown caller"
    direction = str(data.get("direction") or "").strip().lower()
    destination_keys = (
        ("answered_by", "connected_party", "local_name", "callee", "dialed_target")
        if direction == "incoming"
        else ("answered_by", "connected_party", "callee", "dialed_target", "local_name")
    )
    route_destination = (
        _route_destination(data)
        if direction == "incoming"
        else ""
    )
    destination = route_destination or _party(data, *destination_keys)
    destination = destination or "unknown destination"
    event_type = str(data.get("type") or "ended").strip().lower()
    duration = _format_duration(data.get("duration_seconds"))
    state = str(data.get("state") or "").strip().lower()
    terminal_reason = str(
        data.get("terminal_reason") or data.get("reason") or ""
    ).strip().lower()
    declined = (
        event_type in {"decline", "declined"}
        or state == "declined"
        or terminal_reason in {"declined", "dnd"}
    )
    unanswered_incoming = (
        event_type == "ended"
        and direction == "incoming"
        and not duration
        and not declined
    )

    if event_type == "failed":
        reason = str(
            data.get("terminal_reason") or data.get("reason") or "call failed"
        ).strip()
        message = f"Call from {caller} to {destination} failed"
        if reason:
            message += f" · {reason.replace('_', ' ')}"
    elif declined:
        message = f"Declined call from {caller} to {destination}"
    elif event_type == "missed" or unanswered_incoming:
        message = f"Missed call from {caller} to {destination}"
    else:
        message = f"{caller} called {destination}"
        if duration:
            message += f" · {duration}"

    return {
        LOGBOOK_ENTRY_NAME: "VoIP Stack",
        LOGBOOK_ENTRY_MESSAGE: message,
        **({LOGBOOK_ENTRY_ENTITY_ID: entity_id} if entity_id else {}),
        LOGBOOK_ENTRY_ICON: "mdi:phone-log",
    }


@callback
def describe_queue_event(event):
    """Describe the raw queue occurrence without querying Recorder."""
    data = event.data
    name = data.get("queue_name") or data.get("queue_id") or "Queue"
    action = str(data.get("event_type") or "changed").replace("_", " ")
    reason = str(data.get("outcome") or data.get("reason") or "").replace("_", " ")
    return {"name": str(name), "message": action + (f": {reason}" if reason else ""), "icon": "mdi:account-clock"}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[Event[dict[str, Any]]], dict[str, str]]], None
    ],
) -> None:
    """Describe completed calls without recording every signaling transition."""

    @callback
    def async_describe_call_event(
        event: Event[dict[str, Any]],
    ) -> dict[str, str]:
        return _describe_call(event.data, shared_entity_ids(hass)["call_event"])

    async_describe_event(DOMAIN, SIP_CALL_ENDED_EVENT, async_describe_call_event)
    async_describe_event(DOMAIN, QUEUE_EVENT, describe_queue_event)
