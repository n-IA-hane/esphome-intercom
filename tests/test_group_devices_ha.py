"""Group service devices share the persistent owners of phone membership."""

import pytest
from unittest.mock import AsyncMock
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_managed_phones_ha import phone_lab as phone_lab
from .test_managed_phones_ha import managed as managed

pytestmark = pytest.mark.ha


def add_phone(lab, identity, *, ring="", queues=()):
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    lab.hass.config_entries.async_add_subentry(lab.entry, ConfigSubentry(
        subentry_type="phone", title=identity, unique_id=identity,
        data={"endpoint_id": identity, "kind": "browser", "name": identity,
              "ring_group": ring, "queues": list(queues), "enabled": True}))
    sync_registry_from_entry(lab.hass, lab.entry)


async def test_queue_picker_uses_managed_account_device_and_legacy_esp_hardware(managed):
    from custom_components.voip_stack.endpoint_device import selection_device_refs
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from .test_managed_phones_ha import account_service_device, phone_record

    lab = managed
    await lab.controller.async_sync()
    account = phone_record(lab)
    service = account_service_device(lab)
    hardware = dr.async_get(lab.hass).async_get_or_create(config_entry_id=lab.native.entry_id,
        identifiers={("esphome", "legacy-queue-phone")}, name="Offline legacy phone")
    legacy = PhoneEndpoint(endpoint_id="esphome:" + hardware.id, name="Offline legacy phone",
                           device_id=hardware.id, kind=EndpointKind.ESPHOME, availability="offline")
    lab.runtime.endpoints.register(legacy)
    assert selection_device_refs(lab.hass, lab.entry, [account.endpoint_id, legacy.endpoint_id]) == {
        service.id: account.endpoint_id, hardware.id: legacy.endpoint_id}


async def test_missing_saved_queue_member_device_cannot_be_dropped_on_open(phone_lab):
    from custom_components.voip_stack.group_config import group_subentries, reconcile_group_records
    from custom_components.voip_stack.phone_queue_membership import iter_phone_memberships
    from .support.ha_frontend import prepare_frontend_dependency

    lab = phone_lab
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    add_phone(lab, "unmapped", queues=["Support"])
    registry = dr.async_get(lab.hass)
    device_id = lab.runtime.endpoints.require("unmapped").device_id
    if device_id and registry.async_get(device_id):
        registry.async_remove_device(device_id)
    reconcile_group_records(lab.hass, lab.entry)
    (group,) = group_subentries(lab.entry)
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "group"),
        context={"source": "reconfigure", "subentry_id": group.subentry_id})
    assert form["type"] == "abort" and form["reason"] == "queue_member_device_missing"
    assert dict(iter_phone_memberships(lab.hass, lab.entry, "queue")) == {"unmapped": ("Support",)}


async def test_auto_group_device_shared_by_two_phones_and_removed_only_when_empty(phone_lab):
    from custom_components.voip_stack.group_config import reconcile_group_records, group_records
    lab = phone_lab
    add_phone(lab, "first", ring="Reception", queues=["1002"])
    reconcile_group_records(lab.hass, lab.entry)
    original = group_records(lab.entry)
    assert {row["kind"] for row in original.values()} == {"ring", "queue"}
    add_phone(lab, "second", ring="reception", queues=["1002"])
    reconcile_group_records(lab.hass, lab.entry)
    assert group_records(lab.entry) == original
    group_devices = [device for device in dr.async_get(lab.hass).devices
                     if any(domain == "voip_stack" and identity.startswith("group:") for domain, identity in device.identifiers)]
    assert len(group_devices) == 2
    lab.runtime.endpoints.update("first", availability="offline")
    reconcile_group_records(lab.hass, lab.entry)
    assert group_records(lab.entry) == original
    for phone in tuple(lab.entry.subentries.values()):
        if phone.subentry_type == "phone":
            lab.hass.config_entries.async_remove_subentry(lab.entry, phone.subentry_id)
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    sync_registry_from_entry(lab.hass, lab.entry)
    reconcile_group_records(lab.hass, lab.entry)
    assert group_records(lab.entry) == {}


async def test_manual_empty_group_survives_and_keeps_identity_on_rename(phone_lab):
    from custom_components.voip_stack.group_config import normalize_group_data, reconcile_group_records, group_records
    lab = phone_lab
    data = normalize_group_data({"kind": "queue", "name": "Support", "extension": "1002"})
    item = ConfigSubentry(subentry_type="group", title="Support", data=data, unique_id=f"group:{data['group_id']}")
    lab.hass.config_entries.async_add_subentry(lab.entry, item)
    reconcile_group_records(lab.hass, lab.entry)
    assert group_records(lab.entry)[data["group_id"]]["origin"] == "manual"
    lab.hass.config_entries.async_update_subentry(lab.entry, item, title="Helpdesk", data={**data, "name": "Helpdesk"})
    reconcile_group_records(lab.hass, lab.entry)
    assert list(group_records(lab.entry)) == [data["group_id"]]
    device = next(device for device in dr.async_entries_for_config_entry(dr.async_get(lab.hass), lab.entry.entry_id)
                  if ("voip_stack", f"group:{data['group_id']}") in device.identifiers)
    assert device.name == "Helpdesk"


