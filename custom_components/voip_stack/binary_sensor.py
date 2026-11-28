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
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .endpoint_device import (
    endpoint_public_attributes,
    enum_value,
)
from .phone_entity import PhoneEndpointEntity
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
    from .trunk_config import TRUNK_UPDATE_SIGNAL, trunk_identity, trunk_subentries

    trunk_entities = {}

    @callback
    def update_trunks(identity=""):
        records = {trunk_identity(record): record for record in trunk_subentries(entry)}
        for removed in trunk_entities.keys() - records.keys():
            trunk_entities.pop(removed)
        for trunk_id, record in records.items():
            if trunk_id not in trunk_entities:
                entity = TrunkReadyBinarySensor(hass, trunk_id, record.title)
                trunk_entities[trunk_id] = entity
                async_add_entities([entity], config_subentry_id=record.subentry_id)
            elif not identity or identity == trunk_id:
                trunk_entities[trunk_id].refresh_state()

    entry.async_on_unload(async_dispatcher_connect(hass, TRUNK_UPDATE_SIGNAL, update_trunks))
    update_trunks()


class TrunkReadyBinarySensor(BinarySensorEntity):
    """Registration readiness, or configured static routing, without polling."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "trunk_ready"

    def __init__(self, hass, identity, name):
        from .const import DOMAIN
        from .trunk_config import TRUNK_DEVICE_MODEL

        self._runtime_hass = hass
        self.identity = identity
        self._attr_unique_id = f"trunk_{identity}_ready"
        self._attr_device_info = {"identifiers": {(DOMAIN, f"trunk:{identity}")}, "name": name,
                                  "manufacturer": "VoIP Stack", "model": TRUNK_DEVICE_MODEL}
        self.refresh_state()

    @callback
    def refresh_state(self):
        from .runtime_data import runtime_data, sip_trunk

        runtime = runtime_data(self._runtime_hass)
        config = runtime.trunk_configs.get(self.identity) if runtime is not None else None
        # HA persists the subentry before the validated transport is adopted.
        # Its entity must exist during that interval, without claiming readiness.
        self._attr_available = config is not None
        config = config or {}
        client = sip_trunk(self._runtime_hass, self.identity)
        self._attr_is_on = bool(client is not None and client.ready)
        self._attr_extra_state_attributes = {
            "registration_enabled": bool(config.get("trunk_register", True)),
            "transport": str(config.get("trunk_transport", "")),
            "last_status_code": int(getattr(client, "status_code", 0)),
        }
        if self.hass is not None:
            self.async_write_ha_state()


class PhoneEndpointConnectivityBinarySensor(PhoneEndpointEntity, BinarySensorEntity):
    """Whether the endpoint currently has a usable contact/card connection."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "phone_endpoint_connectivity"

    def __init__(self, hass, endpoint, registry) -> None:
        super().__init__(endpoint, registry, key="connectivity")
        self._attr_is_on = self._connected(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)

    @staticmethod
    def _connected(endpoint) -> bool:
        return enum_value(endpoint.availability) in ONLINE_AVAILABILITY

    @callback
    def apply_endpoint(self, endpoint) -> None:
        super().apply_endpoint(endpoint)
        self._attr_is_on = self._connected(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)
        if self.hass is not None:
            self.async_write_ha_state()
