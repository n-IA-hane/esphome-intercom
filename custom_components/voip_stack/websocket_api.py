"""SIP-only WebSocket API for VoIP Stack."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .config import debug_mode as current_debug_mode
from .config import transport_config as current_transport_config
from .authorization import (
    media_controller_status,
    require_websocket_endpoint_read,
    require_websocket_read,
    websocket_can_control_endpoint,
)
from .automation_routing import (
    CALL_ORIGINS,
    canonical_call_origin,
    is_logbook_call_summary,
)
from .call_registry import TERMINAL_STATES
from .pbx_runtime import SipEndpointRuntime
from .const import (
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
    SIP_CALL_ENDED_EVENT,
)
from .endpoint_lifecycle import call_registry
from .fsm import CallState, TerminalReason, sip_phone_state, sip_public_state as _sip_public_state
from .phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
    PhoneEndpoint,
)
from .phone_config import (
    CONF_PHONE_AUTO_ANSWER,
    CONF_PHONE_CONFERENCE_GROUP,
    CONF_PHONE_CONFERENCE_RING,
    CONF_PHONE_DND,
    CONF_PHONE_EXTENSION,
    CONF_PHONE_RING_GROUP,
    CONF_PHONE_SEND_VIDEO,
    update_phone_subentry,
)
from .debug_capture import debug_capture_pending_writes
from .runtime_diagnostics import runtime_resource_snapshot
from .runtime_data import (
    endpoint_directory,
    preferred_browser_phone,
    require_runtime_data,
    runtime_data,
    sip_endpoint_manager,
    sip_registrar,
    sip_trunk,
)
from .websocket_owner import (
    async_revoke_media_owners,
    media_websocket_owner_status,
)

if TYPE_CHECKING:
    from homeassistant.core import Event

_LOGGER = logging.getLogger(__name__)

CALL_EVENT = "voip_stack.call_event"
HA_SOFTPHONE_STATE_EVENT = "voip_stack.ha_softphone_state"
SIP_CALL_STATE_EVENT = "voip_stack.call_state"
SIP_INCOMING_CALL_EVENT = "voip_stack.incoming_call"
SIP_ROUTE_REQUEST_EVENT = "voip_stack.route_request"
SIP_DTMF_EVENT = "voip_stack.dtmf"
WS_TYPE_LIST = f"{DOMAIN}/list_devices"
WS_TYPE_RESOLVE_DEVICE = f"{DOMAIN}/resolve_device"
WS_TYPE_HA_SOFTPHONE_STATE = f"{DOMAIN}/ha_softphone_state"
WS_TYPE_SUBSCRIBE_CALL_EVENTS = f"{DOMAIN}/subscribe_call_events"
WS_TYPE_SUBSCRIBE_HA_SOFTPHONE = f"{DOMAIN}/subscribe_ha_softphone_state"


def _clean_group_token(value: object) -> str:
    return (
        str(value or "")
        .replace("|", " ")
        .replace(";", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()[:32]
    )


def _clean_group_name(value: object) -> str:
    groups: list[str] = []
    for raw in str(value or "").split(","):
        group = _clean_group_token(raw)
        if group and group not in groups:
            groups.append(group)
    return ", ".join(groups)


def _clean_endpoint_field(value: object, *, max_len: int = 32) -> str:
    return (
        str(value or "")
        .replace("|", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )[:max_len]


def _ha_peer_name(hass: HomeAssistant) -> str:
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


def _direction_for_local(local_name: str, caller: str, callee: str) -> str:
    local = (local_name or "").strip().lower()
    if local and local == (caller or "").strip().lower():
        return "outgoing"
    if local and local == (callee or "").strip().lower():
        return "incoming"
    return ""


def _canonical_session_fields(
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
    role = "caller" if direction == "outgoing" else "callee" if direction == "incoming" else ""
    sip_state = _sip_public_state(state)
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


def _json_event_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_event_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_event_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): clean
            for key, item in value.items()
            if (clean := _json_event_value(item)) is not None
        }
    return None


def _async_fire_with_context(
    hass: HomeAssistant,
    event_type: str,
    payload: dict[str, Any],
    context: Any | None,
) -> None:
    """Publish with call provenance while preserving context-free test buses."""

    if context is None:
        hass.bus.async_fire(event_type, payload)
    else:
        hass.bus.async_fire(event_type, payload, context=context)


def _fire_call_event(hass: HomeAssistant, payload: dict[str, Any], scope: str) -> dict[str, Any]:
    event = _json_event_value(payload) or {}
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
    event["type"] = _call_event_type(event["state"], str(reason) if reason is not None else None)
    call_id = str(event.get("call_id") or "").strip()
    registry = call_registry(hass)
    forwardable_state = event["state"] in {
        "route_requested",
        CallState.CONNECTING.value,
        CallState.RINGING.value,
        CallState.REMOTE_RINGING.value,
    }
    event["automation_control"] = (
        "routable"
        if forwardable_state
        and (call_id in registry.pending_invites or call_id in registry.pending_routes)
        else "ha_anchored"
        if call_id and (
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
    context = registry.ha_context(call_id)
    _async_fire_with_context(hass, CALL_EVENT, event, context)
    _async_fire_with_context(hass, SIP_CALL_STATE_EVENT, event, context)
    if event.get("route_request") or event["state"] == "route_requested":
        _async_fire_with_context(hass, SIP_ROUTE_REQUEST_EVENT, event, context)
    if event.get("direction") == "incoming" and (
        event.get("route_request")
        or event["state"]
        in (
            "route_requested",
            CallState.CONNECTING.value,
            CallState.RINGING.value,
        )
    ):
        _async_fire_with_context(hass, SIP_INCOMING_CALL_EVENT, event, context)
    if is_logbook_call_summary(event) and registry.claim_terminal_summary(call_id):
        _async_fire_with_context(hass, SIP_CALL_ENDED_EVENT, event, context)
    return event


def _endpoint_store_id(value: object) -> str:
    """Require the real browser endpoint that owns one state store."""

    endpoint_id = str(value or "").strip()
    if not endpoint_id:
        raise ValueError("a Home Assistant phone endpoint is required")
    return endpoint_id


def _ha_softphone_store(hass: HomeAssistant, endpoint_id: str) -> dict[str, Any]:
    """Return one endpoint-scoped HA softphone runtime store.

    Runtime state has one authoritative endpoint-keyed mapping.
    """
    stores = require_runtime_data(hass).softphones
    key = _endpoint_store_id(endpoint_id)
    store = stores.setdefault(key, {"dnd": False})
    store.setdefault("endpoint_id", key)
    endpoint = endpoint_directory(hass).get(key)
    if endpoint is not None and endpoint.kind is EndpointKind.BROWSER:
        store.setdefault("device_id", endpoint.device_id)
        store.setdefault("local_name", endpoint.name)
    return store


def _ha_softphone_stores(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return every logical browser softphone store."""
    return require_runtime_data(hass).softphones


def _endpoint_registry(hass: HomeAssistant):
    """Return the entry-owned logical endpoint registry."""
    return endpoint_directory(hass)


def _browser_endpoint(hass: HomeAssistant, endpoint_id: object):
    registry = _endpoint_registry(hass)
    selector = str(endpoint_id or "").strip()
    endpoint = registry.get(selector) if registry is not None and selector else None
    if endpoint is not None and endpoint.kind is EndpointKind.BROWSER:
        return endpoint
    return None


