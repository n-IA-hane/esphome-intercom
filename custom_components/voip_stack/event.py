"""Automation-native call events for VoIP Stack."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .automation_routing import (
    AUTOMATION_EVENT_TYPES,
    CALL_EVENT_SCHEMA_VERSION,
    automation_event_type,
)
from .const import DOMAIN
from .endpoint_device import async_link_endpoint_entity, endpoint_device_info
from .endpoint_entity_manager import (
    EndpointEntityManager,
    event_matches_endpoint,
    register_endpoint_entity_manager,
)
from .websocket_api import CALL_EVENT, SIP_DTMF_EVENT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the single integration-owned call event entity."""
    async_add_entities([VoipStackCallEvent(hass, entry)])
    endpoint_manager = EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        PhoneEndpointCallEvent,
    )
    endpoint_manager.async_setup()
    register_endpoint_entity_manager(
        entry, "endpoint_call_event_entity_manager", endpoint_manager
    )


class VoipStackCallEvent(EventEntity):
    """Publish canonical SIP lifecycle and routing events to automations."""

    _attr_event_types = AUTOMATION_EVENT_TYPES
    _attr_has_entity_name = False
    _attr_icon = "mdi:phone-sync"
    _attr_name = "VoIP Stack Call"
    _attr_should_poll = False
    _attr_unique_id = "voip_stack_call_event"
    _unrecorded_attributes = frozenset(
        {
            "media_debug",
            "route_history",
            "sdp",
            "sip_trunk",
        }
    )

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entity_id = "event.voip_stack_call"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or "VoIP Stack",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe after HA has assigned the entity."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(CALL_EVENT, self._on_call_event))
        self.async_on_remove(self.hass.bus.async_listen(SIP_DTMF_EVENT, self._on_dtmf_event))

    @callback
    def _on_call_event(self, event: Event) -> None:
        payload = dict(event.data)
        event_type = automation_event_type(payload)
        payload["event"] = event_type
        self.async_set_context(event.context)
        self._trigger_event(event_type, payload)
        self.async_write_ha_state()

    @callback
    def _on_dtmf_event(self, event: Event) -> None:
        payload = dict(event.data)
        payload.setdefault("schema_version", CALL_EVENT_SCHEMA_VERSION)
        payload["event"] = "dtmf"
        self.async_set_context(event.context)
        self._trigger_event("dtmf", payload)
        self.async_write_ha_state()


class PhoneEndpointCallEvent(EventEntity):
    """Publish only call lifecycle events involving one logical phone."""

    _attr_event_types = AUTOMATION_EVENT_TYPES
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "phone_endpoint_call"
    _unrecorded_attributes = VoipStackCallEvent._unrecorded_attributes

    def __init__(self, hass, endpoint, registry) -> None:
        self.endpoint = endpoint
        self.registry = registry
        self._attr_unique_id = f"phone_endpoint_{endpoint.endpoint_id}_call_event"
        self._attr_device_info = endpoint_device_info(endpoint)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        async_link_endpoint_entity(
            self.registry, self.endpoint.endpoint_id, self.entity_id
        )
        self.async_on_remove(self.hass.bus.async_listen(CALL_EVENT, self._on_call_event))
        self.async_on_remove(self.hass.bus.async_listen(SIP_DTMF_EVENT, self._on_dtmf_event))

    @callback
    def apply_endpoint(self, endpoint) -> None:
        self.endpoint = endpoint

    @callback
    def _on_call_event(self, event: Event) -> None:
        payload = dict(event.data)
        if not event_matches_endpoint(
            payload,
            self.endpoint,
            self.registry,
            owner_scoped=str(payload.get("scope") or "") == "session",
        ):
            return
        event_type = automation_event_type(payload)
        payload["event"] = event_type
        payload["endpoint_id"] = self.endpoint.endpoint_id
        self.async_set_context(event.context)
        self._trigger_event(event_type, payload)
        self.async_write_ha_state()

    @callback
    def _on_dtmf_event(self, event: Event) -> None:
        payload = dict(event.data)
        if not event_matches_endpoint(payload, self.endpoint, self.registry):
            return
        payload.setdefault("schema_version", CALL_EVENT_SCHEMA_VERSION)
        payload["event"] = "dtmf"
        payload["endpoint_id"] = self.endpoint.endpoint_id
        self.async_set_context(event.context)
        self._trigger_event("dtmf", payload)
        self.async_write_ha_state()
