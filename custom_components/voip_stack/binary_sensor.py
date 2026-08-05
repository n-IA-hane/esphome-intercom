"""Connectivity entities for logical VoIP Stack phone endpoints."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .endpoint_device import (
    async_link_endpoint_entity,
    endpoint_device_info,
    endpoint_public_attributes,
    enum_value,
)
from .endpoint_entity_manager import (
    EndpointEntityManager,
    register_endpoint_entity_manager,
)


ONLINE_AVAILABILITY = frozenset({"online", "available", "registered", "connected"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    manager = EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        PhoneEndpointConnectivityBinarySensor,
    )
    manager.async_setup()
    register_endpoint_entity_manager(
        entry, "endpoint_connectivity_entity_manager", manager
    )


class PhoneEndpointConnectivityBinarySensor(BinarySensorEntity):
    """Whether the endpoint currently has a usable contact/card connection."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "phone_endpoint_connectivity"

    def __init__(self, hass, endpoint, registry) -> None:
        self.endpoint = endpoint
        self.registry = registry
        self._attr_unique_id = f"phone_endpoint_{endpoint.endpoint_id}_connectivity"
        self._attr_device_info = endpoint_device_info(endpoint)
        self._attr_is_on = self._connected(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        async_link_endpoint_entity(
            self.registry, self.endpoint.endpoint_id, self.entity_id
        )

    @staticmethod
    def _connected(endpoint) -> bool:
        return enum_value(endpoint.availability) in ONLINE_AVAILABILITY

    @callback
    def apply_endpoint(self, endpoint) -> None:
        self.endpoint = endpoint
        self._attr_is_on = self._connected(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)
        if self.hass is not None:
            self.async_write_ha_state()
