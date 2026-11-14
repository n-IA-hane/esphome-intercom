"""Native entity identity and registry linkage shared by phone platforms."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .endpoint_device import async_link_endpoint_entity, endpoint_device_info


class PhoneEndpointEntity(Entity):
    """Keep a phone's HA identity independent from its state/control policy."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, endpoint, registry, *, key: str) -> None:
        super().__init__()
        self.endpoint, self.registry = endpoint, registry
        self._attr_unique_id = f"phone_endpoint_{endpoint.endpoint_id}_{key}"
        self._attr_device_info = endpoint_device_info(endpoint)
        self._linked_entity_id = None

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        if parent := getattr(super(), "async_entity_id_changed", None):
            parent(old_entity_id)
        async_link_endpoint_entity(self.registry, self.endpoint.endpoint_id,
                                    self.entity_id, old_entity_id)
        self._linked_entity_id = self.entity_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._unlink_removed_entity)
        async_link_endpoint_entity(self.registry, self.endpoint.endpoint_id,
                                    self.entity_id, self._linked_entity_id)
        self._linked_entity_id = self.entity_id

    @callback
    def _unlink_removed_entity(self) -> None:
        if not self._linked_entity_id:
            return
        # Renames and unloads retain a native registry row. The next add relinks
        # its current ID; actual registry deletion releases only this entity.
        domain = self._linked_entity_id.partition(".")[0]
        if er.async_get(self.hass).async_get_entity_id(domain, DOMAIN, self.unique_id):
            return
        endpoint = self.registry.get(self.endpoint.endpoint_id)
        if endpoint is not None and self._linked_entity_id in endpoint.entity_ids:
            self.registry.update(endpoint.endpoint_id,
                entity_ids=endpoint.entity_ids - {self._linked_entity_id})
        self._linked_entity_id = None

    @callback
    def apply_endpoint(self, endpoint) -> None:
        self.endpoint = endpoint