@callback
def _update_browser_presence(
    hass: HomeAssistant,
    endpoint_id: str,
    delta: int,
) -> None:
    """Track connected cards and expose browser reachability to routing."""
    endpoint_id = _endpoint_store_id(endpoint_id)
    counts = require_runtime_data(hass).softphone_presence
    previous = int(counts.get(endpoint_id, 0) or 0)
    current = max(0, previous + int(delta))
    if current:
        counts[endpoint_id] = current
    else:
        counts.pop(endpoint_id, None)

    endpoint = _browser_endpoint(hass, endpoint_id)
    if endpoint is not None:
        availability = (
            EndpointAvailability.AVAILABLE
            if current
            else EndpointAvailability.OFFLINE
        )
        registry = _endpoint_registry(hass)
        if endpoint.availability is not EndpointAvailability.UNAVAILABLE:
            registry.update(endpoint_id, availability=availability)

    if previous != current:
        _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)


def _endpoint_call_claim(
    hass: HomeAssistant,
    endpoint_id: str,
    call_id: str,
    *,
    terminal: bool,
) -> None:
    """Keep the endpoint busy guard synchronized with softphone call state."""
    registry = _endpoint_registry(hass)
    if registry is None or registry.get(endpoint_id) is None or not call_id:
        return
    try:
        if terminal:
            registry.release_call(endpoint_id, call_id)
        else:
            registry.claim_call(endpoint_id, call_id)
    except ValueError as err:
        # The caller-facing routing path performs the authoritative 486 check.
        # A late/stale state callback must not tear down the call which already
        # owns the endpoint.
        _LOGGER.warning(
            "Could not synchronize endpoint=%s call=%s terminal=%s: %s",
            endpoint_id,
            call_id,
            terminal,
            err,
        )


def _registry_call_is_active(registry: SipEndpointRuntime, call_id: str) -> bool:
    """Return whether a projected call still owns live backend state."""
    call_id = str(call_id or "").strip()
    if not call_id or registry.is_terminated(call_id):
        return False
    session_id = registry.resolve_session_id(call_id)
    session = registry.sessions.get(session_id)
    if session is not None and str(session.state or "").strip().lower() not in TERMINAL_STATES:
        return True
    # A route or media resource may exist briefly before its canonical session
    # is materialised.  These are live ownership records, unlike a stale HA
    # projection left behind after an interrupted teardown.
    for resources in (
        registry.pending_routes,
        registry.pending_invites,
        registry.preanswered,
        registry.softphone_media,
        registry.sip_clients,
        registry.client_watchers,
        registry.relays,
    ):
        if call_id in resources or session_id in resources:
            return True
    return False


def _release_ha_softphone_claim(
    hass: HomeAssistant,
    call_id: str,
    *,
    destination: str = "",
    endpoint_id: str,
) -> bool:
    """Release HA's ringing ownership when the same call is routed elsewhere."""
    endpoint_id = _endpoint_store_id(endpoint_id)
    store = _ha_softphone_store(hass, endpoint_id)
    if str(store.get("call_id") or "") != str(call_id or ""):
        return False
    _set_ha_softphone_call_state(
        hass,
        CallState.CANCELLED.value,
        session_device_id=str(store.get("device_id") or ""),
        caller=str(store.get("caller") or ""),
        callee=str(store.get("callee") or ""),
        peer_name=str(store.get("peer_name") or ""),
        direction=str(store.get("direction") or "incoming"),
        call_id=call_id,
        reason=TerminalReason.FORWARDED.value,
        terminal_reason=TerminalReason.FORWARDED.value,
        dialed_target=destination,
        origin="automation",
        last_sip_event="ROUTE_FORWARD",
        endpoint_id=endpoint_id,
    )
    return True


def _ha_softphone_groups(hass: HomeAssistant, endpoint_id: str) -> dict[str, Any]:
    store = _ha_softphone_store(hass, endpoint_id)
    groups = store.setdefault("groups", {})
    return {
        "ring_group": _clean_group_name(groups.get("ring_group")),
        "conference_group": _clean_group_name(groups.get("conference_group")),
        "conference_ring": bool(groups.get("conference_ring", False)),
    }


def _ha_softphone_extension(hass: HomeAssistant, endpoint_id: str) -> str:
    return _clean_endpoint_field(
        _ha_softphone_store(hass, endpoint_id).get("extension")
    )


def _ha_softphone_auto_answer(hass: HomeAssistant, endpoint_id: str) -> bool:
    return bool(_ha_softphone_store(hass, endpoint_id).get("auto_answer", False))


def _ha_softphone_send_video(hass: HomeAssistant, endpoint_id: str) -> bool:
    return bool(_ha_softphone_store(hass, endpoint_id).get("send_video", False))


async def async_set_ha_softphone_settings(
    hass: HomeAssistant,
    *,
    endpoint_id: str,
    extension: object = None,
    ring_group: object = None,
    conference_group: object = None,
    conference_ring: object = None,
    auto_answer: object = None,
    send_video: object = None,
) -> dict[str, Any]:
    endpoint_id = _endpoint_store_id(endpoint_id)
    store = _ha_softphone_store(hass, endpoint_id)
    if extension is not None:
        store["extension"] = _clean_endpoint_field(extension)
    groups = _ha_softphone_groups(hass, endpoint_id)
    if ring_group is not None:
        groups["ring_group"] = _clean_group_name(ring_group)
    if conference_group is not None:
        groups["conference_group"] = _clean_group_name(conference_group)
    if conference_ring is not None:
        groups["conference_ring"] = bool(conference_ring)
    if auto_answer is not None:
        store["auto_answer"] = bool(auto_answer)
    if send_video is not None:
        store["send_video"] = bool(send_video)
    if not groups["conference_group"]:
        groups["conference_ring"] = False
    store["groups"] = groups
    await _async_save_ha_softphone_store(hass, endpoint_id)
    runtime = runtime_data(hass)
    endpoint_sensor = runtime.ha_endpoint_sensor if runtime is not None else None
    if endpoint_sensor is not None:
        await endpoint_sensor.async_update()
    state = _ha_softphone_state(hass, endpoint_id)
    _publish_ha_softphone_state(hass, state, endpoint_id=endpoint_id)
    return state


def _sip_bridge_store(hass: HomeAssistant) -> dict[str, Any]:
    return require_runtime_data(hass).sip_bridge_state


