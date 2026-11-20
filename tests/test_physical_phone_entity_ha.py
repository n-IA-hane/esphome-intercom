"""Physical provisioning entities use the existing owner and authorized actions."""

import json
import asyncio
from datetime import timedelta
import logging
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er

from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path
from .test_physical_phone_runtime_ha import add, phone

pytestmark = pytest.mark.ha


@pytest.fixture
def ha11_device_info():
    class DeviceInfoMapping(dict):
        # HA2026.11 _DeviceInfoMapping.update takes one positional mapping:
        # homeassistant/helpers/device_registry.py, lines227-231.
        def update(self, other):
            for key, value in other.items():
                self[key] = value

    return DeviceInfoMapping


@pytest.fixture
async def physical_entities(phone_lab):
    from custom_components.voip_stack.physical_phone import sync_physical_phone_devices
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from custom_components.voip_stack.physical_phone_services import register_physical_phone_services, unregister_physical_phone_services
    lab = phone_lab
    data = phone()
    subentry = add(lab, data)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "phone_provisioning": {"enabled": True}})
    sync_physical_phone_devices(lab.hass, lab.entry)
    manager = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    lab.runtime.physical_phones = manager
    manager.schedule = Mock()
    register_physical_phone_services(lab.hass)
    lab.inventory, lab.subentry, lab.manager = data, subentry, manager
    yield lab
    unregister_physical_phone_services(lab.hass)
    await manager.async_close()


async def test_physical_entities_follow_subentry_owner_state_and_never_publish_private_assets(physical_entities):
    from homeassistant.components.sensor import SensorEntity
    from custom_components.voip_stack import physical_phone_entity, button
    lab = physical_entities
    added = []
    registry = er.async_get(lab.hass)

    def capture(rows, **kwargs):
        for entity in rows:
            domain = "sensor" if isinstance(entity, SensorEntity) else "button"
            registration = registry.async_get_or_create(domain, "voip_stack", entity.unique_id,
                config_entry=lab.entry, config_subentry_id=kwargs["config_subentry_id"])
            entity.entity_id = registration.entity_id
            entity.async_write_ha_state = Mock()
            entity.async_remove = AsyncMock()
            added.append((entity, kwargs["config_subentry_id"]))

    await physical_phone_entity.async_setup_physical_phone_sensors(lab.hass, lab.entry, capture)
    await button.async_setup_entry(lab.hass, lab.entry, capture)
    assert len(added) == 3
    original = list(added)
    assert all(subentry == lab.subentry.subentry_id for _entity, subentry in added)
    assert all(not entity.should_poll for entity, _subentry in added)
    assert all(("voip_stack", "physical_phone:" + lab.inventory["phone_id"]) in entity.device_info["identifiers"]
               for entity, _subentry in added)
    sensor = next(entity for entity, _ in added if isinstance(entity, SensorEntity))
    assert sensor.native_value == "pending"
    lab.manager.states[lab.inventory["phone_id"]] = {"state": "downloaded", "desired_revision": "a" * 64,
        "downloaded_revision": "a" * 64, "last_download": 1234.0, "error": "TimeoutError",
        "apply_status": "requested", "apply_error": "",
        "url": "https://private.invalid/token", "password": "never-publish", "provisioning_token": "private-token",
        "files": {"account.cfg": "Authorization: private"}}
    lab.manager._changed(lab.inventory["phone_id"])
    await lab.hass.async_block_till_done()
    assert sensor.native_value == "downloaded"
    attributes = sensor.extra_state_attributes
    assert attributes["desired_revision"] == "a" * 64
    assert attributes["apply_status"] == "requested" and attributes["apply_error"] == ""
    assert attributes["lines"][0]["device_registration_verified"] is False
    rendered = json.dumps(attributes)
    assert not any(word in rendered for word in ("private", "password", "Authorization", "https://", "provisioning_token", "files"))
    sensor.async_write_ha_state.assert_called()
    lab.hass.config_entries.async_update_subentry(lab.entry, lab.subentry,
        data={**lab.inventory, "name": "Renamed desk"}, title="Renamed desk")
    lab.manager._changed("")
    await lab.hass.async_block_till_done()
    assert len(added) == 3 and sensor.device_info["name"] == "Renamed desk"
    second = add(lab, phone(name="New live phone", mac="00:11:22:33:44:66"))
    lab.manager._changed("")
    await lab.hass.async_block_till_done()
    assert len(added) == 6
    assert all(subentry == second.subentry_id for _entity, subentry in added[3:])
    assert len({entity.unique_id for entity, _subentry in added}) == 6
    lab.hass.config_entries.async_remove_subentry(lab.entry, lab.subentry.subentry_id)
    lab.manager._changed("")
    await lab.hass.async_block_till_done()
    for entity, _ in original:
        entity.async_remove.assert_awaited_once_with(force_remove=True)
        assert registry.async_get(entity.entity_id) is None
    for entity, _ in added[3:]:
        entity.async_remove.assert_not_awaited()
        assert entity.available