async def test_queue_rename_keeps_logged_out_intent_and_service_changes_policy(phone_lab):
    from custom_components.voip_stack.group_config import group_subentries
    from custom_components.voip_stack.call_queues import QueueManager
    lab = phone_lab
    add_phone(lab, "first", queues=["Support"])
    manager = QueueManager(lab.hass, lab.entry)
    lab.runtime.queues = manager
    manager.refresh()
    queue = manager.resolve("Support")
    manager.set_agent_state(queue.queue_id, "first", "logged_out")
    manager.configure(queue.queue_id, {"max_wait": 20})
    item = group_subentries(lab.entry)[0]
    lab.hass.config_entries.async_update_subentry(lab.entry, item, title="Helpdesk", data={**item.data, "name": "Helpdesk"})
    phone = next(item for item in lab.entry.subentries.values() if item.subentry_type == "phone")
    lab.hass.config_entries.async_update_subentry(lab.entry, phone, data={**phone.data, "queues": ["Helpdesk"]})
    manager.refresh()
    assert manager.resolve("Helpdesk").queue_id == queue.queue_id
    assert manager.agent_states[queue.queue_id]["first"] == "logged_out"
    assert manager.policy(queue.queue_id).max_wait == 20


async def test_namespace_conflict_rejected_before_manual_group_save(phone_lab):
    from custom_components.voip_stack.group_config import normalize_group_data, validate_group_namespace
    lab = phone_lab
    add_phone(lab, "Existing")
    with pytest.raises(ValueError, match="another destination"):
        validate_group_namespace(lab.hass, lab.entry, normalize_group_data({"kind": "ring", "name": "Existing"}))


async def test_existing_long_ring_text_survives_import_and_group_reconcile(managed, caplog):
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.group_config import reconcile_group_records, group_records
    from custom_components.voip_stack.managed_phones import managed_records
    from custom_components.voip_stack.router import RouteAction, resolve_ha_router
    lab = managed
    name = "Legacy ring group with a name longer than thirty two bytes"
    entity = er.async_get(lab.hass).async_get_or_create("text", "esphome", "84c7bb000001-voip_ring_groups",
        config_entry=lab.native, device_id=lab.device.id, suggested_object_id="voip_ring_groups")
    lab.hass.states.async_set(entity.entity_id, name)
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    phone = managed_records(lab.entry)[lab.device.id]
    assert phone.ring_group == name
    assert lab.runtime.endpoints.get(phone.endpoint_id).ring_group == name
    reconcile_group_records(lab.hass, lab.entry)
    (group,) = group_records(lab.entry, "ring").values()
    assert group["name"] == name and group["origin"] == "auto"
    caplog.clear()
    await async_refresh_directory(lab.hass)
    rows = lab.runtime.directory.snapshot()
    (published,) = [row for row in rows if row.name == name]
    assert published.metadata["group_id"] == group["group_id"]
    assert published.metadata["members"] == [phone.username]
    route = resolve_ha_router(name, rows)
    assert route.action == RouteAction.GROUP and route.entry is published
    assert f"Skipping group {name}" not in caplog.text


