"""Update existing browser phone views, persist settings and drain owned media."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import (
    HomeAssistant,
    callback,
)

from .endpoint_lifecycle import call_registry
from .counterpart_display import counterpart_display
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state as _sip_public_state,
)
from .phone_config import (
    CONF_PHONE_AUTO_ANSWER,
    CONF_PHONE_CONFERENCE_GROUP,
    CONF_PHONE_CONFERENCE_RING,
    CONF_PHONE_DND,
    CONF_PHONE_EXTENSION,
    CONF_PHONE_RING_GROUP,
    CONF_PHONE_QUEUES,
    CONF_PHONE_SEND_VIDEO,
    phone_subentry_by_endpoint_id,
    phone_subentry_data,
    update_phone_subentry,
)
from .phone_queue_membership import normalize_phone_queues
from .runtime_data import (
    require_runtime_data,
    runtime_data,
)
from .websocket_owner import async_revoke_media_owners
from .const import HA_SOFTPHONE_STATE_EVENT as HA_SOFTPHONE_STATE_EVENT
from .call_events import (
    home_phone_name as _ha_peer_name,
    canonical_session_fields as _canonical_session_fields,
    fire_with_call_context as _async_fire_with_context,
    fire_call_event as _fire_call_event,
)
from .browser_phone_data import (
    _clean_group_name,
    _clean_endpoint_field,
    _endpoint_store_id,
    _ha_softphone_store,
    _ha_softphone_stores,
    _browser_endpoint,
    _ha_softphone_groups,
    _MEDIA_COUNTER_KEYS,
    _CALL_SCOPED_SOFTPHONE_FIELDS,
)
from .browser_phone_projection import _ha_softphone_state

_LOGGER = logging.getLogger(f"{__package__}.websocket_api")


@callback
def _update_browser_presence(
    hass: HomeAssistant,
    endpoint_id: str,
    delta: int,
    *, publish: bool = True,
) -> None:
    """Track browser media clients independently of logical phone availability."""
    endpoint_id = _endpoint_store_id(endpoint_id)
    counts = require_runtime_data(hass).softphone_presence
    previous = int(counts.get(endpoint_id, 0) or 0)
    current = max(0, previous + int(delta))
    if current:
        counts[endpoint_id] = current
    else:
        counts.pop(endpoint_id, None)

    if previous != current and publish:
        _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)


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
    registry = call_registry(hass)
    session_id = registry.resolve_session_id(str(call_id or "").strip())
    session = registry.sessions.get(session_id)
    if session is None or not registry.release_endpoint_claim(call_id, endpoint_id):
        return False
    from .call_projection import observe_phone_leg_projection

    return observe_phone_leg_projection(
        hass,
        registry,
        session,
        endpoint_id,
        CallState.CANCELLED.value,
        leg_id=f"browser:{endpoint_id}",
        peer_name=str(store.get("peer_name") or ""),
        direction=str(store.get("direction") or "incoming"),
        reason=TerminalReason.FORWARDED.value,
        terminal_reason=TerminalReason.FORWARDED.value,
        dialed_target=destination,
        origin="automation",
        last_sip_event="ROUTE_FORWARD",
    )


async def async_set_ha_softphone_settings(
    hass: HomeAssistant,
    *,
    endpoint_id: str,
    extension: object = None,
    ring_group: object = None,
    queues: object = None,
    conference_group: object = None,
    conference_ring: object = None,
    auto_answer: object = None,
    send_video: object = None,
) -> dict[str, Any]:
    endpoint_id = _endpoint_store_id(endpoint_id)
    updates = {}
    if extension is not None:
        updates[CONF_PHONE_EXTENSION] = _clean_endpoint_field(extension)
    if queues is not None:
        updates[CONF_PHONE_QUEUES] = list(normalize_phone_queues(queues))
    if ring_group is not None:
        updates[CONF_PHONE_RING_GROUP] = _clean_group_name(ring_group)
    if conference_group is not None:
        updates[CONF_PHONE_CONFERENCE_GROUP] = _clean_group_name(conference_group)
    if conference_ring is not None:
        updates[CONF_PHONE_CONFERENCE_RING] = bool(conference_ring)
    if auto_answer is not None:
        updates[CONF_PHONE_AUTO_ANSWER] = bool(auto_answer)
    if send_video is not None:
        updates[CONF_PHONE_SEND_VIDEO] = bool(send_video)
    await _async_save_ha_softphone_store(hass, endpoint_id, updates)
    state = _ha_softphone_state(hass, endpoint_id)
    _publish_ha_softphone_state(hass, state, endpoint_id=endpoint_id)
    return state


async def _async_shutdown_all(hass: HomeAssistant) -> None:
    """Clear HA softphone volatile state before SIP transports are stopped."""
    registry = call_registry(hass)
    for _call_id, route in registry.artifact_items("pending_route"):
        future = route.get("future") if isinstance(route, dict) else None
        if future is not None and not future.done():
            future.cancel()
    runtime = runtime_data(hass)
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
        _reset_ha_softphone_store(hass, endpoint_id, store, event="shutdown")


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
    runtime["queues"] = list(normalize_phone_queues(data.get(CONF_PHONE_QUEUES, runtime.get("queues"))))
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
    hass: HomeAssistant, endpoint_id: str, updates: dict[str, Any],
) -> None:
    """Persist only the requested fields; the browser store is a projection."""
    from homeassistant.exceptions import ServiceValidationError
    from .store import config_entry

    endpoint_id = _endpoint_store_id(endpoint_id)
    entry = config_entry(hass)
    subentry = phone_subentry_by_endpoint_id(entry, endpoint_id) if entry is not None else None
    if subentry is None:
        raise ServiceValidationError("This phone is no longer configured. Reload the phone list and choose a configured SIP account.")
    update_phone_subentry(hass, entry, endpoint_id, updates)
    await _async_load_ha_softphone_store(hass, entry, endpoint_id=endpoint_id,
                                        endpoint_data=phone_subentry_data(subentry))


def _publish_ha_softphone_state(
    hass: HomeAssistant,
    state: dict[str, Any] | None = None,
    *,
    endpoint_id: str,
) -> None:
    """Publish one complete authoritative HA softphone snapshot."""
    endpoint_id = _endpoint_store_id((state or {}).get("endpoint_id") or endpoint_id)
    payload = (
        dict(state) if state is not None else _ha_softphone_state(hass, endpoint_id)
    )
    payload["endpoint_id"] = endpoint_id
    call_id = str(payload.get("call_id") or payload.get("last_terminal_call_id") or "")
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
    previous_call_id = str(store.get("call_id") or "")
    call_id = call_id or previous_call_id
    if previous_call_id and call_id != previous_call_id:
        for key in ("caller", "callee", "direction", "peer_name", "connected_party", "connected_uri", "connected_display_name", "caller_name", "answered_by", "dialed_target"):
            store.pop(key, None)

    selected_direction = direction or str(store.get("direction", ""))
    peer_name = peer_name or extra.get("connected_party") or extra.get("answered_by")
    if peer_name:
        # A new explicit identity supersedes older display aliases for this call.
        for key in ("connected_party", "answered_by", "connected_display_name", "caller_name"):
            if key == "caller_name" and selected_direction == "incoming":
                if caller and store.get("caller") and caller != store["caller"] and not extra.get(key):
                    store.pop(key, None)
                continue
            if not extra.get(key) and store.get(key) != peer_name:
                store.pop(key, None)
    elif call_id and call_id == previous_call_id:
        peer_name = store.get("peer_name", "")
    elif terminal and call_id and call_id == store.get("last_terminal_call_id"):
        peer_name = store.get("last_terminal_peer_name", "")

    runtime = require_runtime_data(hass)
    session = runtime.sip.get_session(call_id) if runtime.sip is not None else None
    metadata = session.metadata if session is not None else {}
    peer_id = metadata.get("dest_endpoint_id") or metadata.get("target_endpoint_id") if selected_direction == "outgoing" else metadata.get("source_endpoint_id")
    name_key = "connected_display_name" if selected_direction == "outgoing" else "caller_name"
    display_name = extra.get(name_key) or metadata.get(name_key) or store.get(name_key) or ""
    if selected_direction == "incoming":
        if caller and store.get("caller") and caller != store["caller"] and not extra.get(name_key):
            display_name = ""
    elif peer_name and display_name != peer_name:
        display_name = ""
    peer_name = counterpart_display(
        peer_name,
        fallback=(callee or store.get("callee")) if selected_direction == "outgoing" else (caller or store.get("caller")),
        endpoint=runtime.endpoints.get(str(peer_id or "")),
        directory=runtime.directory,
        updated=bool(extra.get("connected_uri") or metadata.get("connected_uri") or store.get("connected_uri")),
        display_name=display_name,
        direction=selected_direction, caller=caller or store.get("caller", ""),
        callee=callee or store.get("callee", ""),
        connected_party=extra.get("connected_party") or store.get("connected_party", ""),
    )
    canonical = _canonical_session_fields(
        state=state,
        local_name=local_name,
        peer_name=peer_name,
        caller=caller or str(store.get("caller", "")),
        callee=callee or str(store.get("callee", "")),
        direction=direction or str(store.get("direction", "")),
        call_id=call_id,
    )
    state = canonical["state"]
    next_call_id = str(canonical.get("call_id") or "")
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
    if terminal:
        store["terminal_reason"] = (
            extra.get("reason") or extra.get("terminal_reason") or state
        )
        store["sip_status_code"] = (
            extra.get("code")
            or extra.get("sip_status_code")
            or store.get("sip_status_code", "")
        )
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
        for key in _CALL_SCOPED_SOFTPHONE_FIELDS:
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
            if key in {"video_failure_reason", "target_device_id", "target_endpoint_id", "connected_display_name", "caller_name"}:
                store[key] = str(value or "")
            elif value not in (None, ""):
                store[key] = value

    payload = {
        "endpoint_id": endpoint_id,
        "device_id": store.get("device_id", ""),
        "session_device_id": session_device_id or "",
    }
    payload.update(canonical)
    payload.update({
        key: str(value or "") if key in {"target_device_id", "target_endpoint_id"} else value
        for key, value in extra.items()
        if value not in (None, "") or key in {"target_device_id", "target_endpoint_id"}
    })
    if state == CallState.IN_CALL.value and session is not None and session.live:
        payload["connected_at"] = session.event_context.connected_at_epoch
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


def _reset_ha_softphone_store(
    hass: HomeAssistant,
    endpoint_id: str,
    store: dict[str, Any] | None = None,
    *,
    event: str,
) -> None:
    """Reset one detached view after its owning sessions have stopped."""

    store = store if store is not None else _ha_softphone_store(hass, endpoint_id)
    for key in _CALL_SCOPED_SOFTPHONE_FIELDS:
        store.pop(key, None)
    store["state"] = CallState.IDLE.value
    store["sip_state"] = CallState.IDLE.value
    store["last_sip_event"] = event
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
    state = _sip_public_state(state)
    payload = {
        "state": state,
        "sip_state": state,
        "call_id": call_id,
        "dest_call_id": dest_call_id,
        "caller": caller,
        "callee": callee,
        "peer_name": counterpart_display(peer_name, fallback=target or callee),
        "target": target or callee,
        "terminal_reason": extra.get("terminal_reason") or extra.get("reason") or "",
    }
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
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
