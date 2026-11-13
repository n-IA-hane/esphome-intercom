"""Editable settings for Home Assistant logical phone endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .endpoint_device import async_link_endpoint_entity, endpoint_device_info
from .endpoint_entity_manager import (
    EndpointEntityManager,
    register_endpoint_entity_manager,
)


@dataclass(frozen=True, slots=True)
class _PhoneTextSetting:
    key: str
    translation_key: str
    icon: str
    maximum: int


_SETTINGS = (
    _PhoneTextSetting("extension", "phone_endpoint_extension", "mdi:dialpad", 255),
    _PhoneTextSetting(
        "ring_group", "phone_endpoint_ring_group", "mdi:phone-ring", 255
    ),
    _PhoneTextSetting("queues", "phone_endpoint_queues", "mdi:human-queue", 240),
    _PhoneTextSetting(
        "conference_group",
        "phone_endpoint_conference_group",
        "mdi:account-group",
        255,
    ),
)


def _is_browser_phone(endpoint) -> bool:
    return endpoint.supports_ha_client


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for setting in _SETTINGS:
        manager = EndpointEntityManager(
            hass,
            entry,
            async_add_entities,
            partial(PhoneEndpointSettingText, setting=setting),
            predicate=_is_browser_phone,
        )
        manager.async_setup()
        register_endpoint_entity_manager(
            entry,
            f"endpoint_{setting.key}_entity_manager",
            manager,
        )


class PhoneEndpointSettingText(TextEntity):
    """One editable string setting for a Home Assistant browser phone."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = TextMode.TEXT
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, endpoint, registry, *, setting: _PhoneTextSetting) -> None:
        self.endpoint = endpoint
        self.registry = registry
        self.setting = setting
        self._attr_translation_key = setting.translation_key
        self._attr_icon = setting.icon
        self._attr_native_max = setting.maximum
        self._attr_unique_id = (
            f"phone_endpoint_{endpoint.endpoint_id}_{setting.key}"
        )
        self._attr_device_info = endpoint_device_info(endpoint)
        value = getattr(endpoint, setting.key, "")
        self._attr_native_value = ", ".join(value) if setting.key == "queues" else str(value or "")

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Keep phone lookup and permissions attached to the renamed entity."""
        super().async_entity_id_changed(old_entity_id)
        async_link_endpoint_entity(
            self.registry, self.endpoint.endpoint_id, self.entity_id, old_entity_id
        )
        self._linked_entity_id = self.entity_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        async_link_endpoint_entity(
            self.registry, self.endpoint.endpoint_id, self.entity_id,
            getattr(self, "_linked_entity_id", None),
        )
        self._linked_entity_id = self.entity_id

    @callback
    def apply_endpoint(self, endpoint) -> None:
        self.endpoint = endpoint
        value = getattr(endpoint, self.setting.key, "")
        self._attr_native_value = ", ".join(value) if self.setting.key == "queues" else str(value or "")
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_set_value(self, value: str) -> None:
        from .websocket_api import async_set_ha_softphone_settings

        await async_set_ha_softphone_settings(
            self.hass,
            endpoint_id=self.endpoint.endpoint_id,
            **{self.setting.key: value},
        )
