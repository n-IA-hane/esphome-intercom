"""Switch entities for logical VoIP Stack phone endpoints."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .phone_entity import PhoneEndpointEntity
from .endpoint_entity_manager import (
    EndpointEntityManager,
    register_endpoint_entity_manager,
)
from .runtime_data import endpoint_directory
from .service_entity import NativeServiceEntity, async_setup_native_service_entities


async def async_set_endpoint_dnd(
    hass: HomeAssistant,
    endpoint_id: str,
    enabled: bool,
) -> None:
    """Persist DND through the canonical phone configuration."""
    registry = endpoint_directory(hass)
    endpoint = registry.get(endpoint_id)
    if endpoint is None:
        raise ValueError(f"Unknown phone endpoint: {endpoint_id}")

    from .phone_config import CONF_PHONE_DND, update_phone_subentry
    from .store import config_entry

    entry = config_entry(hass)
    if entry is not None:
        update_phone_subentry(
            hass,
            entry,
            endpoint_id,
            {CONF_PHONE_DND: bool(enabled)},
        )

    current = registry.get(endpoint_id)
    if current is not None and bool(current.dnd) != bool(enabled):
        registry.update(endpoint_id, dnd=bool(enabled))
    if endpoint.supports_ha_client:
        from .browser_phone_runtime import _publish_ha_softphone_state
        _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_native_service_entities(hass, entry, async_add_entities,
        lambda owner, row: [NativeServiceEnabledSwitch(owner, row.subentry_id)],
        include=lambda row: row.subentry_type in {"group", "paging"})
    manager = EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        PhoneEndpointDndSwitch,
        predicate=lambda endpoint: endpoint.supports_ha_client,
    )
    manager.async_setup()
    register_endpoint_entity_manager(
        entry, "endpoint_dnd_entity_manager", manager
    )
    conference_manager = EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        PhoneEndpointConferenceRingSwitch,
        predicate=lambda endpoint: endpoint.supports_ha_client,
    )
    conference_manager.async_setup()
    register_endpoint_entity_manager(
        entry,
        "endpoint_conference_ring_entity_manager",
        conference_manager,
    )
    for key, entity_class in (
        ("endpoint_auto_answer_entity_manager", PhoneEndpointAutoAnswerSwitch),
        ("endpoint_send_video_entity_manager", PhoneEndpointSendVideoSwitch),
    ):
        preference_manager = EndpointEntityManager(
            hass,
            entry,
            async_add_entities,
            entity_class,
            predicate=lambda endpoint: endpoint.supports_ha_client,
        )
        preference_manager.async_setup()
        register_endpoint_entity_manager(
            entry,
            key,
            preference_manager,
        )


class NativeServiceEnabledSwitch(NativeServiceEntity, SwitchEntity):
    """Change the same enabled setting used by the object's native editor."""

    _attr_translation_key = "service_enabled"
    _attr_icon = "mdi:phone-settings"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry, subentry_id):
        super().__init__(entry, subentry_id, "enabled")

    @property
    def is_on(self):
        row = self.record
        return bool(row is not None and row.data.get("enabled", True))

    async def _async_set(self, enabled):
        from homeassistant.exceptions import ServiceValidationError
        row = self.record
        if row is None:
            raise ServiceValidationError("The configured PBX application no longer exists")
        manager = self.runtime.queues
        if (row.subentry_type == "group" and row.data.get("kind") == "queue"
                and manager is not None and manager.resolve(row.data["group_id"]) is not None):
            manager.configure(row.data["group_id"], {"enabled": bool(enabled)})
        else:
            self.hass.config_entries.async_update_subentry(self.entry, row,
                data={**row.data, "enabled": bool(enabled)})
        self.refresh()

    async def async_turn_on(self, **kwargs):
        await self._async_set(True)

    async def async_turn_off(self, **kwargs):
        await self._async_set(False)


class PhoneEndpointSettingSwitch(PhoneEndpointEntity, SwitchEntity):
    """Read native switch metadata and persist one semantic setting."""

    _attr_entity_category = EntityCategory.CONFIG
    entity_description: SwitchEntityDescription

    def __init__(self, hass, endpoint, registry) -> None:
        super().__init__(endpoint, registry, key=self.entity_description.key)
        self._attr_is_on = bool(getattr(endpoint, self.entity_description.key))

    @callback
    def apply_endpoint(self, endpoint) -> None:
        super().apply_endpoint(endpoint)
        self._attr_is_on = bool(getattr(endpoint, self.entity_description.key))
        if self.hass is not None:
            self.async_write_ha_state()

    async def _async_set(self, enabled: bool) -> None:
        from .browser_phone_runtime import async_set_ha_softphone_settings
        await async_set_ha_softphone_settings(self.hass, endpoint_id=self.endpoint.endpoint_id,
                                              **{self.entity_description.key: enabled})

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class PhoneEndpointDndSwitch(PhoneEndpointSettingSwitch):
    """DND keeps its endpoint policy and transport-specific setter."""

    entity_description = SwitchEntityDescription(key="dnd", translation_key="phone_endpoint_dnd", icon="mdi:phone-off")

    async def _async_set(self, enabled: bool) -> None:
        await async_set_endpoint_dnd(self.hass, self.endpoint.endpoint_id, enabled)


class PhoneEndpointConferenceRingSwitch(PhoneEndpointSettingSwitch):
    entity_description = SwitchEntityDescription(key="conference_ring", translation_key="phone_endpoint_conference_ring",
                                                 icon="mdi:phone-in-talk")


class PhoneEndpointAutoAnswerSwitch(PhoneEndpointSettingSwitch):
    entity_description = SwitchEntityDescription(key="auto_answer", translation_key="phone_endpoint_auto_answer",
                                                 icon="mdi:phone-check")


class PhoneEndpointSendVideoSwitch(PhoneEndpointSettingSwitch):
    entity_description = SwitchEntityDescription(key="send_video", translation_key="phone_endpoint_send_video",
                                                 icon="mdi:video-wireless")
