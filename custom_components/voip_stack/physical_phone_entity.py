"""Native, event-driven views of the existing physical provisioning owner."""

from __future__ import annotations

import math
import re

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .physical_phone import PHONE_PROVISIONING_CHANGED, physical_phone_records, physical_phone_subentries, provisioning_enabled


async def async_setup_physical_phone_entities(hass, entry, async_add_entities, factory):
    """Reconcile each platform's entities without adopting logical line devices."""
    entities = {}

    async def remove(entity):
        entity_id = entity.entity_id
        await entity.async_remove(force_remove=True)
        registry = er.async_get(hass)
        if entity_id and registry.async_get(entity_id) is not None:
            registry.async_remove(entity_id)

    @callback
    def reconcile(_event=None):
        manager = entry.runtime_data.physical_phones
        records = physical_phone_records(entry) if provisioning_enabled(entry) and manager is not None and manager.owned() else {}
        for identity in entities.keys() - records.keys():
            for entity in entities.pop(identity):
                if entity.entity_id is not None:
                    hass.async_create_task(remove(entity))
        for identity in records:
            if identity not in entities:
                rows = entities[identity] = factory(hass, entry, identity)
                subentry = next(item for item in physical_phone_subentries(entry) if item.data["phone_id"] == identity)
                async_add_entities(rows, config_subentry_id=subentry.subentry_id)
            else:
                for entity in entities[identity]:
                    entity.apply_record()

    entry.async_on_unload(hass.bus.async_listen(PHONE_PROVISIONING_CHANGED, reconcile))
    reconcile()


class PhysicalPhoneEntity:
    """Common physical inventory identity, separate from SIP account identities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass, entry, phone_id, suffix):
        self.hass, self.entry, self.phone_id = hass, entry, phone_id
        self._attr_unique_id = f"physical_phone:{phone_id}:{suffix}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, f"physical_phone:{phone_id}")})
        self.apply_record()

    @property
    def available(self):
        manager = self.entry.runtime_data.physical_phones
        return (provisioning_enabled(self.entry) and manager is not None and manager.owned()
                and self.phone_id in physical_phone_records(self.entry))

    @callback
    def apply_record(self):
        phone = physical_phone_records(self.entry).get(self.phone_id)
        if phone is not None:
            vendor, model = phone["profile_id"].split("/", 1)
            self._attr_device_info.update({"name": phone["name"], "manufacturer": vendor.title(), "model": model})
        if self.entity_id is not None:
            self.async_write_ha_state()


class PhysicalPhoneProvisioningSensor(PhysicalPhoneEntity, SensorEntity):
    """Observe preparation and file delivery, without claiming device adoption."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "physical_phone_provisioning"
    _attr_icon = "mdi:phone-settings"
    _attr_options = ["pending", "waiting_idle", "ready", "downloaded", "error", "disabled"]

    def __init__(self, hass, entry, phone_id):
        super().__init__(hass, entry, phone_id, "provisioning")

    @property
    def native_value(self):
        if not self.available:
            return None
        state = self.entry.runtime_data.physical_phones.public_state(self.phone_id).get("state", "pending")
        return state if state in self._attr_options else None

    @property
    def extra_state_attributes(self):
        if not self.available:
            return {"phone_id": self.phone_id}
        phone = physical_phone_records(self.entry)[self.phone_id]
        state = self.entry.runtime_data.physical_phones.public_state(self.phone_id)
        result = {"phone_id": self.phone_id, "profile_id": phone["profile_id"]}
        for key in ("desired_revision", "downloaded_revision"):
            if isinstance(value := state.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", value):
                result[key] = value
        if isinstance(value := state.get("last_download"), (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            result["last_download"] = value
        if isinstance(value := state.get("error"), str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:Error|Exception)", value):
            result["error"] = value
        if isinstance(value := state.get("language_code"), str) and re.fullmatch(r"[a-zA-Z0-9_-]{0,24}", value):
            result["language_code"] = value
        result["lines"] = [{"slot": line["slot"], "endpoint_id": line["endpoint_id"],
            "account_registered": bool(line.get("account_registered")),
            "device_registration_verified": bool(line.get("device_registration_verified"))}
            for line in state.get("lines", ())]
        return result


async def async_setup_physical_phone_sensors(hass, entry, async_add_entities):
    await async_setup_physical_phone_entities(hass, entry, async_add_entities,
        lambda _hass, _entry, identity: [PhysicalPhoneProvisioningSensor(_hass, _entry, identity)])
