"""Project ESPHome phone state changes onto the public VoIP call bus."""

from __future__ import annotations

import asyncio

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .device_resolver import entity_role
from .endpoint_lifecycle import create_runtime_task
from .fsm import CallState, sip_public_state
from .outbound_lifecycle import HA_SOFTPHONE_ACTIVE_STATES
from .peer_snapshot import device_entity_state
from .runtime_data import runtime_data
from .websocket_api import _fire_call_event, _get_voip_devices

_TERMINAL_ESP_STATES = {
    "idle",
    "ended",
    "busy",
    "declined",
    "cancelled",
    "local_hangup",
    "remote_hangup",
    "not_in_call",
    "timeout",
    "error",
}


async def async_emit_state_event(
    hass: HomeAssistant,
    entity_id: str,
    state: str,
    old_state: str,
    delay: float = 0.0,
    *,
    generation: int = 0,
    expected_endpoint_id: str = "",
    expected_call_id: str = "",
) -> None:
    """Mirror an ESP-published ``voip_state`` change onto the call bus."""
    if delay > 0:
        await asyncio.sleep(delay)
    runtime = runtime_data(hass)
    if runtime is None:
        return
    if generation and int(runtime.esp_state_event_generations.get(entity_id, 0)) != int(
        generation
    ):
        return
    endpoint_registry = runtime.endpoints
    guarded_endpoint = (
        endpoint_registry.get(expected_endpoint_id) if expected_endpoint_id else None
    )
    raw_state = state.strip().lower()
    terminal_state = raw_state in _TERMINAL_ESP_STATES
    if (
        terminal_state
        and guarded_endpoint is not None
        and guarded_endpoint.active_call_id != expected_call_id
    ):
        # The delayed terminal event belongs to an earlier dialog. Never emit
        # it as the state of, or release, a newer call (classic ABA race).
        return
    devices = await _get_voip_devices(hass)
    device = next(
        (
            item
            for item in devices
            if (item.get("entities") or {}).get("voip_state") == entity_id
        ),
        None,
    )
    payload = {
        "state": state,
        "old_state": old_state,
        "entity_id": entity_id,
        "direction": "",
        "call_id": "",
    }
    if device is not None:
        entities = device.get("entities") or {}
        endpoint = guarded_endpoint or endpoint_registry.by_device_id(
            device.get("device_id")
        )
        canonical_state = (
            CallState.RINGING.value
            if raw_state == "incoming"
            else sip_public_state(raw_state)
        )
        if endpoint is not None and (
            canonical_state in HA_SOFTPHONE_ACTIVE_STATES
            or raw_state in _TERMINAL_ESP_STATES
        ):
            active = canonical_state in HA_SOFTPHONE_ACTIVE_STATES
            transport_call_id = (
                expected_call_id
                or endpoint.active_call_id
                or (f"physical:{endpoint.endpoint_id}" if active else "")
            )
            if active:
                endpoint = endpoint_registry.sync_transport_call(
                    endpoint.endpoint_id,
                    active=True,
                    fallback_call_id=f"physical:{endpoint.endpoint_id}",
                )
            elif transport_call_id:
                endpoint_registry.release_call(
                    endpoint.endpoint_id,
                    transport_call_id,
                )
                endpoint = endpoint_registry.require(endpoint.endpoint_id)
            payload["call_id"] = transport_call_id
        caller = device_entity_state(hass, device, "incoming_caller")
        destination = device_entity_state(hass, device, "destination")
        reason = device_entity_state(hass, device, "last_reason")
        payload.update(
            {
                "device_id": device.get("device_id", ""),
                "endpoint_id": str(getattr(endpoint, "endpoint_id", "") or ""),
                "peer_name": device.get("name", ""),
                "local_name": device.get("name", ""),
                "caller": caller,
                "callee": destination,
                "destination": destination,
                "reason": reason,
                "endpoint": device_entity_state(hass, device, "voip_endpoint"),
                "caller_entity_id": entities.get("incoming_caller", ""),
                "destination_entity_id": entities.get("destination", ""),
                "last_reason_entity_id": entities.get("last_reason", ""),
            }
        )
        if raw_state in ("ringing", "incoming"):
            payload["direction"] = "incoming"
        elif raw_state in ("calling", "remote_ringing"):
            payload["direction"] = "outgoing"
    _fire_call_event(hass, payload, "esp")


def register_state_event_bridge(hass: HomeAssistant) -> None:
    """Forward ESP ``voip_state`` entity changes to the VoIP event bus."""
    runtime = runtime_data(hass)
    if runtime is None:
        return
    if runtime.esp_state_event_bridge_unsub is not None:
        return

    @callback
    def _on_state_changed(event: Event) -> None:
        entity_id = str(event.data.get("entity_id") or "")
        entity = er.async_get(hass).async_get(entity_id)
        if entity is None or entity_role(entity) != "voip_state":
            return
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if new is None:
            return
        old_value = "" if old is None else str(old.state or "")
        new_value = str(new.state or "")
        if old_value == new_value:
            return
        if new_value.lower() in ("unknown", "unavailable"):
            return
        runtime = runtime_data(hass)
        if runtime is None:
            return
        generations = runtime.esp_state_event_generations
        generation = int(generations.get(entity_id, 0) or 0) + 1
        generations[entity_id] = generation
        endpoint_registry = runtime.endpoints
        endpoint = endpoint_registry.by_entity_id(entity_id)
        terminal_delay = (
            0.2 if new_value.strip().lower() in ("idle", "ended", "declined") else 0.0
        )
        create_runtime_task(
            hass,
            async_emit_state_event(
                hass,
                entity_id,
                new_value,
                old_value,
                terminal_delay,
                generation=generation,
                expected_endpoint_id=str(getattr(endpoint, "endpoint_id", "") or ""),
                expected_call_id=str(getattr(endpoint, "active_call_id", "") or ""),
            ),
        )

    runtime.esp_state_event_bridge_unsub = hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        _on_state_changed,
    )
