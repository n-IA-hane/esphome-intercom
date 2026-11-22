"""Pure automation-facing routing primitives."""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass
from functools import lru_cache
import re


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """Logical outgoing facts available before there is any SIP/media leg."""

    call_id: str
    caller: str
    target: str
    caller_number: str = ""
    caller_name: str = ""
    called_number: str = ""
    outbound_trunk_id: str = ""
    source_host: str = ""
    source_account_username: str = ""
    source_extension: str = ""

    @property
    def routing_caller(self):
        return self.caller_number or self.caller

    @property
    def routing_target(self):
        return self.target


CALL_EVENT_SCHEMA_VERSION = 2
CALL_ORIGINS = frozenset({"trunk", "extension"})


def canonical_call_origin(
    ingress: object,
    route_kind: object = "",
) -> str:
    """Return the stable transport origin used by event schema v2."""

    value = str(ingress or "").strip().lower()
    if value in CALL_ORIGINS:
        return value
    return "trunk" if str(route_kind or "").strip().lower() == "trunk" else "extension"


AUTOMATION_EVENT_TYPES = [
    "automation_requested",
    "route_requested",
    "outgoing_call",
    "calling",
    "ringing",
    "remote_ringing",
    "forwarding",
    "calling_timeout_requested",
    "ringing_timeout_requested",
    "answered",
    "connected",
    "picked_up",
    "dtmf",
    "ended",
    "missed",
    "failed",
    "state_changed",
]


def automation_event_type(payload: dict[str, Any]) -> str:
    """Map the stable call envelope to one browsable HA event type."""
    explicit = str(payload.get("event_type") or "").strip().lower()
    if explicit in AUTOMATION_EVENT_TYPES:
        return explicit
    state = str(payload.get("state") or "").strip().lower()
    legacy_type = str(payload.get("type") or "").strip().lower()
    if payload.get("route_request") or state == "route_requested":
        return "route_requested"
    if state == "calling":
        return "outgoing_call"
    if state == "connecting":
        return "state_changed" if payload.get("direction") == "incoming" else "calling"
    if state == "remote_ringing":
        return "remote_ringing"
    if state == "ringing":
        return "ringing"
    if state == "in_call":
        return "connected" if payload.get("direction") == "outgoing" else "answered"
    if state in {"calling_timeout_requested", "ringing_timeout_requested"}:
        return state
    if legacy_type in {"ended", "missed", "failed"}:
        return legacy_type
    return "state_changed"


def is_logbook_call_summary(payload: dict[str, Any]) -> bool:
    """Return whether a terminal occurrence represents the logical call.

    Ring groups publish terminal state for every losing leg. ESP physical
    state mirrors are also observations, not a second logical call. The
    Logbook must therefore consume only the winner/final PBX occurrence.
    """

    if not str(payload.get("call_id") or "").strip():
        return False
    if str(payload.get("scope") or "").strip().lower() == "esp":
        return False
    if str(payload.get("type") or "").strip().lower() not in {
        "ended",
        "missed",
        "failed",
    }:
        return False

    state = str(payload.get("state") or "").strip().lower()
    phase = str(payload.get("pbx_phase") or "").strip().lower()
    owner = str(payload.get("owner") or "").strip().lower()
    if phase and phase not in {"terminating", "terminated"}:
        return False
    if payload.get("duration_seconds") is not None or state == "idle":
        return True
    if owner not in {"router", "bridge"}:
        return True
    return str(payload.get("type") or "").strip().lower() == "failed" and phase in {
        "terminating",
        "terminated",
    }


def deadline_is_current(
    current_state: str,
    current_sequence: int,
    *,
    armed_state: str,
    armed_sequence: int,
) -> bool:
    """Return whether a deadline still belongs to the same call phase."""
    return (
        str(current_state or "") == str(armed_state or "")
        and int(current_sequence) == int(armed_sequence)
    )


def resolve_forward_call_id(
    requested_call_id: str,
    pending_routes: object,
    pending_invites: object,
) -> str:
    """Resolve the only forwardable call without exposing IDs to normal automations."""
    requested = str(requested_call_id or "").strip()
    if requested:
        return requested
    candidates = sorted(set(pending_routes) | set(pending_invites))
    if not candidates:
        raise ValueError("No forwardable SIP call is active")
    if len(candidates) > 1:
        raise ValueError(
            "More than one forwardable SIP call is active; provide call_id"
        )
    return candidates[0]


def resolve_pending_route_call_id(
    requested_call_id: str,
    pending_routes: object,
) -> str:
    """Resolve exactly one initial inbound decision without guessing."""
    requested = str(requested_call_id or "").strip()
    if requested:
        return requested
    candidates = sorted(set(pending_routes))
    if not candidates:
        raise ValueError("No inbound route is waiting for a destination")
    if len(candidates) > 1:
        raise ValueError(
            "More than one inbound route is waiting; provide call_id"
        )
    return candidates[0]


MATCH_MODES = ("exact", "prefix", "range", "pattern")


def _number(value):
    text = str(value).strip().removeprefix("+")
    return int(text) if text and len(text) <= 256 and text.isascii() and text.isdecimal() else None


