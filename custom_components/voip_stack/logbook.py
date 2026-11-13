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


def _sip_code(data):
    """Read a result only from this call occurrence, never global diagnostics."""
    value = data.get("sip_status_code")
    if isinstance(value, bool):
        return ""
    try:
        code = int(value)
    except (ValueError, TypeError):
        return ""
    return f" · SIP {code}" if 100 <= code <= 699 else ""


def _logical_calls(phone):
    result = {}
    for row in (phone or {}).get("calls", ()):
        call_id = str(row.get("call_id") or "")
        if not call_id or row.get("state") == "terminated":
            continue
        key = (call_id, row.get("generation", 0))
        phase = "answered" if row.get("state") == "confirmed" else "calling" if row.get("direction") == "initiator" else "ringing"
        current = result.get(key)
        if current is None or phase == "answered":
            result[key] = (phase, row)
    return result


def _account_activity(data):
    """Derive one logical phase per account without remembering Contact events."""
    before, after = _logical_calls(data.get("previous")), _logical_calls(data.get("phone"))
    account = data.get("phone") or data.get("previous") or {}
    name = str(account.get("name") or "SIP account")
    occurrence = data.get("call") or {}
    occurrence_key = (str(occurrence.get("call_id") or ""), occurrence.get("generation", 0))
    entries = []
    for key, (phase, row) in after.items():
        if key in before and before[key][0] == phase:
            continue
        details = occurrence if key == occurrence_key else {}
        if phase == "answered":
            winner = _party(details, "answered_by", "connected_party")
            message = f"Answered by {winner}" if winner else "Call answered"
        else:
            target = (_party(details, "dialed_target", "called_number", "callee") if phase == "calling"
                      else _party(details, "caller")) or _party(row, "peer_user", "peer_name")
            message = ("Calling " if phase == "calling" else "Incoming call from ") + (target or "unknown party")
        entries.append({LOGBOOK_ENTRY_NAME: name, LOGBOOK_ENTRY_MESSAGE: message, "phase": phase})
    terminal = (str(occurrence.get("type") or "") in {"ended", "failed", "missed"}
                or str(occurrence.get("pbx_phase") or "") in {"terminating", "terminated"})
    if terminal and occurrence_key in before and occurrence_key not in after:
        entries.append({LOGBOOK_ENTRY_NAME: name, LOGBOOK_ENTRY_MESSAGE: _account_terminal_message(occurrence), "phase": "terminal"})
    return entries


def _account_terminal_message(occurrence):
    failed = occurrence.get("type") in {"failed", "missed"} or occurrence.get("state") in {"failed", "error", "declined"}
    message = "Call failed" if failed else "Call ended"
    reason = str(occurrence.get("terminal_reason") or occurrence.get("reason") or "").strip()
    if reason:
        message += " · " + reason.replace("_", " ")
    message += _sip_code(occurrence)
    winner = _party(occurrence, "answered_by", "connected_party")
    return message + (" · Answered by " + winner if winner else "")


@callback
def async_setup_account_activity(hass, entry):
    """Use the existing Recorder/Logbook stream with this entry's callback owner."""
    from homeassistant.components.logbook import async_log_entry
    from homeassistant.helpers import entity_registry as er
    from .phone_state import PHONE_STATE_CHANGED
    from .runtime_data import runtime_data

    owner = entry.runtime_data
    def publish(endpoint, message, context):
        entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN,
            f"phone_endpoint_{endpoint.endpoint_id}_call_state")
        async_log_entry(hass, endpoint.name, message, domain=DOMAIN, entity_id=entity_id, context=context)

    @callback
    def changed(event):
        if runtime_data(hass) is not owner:
            return
        endpoint_id = event.data.get("endpoint_id", "")
        endpoint = owner.endpoints.get(endpoint_id)
        if endpoint is None or not endpoint.supports_ha_client:
            return
        for item in _account_activity(event.data):
            # The existing one-per-call terminal occurrence is authoritative,
            # including failures before any live phone dialog became visible.
            if item["phase"] != "terminal":
                publish(endpoint, item[LOGBOOK_ENTRY_MESSAGE], event.context)

    @callback
    def ended(event):
        if runtime_data(hass) is not owner or not event.data.get("call_id"):
            return
        participants = set(event.data.get("participant_endpoint_ids") or ())
        participants.update(str(event.data.get(key) or "") for key in (
            "endpoint_id", "source_endpoint_id", "dest_endpoint_id", "target_endpoint_id"))
        for identity in participants:
            endpoint = owner.endpoints.get(identity)
            if endpoint is not None and endpoint.supports_ha_client:
                publish(endpoint, _account_terminal_message(event.data), event.context)
    entry.async_on_unload(hass.bus.async_listen(PHONE_STATE_CHANGED, changed))
    entry.async_on_unload(hass.bus.async_listen(SIP_CALL_ENDED_EVENT, ended))


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
    message += _sip_code(data)
    winner = _party(data, "answered_by")
    if winner and winner != destination:
        message += " · Answered by " + winner

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