async def _async_shutdown_all(hass: HomeAssistant) -> None:
    """Clear HA softphone volatile state before SIP transports are stopped."""
    registry = call_registry(hass)
    for route in list(registry.pending_routes.values()):
        future = route.get("future") if isinstance(route, dict) else None
        if future is not None and not future.done():
            future.cancel()
    runtime = runtime_data(hass)
    if runtime is not None:
        runtime.sip_bridge_state.clear()
    # HTTP views remain registered across a config-entry reload. Gate claims
    # before taking owner snapshots so a concurrent GET cannot outlive unload.
    media = runtime.media if runtime is not None else None
    if media is not None:
        media.shutdown.set()
    owner_shutdowns = []
    for channel in ("audio", "video"):
        owners = media.owners_for(channel) if media is not None else {}
        owner_lock = (
            media.owner_lock_for(channel) if media is not None else asyncio.Lock()
        )
        owner_shutdowns.append(
            async_revoke_media_owners(owners, owner_lock, timeout=5.0)
        )
    pending_owner_sets = await asyncio.gather(*owner_shutdowns)
    pending_owner_ids = set().union(*pending_owner_sets)
    if pending_owner_ids:
        _LOGGER.warning(
            "Could not confirm HA softphone media owner shutdown call_ids=%s",
            sorted(pending_owner_ids),
        )
    capture_tasks = {
        task
        for task in (media.debug_capture_tasks if media is not None else set())
        if not task.done()
    }
    if capture_tasks:
        _done, pending_captures = await asyncio.wait(capture_tasks, timeout=2.0)
        if pending_captures:
            # Executor Future cancellation cannot stop a worker that is
            # already writing. Keep it tracked until its done callback sees
            # the real result; capture transactions are atomic and serialized,
            # so a bounded post-unload completion is safer than masking a late
            # write failure as ``cancelled``.
            _LOGGER.warning(
                "HA softphone debug capture shutdown timed out; %d atomic write(s) will finish in background",
                len(pending_captures),
            )
    # Local browser calls have no SIP transport whose shutdown would end
    # them. Close their dual endpoint state and wake media waiters explicitly.
    from .local_softphone_runtime import async_shutdown_local_softphone_bridge

    async_shutdown_local_softphone_bridge(hass)
    for endpoint_id, store in tuple(_ha_softphone_stores(hass).items()):
        active_call_id = str(store.get("call_id") or "")
        active_state = str(store.get("state") or "").lower()
        if active_call_id or active_state in {
            CallState.CALLING.value,
            CallState.REMOTE_RINGING.value,
            CallState.RINGING.value,
            CallState.CONNECTING.value,
            CallState.IN_CALL.value,
            CallState.TERMINATING.value,
        }:
            _set_ha_softphone_call_state(
                hass,
                CallState.IDLE.value,
                session_device_id=str(store.get("session_device_id") or ""),
                caller=str(store.get("caller") or ""),
                callee=str(store.get("callee") or ""),
                peer_name=str(store.get("peer_name") or ""),
                direction=str(store.get("direction") or ""),
                call_id=active_call_id,
                reason=TerminalReason.LOCAL_HANGUP.value,
                terminal_reason=TerminalReason.LOCAL_HANGUP.value,
                last_sip_event="shutdown",
                endpoint_id=endpoint_id,
            )
        else:
            # Reloading an already-idle integration is not a call lifecycle
            # event. Publish a dedicated state snapshot for every endpoint.
            store["state"] = CallState.IDLE.value
            store["sip_state"] = CallState.IDLE.value
            store["last_sip_event"] = "shutdown"
            _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)


async def _async_load_ha_softphone_store(
    hass: HomeAssistant,
    config_entry: object | None = None,
    *,
    endpoint_id: str,
    endpoint_data: dict[str, Any] | None = None,
) -> None:
    endpoint_id = _endpoint_store_id(endpoint_id)
    runtime = _ha_softphone_store(hass, endpoint_id)
    if config_entry is not None:
        runtime["config_entry_id"] = str(getattr(config_entry, "entry_id", ""))
    data = endpoint_data or {}
    runtime["dnd"] = bool(data.get(CONF_PHONE_DND, runtime.get("dnd", False)))
    runtime["auto_answer"] = bool(
        data.get(CONF_PHONE_AUTO_ANSWER, runtime.get("auto_answer", False))
    )
    runtime["send_video"] = bool(
        data.get(CONF_PHONE_SEND_VIDEO, runtime.get("send_video", False))
    )
    runtime["extension"] = _clean_endpoint_field(
        data.get(CONF_PHONE_EXTENSION, runtime.get("extension", ""))
    )
    current_groups = _ha_softphone_groups(hass, endpoint_id)
    runtime["groups"] = {
        "ring_group": _clean_group_name(
            data.get(CONF_PHONE_RING_GROUP, current_groups["ring_group"])
        ),
        "conference_group": _clean_group_name(
            data.get(
                CONF_PHONE_CONFERENCE_GROUP,
                current_groups["conference_group"],
            )
        ),
        "conference_ring": bool(
            data.get(
                CONF_PHONE_CONFERENCE_RING,
                current_groups["conference_ring"],
            )
        ),
    }
    endpoint = _browser_endpoint(hass, endpoint_id)
    if endpoint is not None:
        runtime["device_id"] = endpoint.device_id
        runtime["local_name"] = endpoint.name


async def _async_save_ha_softphone_store(
    hass: HomeAssistant, endpoint_id: str
) -> None:
    endpoint_id = _endpoint_store_id(endpoint_id)
    runtime = _ha_softphone_store(hass, endpoint_id)
    groups = _ha_softphone_groups(hass, endpoint_id)
    persisted = {
        CONF_PHONE_DND: bool(runtime.get("dnd", False)),
        CONF_PHONE_AUTO_ANSWER: _ha_softphone_auto_answer(hass, endpoint_id),
        CONF_PHONE_SEND_VIDEO: _ha_softphone_send_video(hass, endpoint_id),
        CONF_PHONE_EXTENSION: _ha_softphone_extension(hass, endpoint_id),
        CONF_PHONE_RING_GROUP: groups["ring_group"],
        CONF_PHONE_CONFERENCE_GROUP: groups["conference_group"],
        CONF_PHONE_CONFERENCE_RING: groups["conference_ring"],
    }
    entry_id = str(runtime.get("config_entry_id") or "")
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if entry is not None:
        update_phone_subentry(
            hass,
            entry,
            endpoint_id,
            persisted,
        )


def _ha_softphone_dnd(hass: HomeAssistant, endpoint_id: str) -> bool:
    return bool(_ha_softphone_store(hass, endpoint_id).get("dnd"))


_MEDIA_COUNTER_KEYS = (
    "rtp_tx_packets",
    "rtp_rx_packets",
    "rtp_tx_bytes",
    "rtp_rx_bytes",
    "video_rtp_tx_packets",
    "video_rtp_rx_packets",
    "video_rtp_tx_bytes",
    "video_rtp_rx_bytes",
    "video_rtp_tx_payload_bytes",
    "video_rtp_dropped_packets",
    "video_access_units_tx",
    "video_access_units_rx",
    "video_drop_addr",
    "video_drop_payload_type",
    "video_drop_error",
    "video_drop_direction",
    "video_drop_connection_hold",
    "video_reordered_packets",
    "video_lost_packets",
    "video_duplicate_packets",
    "video_keyframe_requests",
    "video_symmetric_rtp_keepalives",
    "video_symmetric_rtp_keepalive_payload_type",
    "video_access_unit_queue_max",
    "video_access_unit_queue_drops",
    "video_browser_keyframe_requests",
    "video_rtcp_rx_packets",
    "video_rtcp_rx_bytes",
    "video_rtcp_tx_packets",
    "video_rtcp_tx_bytes",
    "video_rtcp_drop_addr",
    "video_rtcp_drop_error",
    "video_rtcp_drop_queue",
    "video_rtcp_task_errors",
    "video_keepalive_task_errors",
    "video_rtcp_pli_rx",
    "video_rtcp_fir_rx",
    "video_rtcp_keyframe_requests_to_browser",
    "video_jpeg_normalized",
    "video_jpeg_normalizer_errors",
)


def _runtime_counter(store: dict[str, Any], runtime: dict[str, Any], key: str) -> int:
    del runtime
    store_value = int(store.get(key, 0) or 0)
    # Runtime totals aggregate every relay in the integration, so they cannot
    # safely represent one HA softphone call even while its call-id is active.
    # Browser audio/video reporters persist call-scoped counters in ``store``;
    # aggregate relay counters remain available under debug topology maps.
    return store_value


