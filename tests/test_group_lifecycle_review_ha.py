"""Focused witnesses for persisted group commands and canonical membership."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.config_entries import ConfigSubentry
import pytest

from .test_group_devices_ha import add_phone
from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_managed_phones_ha import phone_lab as phone_lab

pytestmark = pytest.mark.ha


def _group(lab, **fields):
    from custom_components.voip_stack.group_config import normalize_group_data

    data = normalize_group_data({"kind": "queue", "name": "Support", **fields})
    item = ConfigSubentry(subentry_type="group", title=data["name"], data=data,
                          unique_id=f"group:{data['group_id']}")
    lab.hass.config_entries.async_add_subentry(lab.entry, item)
    return item


async def test_startup_consumes_saved_membership_command_before_queue_reconcile(phone_lab, monkeypatch):
    from custom_components import voip_stack
    from custom_components.voip_stack import mailbox, phone_services, esphome_provisioning
    from custom_components.voip_stack.group_config import group_records
    from custom_components.voip_stack.phone_queue_membership import iter_phone_memberships
    from homeassistant.helpers import storage

    lab = phone_lab
    add_phone(lab, "first", queues=["Old team"])
    item = _group(lab, name="New team", origin="auto")
    lab.hass.config_entries.async_update_subentry(lab.entry, item,
        data={**item.data, "member_update": ["first"], "previous_name": "Old team"})
    store = SimpleNamespace(async_load=AsyncMock(return_value={}), async_save=AsyncMock(), async_delay_save=Mock())
    monkeypatch.setattr(storage, "Store", lambda *args, **kwargs: store)
    for name in ("_async_setup_shared", "_async_apply_assist_intents", "_async_start_sip_trunk",
                 "_deferred_phonebook_sync", "_async_load_ha_softphone_store"):
        monkeypatch.setattr(voip_stack, name, AsyncMock())
    monkeypatch.setattr(mailbox, "async_setup_mailboxes", AsyncMock())
    monkeypatch.setattr(phone_services, "async_setup_phone_services", AsyncMock())
    monkeypatch.setattr(esphome_provisioning, "setup_provisioning", Mock())
    monkeypatch.setattr(lab.hass.config_entries, "async_forward_entry_setups", AsyncMock())

    async def start(_hass):
        lab.entry.runtime_data.sip = lab.runtime.sip
        return True

    monkeypatch.setattr(voip_stack, "_async_start_sip_endpoint", start)
    assert await voip_stack.async_setup_entry(lab.hass, lab.entry)
    manager = lab.entry.runtime_data.queues
    try:
        assert dict(iter_phone_memberships(lab.hass, lab.entry, "queue"))["first"] == ("New team",)
        records = group_records(lab.entry)
        assert list(records) == [item.data["group_id"]]
        assert "member_update" not in records[item.data["group_id"]]
        assert manager.resolve("New team").members == ("first",)
    finally:
        await manager.close()


async def test_first_stable_group_materialization_migrates_existing_name_keyed_agent_intent(phone_lab, monkeypatch):
    from custom_components.voip_stack.call_queues import QueuePolicy, async_setup_queues
    from dataclasses import asdict
    from homeassistant.helpers import storage

    lab = phone_lab
    add_phone(lab, "first", queues=["Support"])
    old_policy = asdict(QueuePolicy(max_wait=17, wrap_up=9))
    store = SimpleNamespace(async_load=AsyncMock(return_value={
        "policies": {"support": old_policy}, "agent_states": {"support": {"first": "logged_out"}},
    }), async_save=AsyncMock(), async_delay_save=Mock())
    monkeypatch.setattr(storage, "Store", lambda *args, **kwargs: store)
    manager = await async_setup_queues(lab.hass, lab.entry)
    try:
        queue = manager.resolve("Support")
        assert queue.queue_id != "support"
        assert manager.agent_states[queue.queue_id]["first"] == "logged_out"
        assert manager.policy(queue.queue_id).max_wait == 17
        assert manager.policy(queue.queue_id).wrap_up == 9
    finally:
        await manager.close()


@pytest.mark.parametrize("kind", ["ring", "queue"])
async def test_group_checkbox_removal_uses_the_same_aliases_as_group_members(phone_lab, kind):
    from custom_components.voip_stack.group_config import group_members
    from custom_components.voip_stack.phone_queue_membership import async_update_group_members, iter_phone_memberships

    lab = phone_lab
    add_phone(lab, "first", **({"queues": ["Team-A"]} if kind == "queue" else {"ring": "Team-A"}))
    add_phone(lab, "second", **({"queues": ["Team A"]} if kind == "queue" else {"ring": "Team A"}))
    item = _group(lab, kind=kind, name="Team-A")
    assert group_members(lab.hass, lab.entry, item.data) == ("first", "second")
    await async_update_group_members(lab.hass, lab.entry, kind, "Team-A", ["first"])
    assert dict(iter_phone_memberships(lab.hass, lab.entry, kind))["second"] == ()
    assert group_members(lab.hass, lab.entry, item.data) == ("first",)


@pytest.mark.parametrize("kind", ["ring", "queue"])
async def test_deleted_manual_record_does_not_reappear_from_equivalent_phone_alias(phone_lab, kind):
    from custom_components.voip_stack.group_config import async_apply_group_updates, group_records

    lab = phone_lab
    add_phone(lab, "first", **({"queues": ["Team-A"]} if kind == "queue" else {"ring": "Team-A"}))
    add_phone(lab, "second", **({"queues": ["Team A"]} if kind == "queue" else {"ring": "Team A"}))
    item = _group(lab, kind=kind, name="Team-A")
    previous = group_records(lab.entry)
    lab.hass.config_entries.async_remove_subentry(lab.entry, item.subentry_id)
    await async_apply_group_updates(lab.hass, lab.entry, previous)
    assert group_records(lab.entry) == {}


@pytest.mark.parametrize("fields", [
    {"kind": "queue", "name": "Support", "extension": "701"},
    {"kind": "ring", "name": "park"},
])
async def test_group_flow_namespace_protects_existing_parking_feature_codes(phone_lab, fields):
    from custom_components.voip_stack.group_config import normalize_group_data, validate_group_namespace

    lab = phone_lab
    with pytest.raises(ValueError):
        validate_group_namespace(lab.hass, lab.entry, normalize_group_data(fields))


async def test_invalid_member_batch_keeps_every_owner_unchanged(phone_lab):
    from custom_components.voip_stack.phone_queue_membership import async_update_group_members, iter_phone_memberships
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint

    lab = phone_lab
    add_phone(lab, "first", queues=["Old"])
    # The first owner validates; a later runtime-only browser has no persistent
    # configuration owner. Failure must precede committing the first write.
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="orphan", name="Orphan", kind="browser", queues=["Old"]))
    before = {key: dict(item.data) for key, item in lab.entry.subentries.items()}
    with pytest.raises(ValueError, match="no configuration owner"):
        await async_update_group_members(lab.hass, lab.entry, "queue", "New", ["first", "orphan"], old_name="Old")
    assert {key: dict(item.data) for key, item in lab.entry.subentries.items()} == before
    assert dict(iter_phone_memberships(lab.hass, lab.entry, "queue")) == {"first": ("Old",), "orphan": ("Old",)}
