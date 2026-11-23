"""Import native legacy settings once when an ESP first gains a SIP account."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    native_services,
    phone_lab as phone_lab,
)
from .test_esphome_provisioning_batch_ha import add_phone

pytestmark = pytest.mark.ha


def add_native_setting(lab, origin, device, role, value):
    domain = "switch" if role == "voip_ring_on_conference" else "text"
    entity = er.async_get(lab.hass).async_get_or_create(domain, "esphome",
        origin.entry_id + "/" + role, config_entry=origin, device_id=device.id,
        suggested_object_id=origin.data["device_name"] + "_" + role)
    if value is not None:
        lab.hass.states.async_set(entity.entity_id, value)
    return entity


@pytest.fixture
async def legacy_pair(phone_lab):
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint

    lab = phone_lab
    lab.expected_count = 2
    second, second_device = add_phone(lab, 1)
    phones = []
    policies = {}
    for origin, device, name, extension, ring in (
        (lab.native, lab.device, "Box", "101", "RG Casa"),
        (second, second_device, "P4", "430", ""),
    ):
        native = origin.runtime_data
        native.device_info = replace(native.device_info, friendly_name=name)
        native.services = native_services(managed=False)
        settings = {role: add_native_setting(lab, origin, device, role, value)
                    for role, value in (("voip_extension", extension), ("voip_ring_groups", ring),
                        ("voip_conference_groups", "CG Casa"), ("voip_ring_on_conference", "on"))}
        old = "esphome:" + device.id
        policies[old] = {"ringing_timeout": 13, "no_answer": {"kind": "hangup"}}
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=old, name=name,
            username=native.device_info.name, device_id=device.id, kind=EndpointKind.ESPHOME))
        phones.append(SimpleNamespace(origin=origin, device=device, name=name, old=old,
            extension=extension, settings=settings))
    lab.hass.config_entries.async_update_entry(lab.entry, version=6, minor_version=4,
        data={**lab.entry.data, "group_membership_version": 1, "esphome_phone_policies": policies})
    lab.runtime.sip.bind_endpoint_registry(lab.runtime.endpoints)
    lab.legacy_phones = phones
    return lab


def account_rows(lab):
    return {row.data.get("device_id"): row for row in lab.entry.subentries.values()
            if row.subentry_type == "phone" and row.data.get("managed_esphome")}


def registry_snapshot(lab):
    return ({device.id: (device.config_entry_id, device.config_subentry_id, device.connections,
                        device.identifiers, device.name, device.name_by_user)
             for device in dr.async_get(lab.hass).devices},
            {entity.entity_id: (entity.id, entity.unique_id, entity.device_id, entity.config_entry_id)
             for entity in er.async_get(lab.hass).entities.values() if entity.platform == "esphome"})


async def test_real_legacy_creation_imports_native_extensions_conference_and_group_owner(legacy_pair):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.group_config import group_records

    lab = legacy_pair
    before = registry_snapshot(lab)
    assert await async_migrate_entry(lab.hass, lab.entry)
    rows = account_rows(lab)
    observed = {phone.name: {field: rows[phone.device.id].data.get(field)
                           for field in ("extension", "conference_group", "conference_ring")}
                for phone in lab.legacy_phones}
    assert observed == {
        "Box": {"extension": "101", "conference_group": "CG Casa", "conference_ring": True},
        "P4": {"extension": "430", "conference_group": "CG Casa", "conference_ring": True},
    }
    assert {group["name"]: group["members"] for group in group_records(lab.entry, "ring").values()} == {"RG Casa": ["Box"]}
    assert registry_snapshot(lab) == before
    for phone in lab.legacy_phones:
        row = rows[phone.device.id]
        assert row.data["call_policy"] == {"ringing_timeout": 13, "no_answer": {"kind": "hangup"}}
        assert "ring_group" not in row.data and "queues" not in row.data
        assert lab.entry.data["legacy_phone_references"][phone.old] == phone.name
    assert all(phone.origin.runtime_data.first_connect_done.is_set() for phone in lab.legacy_phones)


@pytest.mark.parametrize("pending_state", [None, "unknown", "unavailable"])
@pytest.mark.parametrize("role", ["voip_extension", "voip_conference_groups"])
async def test_missing_native_state_defers_only_that_esp_then_existing_event_retries(legacy_pair, pending_state, role):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.esphome_provisioning import EspHomeProvisioner

    lab = legacy_pair
    box, p4 = lab.legacy_phones
    entity = box.settings[role]
    expected = "101" if role == "voip_extension" else "CG Casa"
    if pending_state is None:
        lab.hass.states.async_remove(entity.entity_id)
    else:
        lab.hass.states.async_set(entity.entity_id, pending_state)
    original_policy = deepcopy(lab.entry.data["esphome_phone_policies"][box.old])
    assert await async_migrate_entry(lab.hass, lab.entry)
    assert set(account_rows(lab)) == {p4.device.id}
    assert lab.entry.data["esphome_phone_policies"][box.old] == original_policy
    assert box.old not in lab.entry.data.get("legacy_phone_references", {})
    assert box.device.id not in lab.entry.data.get("esphome_sip_enrollments", {})
    controller = lab.runtime.phone_provisioning = EspHomeProvisioner(lab.hass, lab.entry)
    controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert set(account_rows(lab)) == {p4.device.id}
    assert controller._identity_retry_signature is not None
    # No explicit migration or coordinator call: this real HA state event
    # must re-arm the existing retry owner, including a previously missing state.
    lab.hass.states.async_set(entity.entity_id, expected)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    rows = account_rows(lab)
    assert set(rows) == {box.device.id, p4.device.id}
    assert rows[box.device.id].data["extension"] == "101"
    assert rows[box.device.id].data["conference_group"] == "CG Casa"
    assert rows[box.device.id].data["call_policy"] == original_policy


async def test_incomplete_inventory_waits_for_existing_service_inventory_event(legacy_pair):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.esphome_provisioning import EspHomeProvisioner

    lab = legacy_pair
    box, p4 = lab.legacy_phones
    box.origin.runtime_data.first_connect_done.clear()
    assert await async_migrate_entry(lab.hass, lab.entry)
    assert set(account_rows(lab)) == {p4.device.id}
    controller = lab.runtime.phone_provisioning = EspHomeProvisioner(lab.hass, lab.entry)
    controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    box.origin.runtime_data.first_connect_done.set()
    async def inventory_action(_call):
        pass
    lab.hass.services.async_register("esphome", "legacy_inventory_available", inventory_action)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert account_rows(lab)[box.device.id].data["extension"] == "101"


async def test_available_empty_native_text_and_false_switch_are_legitimate_values(legacy_pair):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.group_config import group_records

    lab = legacy_pair
    box = lab.legacy_phones[0]
    for role in ("voip_extension", "voip_ring_groups", "voip_conference_groups"):
        lab.hass.states.async_set(box.settings[role].entity_id, "")
    lab.hass.states.async_set(box.settings["voip_ring_on_conference"].entity_id, "off")
    assert await async_migrate_entry(lab.hass, lab.entry)
    row = account_rows(lab)[box.device.id]
    assert row.data["extension"] == row.data["conference_group"] == ""
    assert row.data["conference_ring"] is False
    assert lab.entry.data["legacy_phone_references"][box.old] == "Box"
    assert not group_records(lab.entry, "ring")


async def test_optional_controls_absent_from_complete_native_inventory_do_not_block_creation(legacy_pair):
    from custom_components.voip_stack import async_migrate_entry

    lab = legacy_pair
    box = lab.legacy_phones[0]
    for role in ("voip_conference_groups", "voip_ring_on_conference"):
        entity = box.settings[role]
        er.async_get(lab.hass).async_remove(entity.entity_id)
        lab.hass.states.async_remove(entity.entity_id)
    assert await async_migrate_entry(lab.hass, lab.entry)
    row = account_rows(lab)[box.device.id]
    assert row.data["extension"] == "101"
    assert row.data["conference_group"] == "" and row.data["conference_ring"] is False


async def test_native_ring_and_queue_texts_seed_existing_ordered_group_owner(legacy_pair):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.group_config import group_records

    lab = legacy_pair
    box, p4 = lab.legacy_phones
    add_native_setting(lab, box.origin, box.device, "voip_queues", "Support, Sales")
    add_native_setting(lab, p4.origin, p4.device, "voip_queues", "Support")
    assert await async_migrate_entry(lab.hass, lab.entry)
    assert {group["name"]: group["members"] for group in group_records(lab.entry, "queue").values()} == {
        "Support": ["Box", "P4"], "Sales": ["Box"]}
    assert {group["name"]: group["members"] for group in group_records(lab.entry, "ring").values()} == {"RG Casa": ["Box"]}
    assert all("ring_group" not in row.data and "queues" not in row.data for row in account_rows(lab).values())


async def test_saved_ha_membership_desire_takes_precedence_over_native_snapshot(legacy_pair):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.group_config import group_records

    lab = legacy_pair
    box = lab.legacy_phones[0]
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "esphome_phone_memberships": {box.old: {"ring_group": "HA Ring", "queues": ["HA Queue"]}}})
    assert await async_migrate_entry(lab.hass, lab.entry)
    assert {group["name"]: group["members"] for group in group_records(lab.entry).values()} == {
        "HA Ring": ["Box"], "HA Queue": ["Box"]}


async def test_created_account_never_reimports_later_hardware_settings_or_name(legacy_pair):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.esphome_provisioning import EspHomeProvisioner
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    from custom_components.voip_stack.group_config import group_records

    lab = legacy_pair
    assert await async_migrate_entry(lab.hass, lab.entry)
    before = {device_id: (row.subentry_id, {field: row.data[field] for field in
              ("username", "password", "extension", "conference_group", "conference_ring")})
              for device_id, row in account_rows(lab).items()}
    groups = {identity: deepcopy(data) for identity, data in group_records(lab.entry).items()}
    delivered = []
    async def set_value(call):
        delivered.append(dict(call.data))
    lab.hass.services.async_register("text", "set_value", set_value)
    for phone in lab.legacy_phones:
        native = phone.origin.runtime_data
        native.device_info = replace(native.device_info, friendly_name="New hardware " + phone.name)
        native.services = native_services()
        lab.hass.states.async_set(phone.settings["voip_extension"].entity_id, "999")
        lab.hass.states.async_set(phone.settings["voip_conference_groups"].entity_id, "New conference")
        lab.hass.states.async_set(phone.settings["voip_ring_groups"].entity_id, "New hardware ring")
        lab.hass.states.async_set(phone.settings["voip_ring_on_conference"].entity_id, "off")
    await async_migrate_account_identities(lab.hass, lab.entry)
    controller = lab.runtime.phone_provisioning = EspHomeProvisioner(lab.hass, lab.entry)
    await controller.async_sync()
    after = {device_id: (row.subentry_id, {field: row.data[field] for field in
             ("username", "password", "extension", "conference_group", "conference_ring")})
             for device_id, row in account_rows(lab).items()}
    assert after == before
    assert group_records(lab.entry) == groups
    assert {data["username"] for action, data in lab.sent if action == "set_account"} == {"Box", "P4"}
    assert delivered and all("New hardware ring" != item["value"] for item in delivered)


async def test_native_hardware_automation_and_entity_ids_survive_settings_import(legacy_pair):
    from homeassistant.components.automation import DATA_COMPONENT
    from homeassistant.setup import async_setup_component
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.phone_config import sync_registry_from_entry

    lab = legacy_pair
    box = lab.legacy_phones[0]
    seen = []
    async def notify(call):
        seen.append(dict(call.data))
    lab.hass.services.async_register("legacy_settings_test", "notify", notify)
    configured = {"id": "retained_hardware_ring", "alias": "Retained hardware ring",
        "triggers": [{"trigger": "voip_stack.phone_state_changed", "options": {"phone": box.device.id, "to": "ringing"}}],
        "actions": [{"action": "legacy_settings_test.notify", "data": {"message": "kept-trigger"}}]}
    assert await async_setup_component(lab.hass, "automation", {"automation": [configured]})
    await lab.hass.async_block_till_done()
    automation = lab.hass.data[DATA_COMPONENT].get_entity("automation.retained_hardware_ring")
    raw = deepcopy(automation.raw_config)
    identities = registry_snapshot(lab)
    assert await async_migrate_entry(lab.hass, lab.entry)
    sync_registry_from_entry(lab.hass, lab.entry)
    assert automation.raw_config == raw
    assert registry_snapshot(lab) == identities
    session = lab.runtime.sip.upsert("migrated-hardware-ring", state="ringing", owner="router",
        caller="Caller", callee="Box", dest_endpoint_id="Box")
    lab.runtime.sip.add_leg(session.call_id, "migrated-callee", role="callee", state="ringing", endpoint_id="Box")
    lab.runtime.sip.claim_endpoint(session.call_id, "Box", role="destination")
    await lab.hass.async_block_till_done()
    assert seen == [{"message": "kept-trigger"}]
    await lab.runtime.sip.request_termination(session.call_id, TerminationIntent("test_complete"))


async def test_failed_creation_commit_rolls_back_settings_policy_groups_and_native_registries(legacy_pair, monkeypatch):
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities

    lab = legacy_pair
    before = deepcopy(lab.entry.as_dict())
    identities = registry_snapshot(lab)
    original = lab.hass.config_entries.async_update_entry
    injected = False
    def fail_once(entry, **values):
        nonlocal injected
        if entry is lab.entry and not injected and values.get("data", {}).get("legacy_phone_references"):
            injected = True
            raise RuntimeError("injected native migration commit failure")
        return original(entry, **values)
    with monkeypatch.context() as patcher:
        patcher.setattr(lab.hass.config_entries, "async_update_entry", fail_once)
        with pytest.raises(RuntimeError, match="native migration commit failure"):
            await async_migrate_account_identities(lab.hass, lab.entry)
    assert injected
    restored = lab.entry.as_dict()
    # HA updates its modification timestamp for rollback writes. Every saved
    # configuration value, native subentry and identity must still match.
    assert restored.pop("modified_at") >= before.pop("modified_at")
    assert restored == before
    assert registry_snapshot(lab) == identities
    assert not account_rows(lab)
    await async_migrate_account_identities(lab.hass, lab.entry)
    assert {row.data["extension"] for row in account_rows(lab).values()} == {"101", "430"}
