"""Automation contacts have native HA identity without becoming SIP accounts."""

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voip_stack.automation_contact_device import (
    AUTOMATION_CONTACT_MODEL,
    async_sync_automation_contact_devices,
    automation_contact_device_identifier,
    automation_contact_event_unique_id,
    automation_contact_id_for_device,
    automation_contact_id_for_entity,
)
from custom_components.voip_stack.const import DOMAIN

pytestmark = pytest.mark.ha


def contact_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="PBX", data={})
    entry.add_to_hass(hass)
    contact = ConfigSubentry(subentry_type="contact", title="Welcome", unique_id="contact:welcome-id",
        data={"id": "welcome-id", "name": "Welcome", "enabled": True,
              "metadata": {"virtual_endpoint": "automation", "automation_timeout": 30}})
    hass.config_entries.async_add_subentry(entry, contact)
    return entry, contact


async def test_native_contact_identity_survives_rename_disable_and_removal(hass):
    entry, contact = contact_entry(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    unrelated = devices.async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "unrelated-phone")}, name="Other phone", model="Desk phone")
    async_sync_automation_contact_devices(hass, entry)
    identifier = automation_contact_device_identifier(entry.entry_id, "welcome-id")
    device = devices.async_get_device_by_identifier((DOMAIN, identifier), entry.entry_id)
    assert device is not None and device.model == AUTOMATION_CONTACT_MODEL
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    original_id = device.id
    assert automation_contact_id_for_device(hass, device.id, entry.entry_id) == "welcome-id"
    event = entities.async_get_or_create("event", DOMAIN,
        automation_contact_event_unique_id(entry.entry_id, "welcome-id"), config_entry=entry,
        config_subentry_id=contact.subentry_id, device_id=device.id, suggested_object_id="welcome_call")
    assert automation_contact_id_for_entity(hass, event.entity_id, entry.entry_id) == "welcome-id"
    event = entities.async_update_entity(event.entity_id, new_entity_id="event.operator_ivr_call")
    assert automation_contact_id_for_entity(hass, event.entity_id) == "welcome-id"
    devices.async_update_device(device.id, name_by_user="Operator menu")
    hass.config_entries.async_update_subentry(entry, contact, title="Renamed menu",
        data={**contact.data, "name": "Renamed menu"})
    async_sync_automation_contact_devices(hass, entry)
    device = devices.async_get(original_id)
    assert device is not None and device.name == "Renamed menu" and device.name_by_user == "Operator menu"
    assert automation_contact_id_for_device(hass, original_id) == "welcome-id"
    contact = entry.subentries[contact.subentry_id]
    hass.config_entries.async_update_subentry(entry, contact, data={**contact.data, "enabled": False})
    async_sync_automation_contact_devices(hass, entry)
    assert devices.async_get(original_id) is not None
    assert automation_contact_id_for_device(hass, original_id) is None
    assert automation_contact_id_for_entity(hass, event.entity_id) is None
    hass.config_entries.async_remove_subentry(entry, contact.subentry_id)
    async_sync_automation_contact_devices(hass, entry)
    assert devices.async_get(original_id) is None
    assert entities.async_get(event.entity_id) is None
    assert devices.async_get(unrelated.id) is not None
    assert not any(item.subentry_type == "phone" for item in entry.subentries.values())


async def test_native_contact_mapping_rejects_other_owners_and_entity_kinds(hass):
    entry, contact = contact_entry(hass)
    async_sync_automation_contact_devices(hass, entry)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_device_by_identifier((DOMAIN,
        automation_contact_device_identifier(entry.entry_id, "welcome-id")), entry.entry_id)
    other = MockConfigEntry(domain=DOMAIN, title="Other PBX", data={})
    other.add_to_hass(hass)
    assert automation_contact_id_for_device(hass, device.id, other.entry_id) is None
    wrong = entities.async_get_or_create("sensor", DOMAIN,
        automation_contact_event_unique_id(entry.entry_id, "welcome-id"), config_entry=entry,
        config_subentry_id=contact.subentry_id, device_id=device.id, suggested_object_id="not_a_call_event")
    assert automation_contact_id_for_entity(hass, wrong.entity_id, entry.entry_id) is None
    ordinary = devices.async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "account:ordinary")}, name="Ordinary account", model="SIP account")
    assert automation_contact_id_for_device(hass, ordinary.id) is None
    assert automation_contact_id_for_device(hass, "missing") is None