@pytest.mark.parametrize(("status", "error"), [
    ("pending", ""), ("waiting_idle", ""), ("not_qualified", ""), ("manual", ""),
    ("requesting", ""), ("requested", ""), ("error", "phone_apply_authentication_failed"),
])
async def test_physical_apply_attributes_observe_owner_without_claiming_adoption(physical_entities, status, error):
    from custom_components.voip_stack.physical_phone_entity import PhysicalPhoneProvisioningSensor

    lab = physical_entities
    state = {"state": "ready", "apply_status": status, "apply_error": error}
    lab.manager.states[lab.inventory["phone_id"]] = dict(state)
    sensor = PhysicalPhoneProvisioningSensor(lab.hass, lab.entry, lab.inventory["phone_id"])
    attributes = sensor.extra_state_attributes
    assert attributes["apply_status"] == status and attributes["apply_error"] == error
    assert sensor.native_value == "ready"
    assert "applied" not in attributes and "device_applied" not in attributes
    assert lab.manager.states[lab.inventory["phone_id"]] == state


@pytest.mark.parametrize(("status", "error"), [
    ("applied", "Authorization: private-token"),
    ("https://private.invalid/token", "phone_apply_private_token"),
    ({"password": "private"}, {"body": "private"}),
])
async def test_physical_apply_attributes_reject_unknown_or_private_values(physical_entities, status, error):
    from custom_components.voip_stack.physical_phone_entity import PhysicalPhoneProvisioningSensor

    lab = physical_entities
    lab.manager.states[lab.inventory["phone_id"]] = {"state": "ready", "apply_status": status,
        "apply_error": error, "apply_vendor_body": "private", "admin_password": "private"}
    sensor = PhysicalPhoneProvisioningSensor(lab.hass, lab.entry, lab.inventory["phone_id"])
    attributes = sensor.extra_state_attributes
    assert sensor.native_value == "ready"
    assert "apply_status" not in attributes and "apply_error" not in attributes
    assert "private" not in json.dumps(attributes)


@pytest.mark.parametrize(("error", "status"), [
    ("phone_ldap_mode_unsupported", "error"),
    ("phone_apply_result_uncertain", "uncertain"),
])
async def test_domain_error_codes_and_uncertain_outcome_are_visible(physical_entities, error, status):
    from custom_components.voip_stack.physical_phone_entity import PhysicalPhoneProvisioningSensor

    lab = physical_entities
    lab.manager.states[lab.inventory["phone_id"]] = {"state": "error", "error": error,
        "apply_status": status, "apply_error": error if status == "uncertain" else ""}
    sensor = PhysicalPhoneProvisioningSensor(lab.hass, lab.entry, lab.inventory["phone_id"])
    attributes = sensor.extra_state_attributes
    assert attributes.get("error") == error
    assert attributes.get("apply_status") == status


async def test_physical_entities_can_enable_late_and_remove_on_feature_disable(physical_entities):
    from custom_components.voip_stack import physical_phone_entity
    lab = physical_entities
    added = []

    def capture(rows, **_kwargs):
        for entity in rows:
            entity.entity_id = f"sensor.physical_{len(added)}"
            entity.async_remove = AsyncMock()
            entity.async_write_ha_state = Mock()
            added.append(entity)

    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "phone_provisioning": {"enabled": False}})
    await physical_phone_entity.async_setup_physical_phone_sensors(lab.hass, lab.entry, capture)
    assert not added
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "phone_provisioning": {"enabled": True}})
    lab.manager._changed("")
    await lab.hass.async_block_till_done()
    assert len(added) == 1
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "phone_provisioning": {"enabled": False}})
    lab.runtime.physical_phones = None
    lab.manager._changed("")
    await lab.hass.async_block_till_done()
    added[0].async_remove.assert_awaited_once_with(force_remove=True)


async def test_device_metadata_distinguishes_pbx_and_hardware_without_renaming(physical_entities):
    from homeassistant.helpers import device_registry as dr
    from custom_components.voip_stack.event import VoipStackCallEvent
    from custom_components.voip_stack.physical_phone import sync_physical_phone_devices

    lab = physical_entities
    devices = dr.async_get(lab.hass)
    identifier = ("voip_stack", "physical_phone:" + lab.inventory["phone_id"])
    before = devices.async_get_device_by_identifier(identifier, lab.entry.entry_id)
    assert before is not None
    devices.async_update_device(before.id, name_by_user="Personal desk name")
    sync_physical_phone_devices(lab.hass, lab.entry)
    after = devices.async_get_device_by_identifier(identifier, lab.entry.entry_id)
    assert after.id == before.id and after.name_by_user == "Personal desk name"
    assert after.name == lab.inventory["name"]
    assert after.manufacturer == "Fanvil" and after.model == "X3U Pro"
    assert after.config_entry_id == before.config_entry_id
    assert after.config_subentry_id == before.config_subentry_id

    pbx = VoipStackCallEvent(lab.hass, lab.entry).device_info
    assert pbx["identifiers"] == {("voip_stack", lab.entry.entry_id)}
    assert pbx["name"] == lab.entry.title
    assert pbx["manufacturer"] == "VoIP Stack" and pbx["model"] == "PBX"


