"""Native SIP account administration, memberships and recovery lifecycle."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
import json

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    managed as managed,
    phone_lab as phone_lab,
    phone_subentry, native_services,
)

pytestmark = pytest.mark.ha


def native_account(hass, entry, username, **data):
    row = ConfigSubentry(subentry_type="phone", title=data.pop("title", username),
        unique_id="phone:" + username, data={"kind": "sip_account", "name": username,
            "username": username, "username_source": "user", "password": "fixture-account-password",
            "enabled": True, **data})
    hass.config_entries.async_add_subentry(entry, row)
    return row


def second_native_phone(lab):
    """Add a real native HA hardware owner; only its API transport is an IO spy."""
    from homeassistant.components.esphome.entry_data import RuntimeEntryData

    native = MockConfigEntry(domain="esphome", title="Second phone", data={
        "host": "127.0.0.3", "device_name": "second_node"})
    native.add_to_hass(lab.hass)
    device = dr.async_get(lab.hass).async_get_or_create(config_entry_id=native.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, "84:c7:bb:00:00:02")}, name="Second phone")
    native.runtime_data = RuntimeEntryData(entry_id=native.entry_id, title=native.title,
        client=SimpleNamespace(is_connected=True, execute_service=AsyncMock()), store=MagicMock(),
        device_info=replace(lab.native.runtime_data.device_info, name="second_node",
            friendly_name="Second phone", mac_address="84:C7:BB:00:00:02"),
        services=native_services(), available=True)
    native.runtime_data.first_connect_done.set()
    sensor = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "84c7bb000002-voip_state",
        config_entry=native, device_id=device.id)
    lab.hass.states.async_set(sensor.entity_id, "idle")
    return native, device


async def account_action(lab, action, username):
    from custom_components.voip_stack.account_services import build_account_service_handlers

    refresh = AsyncMock()
    handlers = build_account_service_handlers(refresh)
    data = {"username": username}
    if action == "create_account":
        data.update(password="fixture-created-password", display_name=username)
    result = await handlers[action](ServiceCall(lab.hass, "voip_stack", action, data))
    refresh.assert_awaited_once()
    return result


@pytest.mark.parametrize(("action", "target"), [
    ("create_account", "New"), ("remove_account", "Manual"),
    ("rotate_account_password", "Manual"), ("enable_account", "Manual"),
    ("disable_account", "Manual"), ("remove_account", "Ingresso"),
    ("rotate_account_password", "Ingresso"), ("enable_account", "Ingresso"),
    ("disable_account", "Ingresso"),
])
async def test_standard_account_crud_with_esp_preserves_the_other_native_owner(managed, action, target):
    from custom_components.voip_stack.phone_config import sync_registry_from_entry

    lab = managed
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=4,
        data={**lab.entry.data, "group_membership_version": 1})
    await lab.controller.async_sync()
    esp = phone_subentry(lab)
    manual = native_account(lab.hass, lab.entry, "Manual")
    sync_registry_from_entry(lab.hass, lab.entry)
    other = manual if target == "Ingresso" else esp
    other_before = deepcopy(other.as_dict())
    target_row = esp if target == "Ingresso" else manual
    if action == "enable_account":
        lab.hass.config_entries.async_update_subentry(lab.entry, target_row,
            data={**target_row.data, "enabled": False})
    target_before = deepcopy(target_row.as_dict())
    native_ids = esp.subentry_id, lab.device.id
    await account_action(lab, action, target)
    assert other.as_dict() == other_before
    assert native_ids == (esp.subentry_id, lab.device.id)
    if action == "remove_account":
        assert target_row.subentry_id not in lab.entry.subentries
        if target == "Ingresso":
            await lab.controller.async_sync()
            assert not any(row.data.get("username") == target for row in lab.entry.subentries.values())
    elif action == "create_account":
        assert any(row.data.get("username") == "New" for row in lab.entry.subentries.values())
        assert manual.as_dict() == target_before
    else:
        assert target_row.subentry_id in lab.entry.subentries
        expected = deepcopy(target_before)
        if action == "rotate_account_password":
            assert target_row.data["password"] != expected["data"]["password"]
            expected["data"]["password"] = target_row.data["password"]
        else:
            expected["data"]["enabled"] = action == "enable_account"
        assert target_row.as_dict() == expected
    assert lab.device.config_entry_id == lab.native.entry_id


@pytest.mark.parametrize("operation", ["password", "dnd", "create"])
async def test_unrelated_account_edits_keep_ordered_groups_queue_states_and_no_phone_copies(phone_lab, operation):
    from custom_components.voip_stack.group_config import normalize_group_data, group_records
    from custom_components.voip_stack.phone_config import sync_registry_from_entry, update_phone_subentry

    lab = phone_lab
    first = native_account(lab.hass, lab.entry, "First")
    native_account(lab.hass, lab.entry, "Second")
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "group_membership_version": 1})
    groups = []
    for kind, name in [("ring", "Reception"), ("queue", "Support")]:
        data = normalize_group_data({"kind": kind, "name": name, "members": ["Second", "First"], "enabled": False})
        row = ConfigSubentry(subentry_type="group", title=name, unique_id="group:" + data["group_id"], data=data)
        lab.hass.config_entries.async_add_subentry(lab.entry, row)
        groups.append(row)
    store = Store(lab.hass, 1, "voip_stack.queues." + lab.entry.entry_id)
    saved = {"agent_states": {groups[1].data["group_id"]: {"First": "paused", "Second": "available"}}}
    await store.async_save(saved)
    before = [deepcopy(row.as_dict()) for row in groups]
    sync_registry_from_entry(lab.hass, lab.entry)
    if operation == "password":
        await account_action(lab, "rotate_account_password", "First")
    elif operation == "create":
        await account_action(lab, "create_account", "Third")
    else:
        update_phone_subentry(lab.hass, lab.entry, "First", {"dnd": True})
        assert first.data["dnd"] is True
    assert [row.as_dict() for row in groups] == before
    assert all(record["members"] == ["Second", "First"] and not record["enabled"]
               for record in group_records(lab.entry).values())
    assert await store.async_load() == saved
    assert all("queues" not in row.data and "ring_group" not in row.data
               for row in lab.entry.subentries.values() if row.subentry_type == "phone")


async def test_deleted_invalid_record_closes_native_repair_without_touching_waves(hass, tmp_path):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.components.repairs.issue_handler import async_setup
    from custom_components.voip_stack.repairs import async_sync_archive_record_issues

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=4, data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    hass.config.media_dirs = {"local": str(tmp_path)}
    root = tmp_path / "voip_stack"
    root.mkdir()
    invalid = root / ("a" * 32 + ".json")
    invalid.write_bytes(b"{invalid")
    wave = root / ("a" * 32 + "-000.wav")
    wave.write_bytes(b"preserved-orphan-audio")
    await async_sync_archive_record_issues(hass, entry, {invalid.name: "invalid_recording_metadata"})
    issue_id = "archive_record_" + entry.entry_id + "_" + invalid.name
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is not None
    unavailable = tmp_path / "temporarily-unavailable-archive"
    root.rename(unavailable)
    await async_sync_archive_record_issues(hass, entry, {})
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is not None
    unavailable.rename(root)
    invalid.unlink()
    invalid.symlink_to(root / "missing-metadata-target")
    await async_sync_archive_record_issues(hass, entry, {})
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is not None
    invalid.unlink()
    hass.config.components.add("voip_stack")
    hass.data.setdefault("repairs", {})
    async_setup(hass)
    result = await repairs_flow_manager(hass).async_init("voip_stack", context={"issue_id": issue_id})
    assert result["type"] == "create_entry"
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is None
    assert wave.read_bytes() == b"preserved-orphan-audio"


async def test_pending_native_name_confirmation_keeps_explicit_account_labels_and_hardware_rename(managed):
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    from custom_components.voip_stack.managed_phones import managed_records

    lab = managed
    row = native_account(lab.hass, lab.entry, "esp_84c7bb000001", title="Explicit native title",
        name="Chosen account name", display_name="Chosen display", username_source="esphome_pending",
        managed_esphome=True, managed_exclusive=True, device_id=lab.device.id,
        origin_entry_id=lab.native.entry_id, mac="84c7bb000001", route_id="phone_node")
    before_id = row.subentry_id
    await async_migrate_account_identities(lab.hass, lab.entry)
    assert row.data["username"] == "Ingresso"
    assert row.data["name"] == "Chosen account name" and row.data["display_name"] == "Chosen display"
    assert row.title == "Explicit native title" and row.subentry_id == before_id
    password = row.data["password"]
    lab.native.runtime_data.device_info = replace(lab.native.runtime_data.device_info,
        friendly_name="Changed hardware label", name="changed_node")
    dr.async_get(lab.hass).async_update_device(lab.device.id, name="Changed hardware label", name_by_user="Custom hardware")
    await lab.controller.async_sync()
    assert row.data["username"] == "Ingresso" and row.data["password"] == password
    assert (row.data["name"], row.data["display_name"], row.title) == (
        "Chosen account name", "Chosen display", "Explicit native title")
    assert managed_records(lab.entry)[lab.device.id].display_name == "Chosen account name"


async def test_corrupt_legacy_record_does_not_rescan_archive_or_block_healthy_provisioning(managed, monkeypatch):
    from custom_components.voip_stack import sip_account_identity

    lab = managed
    await lab.controller.async_sync()
    healthy = phone_subentry(lab)
    before = deepcopy(healthy.as_dict())
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=4,
        data={**lab.entry.data, "managed_esphome_phones": {"broken-hardware": {"queues": 42}}})
    migration = AsyncMock(wraps=sip_account_identity.async_migrate_account_identities)
    monkeypatch.setattr(sip_account_identity, "async_migrate_account_identities", migration)
    lab.controller._delivered.clear()
    lab.sent.clear()
    await lab.controller.async_sync()
    await lab.controller.async_sync()
    migration.assert_not_awaited()
    assert ir.async_get(lab.hass).async_get_issue("voip_stack",
        "managed_record_" + lab.entry.entry_id + "_broken-hardware").is_fixable
    assert healthy.as_dict() == before
    assert lab.entry.data["managed_esphome_phones"]["broken-hardware"] == {"queues": 42}
    assert any(name == "set_account" for name, _values in lab.sent)


async def test_failed_legacy_retry_runs_once_per_changed_configuration_not_per_call(managed, monkeypatch):
    from custom_components.voip_stack import sip_account_identity

    lab = managed
    old = "esphome:" + lab.device.id
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=4,
        data={**lab.entry.data, "esphome_phone_memberships": {old: {"queues": ["Support"]}}})
    migration = AsyncMock(side_effect=OSError("fixture storage failure"))
    monkeypatch.setattr(sip_account_identity, "async_migrate_account_identities", migration)
    assert await lab.controller._async_reconcile_account_names()
    assert await lab.controller._async_reconcile_account_names()
    migration.assert_awaited_once()
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "esphome_phone_memberships": {old: {"queues": ["Changed"]}}})
    assert await lab.controller._async_reconcile_account_names()
    assert migration.await_count == 2


@pytest.mark.parametrize("fault", [None, "group", "physical_phone", "entity"])
async def test_rename_membership_and_physical_binding_commit_and_rollback_share_one_owner(hass, tmp_path, monkeypatch, fault):
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.media_archive import MediaArchive
    from custom_components.voip_stack.group_config import normalize_group_data
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=4,
        data={"initial_phone_created": True, "group_membership_version": 1})
    entry.add_to_hass(hass)
    account = native_account(hass, entry, "Desk")
    group_data = normalize_group_data({"kind": "ring", "name": "Reception", "members": ["Desk"]})
    group = ConfigSubentry(subentry_type="group", title="Reception", unique_id="group:" + group_data["group_id"], data=group_data)
    physical = ConfigSubentry(subentry_type="physical_phone", title="Physical", unique_id="physical_phone:hardware",
        data={"phone_id": "hardware", "name": "Physical", "mac": "001122334455",
              "lines": [{"slot": "1", "endpoint_id": "Desk"}], "keys": [], "provisioning_token": "fixture-kept-token"})
    for row in (group, physical):
        hass.config_entries.async_add_subentry(entry, row)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=account.subentry_id,
        identifiers={("voip_stack", "phone_endpoint:Desk")}, name="Native account")
    entity = er.async_get(hass).async_get_or_create("sensor", "voip_stack", "phone_endpoint_Desk_call_state",
        config_entry=entry, config_subentry_id=account.subentry_id, device_id=device.id)
    hass.config.media_dirs = {"local": str(tmp_path)}
    archive = MediaArchive(tmp_path / "voip_stack", hass.async_add_executor_job)
    await archive.load()
    writer = await archive.begin("Desk")
    assert writer.offer(b"\x12\x34" * 320, AudioFormat(16000, "s16le", 1, 20), offset=0)
    message = await writer.finish()
    await archive.close()
    metadata = archive.root / (message["id"] + ".json")
    waves = {p.name: p.read_bytes() for p in archive.root.glob("*.wav")}
    queue = Store(hass, 1, "voip_stack.queues." + entry.entry_id)
    queue_before = {"agent_states": {"support": {"Desk": "paused"}}}
    await queue.async_save(queue_before)
    before = {row.subentry_id: deepcopy(row.as_dict()) for row in entry.subentries.values()}
    old_metadata = metadata.read_bytes()
    original_update = hass.config_entries.async_update_subentry
    injected = False
    def fail_subentry(owner, row, **values):
        nonlocal injected
        if fault == row.subentry_type and not injected:
            injected = True
            raise ValueError("fixture native commit failure")
        return original_update(owner, row, **values)
    monkeypatch.setattr(hass.config_entries, "async_update_subentry", fail_subentry)
    entities = er.async_get(hass)
    original_entity = entities.async_update_entity
    def fail_entity(identity, **values):
        nonlocal injected
        if fault == "entity" and not injected:
            injected = True
            raise ValueError("fixture entity commit failure")
        return original_entity(identity, **values)
    monkeypatch.setattr(entities, "async_update_entity", fail_entity)
    update = {**account.data, "username": "Renamed", "ring_group": "Other", "queues": []}
    binding = {**physical.data, "lines": [{"slot": "2", "endpoint_id": "Desk"}]}
    if fault:
        with pytest.raises(ValueError, match="fixture .*commit failure"):
            await async_migrate_account_identities(hass, entry,
                account_updates={account.subentry_id: update}, physical_bindings=((physical, binding),))
        assert injected
        assert {row.subentry_id: row.as_dict() for row in entry.subentries.values()} == before
        assert metadata.read_bytes() == old_metadata
        assert await queue.async_load() == queue_before
        assert entities.async_get(entity.entity_id).unique_id == "phone_endpoint_Desk_call_state"
        assert dr.async_get(hass).async_get(device.id).identifiers == {("voip_stack", "phone_endpoint:Desk")}
    else:
        await async_migrate_account_identities(hass, entry,
            account_updates={account.subentry_id: update}, physical_bindings=((physical, binding),))
        assert account.unique_id == "phone:Renamed"
        assert group.data["members"] == []
        assert next(row for row in entry.subentries.values() if row.data.get("name") == "Other").data["members"] == ["Renamed"]
        assert physical.data["lines"] == [{"slot": "2", "endpoint_id": "Renamed"}]
        assert physical.data["provisioning_token"] == "fixture-kept-token"
        assert json.loads(metadata.read_text())["mailbox_id"] == "Renamed"
        assert (await queue.async_load())["agent_states"]["support"] == {"Renamed": "paused"}
        assert entities.async_get(entity.entity_id).id == entity.id
    assert {p.name: p.read_bytes() for p in archive.root.glob("*.wav")} == waves


async def test_legacy_entity_prefix_uses_its_exact_native_owner_not_another_account_prefix(hass):
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=4, data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    desk = native_account(hass, entry, "desk")
    native_account(hass, entry, "desk_phone")
    registry = er.async_get(hass)
    entity = registry.async_get_or_create("sensor", "voip_stack", "phone_endpoint_desk_phone_call_state", config_entry=entry)
    native_id, entity_id = entity.id, entity.entity_id
    assert entity.config_subentry_id is None
    await async_migrate_account_identities(hass, entry,
        account_updates={desk.subentry_id: {**desk.data, "username": "renamed"}})
    current = registry.async_get(entity_id)
    assert current.unique_id == "phone_endpoint_desk_phone_call_state"
    assert (current.id, current.entity_id) == (native_id, entity_id)


async def test_corrupt_legacy_memberships_are_isolated_and_repaired_on_the_original_native_object(managed):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.components.repairs.issue_handler import async_setup

    lab = managed
    await lab.controller.async_sync()
    healthy = phone_subentry(lab)
    healthy_username = healthy.data["username"]
    native, hardware = second_native_phone(lab)
    old = "esphome:" + hardware.id
    broken = {"queues": 42, "extension": "201", "unrelated": {"keep": True}}
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=4,
        data={**lab.entry.data, "esphome_phone_memberships": {old: broken}})
    before = deepcopy(healthy.as_dict())
    lab.controller._delivered.clear()
    lab.sent.clear()
    issue_id = "managed_record_" + lab.entry.entry_id + "_" + hardware.id
    for _ in range(2):
        # Exercise the complete owner, including its continuation after the
        # quarantined record was rejected. The saved payload has no device_id.
        await lab.controller.async_sync()
        assert healthy.as_dict() == before
        assert lab.entry.data["esphome_phone_memberships"][old] == broken
        issue = ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id)
        assert issue is not None and issue.is_fixable
        assert issue.data["membership_reference"] == old
    assert any(name == "set_account" and values["username"] == healthy_username for name, values in lab.sent)
    native.runtime_data.client.execute_service.assert_not_awaited()
    lab.hass.config.components.add("voip_stack")
    lab.hass.data.setdefault("repairs", {})
    async_setup(lab.hass)
    manager = repairs_flow_manager(lab.hass)
    form = await manager.async_init("voip_stack", context={"issue_id": issue_id})
    assert form["step_id"] == "account"
    result = await manager.async_configure(form["flow_id"], {"device_id": hardware.id,
        "username": "Recovered", "name": "Recovered", "queues": ["Support"]})
    assert result["type"] == "create_entry", result
    recovered = next(row for row in lab.entry.subentries.values() if row.data.get("username") == "Recovered")
    assert recovered.data["device_id"] == hardware.id
    assert recovered.data["origin_entry_id"] == native.entry_id
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is None
    assert healthy.as_dict() == before


async def test_complete_legacy_record_with_wrong_hardware_association_retains_actionable_repair_across_syncs(managed):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.components.repairs.issue_handler import async_setup
    lab = managed
    await lab.controller.async_sync()
    healthy = phone_subentry(lab)
    healthy_username = healthy.data["username"]
    native, hardware = second_native_phone(lab)
    broken = {"device_id": lab.device.id, "origin_entry_id": native.entry_id,
        "mac": "84c7bb000002", "username": "Pending account", "password": "fixture-retained-password",
        "endpoint_id": "esphome:" + hardware.id, "display_name": "Retained account", "queues": ["Support"]}
    assert broken["device_id"] != hardware.id
    from custom_components.voip_stack.managed_phones import ManagedPhone
    ManagedPhone(**broken)  # Complete, valid fields; only the map's owner disagrees.
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=4,
        data={**lab.entry.data, "managed_esphome_phones": {hardware.id: broken}})
    before = deepcopy(healthy.as_dict())
    lab.controller._delivered.clear()
    lab.sent.clear()
    issue_id = "managed_record_" + lab.entry.entry_id + "_" + hardware.id
    for _ in range(2):
        await lab.controller.async_sync()
        assert lab.entry.data["managed_esphome_phones"][hardware.id] == broken
        assert healthy.as_dict() == before
        issue = ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id)
        assert issue is not None and issue.is_fixable
        assert issue.data["hardware"] == hardware.id
    assert any(name == "set_account" and values["username"] == healthy_username for name, values in lab.sent)
    native.runtime_data.client.execute_service.assert_not_awaited()
    lab.hass.config.components.add("voip_stack")
    lab.hass.data.setdefault("repairs", {})
    async_setup(lab.hass)
    form = await repairs_flow_manager(lab.hass).async_init("voip_stack", context={"issue_id": issue_id})
    assert form["step_id"] == "account"
    assert lab.entry.data["managed_esphome_phones"][hardware.id] == broken
    assert healthy.as_dict() == before


async def test_rename_rechecks_live_call_claim_after_storage_prepare_before_native_writes(phone_lab, tmp_path, monkeypatch):
    import asyncio
    from custom_components.voip_stack import sip_account_identity
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.media_archive import MediaArchive
    from custom_components.voip_stack.phone_config import sync_registry_from_entry, PhoneIdentityConflict

    lab = phone_lab
    account = native_account(lab.hass, lab.entry, "Desk")
    sync_registry_from_entry(lab.hass, lab.entry)
    lab.runtime.sip.bind_endpoint_registry(lab.runtime.endpoints)
    device = dr.async_get(lab.hass).async_get_or_create(config_entry_id=lab.entry.entry_id,
        config_subentry_id=account.subentry_id, identifiers={("voip_stack", "phone_endpoint:Desk")})
    entity = er.async_get(lab.hass).async_get_or_create("sensor", "voip_stack", "phone_endpoint_Desk_call_state",
        config_entry=lab.entry, config_subentry_id=account.subentry_id, device_id=device.id)
    lab.hass.config.media_dirs = {"local": str(tmp_path)}
    archive = MediaArchive(tmp_path / "voip_stack", lab.hass.async_add_executor_job)
    await archive.load()
    writer = await archive.begin("Desk")
    assert writer.offer(b"\x12\x34" * 320, AudioFormat(16000, "s16le", 1, 20), offset=0)
    await writer.finish()
    await archive.close()
    files = {p.name: p.read_bytes() for p in archive.root.iterdir()}
    before = deepcopy(account.as_dict()), deepcopy(dict(lab.entry.data)), deepcopy(dict(lab.entry.options))
    entered, release = asyncio.Event(), asyncio.Event()
    original = sip_account_identity._async_prepare_account_queue_migration
    async def prepare(*args):
        result = await original(*args)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(sip_account_identity, "_async_prepare_account_queue_migration", prepare)
    task = lab.hass.async_create_task(sip_account_identity.async_migrate_account_identities(lab.hass, lab.entry,
        account_updates={account.subentry_id: {**account.data, "username": "Renamed"}}))
    try:
        await entered.wait()
        session = lab.runtime.sip.create_session("call-started-during-prepare")
        assert lab.runtime.sip.claim_endpoint(session.call_id, "Desk", role="source")
        runtime_before = lab.runtime.endpoints.endpoints
        release.set()
        with pytest.raises(PhoneIdentityConflict):
            await task
        assert session.live and session.endpoint_claims
        assert lab.runtime.endpoints.require("Desk").active_call_id == session.call_id
        assert lab.runtime.endpoints.get("Renamed") is None
        assert lab.runtime.endpoints.endpoints == runtime_before
        assert (account.as_dict(), dict(lab.entry.data), dict(lab.entry.options)) == before
        assert dr.async_get(lab.hass).async_get(device.id).identifiers == {("voip_stack", "phone_endpoint:Desk")}
        assert er.async_get(lab.hass).async_get(entity.entity_id).unique_id == "phone_endpoint_Desk_call_state"
        assert {p.name: p.read_bytes() for p in archive.root.iterdir()} == files
    finally:
        release.set()
        if not task.done():
            await task


async def test_manual_native_titles_survive_account_services_and_dnd_patches(phone_lab):
    from custom_components.voip_stack.phone_config import sync_registry_from_entry, update_phone_subentry

    lab = phone_lab
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "group_membership_version": 1})
    edited = native_account(lab.hass, lab.entry, "Desk", title="Explicit edited title", name="Desk display")
    other = native_account(lab.hass, lab.entry, "Other", title="Explicit other title", name="Other display")
    sync_registry_from_entry(lab.hass, lab.entry)
    for action in ("rotate_account_password", "disable_account", "enable_account"):
        await account_action(lab, action, "Desk")
        assert (edited.title, other.title) == ("Explicit edited title", "Explicit other title")
    await account_action(lab, "create_account", "Third")
    update_phone_subentry(lab.hass, lab.entry, "Desk", {"dnd": True})
    assert edited.data["dnd"] is True
    assert (edited.title, other.title) == ("Explicit edited title", "Explicit other title")
    assert (edited.data["name"], other.data["name"]) == ("Desk display", "Other display")


async def test_partial_group_conversion_retains_phone_inputs_until_native_version_one(phone_lab):
    from custom_components.voip_stack.group_config import group_records, reconcile_group_records

    lab = phone_lab
    desk = native_account(lab.hass, lab.entry, "Desk", ring_group="Front Ring", queues=["Support"])
    native_account(lab.hass, lab.entry, "Taken")
    contact = ConfigSubentry(subentry_type="contact", title="External", unique_id="contact:external",
        data={"id": "external", "name": "External", "number": "123", "metadata": {"ring_group": "Taken"}})
    lab.hass.config_entries.async_add_subentry(lab.entry, contact)
    await account_action(lab, "rotate_account_password", "Desk")
    assert lab.entry.data.get("group_membership_version") != 1
    assert desk.data["ring_group"] == "Front Ring" and desk.data["queues"] == ["Support"]
    lab.hass.config_entries.async_update_subentry(lab.entry, contact, data={**contact.data, "metadata": {}})
    reconcile_group_records(lab.hass, lab.entry)
    assert lab.entry.data["group_membership_version"] == 1
    assert "ring_group" not in desk.data and "queues" not in desk.data
    assert {row["name"]: row["members"] for row in group_records(lab.entry).values()} == {
        "Front Ring": ["Desk"], "Support": ["Desk"]}


async def test_pending_identity_retry_recovers_after_call_starts_during_prepare(managed, monkeypatch):
    import asyncio
    from custom_components.voip_stack import sip_account_identity
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.phone_config import sync_registry_from_entry

    lab = managed
    old = "esp_84c7bb000001"
    account = native_account(lab.hass, lab.entry, old, username_source="esphome_pending",
        managed_esphome=True, managed_exclusive=True, device_id=lab.device.id,
        origin_entry_id=lab.native.entry_id, mac="84c7bb000001", route_id="phone_node")
    sync_registry_from_entry(lab.hass, lab.entry)
    lab.runtime.sip.bind_endpoint_registry(lab.runtime.endpoints)
    entered, release = asyncio.Event(), asyncio.Event()
    original = sip_account_identity._async_prepare_account_queue_migration
    attempts = 0
    async def pause_once(*args):
        nonlocal attempts
        attempts += 1
        prepared = await original(*args)
        if attempts == 1:
            entered.set()
            await release.wait()
        return prepared
    monkeypatch.setattr(sip_account_identity, "_async_prepare_account_queue_migration", pause_once)
    lab.controller.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        session = lab.runtime.sip.create_session("call-during-pending-name-prepare")
        assert lab.runtime.sip.claim_endpoint(session.call_id, old, role="source")
        release.set()
        await lab.hass.async_block_till_done()
        assert account.data["username"] == old and session.live
        assert attempts == 1
        barrier = lab.runtime.sip.request_termination(session.call_id, TerminationIntent("test_finished"))
        if barrier is not None:
            await barrier
        await lab.hass.async_block_till_done()
        assert account.data["username"] == "Ingresso"
        assert attempts == 2
    finally:
        release.set()
        await lab.controller.async_close()


async def test_pending_identity_retry_observes_extension_only_namespace_correction(managed):
    from custom_components.voip_stack.phone_config import sync_registry_from_entry, update_phone_subentry

    lab = managed
    lab.native.runtime_data.device_info = replace(lab.native.runtime_data.device_info, friendly_name="101")
    account = native_account(lab.hass, lab.entry, "esp_84c7bb000001", username_source="esphome_pending",
        managed_esphome=True, managed_exclusive=True, device_id=lab.device.id,
        origin_entry_id=lab.native.entry_id, mac="84c7bb000001", route_id="phone_node")
    desk = native_account(lab.hass, lab.entry, "Desk", extension="101")
    sync_registry_from_entry(lab.hass, lab.entry)
    await lab.controller._async_reconcile_account_names()
    assert account.data["username"] == "esp_84c7bb000001"
    previous_username = desk.data["username"]
    update_phone_subentry(lab.hass, lab.entry, "Desk", {"extension": "102"})
    await lab.controller._async_reconcile_account_names()
    assert desk.data["username"] == previous_username and desk.data["extension"] == "102"
    assert account.data["username"] == "101"


async def test_existing_native_repair_retries_io_failure_without_restart_or_per_call_load(managed, monkeypatch):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.components.repairs.issue_handler import async_setup
    from custom_components.voip_stack import sip_account_identity

    lab = managed
    raw = {"device_id": lab.device.id, "origin_entry_id": lab.native.entry_id,
        "mac": "84c7bb000001", "username": "Pending", "username_source": "user",
        "password": "fixture-kept-password", "endpoint_id": "esphome:" + lab.device.id,
        "display_name": "Pending"}
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=4,
        data={**lab.entry.data, "managed_esphome_phones": {lab.device.id: raw}})
    original = sip_account_identity._async_prepare_account_queue_migration
    attempts = 0
    async def fail_once(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("fixture temporary storage failure")
        return await original(*args)
    monkeypatch.setattr(sip_account_identity, "_async_prepare_account_queue_migration", fail_once)
    await lab.controller._async_reconcile_account_names()
    await lab.controller._async_reconcile_account_names()
    assert attempts == 1 and not lab.entry.subentries
    issue_id = "phone_identity_conflict_" + lab.entry.entry_id
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id).is_fixable
    lab.hass.config.components.add("voip_stack")
    lab.hass.data.setdefault("repairs", {})
    async_setup(lab.hass)
    manager = repairs_flow_manager(lab.hass)
    form = await manager.async_init("voip_stack", context={"issue_id": issue_id})
    assert form["step_id"] == "confirm"
    result = await manager.async_configure(form["flow_id"], {})
    assert result["type"] == "create_entry", result
    assert attempts == 2
    assert any(row.data.get("username") == "Pending" for row in lab.entry.subentries.values())
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is None
