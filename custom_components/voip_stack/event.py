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
    is_logbook_call_summary,
)
from .const import DOMAIN
from .phone_entity import PhoneEndpointEntity
from .endpoint_entity_manager import (
    EndpointEntityManager,
    event_matches_endpoint,
    register_endpoint_entity_manager,
)
from .const import CALL_EVENT, SIP_DTMF_EVENT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up canonical PBX, phone and Automation contact event projections."""
    async_add_entities([VoipStackCallEvent(hass, entry)])
    _setup_automation_contact_events(hass, entry, async_add_entities)
    from .queue_activity import async_setup_queue_activity
    async_setup_queue_activity(hass, entry, async_add_entities)
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
            manufacturer="VoIP Stack",
            model="PBX",
        )

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Preserve subscriptions when Home Assistant renames this entity."""
        super().async_entity_id_changed(old_entity_id)

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
        if is_logbook_call_summary(payload):
            from .logbook import _describe_call

            payload["call_summary"] = _describe_call(payload)["message"]
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


class PhoneEndpointCallEvent(PhoneEndpointEntity, EventEntity):
    """Publish only call lifecycle events involving one logical phone."""

    _attr_event_types = AUTOMATION_EVENT_TYPES
    _attr_translation_key = "phone_endpoint_call"
    _unrecorded_attributes = VoipStackCallEvent._unrecorded_attributes

    def __init__(self, hass, endpoint, registry) -> None:
        super().__init__(endpoint, registry, key="call_event")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(CALL_EVENT, self._on_call_event))
        self.async_on_remove(self.hass.bus.async_listen(SIP_DTMF_EVENT, self._on_dtmf_event))

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
        from .logbook import account_call_details

        direction, peer = account_call_details(payload, self.endpoint.endpoint_id)
        if direction:
            payload["account_direction"] = direction
        if peer:
            payload["peer_name"] = peer
        if is_logbook_call_summary(payload):
            from .logbook import _account_terminal_message

            payload["call_summary"] = _account_terminal_message(payload, self.endpoint.endpoint_id)
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


def _setup_automation_contact_events(hass, entry, async_add_entities):
    """Follow canonical contact records through HA's entry/entity lifecycle."""
    from .automation_contact_device import (
        async_sync_automation_contact_devices,
        automation_contact_records,
        automation_contact_subentry_id,
    )

    entities = {}

    @callback
    def reconcile():
        async_sync_automation_contact_devices(hass, entry)
        contacts = automation_contact_records(entry)
        for identity in entities.keys() - contacts.keys():
            entity = entities.pop(identity)
            if entity.hass is not None:
                hass.async_create_task(entity.async_remove())
        for identity, contact in contacts.items():
            if identity in entities:
                entities[identity].async_update_contact(contact)
            else:
                entity = entities[identity] = AutomationContactCallEvent(hass, entry, contact)
                async_add_entities([entity], config_subentry_id=automation_contact_subentry_id(entry, identity))

    async def entry_updated(_hass, _entry):
        reconcile()

    entry.async_on_unload(entry.add_update_listener(entry_updated))
    reconcile()


class AutomationContactCallEvent(EventEntity):
    """Project actual contact calls without creating a phone or routing owner."""

    _attr_event_types = AUTOMATION_EVENT_TYPES
    _attr_has_entity_name = True
    _attr_name = "Call"
    _attr_icon = "mdi:robot-outline"
    _attr_should_poll = False
    _unrecorded_attributes = VoipStackCallEvent._unrecorded_attributes

    def __init__(self, hass, entry, contact):
        from .automation_contact_device import automation_contact_event_unique_id

        self.hass, self.entry, self.contact = hass, entry, contact
        self._attr_unique_id = automation_contact_event_unique_id(entry.entry_id, contact.id)
        # Projection-only tokens retain terminal display across a contact rename.
        # The canonical call registry remains the sole call/lifecycle owner.
        self._calls = set()
        self._added = False
        self.async_update_contact(contact)

    @callback
    def async_update_contact(self, contact):
        from .automation_contact_device import AUTOMATION_CONTACT_MODEL, automation_contact_device_identifier

        self.contact = contact
        self._attr_available = contact.enabled
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, automation_contact_device_identifier(self.entry.entry_id, contact.id))},
            name=contact.display_name, manufacturer="VoIP Stack", model=AUTOMATION_CONTACT_MODEL,
        )
        if self._added:
            self.async_write_ha_state()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._added = True
        self.async_on_remove(self.hass.bus.async_listen(CALL_EVENT, self._on_call_event))

    async def async_will_remove_from_hass(self):
        self._added = False
        self._calls.clear()
        await super().async_will_remove_from_hass()

    def _matches(self, payload):
        identity = str(payload.get("call_id") or "")
        try:
            token = (identity, int(payload.get("generation") or 0))
        except (TypeError, ValueError):
            return False
        if identity and token in self._calls:
            return True
        if payload.get("source_automation") == self.contact.id:
            return True
        from .runtime_data import runtime_data
        runtime = runtime_data(self.hass)
        registry = runtime.sip if runtime is not None else None
        application = registry.resource_for(identity, "automation") if registry is not None else None
        if application is not None:
            return (application.contact.id == self.contact.id
                    and application.contact.metadata.get("virtual_endpoint") == "automation")
        if payload.get("route_kind") != "automation":
            return False
        aliases = {self.contact.id.casefold(), self.contact.display_name.casefold()}
        if self.contact.extension:
            aliases.add(self.contact.extension.casefold())
        return any(str(payload.get(key) or "").casefold() in aliases
                   for key in ("destination", "callee", "called_extension"))

    @callback
    def _on_call_event(self, event):
        payload = dict(event.data)
        if not self.contact.enabled or not self._matches(payload):
            return
        identity = str(payload.get("call_id") or "")
        token = (identity, int(payload.get("generation") or 0))
        event_type = automation_event_type(payload)
        if identity:
            if is_logbook_call_summary(payload):
                self._calls.discard(token)
            else:
                self._calls.add(token)
        payload["event"] = event_type
        payload["automation_contact_id"] = self.contact.id
        self.async_set_context(event.context)
        self._trigger_event(event_type, payload)
        self.async_write_ha_state()
