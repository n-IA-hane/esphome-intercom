"""Publish canonical call occurrences through the existing Home Assistant event bus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import Context, HomeAssistant

from .automation_routing import (
    CALL_ORIGINS,
    canonical_call_origin,
    is_logbook_call_summary,
)
from .const import (
    CALL_EVENT,
    HA_PEER_FALLBACK_NAME,
    SIP_CALL_ENDED_EVENT,
    SIP_CALL_STATE_EVENT,
    SIP_INCOMING_CALL_EVENT,
    SIP_ROUTE_REQUEST_EVENT,
)
from .endpoint_lifecycle import call_registry
from .fsm import (
    CallState,
    TerminalReason,
)
from .fsm import (
    sip_public_state as _sip_public_state,
)
from .pbx_trace import trace_call

_LOGGER = logging.getLogger(f"{__package__}.websocket_api")


def home_phone_name(hass: HomeAssistant) -> str:
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


def _direction_for_local(local_name: str, caller: str, callee: str) -> str:
    local = (local_name or "").strip().lower()
    if local and local == (caller or "").strip().lower():
        return "outgoing"
    if local and local == (callee or "").strip().lower():
        return "incoming"
    return ""


def phone_projection_state(state: str, direction: str) -> str:
    """Project the same early SIP state from the selected phone's side."""
    state = _sip_public_state(state)
    return (
        CallState.REMOTE_RINGING.value
        if state == CallState.RINGING.value and direction == "outgoing"
        else state
    )


def canonical_session_fields(
    *,
    state: str,
    local_name: str = "",
    peer_name: str = "",
    caller: str = "",
    callee: str = "",
    direction: str = "",
    call_id: str = "",
) -> dict[str, str]:
    caller = (caller or "").strip()
    callee = (callee or "").strip()
    local_name = (local_name or "").strip()
    peer_name = (peer_name or "").strip()
    if not direction and caller and callee:
        direction = _direction_for_local(local_name, caller, callee)
    if not peer_name:
        peer_name = callee if direction == "outgoing" else caller
    if not local_name:
        local_name = caller if direction == "outgoing" else callee
    role = (
        "caller"
        if direction == "outgoing"
        else "callee"
        if direction == "incoming"
        else ""
    )
    sip_state = phone_projection_state(state, direction)
    return {
        "call_id": call_id or (f"{caller}<->{callee}" if caller and callee else ""),
        "caller": caller,
        "callee": callee,
        "local_name": local_name,
        "peer_name": peer_name,
        "direction": direction,
        "role": role,
        "state": sip_state,
        "sip_state": sip_state,
    }


def _call_event_type(state: str, reason: str | None = None) -> str:
    state = _sip_public_state(state)
    reason = (reason or "").strip().lower()
    if state in (CallState.RINGING.value, CallState.REMOTE_RINGING.value):
        return "ringing"
    if state == CallState.CALLING.value:
        return "outgoing"
    if state == CallState.IN_CALL.value:
        return "answered"
    if state in (
        "error",
        CallState.MEDIA_INCOMPATIBLE.value,
        CallState.TRANSPORT_UNREACHABLE.value,
        CallState.AUTH_REQUIRED_UNSUPPORTED.value,
    ):
        return "failed"
    if state in (
        CallState.IDLE.value,
        CallState.BUSY.value,
        CallState.DECLINED.value,
        CallState.CANCELLED.value,
    ):
        return "missed" if reason == TerminalReason.TIMEOUT.value else "ended"
    return state or "state"


def json_event_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [json_event_value(item) for item in value]
    if isinstance(value, tuple):
        return [json_event_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): clean
            for key, item in value.items()
            if (clean := json_event_value(item)) is not None
        }
    return None


def fire_with_call_context(
    hass: HomeAssistant,
    event_type: str,
    payload: dict[str, Any],
    context: Any | None,
) -> None:
    """Publish with call provenance while preserving context-free test buses."""

    from .sip_account_identity import ha_event_payload

    projected = ha_event_payload(hass, payload)
    if context is None:
        hass.bus.async_fire(event_type, projected)
    else:
        hass.bus.async_fire(event_type, projected, context=context)


