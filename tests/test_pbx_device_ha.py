"""PBX grouping uses native registries while the parent owns its settings."""

from datetime import timedelta
import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_managed_phones_ha import phone_lab as phone_lab

pytestmark = pytest.mark.ha


async def test_pbx_registry_migration_preserves_ids_names_and_other_owners(phone_lab):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.pbx_device import async_ensure_pbx_device, pbx_subentry_id
    from custom_components.voip_stack.config_flow import VoipStackConfigFlow

    lab = phone_lab
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=7)
    devices, entities = dr.async_get(lab.hass), er.async_get(lab.hass)
    before_data = dict(lab.entry.data)
    device = devices.async_get_or_create(config_entry_id=lab.entry.entry_id,
        identifiers={("voip_stack", lab.entry.entry_id)}, name="VoIP Stack", model="PBX")
    devices.async_update_device(device.id, name_by_user="My PBX")
    rows = [entities.async_get_or_create(domain, "voip_stack", identity,
        config_entry=lab.entry, device_id=device.id, suggested_object_id=name)
        for domain, identity, name in (
            ("event", "voip_stack_call_event", "user_calls"),
            ("sensor", f"voip_stack:{lab.entry.entry_id}:pbx:active_calls", "user_active"),
            ("sensor", f"voip_stack:{lab.entry.entry_id}:pbx:registered_contacts", "user_registered"))]
    for row in rows:
        entities.async_update_entity(row.entity_id, name="User label", disabled_by=er.RegistryEntryDisabler.USER)
    other = entities.async_get_or_create("sensor", "voip_stack", "voip_stack_phonebook",
        config_entry=lab.entry, suggested_object_id="user_phonebook")
    identities = {row.entity_id: (row.id, row.unique_id) for row in rows}
    async_ensure_pbx_device(lab.hass, lab.entry)
    await lab.hass.async_block_till_done()
    identity = pbx_subentry_id(lab.entry)
    assert identity is not None
    pbx = lab.entry.subentries[identity]
    assert pbx.title == "VoIP Stack PBX" and dict(pbx.data) == {}
    assert "pbx" not in VoipStackConfigFlow.async_get_supported_subentry_types(lab.entry)
    current = devices.async_get(device.id)
    assert current.config_subentry_id == identity and current.name_by_user == "My PBX"
    assert current.identifiers == device.identifiers
    assert current.name == current.model == "VoIP Stack PBX"
    assert current.entry_type is dr.DeviceEntryType.SERVICE
    for entity_id, (row_id, unique_id) in identities.items():
        current = entities.async_get(entity_id)
        assert (current.id, current.unique_id) == (row_id, unique_id)
        assert current.device_id == device.id and current.config_subentry_id == identity
        assert current.name == "User label" and current.disabled_by is er.RegistryEntryDisabler.USER
    assert entities.async_get(other.entity_id).config_subentry_id is None
    assert dict(lab.entry.data) == before_data
    saved = lab.entry.as_dict()
    async_ensure_pbx_device(lab.hass, lab.entry)
    await lab.hass.async_block_till_done()
    assert lab.entry.as_dict() == saved
    assert devices.async_get(device.id).config_subentry_id == identity
    assert await async_migrate_entry(lab.hass, lab.entry)
    assert lab.entry.as_dict() == saved
    lab.hass.config_entries.async_remove_subentry(lab.entry, identity)
    await lab.hass.async_block_till_done()
    assert await async_migrate_entry(lab.hass, lab.entry)
    assert pbx_subentry_id(lab.entry) is None