def _sip_runtime_snapshot(
    hass: HomeAssistant,
    *,
    detailed: bool = False,
) -> dict[str, Any]:
    bucket = hass.data.get(DOMAIN, {})
    registry = call_registry(hass)
    if registry is None:
        registry = None
    endpoint = sip_endpoint_manager(hass)
    data: dict[str, Any] = {
        "sip_udp_ready": False,
        "sip_tcp_ready": False,
        "pending_transactions": 0,
        "active_dialogs": 0,
        "pending_call_ids": [],
        "active_call_ids": [],
        "last_sip_event": "",
        "last_sip_status_code": 0,
        "last_sip_reason": "",
        "rtp_tx_packets": 0,
        "rtp_rx_packets": 0,
        "rtp_tx_bytes": 0,
        "rtp_rx_bytes": 0,
        "rtp_dropped_packets": 0,
        "rtp_relays": {},
        "sip_client_dialogs": {},
        "sip_trunk": {},
        "sip_registrar": {},
        "media_debug": {},
        "runtime_resources": {},
    }
    if endpoint is None:
        endpoint = None
    snapshot = getattr(endpoint, "snapshot", None)
    if callable(snapshot):
        snap = snapshot()
        data.update(
            {
                "sip_udp_ready": bool(getattr(snap, "udp_ready", False)),
                "sip_tcp_ready": bool(getattr(snap, "tcp_ready", False)),
                "pending_transactions": int(getattr(snap, "pending_transactions", getattr(snap, "pending_invites", 0))),
                "active_dialogs": int(getattr(snap, "active_dialogs", 0)),
                "pending_call_ids": list(getattr(snap, "pending_call_ids", ()) or ()),
                "active_call_ids": list(getattr(snap, "active_call_ids", ()) or ()),
                "last_sip_event": str(getattr(snap, "last_sip_event", "") or ""),
                "last_sip_status_code": int(getattr(snap, "last_sip_status_code", 0) or 0),
                "last_sip_reason": str(getattr(snap, "last_sip_reason", "") or ""),
            }
        )
    for call_id, relay in dict((registry.relays if registry is not None else {}) or {}).items():
        if detailed:
            snap = getattr(relay, "snapshot", None)
            if not callable(snap):
                continue
            relay_data = dict(snap())
            data["rtp_relays"][call_id] = relay_data
        else:
            relay_data = {
                "left_rx_packets": getattr(relay, "left_rx_packets", 0),
                "left_rx_bytes": getattr(relay, "left_rx_bytes", 0),
                "left_tx_packets": getattr(relay, "left_tx_packets", 0),
                "left_tx_bytes": getattr(relay, "left_tx_bytes", 0),
                "right_rx_packets": getattr(relay, "right_rx_packets", 0),
                "right_rx_bytes": getattr(relay, "right_rx_bytes", 0),
                "right_tx_packets": getattr(relay, "right_tx_packets", 0),
                "right_tx_bytes": getattr(relay, "right_tx_bytes", 0),
                "dropped_packets": getattr(relay, "dropped", 0),
            }
        data["rtp_rx_packets"] += int(relay_data.get("left_rx_packets", 0)) + int(relay_data.get("right_rx_packets", 0))
        data["rtp_rx_bytes"] += int(relay_data.get("left_rx_bytes", 0)) + int(relay_data.get("right_rx_bytes", 0))
        data["rtp_tx_packets"] += int(relay_data.get("left_tx_packets", 0)) + int(relay_data.get("right_tx_packets", 0))
        data["rtp_tx_bytes"] += int(relay_data.get("left_tx_bytes", 0)) + int(relay_data.get("right_tx_bytes", 0))
        data["rtp_dropped_packets"] += int(relay_data.get("dropped_packets", 0))
    for key, client in dict((registry.sip_clients if registry is not None else {}) or {}).items():
        snap = getattr(client, "snapshot", None)
        if not callable(snap):
            continue
        client_data = dict(snap())
        if detailed:
            data["sip_client_dialogs"][key] = client_data
        if client_data.get("dialog_active"):
            data["active_dialogs"] += 1
            call_id = str(client_data.get("call_id") or "")
            if call_id and call_id not in data["active_call_ids"]:
                data["active_call_ids"].append(call_id)
        elif client_data.get("pending_invite"):
            data["pending_transactions"] += 1
            call_id = str(client_data.get("call_id") or "")
            if call_id and call_id not in data["pending_call_ids"]:
                data["pending_call_ids"].append(call_id)
        if client_data.get("last_sip_event"):
            data["last_sip_event"] = str(client_data.get("last_sip_event") or data["last_sip_event"])
            data["last_sip_status_code"] = int(client_data.get("last_sip_status_code") or data["last_sip_status_code"] or 0)
            data["last_sip_reason"] = str(client_data.get("last_sip_reason") or data["last_sip_reason"])
    trunk = sip_trunk(hass)
    trunk_snapshot = getattr(trunk, "snapshot", None)
    if callable(trunk_snapshot):
        trunk_data = dict(trunk_snapshot())
        if detailed:
            data["sip_trunk"] = trunk_data
        if trunk_data.get("trunk_last_sip_event"):
            data["last_sip_event"] = str(
                trunk_data.get("trunk_last_sip_event") or data["last_sip_event"]
            )
        if trunk_data.get("trunk_status_code"):
            data["last_sip_status_code"] = int(
                trunk_data.get("trunk_status_code") or 0
            )
            data["last_sip_reason"] = str(
                trunk_data.get("trunk_status_reason") or ""
            )
    registrar = sip_registrar(hass)
    registrar_snapshot = getattr(registrar, "snapshot", None)
    if detailed and callable(registrar_snapshot):
        data["sip_registrar"] = dict(registrar_snapshot())
        if data["sip_registrar"].get("registrar_last_sip_event"):
            data["last_sip_event"] = str(data["sip_registrar"].get("registrar_last_sip_event") or data["last_sip_event"])
        if data["sip_registrar"].get("registrar_last_sip_status_code"):
            data["last_sip_status_code"] = int(data["sip_registrar"].get("registrar_last_sip_status_code") or 0)
            data["last_sip_reason"] = str(data["sip_registrar"].get("registrar_last_sip_reason") or "")
    elif registrar is not None:
        if getattr(registrar, "last_sip_event", ""):
            data["last_sip_event"] = str(registrar.last_sip_event)
        if getattr(registrar, "last_sip_status_code", 0):
            data["last_sip_status_code"] = int(registrar.last_sip_status_code)
            data["last_sip_reason"] = str(
                getattr(registrar, "last_sip_reason", "") or ""
            )
    data["pending_call_ids"] = sorted(set(data["pending_call_ids"]))
    data["active_call_ids"] = sorted(set(data["active_call_ids"]))
    runtime = runtime_data(hass)
    data["runtime_resources"] = runtime_resource_snapshot(
        bucket,
        registry,
        detailed=detailed,
        rtp_port_pool=runtime.rtp_port_pool if runtime is not None else None,
        call_artifacts=runtime.sip if runtime is not None else None,
        browser_media=runtime.media if runtime is not None else None,
    )
    return data