def fire_call_event(
    hass: HomeAssistant, payload: dict[str, Any], scope: str
) -> dict[str, Any]:
    event = json_event_value(payload) or {}
    # Scope identifies the state owner publishing this occurrence. Transport
    # provenance belongs in origin/source and must not replace the owner.
    event["scope"] = scope
    occurrence_origin = str(event.get("origin") or event["scope"]).strip()
    event.setdefault("direction", "")
    event.setdefault("caller", "")
    event.setdefault("callee", "")
    event.setdefault("dialed_target", event.get("target") or event.get("callee") or "")
    event.setdefault("route_kind", "")
    event["state"] = _sip_public_state(str(event.get("state") or ""))
    event["sip_state"] = event["state"]
    reason = event.get("reason") or event.get("terminal_reason")
    event["type"] = _call_event_type(
        event["state"], str(reason) if reason is not None else None
    )
    call_id = str(event.get("call_id") or "").strip()
    registry = call_registry(hass)
    session = registry.get_session(registry.resolve_session_id(call_id))
    publisher = registry.component("state_publisher")
    if (
        session is not None
        and publisher is not None
        and scope in {"session", "sip_bridge"}
    ):
        publisher.acknowledge(
            session,
            "phone" if scope == "session" else "sip_bridge",
            str(event.get("endpoint_id") or "") if scope == "session" else "",
        )
    forwardable_state = event["state"] in {
        "route_requested",
        CallState.CONNECTING.value,
        CallState.RINGING.value,
        CallState.REMOTE_RINGING.value,
    }
    event["automation_control"] = (
        "routable"
        if forwardable_state
        and (
            registry.artifact_for(call_id, "pending_invite") is not None
            or registry.artifact_for(call_id, "pending_route") is not None
        )
        else "ha_anchored"
        if call_id
        and (
            registry.event_context(call_id) is not None
            or registry.resolve_session_id(call_id) in registry.sessions
        )
        else "observed"
    )
    registry_fields = registry.event_fields(call_id, event["state"])
    # ``endpoint_id``/``device_id`` identify the phone whose state is being
    # projected by _set_ha_softphone_call_state().  A local browser call has
    # one canonical session plus two independently published phone legs; the
    # session metadata therefore must not replace the callee leg with the
    # caller identity.  Registry-owned source/destination/participant fields
    # still win and remain the authoritative call topology.
    for owner_key in ("endpoint_id", "device_id"):
        if event.get(owner_key) not in (None, ""):
            registry_fields.pop(owner_key, None)
    event.update(registry_fields)
    call_origin = canonical_call_origin(
        event.get("ingress") or event.get("origin"),
        event.get("route_kind"),
    )
    event["actor"] = str(
        event.get("actor")
        or (
            occurrence_origin
            if occurrence_origin not in CALL_ORIGINS
            else event["scope"]
        )
    )
    event["ingress"] = call_origin
    event["origin"] = call_origin
    # Arm the session-owned policy before publishing the HA event projection.
    from .phone_policy_runtime import apply_phone_policy_state

    apply_phone_policy_state(hass, event)
    terminal_summary = is_logbook_call_summary(event)
    context = registry.ha_context(call_id)
    if context is None and terminal_summary:
        # Cleanup may already have retired the session. Keep all event types
        # emitted for this terminal occurrence on one native Activity context.
        context = Context()
    fire_with_call_context(hass, CALL_EVENT, event, context)
    fire_with_call_context(hass, SIP_CALL_STATE_EVENT, event, context)
    if event.get("route_request") or event["state"] == "route_requested":
        fire_with_call_context(hass, SIP_ROUTE_REQUEST_EVENT, event, context)
    if event.get("direction") == "incoming" and (
        event.get("route_request")
        or event["state"]
        in (
            "route_requested",
            CallState.CONNECTING.value,
            CallState.RINGING.value,
        )
    ):
        fire_with_call_context(hass, SIP_INCOMING_CALL_EVENT, event, context)
    if terminal_summary and registry.claim_terminal_summary(call_id):
        trace_call(
            _LOGGER,
            call_id,
            "Call %s: %s to %s (%s)",
            event["type"],
            event.get("caller"),
            event.get("connected_party")
            or event.get("answered_by")
            or event.get("callee")
            or event.get("dialed_target"),
            event.get("terminal_reason") or event.get("reason") or event["state"],
            level=logging.INFO,
        )
        from .entity_identity import shared_entity_ids

        # Recorder filters raw identity before its Logbook describer runs.
        # Associate this one terminal summary with the real PBX Call entity;
        # adding the entity only inside the describer is too late for Activity.
        summary = dict(event)
        if entity_id := shared_entity_ids(hass)["call_event"]:
            summary["entity_id"] = entity_id
        fire_with_call_context(hass, SIP_CALL_ENDED_EVENT, summary, context)
    return event
