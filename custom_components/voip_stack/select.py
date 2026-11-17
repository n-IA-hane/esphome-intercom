"""Native controls for each queue/operator pair, owned by QueueManager."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .call_queues import get_queue_manager
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Follow membership and state without another operational store."""
    manager = get_queue_manager(hass)
    if manager is None:
        return
    entities = {}

    async def remove(entity):
        entity_id = entity.entity_id
        await entity.async_remove(force_remove=True)
        registry = er.async_get(hass)
        if entity_id and registry.async_get(entity_id) is not None:
            registry.async_remove(entity_id)

    @callback
    def reconcile():
        wanted = {(queue.queue_id, member) for queue in manager.definitions.values() for member in queue.members}
        for identity in entities.keys() - wanted:
            entity = entities.pop(identity)
            if entity.entity_id is not None:
                hass.async_create_task(remove(entity))
        for queue_id, endpoint_id in sorted(wanted):
            identity = (queue_id, endpoint_id)
            if identity not in entities:
                entity = entities[identity] = QueueAgentStateSelect(hass, manager, queue_id, endpoint_id)
                subentry_id = next((item.subentry_id for item in entry.subentries.values()
                    if item.subentry_type == "group" and item.data.get("group_id") == queue_id), None)
                async_add_entities([entity], config_subentry_id=subentry_id)
            else:
                entities[identity].apply_state()

    entry.async_on_unload(manager.subscribe(reconcile))
    reconcile()


class QueueAgentStateSelect(SelectEntity):
    """Select persistent operator intent while displaying actual eligibility."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "queue_agent_state"
    _attr_icon = "mdi:account-clock"
    _attr_options = ["available", "paused", "logged_out"]

    def __init__(self, hass, manager, queue_id, endpoint_id):
        self.hass, self.manager = hass, manager
        self.queue_id, self.endpoint_id = queue_id, endpoint_id
        self._attr_unique_id = f"queue_agent:{queue_id}:{endpoint_id}"
        self._attr_translation_placeholders = {}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, f"group:{self.queue_id}")})
        self.apply_state()

    @property
    def available(self):
        queue = self.manager.resolve(self.queue_id)
        return not self.manager.closed and queue is not None and self.endpoint_id in queue.members

    @property
    def current_option(self):
        return self.manager.agent_states.get(self.queue_id, {}).get(self.endpoint_id)

    @property
    def extra_state_attributes(self):
        if not self.available:
            return {"queue_id": self.queue_id, "phone": self.endpoint_id}
        state = self.manager.get_state(self.queue_id, self.endpoint_id)
        return {"queue_id": self.queue_id, **state["members"][0]}

    @callback
    def apply_state(self):
        endpoint = self.manager.endpoints.get(self.endpoint_id)
        queue = self.manager.resolve(self.queue_id)
        self._attr_translation_placeholders["phone"] = endpoint.name if endpoint is not None else self.endpoint_id
        self._attr_device_info["name"] = queue.name if queue is not None else self.queue_id
        if self.entity_id is not None:
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        await self.hass.services.async_call(DOMAIN, "set_queue_agent_state", {
            "queue": self.queue_id, "phone": self.endpoint_id, "state": option,
        }, blocking=True, context=self._context)