@lru_cache(maxsize=64)
def _pattern(value):
    return re.compile(value, re.IGNORECASE)


def _matches_value(actual, wanted, mode, upper=""):
    if actual in (None, ""):
        return False
    actual, wanted = str(actual or "").strip(), str(wanted).strip()
    if mode == "prefix":
        return actual.casefold().startswith(wanted.casefold())
    if mode == "range":
        value, lower, end = _number(actual), _number(wanted), _number(upper)
        return value is not None and lower is not None and end is not None and lower <= value <= end
    if mode == "pattern":
        try:
            return len(wanted) <= 256 and _pattern(wanted).fullmatch(actual) is not None
        except re.error:
            return False
    return actual.casefold() == wanted.casefold()


def matches_call(payload: dict, options: dict, *, hass=None) -> bool:
    """Match the event snapshot rather than mutable Event Entity attributes."""
    aliases = {
        "caller": ("caller", "caller_number", "caller_extension", "source_account_username", "caller_account_username"),
        "destination": ("callee", "destination", "called_extension", "called_number", "dest_account_username", "destination_account_username", "target_account_username"),
        "trunk": ("inbound_trunk_id",),
        "trunk_id": ("inbound_trunk_id",),
        "reason": ("reason", "terminal_reason"),
        "outcome": ("type",),
        "caller_number": ("caller_number",),
        "caller_extension": ("source_extension", "caller_extension"),
        "destination_number": ("called_number",),
        "destination_extension": ("dest_extension", "called_extension"),
        "caller_account": ("source_account_username", "caller_account_username", "source_endpoint_id"),
        "destination_account": ("dest_account_username", "destination_account_username", "target_account_username", "dest_endpoint_id", "target_endpoint_id"),
    }
    if hass is not None and any(options.get(field) for field in (
            "caller_account", "destination_account", "caller_extension", "destination_extension")):
        from .sip_account_identity import automation_account_fields, ha_account_username
        payload = automation_account_fields(hass, payload)
        options = dict(options)
        for field in ("caller_account", "destination_account"):
            if options.get(field):
                username = ha_account_username(hass, options[field])
                if not username:
                    return False
                options[field] = username
    for key, wanted in options.items():
        if key == "for" or key.endswith(("_match", "_range_end")):
            continue
        if not str(wanted).strip():
            continue
        fields = aliases.get(key, (key,))
        side = ("caller" if key in {"caller", "caller_number", "caller_extension"} else
                "destination" if key in {"destination", "destination_number", "destination_extension"} else "")
        mode = options.get(side + "_match", "exact") if side else "exact"
        if hass is not None and key in {"caller", "destination"} and mode == 'exact' and getattr(hass, 'config_entries', None) is not None:
            from .sip_account_identity import automation_account_fields, canonical_ha_destination
            canonical = canonical_ha_destination(hass, str(wanted))
            if canonical != wanted:
                projected = automation_account_fields(hass, payload)
                if any(str(projected.get(field) or '').casefold() == canonical.casefold()
                       for field in aliases[key + '_account']):
                    continue
        if mode not in MATCH_MODES or not any(_matches_value(payload.get(field), wanted, mode,
                options.get(side + "_range_end", "")) for field in fields):
            return False
    return True


def route_window_snapshot(invite, decision=None, *, trunk_invite=False, default_destination=""):
    """Use the same original identity and route aliases at every ingress gate."""
    from .inbound_identity import identity_fields

    entry = getattr(decision, "entry", None)
    target = str(getattr(decision, "target", "") or default_destination or invite.target)
    routing_target = str(getattr(invite, "routing_target", "") or invite.target)
    metadata = getattr(entry, 'metadata', {}) or {}
    source = str(getattr(invite, 'identified_source_endpoint_id', '')
                 or getattr(invite, 'source_account_username', '') or '')
    destination = str(metadata.get('endpoint_id') or '') if metadata.get('endpoint_kind') in {
        'sip_account', 'browser', 'esphome'} else ''
    return {**identity_fields(invite),
        "source_account_username": source, "dest_account_username": destination,
        "source_extension": str(getattr(invite, 'source_extension', '') or ''),
        "dest_extension": str(getattr(entry, 'extension', '') or ''),
        "outbound_trunk_id": str(getattr(invite, "outbound_trunk_id", "") or ""),
        "caller": invite.caller, "callee": target,
        "destination": str(getattr(entry, "display_name", "") or target),
        "called_extension": routing_target if trunk_invite else str(getattr(entry, "extension", "") or routing_target),
        "ingress": "trunk" if trunk_invite else "extension"}


def has_native_route_window(hass, invite, decision=None, *, trunk_invite=False, default_destination=""):
    """An attached native trigger is an explicit, scoped initial-route request."""
    from .const import DOMAIN

    registration = getattr(hass, "data", {}).get(DOMAIN, {}).get("registration")
    filters = getattr(registration, "route_trigger_filters", {})
    snapshot = route_window_snapshot(invite, decision, trunk_invite=trunk_invite,
                                     default_destination=default_destination)
    return any(matches_call(snapshot, options, hass=hass) for options in filters.values())