async def test_native_pbx_platforms_reload_and_delete_only_the_admin_projection(phone_lab, monkeypatch):
    from custom_components.voip_stack import event, sensor
    from custom_components.voip_stack.config_entry_runtime import async_config_entry_updated, entry_runtime_signature
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook
    from custom_components.voip_stack.pbx_device import async_ensure_pbx_device, pbx_subentry_id
    from custom_components.voip_stack.group_config import reconcile_group_records

    lab = phone_lab
    lab.runtime.phonebook = entry_phonebook(lab.hass, lab.runtime)
    phone = ConfigSubentry(subentry_type="phone", title="Desk", unique_id="phone:Desk",
        data={"kind": "sip_account", "name": "Desk", "username": "Desk"})
    trunk = ConfigSubentry(subentry_type="trunk", title="Provider", unique_id="trunk:provider",
        data={"trunk_id": "provider", "trunk_enabled": False, "trunk_register": False})
    for row in (phone, trunk):
        lab.hass.config_entries.async_add_subentry(lab.entry, row)
    reconcile_group_records(lab.hass, lab.entry)
    async_ensure_pbx_device(lab.hass, lab.entry)
    identity = pbx_subentry_id(lab.entry)
    device = dr.async_get(lab.hass).async_get_device_by_identifier(
        ("voip_stack", lab.entry.entry_id), lab.entry.entry_id)
    platforms = []

    async def setup_platform(domain, module):
        platform = EntityPlatform(hass=lab.hass, logger=logging.getLogger(__name__), domain=domain,
            platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
        platform.config_entry = lab.entry
        platforms.append(platform)

        def add(entities, **kwargs):
            lab.entry.async_create_task(lab.hass, platform.async_add_entities(entities, **kwargs), "test PBX platform")

        await module.async_setup_entry(lab.hass, lab.entry, add)
        await lab.hass.async_block_till_done()

    try:
        await setup_platform("event", event)
        await setup_platform("sensor", sensor)
        registry = er.async_get(lab.hass)
        rows = er.async_entries_for_device(registry, device.id, include_disabled_entities=True)
        assert {row.unique_id for row in rows} == {"voip_stack_call_event", *(
            f"voip_stack:{lab.entry.entry_id}:pbx:{key}" for key in ("active_calls", "registered_contacts"))}
        assert all(row.config_subentry_id == identity for row in rows)
        assert all(lab.hass.states.get(row.entity_id) is not None for row in rows)
        renamed = registry.async_update_entity(rows[0].entity_id, new_entity_id=rows[0].domain + ".personal_pbx")
        await lab.hass.async_block_till_done()
        kept = {row.unique_id: row.entity_id for row in er.async_entries_for_device(registry, device.id)}
        for platform in platforms:
            await platform.async_reset()
        platforms.clear()
        async_ensure_pbx_device(lab.hass, lab.entry)
        await setup_platform("event", event)
        await setup_platform("sensor", sensor)
        assert pbx_subentry_id(lab.entry) == identity
        assert {row.unique_id: row.entity_id for row in er.async_entries_for_device(registry, device.id)} == kept
        assert lab.hass.states.get(renamed.entity_id) is not None
        call = lab.runtime.sip.upsert("existing-pbx-call", state="in_call", owner="router")
        before_data = dict(lab.entry.data)
        runtime, owner = lab.runtime, lab.runtime.sip
        lab.runtime.entry_runtime_signature = entry_runtime_signature(lab.entry)
        reload = AsyncMock()
        monkeypatch.setattr(lab.hass.config_entries, "async_reload", reload)
        lab.hass.config_entries.async_remove_subentry(lab.entry, identity)
        await lab.hass.async_block_till_done()
        assert dr.async_get(lab.hass).async_get(device.id) is None
        assert all(registry.async_get(entity_id) is None for entity_id in kept.values())
        assert all(lab.hass.states.get(entity_id) is None for entity_id in kept.values())
        await async_config_entry_updated(lab.hass, lab.entry)
        await lab.hass.async_block_till_done()
        assert pbx_subentry_id(lab.entry) is None
        assert lab.entry.subentries[phone.subentry_id] is phone
        assert lab.entry.subentries[trunk.subentry_id] is trunk
        assert dict(lab.entry.data) == before_data
        assert lab.entry.runtime_data is runtime and runtime.sip is owner
        assert owner.get_session(call.call_id) is call and call.live
        reload.assert_not_awaited()
        async_ensure_pbx_device(lab.hass, lab.entry)
        assert pbx_subentry_id(lab.entry) not in {None, identity}
    finally:
        for platform in platforms:
            await platform.async_reset()
        lab.runtime.phonebook.close()