async def test_contact_event_is_native_projection_and_reconciles_entry_changes(hass, enable_custom_integrations):
    from homeassistant.config_entries import ConfigEntryState
    from pytest_homeassistant.common import mock_component
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
    from custom_components.voip_stack.runtime_data import VoipStackRuntime
    from custom_components.voip_stack.const import CALL_EVENT

    entry, contact = contact_entry(hass)
    # The actual event platform/registries run; unrelated UI/audio dependencies
    # are isolated with the native HA test helper.
    for dependency in (DOMAIN, "assist_pipeline", "ffmpeg", "frontend", "http", "lovelace", "media_source", "network", "web_rtc"):
        if dependency not in hass.config.components:
            mock_component(hass, dependency)
    endpoints = EndpointRegistry()
    entry.runtime_data = VoipStackRuntime({}, {}, {}, endpoints, PhoneAdapterRegistry(hass, endpoints))
    entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.config_entries.async_forward_entry_setups(entry, ["event"])
    await hass.async_block_till_done()
    entities, devices = er.async_get(hass), dr.async_get(hass)
    unique_id = automation_contact_event_unique_id(entry.entry_id, "welcome-id")
    entity_id = entities.async_get_entity_id("event", DOMAIN, unique_id)
    assert entity_id is not None
    entity_entry = entities.async_get(entity_id)
    original_device_id = entity_entry.device_id
    assert automation_contact_id_for_entity(hass, entity_id) == "welcome-id"
    payload = {"call_id": "contact-call", "generation": 1, "state": "ringing", "direction": "incoming",
        "scope": "sip_bridge", "route_kind": "automation", "caller": "Door", "callee": "Welcome"}
    hass.bus.async_fire(CALL_EVENT, payload)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.attributes["event_type"] == "ringing"
    assert state.attributes["automation_contact_id"] == "welcome-id"
    assert state.attributes["call_id"] == "contact-call"
    assert entry.runtime_data.sip is None and not endpoints.endpoints
    hass.config_entries.async_update_subentry(entry, contact, title="Renamed Welcome",
        data={**contact.data, "name": "Renamed Welcome"})
    await hass.async_block_till_done()
    assert entities.async_get(entity_id).device_id == original_device_id
    assert devices.async_get(original_device_id).name == "Renamed Welcome"
    # The projection keeps this actual call's token through a display rename.
    hass.bus.async_fire(CALL_EVENT, {**payload, "state": "idle", "type": "ended", "pbx_phase": "terminated"})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["event_type"] == "ended"
    contact = entry.subentries[contact.subentry_id]
    hass.config_entries.async_remove_subentry(entry, contact.subentry_id)
    await hass.async_block_till_done()
    assert entities.async_get(entity_id) is None and devices.async_get(original_device_id) is None
    assert hass.states.get(entity_id) is None
    assert await hass.config_entries.async_unload_platforms(entry, ["event"])


async def test_merged_ambiguous_contact_device_is_not_a_routing_selection(hass):
    entry, first = contact_entry(hass)
    async_sync_automation_contact_devices(hass, entry)
    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier((DOMAIN,
        automation_contact_device_identifier(entry.entry_id, "welcome-id")), entry.entry_id)
    second = ConfigSubentry(subentry_type="contact", title="Other IVR", unique_id="contact:other-id",
        data={**first.data, "id": "other-id", "name": "Other IVR"})
    hass.config_entries.async_add_subentry(entry, second)
    devices.async_update_device(device.id, new_identifiers={*device.identifiers,
        (DOMAIN, automation_contact_device_identifier(entry.entry_id, "other-id"))})
    assert automation_contact_id_for_device(hass, device.id, entry.entry_id) is None
