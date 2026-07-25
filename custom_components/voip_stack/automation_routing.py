"""Pure automation-facing routing primitives."""

from __future__ import annotations

from typing import Any


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