def _ha_softphone_state(hass: HomeAssistant, endpoint_id: str) -> dict[str, Any]:
    endpoint_id = _endpoint_store_id(endpoint_id)
    store = _ha_softphone_store(hass, endpoint_id)
    endpoint = _browser_endpoint(hass, endpoint_id)
    entry_runtime = runtime_data(hass)
    connected_cards = int(
        (entry_runtime.softphone_presence if entry_runtime is not None else {}).get(
            endpoint_id, 0
        )
    )
    debug_mode = current_debug_mode(hass)
    runtime = _sip_runtime_snapshot(hass, detailed=debug_mode)
    transport_config = current_transport_config(hass)
    active_softphone = store.get("state") in {
        CallState.CALLING.value,
        CallState.REMOTE_RINGING.value,
        CallState.RINGING.value,
        CallState.CONNECTING.value,
        CallState.IN_CALL.value,
        CallState.TERMINATING.value,
    }
    last_status = store.get("sip_status_code", "") or runtime["last_sip_status_code"] or ""
    last_event = store.get("last_sip_event", "") or runtime["last_sip_event"]
    caller = store.get("caller", "") or store.get("last_terminal_caller", "")
    callee = store.get("callee", "") or store.get("last_terminal_callee", "")
    connected_party = str(store.get("connected_party", "") or store.get("answered_by", ""))
    peer_name = store.get("peer_name", "") or store.get("last_terminal_peer_name", "")
    if store.get("state") == CallState.IN_CALL.value and connected_party:
        peer_name = connected_party
    dialed_target = store.get("dialed_target", "") or store.get("last_terminal_dialed_target", "")
    direction = store.get("direction", "") or store.get("last_terminal_direction", "")
    call_id = store.get("call_id", "") or store.get("last_terminal_call_id", "")
    registry = call_registry(hass)
    media_debug = dict(store.get("media_debug") or {}) if debug_mode else {}
    if debug_mode:
        active_transcoder = (
            entry_runtime.media.transcoder if entry_runtime is not None else None
        )
        media_debug.update(
            {
                "call_registry": registry.snapshot(),
                "runtime_resources": runtime["runtime_resources"],
                "audio_ws_owner_call_ids": sorted(
                    entry_runtime.media.owners_for("audio")
                    if entry_runtime is not None
                    else ()
                ),
                "video_ws_owner_call_ids": sorted(
                    entry_runtime.media.owners_for("video")
                    if entry_runtime is not None
                    else ()
                ),
                "video_transcoder_call_id": str(
                    getattr(active_transcoder, "call_id", "") or ""
                ),
                "debug_capture_pending_writes": debug_capture_pending_writes(),
                "debug_capture_dropped_writes": (
                    entry_runtime.media.debug_capture_dropped_writes
                    if entry_runtime is not None
                    else 0
                ),
            }
        )
    session = registry.sessions.get(registry.resolve_session_id(str(call_id or "")))
    event_context = registry.event_context(str(call_id or ""))
    video_active = bool(store.get("video_active", False))
    video_offered = bool(store.get("video_offered", False))
    video_requested = bool(store.get("video_requested", video_offered))
    video_negotiated = bool(store.get("video_negotiated", video_active))
    video_status = str(store.get("video_status") or "").strip().lower()
    if not video_status:
        video_status = (
            "active"
            if video_active
            else "offered"
            if video_offered
            else "requested"
            if video_requested
            else "inactive"
        )
    phone = sip_phone_state(
        state=store.get("state", CallState.IDLE.value),
        call_id=call_id,
        direction=direction,
        caller=caller,
        callee=callee,
        local_uri=store.get("local_uri", ""),
        remote_uri=store.get("remote_uri", ""),
        contact=peer_name,
        sip_transport=store.get("sip_transport", "udp+tcp"),
        sip_status_code=int(last_status or 0),
        terminal_reason=store.get("terminal_reason", ""),
        selected_tx_format=store.get("selected_tx_format", ""),
        selected_rx_format=store.get("selected_rx_format", ""),
        rtp_tx_packets=_runtime_counter(store, runtime, "rtp_tx_packets"),
        rtp_rx_packets=_runtime_counter(store, runtime, "rtp_rx_packets"),
        rtp_tx_bytes=_runtime_counter(store, runtime, "rtp_tx_bytes"),
        rtp_rx_bytes=_runtime_counter(store, runtime, "rtp_rx_bytes"),
        last_sip_event=last_event,
    )
    return {
        **phone,
        "endpoint_id": endpoint_id,
        "device_id": (
            endpoint.device_id
            if endpoint is not None and endpoint.device_id
            else store.get("device_id", "")
        ),
        "name": endpoint.name if endpoint is not None else _ha_peer_name(hass),
        "endpoint_type": EndpointKind.BROWSER.value,
        "available": bool(
            endpoint is not None
            and endpoint.availability is EndpointAvailability.AVAILABLE
            and connected_cards
        ),
        "enabled": bool(
            endpoint is not None
            and endpoint.availability is not EndpointAvailability.UNAVAILABLE
        ),
        "connected_cards": connected_cards,
        "capabilities": (
            sorted(endpoint.capabilities) if endpoint is not None else ["audio", "dtmf"]
        ),
        "session_device_id": store.get("session_device_id", ""),
        "dnd": _ha_softphone_dnd(hass, endpoint_id),
        "extension": _ha_softphone_extension(hass, endpoint_id),
        "groups": _ha_softphone_groups(hass, endpoint_id),
        "busy": bool(store.get("session_device_id") and active_softphone),
        "state": store.get("state", CallState.IDLE.value),
        "sip_state": store.get("sip_state", store.get("state", CallState.IDLE.value)),
        "caller": caller,
        "callee": callee,
        "local_name": store.get(
            "local_name", endpoint.name if endpoint is not None else _ha_peer_name(hass)
        ),
        "peer_name": peer_name,
        "dialed_target": dialed_target,
        "connected_party": store.get("connected_party", ""),
        "answered_by": store.get("answered_by", ""),
        "direction": direction,
        "role": store.get("role", ""),
        "call_id": call_id,
        "sequence": event_context.sequence if event_context is not None else 0,
        "revision": session.revision if session is not None else 0,
        "owner": session.owner if session is not None else "",
        "target_device_id": store.get("target_device_id", ""),
        "selected_tx_format": store.get("selected_tx_format", ""),
        "selected_rx_format": store.get("selected_rx_format", ""),
        "selected_tx_rtp_format": store.get("selected_tx_rtp_format", ""),
        "selected_rx_rtp_format": store.get("selected_rx_rtp_format", ""),
        "audio_mode": store.get("audio_mode", ""),
        "audio_direction": store.get("audio_direction", "sendrecv"),
        "audio_connection_held": bool(store.get("audio_connection_held", False)),
        "video_active": video_active,
        "video_offered": video_offered,
        "video_requested": video_requested,
        "video_negotiated": video_negotiated,
        "video_status": video_status,
        "video_failure_reason": str(store.get("video_failure_reason") or ""),
        "video_format": store.get("video_format", ""),
        "video_send_format": store.get(
            "video_send_format", store.get("video_format", "")
        ),
        "video_receive_format": store.get(
            "video_receive_format", store.get("video_format", "")
        ),
        "video_direction": store.get("video_direction", "inactive"),
        "auto_answer": _ha_softphone_auto_answer(hass, endpoint_id),
        "send_video": _ha_softphone_send_video(hass, endpoint_id),
        "video_camera_send_enabled": bool(
            transport_config.get(CONF_VIDEO_CAMERA_SEND, False)
        ),
        "video_transcoding_enabled": bool(
            transport_config.get(CONF_VIDEO_TRANSCODING, False)
        ),
        "connected_at": float(store.get("connected_at", 0.0) or 0.0),
        "sip_transport": store.get("sip_transport", "udp+tcp"),
        "sip_status_code": last_status,
        "terminal_reason": store.get("terminal_reason", ""),
        "last_sip_event": last_event,
        "last_sip_reason": store.get("last_sip_reason", "") or runtime["last_sip_reason"],
        "sip_udp_ready": runtime["sip_udp_ready"],
        "sip_tcp_ready": runtime["sip_tcp_ready"],
        "pending_transactions": runtime["pending_transactions"],
        "active_dialogs": runtime["active_dialogs"],
        "pending_call_ids": runtime["pending_call_ids"],
        "active_call_ids": runtime["active_call_ids"],
        "rtp_tx_packets": _runtime_counter(store, runtime, "rtp_tx_packets"),
        "rtp_rx_packets": _runtime_counter(store, runtime, "rtp_rx_packets"),
        "rtp_tx_bytes": _runtime_counter(store, runtime, "rtp_tx_bytes"),
        "rtp_rx_bytes": _runtime_counter(store, runtime, "rtp_rx_bytes"),
        "rtp_dropped_packets": runtime["rtp_dropped_packets"],
        # Topology and trunk addresses belong to opt-in diagnostics. The
        # authenticated softphone state stream otherwise needs call state and
        # aggregate counters only.
        "rtp_relays": runtime["rtp_relays"] if debug_mode else {},
        "sip_client_dialogs": runtime["sip_client_dialogs"] if debug_mode else {},
        "sip_trunk": runtime["sip_trunk"] if debug_mode else {},
        **{
            key: _runtime_counter(store, runtime, key)
            for key in _MEDIA_COUNTER_KEYS
            if key.startswith("video_")
        },
        "debug_mode": debug_mode,
        "media_debug": media_debug,
    }