async def test_physical_buttons_use_real_admin_service_and_context_without_bypassing_permissions(physical_entities, hass_admin_user, hass_read_only_user):
    from custom_components.voip_stack import button
    lab = physical_entities
    buttons = []
    await button.async_setup_entry(lab.hass, lab.entry, lambda rows, **_kwargs: buttons.extend(rows))
    provision = next(entity for entity in buttons if entity.translation_key == "physical_phone_provision")
    rotate = next(entity for entity in buttons if entity.translation_key == "physical_phone_rotate")
    provision.async_set_context(Context(user_id=hass_admin_user.id))
    await provision.async_press()
    lab.manager.schedule.assert_called_once_with({lab.inventory["phone_id"]})
    previous = lab.subentry.data["provisioning_token"]
    rotate.async_set_context(Context(user_id=hass_read_only_user.id))
    with pytest.raises(Unauthorized):
        await rotate.async_press()
    assert lab.entry.subentries[lab.subentry.subentry_id].data["provisioning_token"] == previous
    rotate.async_set_context(Context(user_id=hass_admin_user.id))
    await rotate.async_press()
    assert lab.entry.subentries[lab.subentry.subentry_id].data["provisioning_token"] != previous
    assert "provisioning_token" not in (rotate.extra_state_attributes or {})


async def test_registered_state_belongs_to_the_downloading_handset(physical_entities, monkeypatch):
    from custom_components.voip_stack.physical_phone import physical_phone_id
    from custom_components.voip_stack.physical_phone_entity import PhysicalPhoneProvisioningSensor
    from .test_physical_phone_runtime_ha import register_cached_account

    lab = physical_entities
    register_cached_account(lab)
    identity = physical_phone_id(lab.subentry)
    sensor = PhysicalPhoneProvisioningSensor(lab.hass, lab.entry, identity)
    state = lab.manager.states[identity] = {"state": "downloaded", "device_download_host": "192.0.2.18"}
    contacts = [SimpleNamespace(source_host="192.0.2.19", instance_id="other")]
    monkeypatch.setattr(lab.registrar, "registered_contacts", lambda _name: contacts)
    monkeypatch.setattr("custom_components.voip_stack.physical_phone_enrollment.phone_dhcp_host",
                        lambda *_: "192.0.2.18")
    assert sensor.native_value == "downloaded"
    contacts.append(SimpleNamespace(source_host="192.0.2.18", instance_id="handset"))
    assert sensor.native_value == "registered"
    state["state"] = "ready"
    assert sensor.native_value == "ready"
    state["state"] = "downloaded"
    contacts.pop()
    assert sensor.native_value == "downloaded"


async def test_full_sensor_setup_with_ha11_device_info_keeps_phonebook_loaded(physical_entities, monkeypatch, ha11_device_info):
    from homeassistant.helpers.entity_platform import EntityPlatform
    from custom_components.voip_stack import physical_phone_entity, sensor
    lab = physical_entities
    monkeypatch.setattr(physical_phone_entity, "DeviceInfo", ha11_device_info)
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook
    coordinator = entry_phonebook(lab.hass, lab.runtime)
    await coordinator.async_start()
    platform = EntityPlatform(hass=lab.hass, logger=logging.getLogger(__name__), domain="sensor",
        platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = lab.entry
    tasks = []

    def add_entities(rows, update_before_add=False, **kwargs):
        tasks.append(asyncio.create_task(platform.async_add_entities(rows, update_before_add, **kwargs)))

    try:
        await sensor.async_setup_entry(lab.hass, lab.entry, add_entities)
        await asyncio.gather(*tasks)
        await lab.hass.async_block_till_done()
        book = lab.hass.states.get("sensor.voip_phonebook")
        assert book is not None and book.state not in {"unknown", "unavailable"}
        assert "roster_json" in book.attributes
        assert lab.runtime.phonebook is coordinator
        physical = [entity for entity in platform.entities.values()
            if isinstance(entity, physical_phone_entity.PhysicalPhoneProvisioningSensor)]
        assert len(physical) == 1 and physical[0].available
        assert physical[0].device_info["name"] == lab.inventory["name"]
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await platform.async_reset()
        await coordinator.async_close()
