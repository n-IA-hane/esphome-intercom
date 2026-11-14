"""Editable settings for Home Assistant logical phone endpoints."""

from __future__ import annotations

from functools import partial

from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .phone_entity import PhoneEndpointEntity
from .endpoint_entity_manager import (
    EndpointEntityManager,
    register_endpoint_entity_manager,
)


_SETTINGS = (
    TextEntityDescription(key="extension", translation_key="phone_endpoint_extension", icon="mdi:dialpad", native_max=255),
    TextEntityDescription(key="ring_group", translation_key="phone_endpoint_ring_group", icon="mdi:phone-ring", native_max=255),
    TextEntityDescription(key="queues", translation_key="phone_endpoint_queues", icon="mdi:human-queue", native_max=240),
    TextEntityDescription(key="conference_group", translation_key="phone_endpoint_conference_group", icon="mdi:account-group", native_max=255),
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


class PhoneEndpointSettingText(PhoneEndpointEntity, TextEntity):
    """One editable string setting for a Home Assistant browser phone."""

    _attr_mode = TextMode.TEXT
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, endpoint, registry, *, setting: TextEntityDescription) -> None:
        self.entity_description = setting
        super().__init__(endpoint, registry, key=setting.key)
        value = getattr(endpoint, setting.key, "")
        self._attr_native_value = ", ".join(value) if setting.key == "queues" else str(value or "")

    @callback
    def apply_endpoint(self, endpoint) -> None:
        super().apply_endpoint(endpoint)
        key = self.entity_description.key
        value = getattr(endpoint, key, "")
        self._attr_native_value = ", ".join(value) if key == "queues" else str(value or "")
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_set_value(self, value: str) -> None:
        from .websocket_api import async_set_ha_softphone_settings

        await async_set_ha_softphone_settings(
            self.hass,
            endpoint_id=self.endpoint.endpoint_id,
            **{self.entity_description.key: value},
        )