def _publish_ha_softphone_state(
    hass: HomeAssistant,
    state: dict[str, Any] | None = None,
    *,
    endpoint_id: str,
) -> None:
    """Publish one complete authoritative HA softphone snapshot."""
    endpoint_id = _endpoint_store_id(
        (state or {}).get("endpoint_id") or endpoint_id
    )
    payload = (
        dict(state)
        if state is not None
        else _ha_softphone_state(hass, endpoint_id)
    )
    payload["endpoint_id"] = endpoint_id
    call_id = str(
        payload.get("call_id") or payload.get("last_terminal_call_id") or ""
    )
    context = call_registry(hass).ha_context(call_id)
    _async_fire_with_context(
        hass,
        HA_SOFTPHONE_STATE_EVENT,
        payload,
        context,
    )


def _set_ha_softphone_call_state(
    hass: HomeAssistant,
    state: str,
    *,
    session_device_id: str = "",
    caller: str = "",
    callee: str = "",
    peer_name: str = "",
    direction: str = "",
    call_id: str = "",
    endpoint_id: str,
    **extra: Any,
) -> None:
    endpoint_id = _endpoint_store_id(endpoint_id)
    store = _ha_softphone_store(hass, endpoint_id)
    state = _sip_public_state(state)
    local_name = str(extra.pop("local_name", _ha_peer_name(hass)))
    terminal = state in {
        CallState.IDLE.value,
        CallState.BUSY.value,
        CallState.DECLINED.value,
        CallState.CANCELLED.value,
        CallState.MEDIA_INCOMPATIBLE.value,
        CallState.TRANSPORT_UNREACHABLE.value,
        CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "error",
    }
    canonical = _canonical_session_fields(
        state=state,
        local_name=local_name,
        peer_name=peer_name,
        caller=caller or str(store.get("caller", "")),
        callee=callee or str(store.get("callee", "")),
        direction=direction or str(store.get("direction", "")),
        call_id=call_id or str(store.get("call_id", "")),
    )
    previous_call_id = str(store.get("call_id") or "")
    next_call_id = str(canonical.get("call_id") or "")
    previous_state = str(store.get("state") or "").strip().lower()
    registry = call_registry(hass)
    previous_call_is_active = _registry_call_is_active(registry, previous_call_id)
    if not terminal and next_call_id and registry.is_terminated(next_call_id):
        _LOGGER.info(
            "Ignoring stale HA softphone state=%s for terminated call_id=%s",
            state,
            next_call_id,
        )
        return
    if state == CallState.IN_CALL.value:
        if next_call_id != previous_call_id or previous_state != CallState.IN_CALL.value:
            extra.setdefault("connected_at", time.time())
        elif store.get("connected_at"):
            extra.setdefault("connected_at", store["connected_at"])
    if not terminal:
        if "video_requested" not in extra and "video_offered" in extra:
            extra["video_requested"] = bool(extra.get("video_offered"))
        if "video_negotiated" not in extra and "video_active" in extra:
            extra["video_negotiated"] = bool(extra.get("video_active"))
        if "video_status" not in extra:
            if extra.get("video_failure_reason"):
                extra["video_status"] = "degraded"
            elif extra.get("video_active"):
                extra["video_status"] = "active"
            elif extra.get("video_offered"):
                extra["video_status"] = "offered"
            elif extra.get("video_requested"):
                extra["video_status"] = "requested"
    if (
        previous_call_id
        and next_call_id
        and next_call_id != previous_call_id
        and previous_state
        in {
            CallState.CALLING.value,
            CallState.REMOTE_RINGING.value,
            CallState.RINGING.value,
            CallState.CONNECTING.value,
            CallState.IN_CALL.value,
            CallState.TERMINATING.value,
        }
        and previous_call_is_active
    ):
        _LOGGER.info(
            "Ignoring stale HA softphone state=%s call_id=%s; active call_id=%s state=%s",
            state,
            next_call_id,
            previous_call_id,
            previous_state,
        )
        return
    if (
        previous_call_id
        and next_call_id
        and next_call_id != previous_call_id
        and not previous_call_is_active
    ):
        _LOGGER.warning(
            "Replacing orphaned HA softphone call_id=%s state=%s with call_id=%s state=%s",
            previous_call_id,
            previous_state,
            next_call_id,
            state,
        )
        registry.release_endpoint_claims(previous_call_id)
        endpoint_registry = _endpoint_registry(hass)
        if endpoint_registry is not None and endpoint_registry.get(endpoint_id) is not None:
            endpoint_registry.release_call(endpoint_id, previous_call_id)
    _endpoint_call_claim(
        hass,
        endpoint_id,
        next_call_id or previous_call_id,
        terminal=terminal,
    )
    if terminal:
        store["terminal_reason"] = extra.get("reason") or extra.get("terminal_reason") or state
        store["sip_status_code"] = extra.get("code") or extra.get("sip_status_code") or store.get("sip_status_code", "")
        store["last_terminal_call_id"] = canonical.get("call_id", "")
        store["last_terminal_direction"] = canonical.get("direction", "")
        store["last_terminal_caller"] = canonical.get("caller", "")
        store["last_terminal_callee"] = canonical.get("callee", "")
        store["last_terminal_peer_name"] = canonical.get("peer_name", "")
        store["last_terminal_dialed_target"] = (
            extra.get("dialed_target")
            or store.get("dialed_target")
            or canonical.get("callee", "")
        )
        if extra.get("last_sip_event"):
            store["last_sip_event"] = extra["last_sip_event"]
        if extra.get("last_sip_reason"):
            store["last_sip_reason"] = extra["last_sip_reason"]
        for key in (
            "session_device_id",
            "caller",
            "callee",
            "local_name",
            "peer_name",
            "dialed_target",
            "connected_party",
            "answered_by",
            "direction",
            "role",
            "call_id",
            "target_device_id",
            "selected_tx_format",
            "selected_rx_format",
            "selected_tx_rtp_format",
            "selected_rx_rtp_format",
            "audio_mode",
            "audio_direction",
            "audio_connection_held",
            "video_active",
            "video_offered",
            "video_requested",
            "video_negotiated",
            "video_status",
            "video_failure_reason",
            "video_format",
            "video_send_format",
            "video_receive_format",
            "video_direction",
            "connected_at",
            "route_kind",
            "sip_uri",
            "media_debug",
        ):
            store.pop(key, None)
        store["state"] = state
        store["sip_state"] = state
    else:
        if next_call_id and next_call_id != previous_call_id:
            for key in _MEDIA_COUNTER_KEYS:
                store[key] = 0
            store["media_debug"] = {}
        for key in (
            "last_terminal_call_id",
            "last_terminal_direction",
            "last_terminal_caller",
            "last_terminal_callee",
            "last_terminal_peer_name",
            "last_terminal_dialed_target",
        ):
            store.pop(key, None)
        store.update(canonical)
        store["session_device_id"] = session_device_id
        store["state"] = state
        store["sip_state"] = state
        store["terminal_reason"] = ""
        for key, value in extra.items():
            if key == "video_failure_reason":
                store[key] = str(value or "")
            elif value not in (None, ""):
                store[key] = value

    payload = {
        "endpoint_id": endpoint_id,
        "device_id": store.get("device_id", ""),
        "session_device_id": session_device_id or "",
    }
    payload.update(canonical)
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    if terminal and store.get("terminal_reason"):
        payload["terminal_reason"] = store["terminal_reason"]
    if terminal and store.get("last_terminal_dialed_target"):
        payload["dialed_target"] = store["last_terminal_dialed_target"]
    _LOGGER.info(
        "HA softphone state=%s call_id=%s direction=%s caller=%s callee=%s peer=%s reason=%s event=%s",
        state,
        canonical.get("call_id", ""),
        canonical.get("direction", ""),
        canonical.get("caller", ""),
        canonical.get("callee", ""),
        canonical.get("peer_name", ""),
        store.get("terminal_reason", "") if terminal else "",
        extra.get("last_sip_event", store.get("last_sip_event", "")),
    )
    _fire_call_event(hass, payload, "session")
    # Non-idle terminal states are lifecycle events, not durable phone states.
    # Keep the reason/last-call metadata for the card's short result message,
    # but expose the HA endpoint as immediately ready for another call.
    if terminal and state != CallState.IDLE.value:
        store["state"] = CallState.IDLE.value
        store["sip_state"] = CallState.IDLE.value
    _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)


