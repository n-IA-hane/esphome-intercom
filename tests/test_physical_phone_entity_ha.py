"""Physical provisioning entities use the existing owner and authorized actions."""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er

from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path
from .test_physical_phone_runtime_ha import add, phone

pytestmark = pytest.mark.ha


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
        "url": "https://private.invalid/token", "password": "never-publish", "provisioning_token": "private-token",
        "files": {"account.cfg": "Authorization: private"}}
    lab.manager._changed(lab.inventory["phone_id"])
    await lab.hass.async_block_till_done()
    assert sensor.native_value == "downloaded"
    attributes = sensor.extra_state_attributes
    assert attributes["desired_revision"] == "a" * 64
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