@pytest.mark.parametrize("kind", ["ring", "queue"])
async def test_native_group_flow_updates_canonical_membership_rename_and_empty_lifecycle(phone_lab, monkeypatch, kind):
    """Exercise HA's subentry flow and its production update listener together."""
    from custom_components.voip_stack import config_entry_runtime, phone_services, trunk_runtime
    from custom_components.voip_stack.call_queues import QueueManager
    from custom_components.voip_stack.group_config import group_records
    from custom_components.voip_stack.phone_queue_membership import iter_phone_memberships

    lab = phone_lab
    add_phone(lab, "first")
    add_phone(lab, "second")
    from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
    registry = lab.runtime.endpoints
    phone_devices = {key: async_ensure_endpoint_device(lab.hass, lab.entry, registry.require(key), registry).id
                     for key in ("first", "second")}

    def membership(*identities):
        if kind == "queue":
            return {"queue_members": [{"device": phone_devices[key]} for key in identities]}
        return {"members": list(identities)}
    from .support.ha_frontend import prepare_frontend_dependency
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    prepare_frontend_dependency(lab.hass)
    reload_entry = AsyncMock()
    monkeypatch.setattr(lab.hass.config_entries, "async_reload", reload_entry)
    monkeypatch.setattr(config_entry_runtime, "async_refresh_and_push_phonebook", AsyncMock())
    monkeypatch.setattr(trunk_runtime, "async_reconcile_trunks", AsyncMock())
    monkeypatch.setattr(phone_services, "async_reconcile_phone_services", AsyncMock())
    lab.runtime.entry_runtime_signature = config_entry_runtime.entry_runtime_signature(lab.entry)
    lab.runtime.entry_phone_signature = config_entry_runtime.entry_phone_signature(lab.entry)

    form = await lab.hass.config_entries.subentries.async_init(
        (lab.entry.entry_id, "group"), context={"source": "user"})
    form = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], {"kind": kind})
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({
        "name": "Team", **membership("first"), "keep_empty": True}))
    assert result["type"] == "create_entry"
    await config_entry_runtime.async_config_entry_updated(lab.hass, lab.entry)
    record = next(iter(group_records(lab.entry).values()))
    identity = record["group_id"]
    assert record["origin"] == "manual" and "member_update" not in record
    assert dict(iter_phone_memberships(lab.hass, lab.entry, kind))["first"] == ("Team",)
    manager = None
    if kind == "queue":
        manager = lab.runtime.queues = QueueManager(lab.hass, lab.entry)
        manager.refresh()
        manager.set_agent_state(identity, "first", "logged_out")
    item = next(item for item in lab.entry.subentries.values() if item.subentry_type == "group")
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "group"),
        context={"source": "reconfigure", "subentry_id": item.subentry_id})
    if kind == "queue":
        assert "agent_order" not in form["data_schema"]({}) and "members" not in form["data_schema"]({})
        assert form["data_schema"]({})["queue_members"] == membership("first")["queue_members"]
        bad = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({
            "name": "Team", **membership("first", "first")}))
        assert bad["errors"] == {"base": "invalid_group"}
        assert next(iter(group_records(lab.entry).values()))["policy"]["agent_order"] == ("first",)
        group_device = dr.async_get(lab.hass).async_get_device_by_identifier(("voip_stack", "group:"+identity), lab.entry.entry_id)
        bad = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({
            "name": "Team", "queue_members": [{"device": group_device.id}]}))
        assert bad["errors"] == {"base": "invalid_group"}

    renamed = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({
        "name": "Renamed", "extension": "1710", **membership("second", "first"), "keep_empty": True}))
    assert renamed["type"] == "abort"
    await config_entry_runtime.async_config_entry_updated(lab.hass, lab.entry)
    records = group_records(lab.entry)
    assert set(records) == {identity}
    assert records[identity]["name"] == "Renamed"
    assert dict(iter_phone_memberships(lab.hass, lab.entry, kind)) == {
        "first": ("Renamed",), "second": ("Renamed",)}
    if manager is not None:
        assert manager.resolve("Renamed").queue_id == identity
        assert manager.agent_states[identity]["first"] == "logged_out"
        assert manager.agent_states[identity]["second"] == "available"
    form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "group"),
        context={"source": "reconfigure", "subentry_id": item.subentry_id})
    if kind == "queue":
        assert records[identity]["policy"]["agent_order"] == ("second", "first")
        assert form["data_schema"]({})["queue_members"] == membership("second", "first")["queue_members"]
        # A policy from an older configuration can omit a member or refer to a deleted phone.
        lab.hass.config_entries.subentries.async_abort(form["flow_id"])
        old = lab.entry.subentries[item.subentry_id]
        lab.hass.config_entries.async_update_subentry(lab.entry, old, data={
            **old.data, "policy": {**old.data["policy"], "agent_order": ["stale", "second"]}})
        form = await lab.hass.config_entries.subentries.async_init((lab.entry.entry_id, "group"),
            context={"source": "reconfigure", "subentry_id": item.subentry_id})
        assert form["data_schema"]({})["queue_members"] == membership("second", "first")["queue_members"]
        assert group_records(lab.entry)[identity]["policy"]["agent_order"] == ["stale", "second"]

    empty = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"]({
        "name": "Renamed", **membership(), "keep_empty": False}))
    assert empty["type"] == "abort"
    await config_entry_runtime.async_config_entry_updated(lab.hass, lab.entry)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert group_records(lab.entry) == {}
    assert not any(("voip_stack", f"group:{identity}") in device.identifiers
                   for device in dr.async_entries_for_config_entry(dr.async_get(lab.hass), lab.entry.entry_id))
    assert all(not names for _endpoint_id, names in iter_phone_memberships(lab.hass, lab.entry, kind))
    reload_entry.assert_not_awaited()
    if manager is not None:
        await manager.close()