def _set_sip_bridge_call_state(
    hass: HomeAssistant,
    state: str,
    *,
    call_id: str = "",
    dest_call_id: str = "",
    caller: str = "",
    callee: str = "",
    peer_name: str = "",
    target: str = "",
    **extra: Any,
) -> None:
    """Publish SIP bridge/B2BUA state without mutating the HA softphone session."""
    store = _sip_bridge_store(hass)
    state = _sip_public_state(state)
    terminal = state in {
        CallState.IDLE.value,
        CallState.BUSY.value,
        CallState.DECLINED.value,
        CallState.CANCELLED.value,
        CallState.MEDIA_INCOMPATIBLE.value,
        CallState.TRANSPORT_UNREACHABLE.value,
        CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "error",
    }
    payload = {
        "state": state,
        "sip_state": state,
        "call_id": call_id,
        "dest_call_id": dest_call_id,
        "caller": caller,
        "callee": callee,
        "peer_name": peer_name or target or callee,
        "target": target or callee,
        "terminal_reason": extra.get("terminal_reason") or extra.get("reason") or "",
    }
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    store.update(payload)
    if terminal:
        store["last_terminal_call_id"] = call_id
        store["last_terminal_dest_call_id"] = dest_call_id
    _LOGGER.info(
        "SIP bridge state=%s call_id=%s dest_call_id=%s caller=%s callee=%s target=%s reason=%s event=%s",
        state,
        call_id,
        dest_call_id,
        caller,
        callee,
        target,
        payload.get("terminal_reason", ""),
        payload.get("last_sip_event", ""),
    )
    _fire_call_event(hass, payload, "sip_bridge")


def _ha_softphone_device(
    hass: HomeAssistant,
    endpoint_id: str,
) -> dict[str, Any]:
    endpoint_id = _endpoint_store_id(endpoint_id)
    state = _ha_softphone_state(hass, endpoint_id)
    return {
        "endpoint_id": endpoint_id,
        "endpoint_type": EndpointKind.BROWSER.value,
        "device_id": state.get("device_id") or "",
        "name": state.get("name") or _ha_peer_name(hass),
        "extension": state.get("extension", ""),
        "availability": (
            EndpointAvailability.AVAILABLE.value
            if state.get("available")
            else EndpointAvailability.OFFLINE.value
        ),
        "capabilities": list(state.get("capabilities") or ()),
        "host": "",
        "sip_transport": "udp+tcp",
        "softphone": True,
        "ha_softphone": state,
    }


def _ha_softphone_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    registry = _endpoint_registry(hass)
    if registry is None:
        return []
    devices = [
        _ha_softphone_device(hass, endpoint.endpoint_id)
        for endpoint in registry.endpoints
        if endpoint.kind is EndpointKind.BROWSER
    ]
    return devices


async def _get_voip_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    from .device_resolver import get_resolver

    devices = await get_resolver(hass).list_devices()
    registry = _endpoint_registry(hass)
    if registry is None:
        return devices

    discovered_ids: set[str] = set()
    for device in devices:
        device_id = str(device.get("device_id") or "").strip()
        name = str(device.get("name") or "").strip()
        if not device_id or not name:
            continue
        endpoint_id = f"esphome:{device_id}"
        discovered_ids.add(endpoint_id)
        previous = registry.get(endpoint_id)
        capabilities = frozenset(
            str(value).strip().casefold()
            for value in (device.get("capabilities") or ("audio", "dtmf"))
            if str(value).strip()
        )
        endpoint = PhoneEndpoint(
            endpoint_id=endpoint_id,
            name=name,
            kind=EndpointKind.ESPHOME,
            extension=str(device.get("extension") or ""),
            username=str(device.get("sip_uri_user") or ""),
            device_id=device_id,
            entity_ids=frozenset(
                str(value)
                for value in (device.get("entities") or {}).values()
                if isinstance(value, str) and "." in value
            ),
            availability=EndpointAvailability.AVAILABLE,
            capabilities=capabilities,
            ring_group=str(device.get("ring_group") or ""),
            conference_group=str(device.get("conference_group") or ""),
            conference_ring=bool(device.get("conference_ring", False)),
            active_call_id=(previous.active_call_id if previous is not None else ""),
        )
        try:
            registry.upsert(endpoint)
        except ValueError as err:
            _LOGGER.warning(
                "Skipping ESPHome phone endpoint device_id=%s name=%s: %s",
                device_id,
                name,
                err,
            )
            continue
        device["endpoint_id"] = endpoint_id
        device["endpoint_type"] = EndpointKind.ESPHOME.value
        device["capabilities"] = sorted(capabilities)

    # Keep physical devices represented when temporarily unreachable; only
    # their transport availability changes. VoIP Stack never creates/adopts
    # their Device Registry entry.
    for endpoint in registry.endpoints:
        if (
            endpoint.kind is EndpointKind.ESPHOME
            and endpoint.endpoint_id not in discovered_ids
            and endpoint.availability is not EndpointAvailability.OFFLINE
        ):
            registry.update(
                endpoint.endpoint_id,
                availability=EndpointAvailability.OFFLINE,
            )
    return devices


def _endpoint_id_from_selector(
    hass: HomeAssistant,
    *,
    endpoint_id: object = "",
    device_id: object = "",
    entity_id: object = "",
) -> str:
    """Resolve endpoint, Device or Entity selectors to one browser phone."""

    def _values(raw: object) -> tuple[str, ...]:
        if isinstance(raw, (list, tuple, set, frozenset)):
            values = raw
        else:
            values = (raw,)
        return tuple(
            text
            for value in values
            if (text := str(value or "").strip())
        )

    explicit = str(endpoint_id or "").strip()
    registry = _endpoint_registry(hass)
    selected_ids: set[str] = set()
    unresolved: list[str] = []

    def _by_current_entity_id(entity_id: str):
        endpoint = registry.by_entity_id(entity_id) if registry is not None else None
        if endpoint is not None:
            return endpoint
        # Entity IDs are user-renamable. Resolve the live Entity Registry row
        # back through its Device instead of relying solely on the ID captured
        # when the entity was first added.
        try:
            from homeassistant.helpers import entity_registry as er

            entity_entry = er.async_get(hass).async_get(entity_id)
        except (AttributeError, ImportError):
            entity_entry = None
        device = str(getattr(entity_entry, "device_id", "") or "")
        return registry.by_device_id(device) if registry is not None and device else None

    for device in _values(device_id):
        endpoint = registry.by_device_id(device) if registry is not None else None
        if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
            unresolved.append(device)
            continue
        selected_ids.add(endpoint.endpoint_id)

    for entity in _values(entity_id):
        endpoint = _by_current_entity_id(entity)
        if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
            unresolved.append(entity)
            continue
        selected_ids.add(endpoint.endpoint_id)

    if explicit:
        resolved_id = _endpoint_store_id(explicit)
        if registry is not None:
            endpoint = registry.get(resolved_id)
            if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
                raise ValueError(f"Unknown Home Assistant phone endpoint: {explicit}")
            # EndpointRegistry lookup is case-insensitive; every downstream
            # store, event and presence key must nevertheless use the stable
            # canonical identity owned by the registry.
            resolved_id = endpoint.endpoint_id
        else:
            raise ValueError(f"Unknown Home Assistant phone endpoint: {explicit}")
        if unresolved:
            raise ValueError(
                "Unknown Home Assistant phone selector: " + ", ".join(unresolved)
            )
        if selected_ids and selected_ids != {resolved_id}:
            raise ValueError(
                f"Home Assistant phone endpoint {explicit} does not match the "
                "selected Device or Entity"
            )
        return resolved_id

    if unresolved:
        raise ValueError(
            "Unknown Home Assistant phone selector: " + ", ".join(unresolved)
        )
    if len(selected_ids) > 1:
        raise ValueError("Selected Devices or Entities belong to different HA phones")
    if not selected_ids:
        endpoint = preferred_browser_phone(hass)
        if endpoint is None:
            raise ValueError("Select a Home Assistant phone")
        return endpoint.endpoint_id
    return next(iter(selected_ids))


def async_register_websocket_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_subscribe_call_events)
    websocket_api.async_register_command(hass, websocket_subscribe_ha_softphone_state)
    websocket_api.async_register_command(hass, websocket_ha_softphone_state)
    websocket_api.async_register_command(hass, websocket_list_devices)
    websocket_api.async_register_command(hass, websocket_resolve_device)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_SUBSCRIBE_CALL_EVENTS,
        vol.Optional("endpoint_id", default=""): str,
        vol.Optional("device_id", default=""): str,
    }
)
@callback
def websocket_subscribe_call_events(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    require_websocket_read(connection)
    msg_id = msg["id"]
    requested_endpoint = str(msg.get("endpoint_id") or "").strip()
    if not requested_endpoint and msg.get("device_id"):
        try:
            requested_endpoint = _endpoint_id_from_selector(
                hass, device_id=msg.get("device_id")
            )
        except ValueError as err:
            connection.send_error(msg_id, "unknown_endpoint", str(err))
            return
    elif requested_endpoint:
        try:
            requested_endpoint = _endpoint_id_from_selector(
                hass, endpoint_id=requested_endpoint
            )
        except ValueError as err:
            connection.send_error(msg_id, "unknown_endpoint", str(err))
            return
    selected_endpoint = _browser_endpoint(hass, requested_endpoint)
    if selected_endpoint is not None:
        require_websocket_endpoint_read(hass, connection, selected_endpoint)

    @callback
    def forward_call_event(event) -> None:
        event_endpoint = str(event.data.get("endpoint_id") or "").strip()
        if requested_endpoint:
            if event_endpoint and event_endpoint != requested_endpoint:
                return
            if not event_endpoint:
                return
        connection.send_event(msg_id, {"event_type": CALL_EVENT, "data": event.data})

    connection.subscriptions[msg_id] = hass.bus.async_listen(CALL_EVENT, forward_call_event)
    connection.send_result(msg_id)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_SUBSCRIBE_HA_SOFTPHONE,
        vol.Optional("endpoint_id", default=""): str,
        vol.Optional("device_id", default=""): str,
    }
)
@callback
def websocket_subscribe_ha_softphone_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Stream authoritative HA softphone snapshots."""
    require_websocket_read(connection)
    msg_id = msg["id"]
    try:
        requested_endpoint = _endpoint_id_from_selector(
            hass,
            endpoint_id=msg.get("endpoint_id"),
            device_id=msg.get("device_id"),
        )
    except ValueError as err:
        connection.send_error(msg_id, "unknown_endpoint", str(err))
        return
    selected_endpoint = _browser_endpoint(hass, requested_endpoint)
    if selected_endpoint is not None:
        require_websocket_endpoint_read(hass, connection, selected_endpoint)
    publishes_presence = websocket_can_control_endpoint(
        hass,
        connection,
        selected_endpoint,
    )

    @callback
    def forward_state(event: Event) -> None:
        event_endpoint = str(event.data.get("endpoint_id") or "").strip()
        if event_endpoint != requested_endpoint:
            return
        connection.send_event(msg_id, event.data)

    unsubscribe_state = hass.bus.async_listen(
        HA_SOFTPHONE_STATE_EVENT, forward_state
    )
    if publishes_presence:
        _update_browser_presence(hass, requested_endpoint, 1)
    released = False

    @callback
    def unsubscribe() -> None:
        nonlocal released
        unsubscribe_state()
        if released:
            return
        released = True
        if publishes_presence:
            _update_browser_presence(hass, requested_endpoint, -1)

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_HA_SOFTPHONE_STATE,
        vol.Optional("endpoint_id", default=""): str,
        vol.Optional("device_id", default=""): str,
        vol.Optional("media_client_id", default=""): vol.Any(
            "",
            vol.Match(r"^[A-Za-z0-9][A-Za-z0-9._~-]{15,127}$"),
        ),
    }
)
@websocket_api.async_response
async def websocket_ha_softphone_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    require_websocket_read(connection)
    try:
        endpoint_id = _endpoint_id_from_selector(
            hass,
            endpoint_id=msg.get("endpoint_id"),
            device_id=msg.get("device_id"),
        )
    except ValueError as err:
        connection.send_error(msg["id"], "unknown_endpoint", str(err))
        return
    selected_endpoint = _browser_endpoint(hass, endpoint_id)
    if selected_endpoint is not None:
        require_websocket_endpoint_read(hass, connection, selected_endpoint)
    state = _ha_softphone_state(hass, endpoint_id)
    media_client_id = str(msg.get("media_client_id") or "")
    if media_client_id:
        owner_status = media_websocket_owner_status(
            require_runtime_data(hass).media,
            call_registry(hass),
            str(state.get("call_id") or ""),
            endpoint_id,
            media_client_id,
        )
        controller_status = media_controller_status(
            call_registry(hass),
            str(state.get("call_id") or ""),
            endpoint_id,
            str(getattr(connection.user, "id", "") or ""),
        )
        state["media_owner"] = (
            "other" if controller_status == "other" else owner_status
        )
    connection.send_result(msg["id"], state)


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST})
@websocket_api.async_response
async def websocket_list_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    require_websocket_read(connection)
    connection.send_result(
        msg["id"],
        {"devices": [*_ha_softphone_devices(hass), *(await _get_voip_devices(hass))]},
    )


@websocket_api.websocket_command(
    {vol.Required("type"): WS_TYPE_RESOLVE_DEVICE, vol.Required("device_id"): str}
)
@websocket_api.async_response
async def websocket_resolve_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    require_websocket_read(connection)
    device_id = str(msg.get("device_id") or "")
    registry = _endpoint_registry(hass)
    endpoint = registry.by_device_id(device_id) if registry is not None else None
    if endpoint is not None and endpoint.kind is EndpointKind.BROWSER:
        require_websocket_endpoint_read(hass, connection, endpoint)
        connection.send_result(
            msg["id"],
            {"device": _ha_softphone_device(hass, endpoint.endpoint_id)},
        )
        return
    for device in await _get_voip_devices(hass):
        if str(device.get("device_id") or "") == device_id:
            connection.send_result(msg["id"], {"device": device})
            return
    connection.send_result(msg["id"], {"device": None})
