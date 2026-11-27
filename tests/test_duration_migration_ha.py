"""Stored policy-duration upgrades preserve native owners and retry safely."""

from copy import deepcopy

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_legacy_esphome_settings_migration_ha import legacy_pair as legacy_pair, phone_lab as phone_lab

pytestmark = pytest.mark.ha


def add_row(hass, entry, kind, identity, data):
    row = ConfigSubentry(subentry_type=kind, title=identity, unique_id=f"{kind}:{identity}", data=data)
    hass.config_entries.async_add_subentry(entry, row)
    return row


def automation(identity, **metadata):
    return {"id": identity, "name": identity,
            "metadata": {"virtual_endpoint": "automation", **metadata}}


def native_ids(hass, entry):
    return (
        [(row.subentry_id, row.unique_id, row.title) for row in entry.subentries.values()],
        [(row.id, row.identifiers, row.config_subentry_id, row.name, row.name_by_user)
         for row in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)],
        [(row.id, row.entity_id, row.unique_id, row.device_id, row.config_subentry_id, row.name)
         for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)],
    )


async def test_76_upgrade_preserves_saved_values_and_native_owners(hass):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.const import CONF_PHONEBOOK_CONTACTS
    from custom_components.voip_stack.duration_config import INITIAL_SERVICE_TIMEOUTS

    parent = {
        "initial_phone_created": True, "default_ringing_timeout": 0,
        "max_dialog_subscription_expires": 420, "unrelated": {"keep": ["literal", 7]},
        "service_timeouts": {"automation_timeout": 49, "contact_step_timeout": 27,
                             "announcement_timeout": 99, "future_duration_option": "kept"},
        "parking": {"timeout": 245, "return_timeout": 0, "hold_media": "media-source://kept"},
        CONF_PHONEBOOK_CONTACTS: [automation("Old root contact"),
                                 automation("Explicit root contact", automation_timeout=17)],
    }
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=6,
                            data=deepcopy(parent), options={"unrelated_option": {"keep": True}})
    entry.add_to_hass(hass)
    short = add_row(hass, entry, "group", "short", {
        "group_id": "short", "kind": "ring", "name": "Short group", "members": ["Desk"],
        "policy": {"overall_timeout": 4}, "future": {"keep": True},
    })
    zero = add_row(hass, entry, "group", "zero", {
        "group_id": "zero", "kind": "ring", "name": "Unlimited group", "members": [],
        "policy": {"overall_timeout": 0, "step_timeout": 0},
    })
    saved = add_row(hass, entry, "group", "saved", {
        "group_id": "saved", "kind": "ring", "name": "Saved group", "members": ["Desk"],
        "policy": {"overall_timeout": None, "step_timeout": 9},
    })
    missing_contact = add_row(hass, entry, "contact", "missing", automation("Missing", fallback_destination="HA"))
    saved_contact = add_row(hass, entry, "contact", "explicit", automation("Explicit", automation_timeout=23))
    missing_page = add_row(hass, entry, "paging", "missing-page", {
        "paging_group_id": "canonical-paging-id", "name": "Missing paging limit", "members": [],
        "answer_timeout": 0, "future": "kept",
    })
    saved_page = add_row(hass, entry, "paging", "saved-page", {
        "paging_group_id": "saved-page", "name": "Explicit paging limit", "members": [],
        "answer_timeout": None, "timeout": 83,
    })
    phone = add_row(hass, entry, "phone", "Desk", {
        "kind": "sip_account", "name": "Desk", "username": "Desk", "password": "fixture-kept-password",
        "call_policy": {"ringing_timeout": 0}, "voicemail": {"max_seconds": 241}, "future": "kept",
    })
    queue = add_row(hass, entry, "group", "queue", {
        "group_id": "queue", "kind": "queue", "name": "Queue", "policy": {
            "ring_timeout": 0, "max_wait": 75, "no_agent_wait": 0, "retry_delay": 2, "wrap_up": 0},
    })
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        config_subentry_id=phone.subentry_id, identifiers={("voip_stack", "phone_endpoint:Desk")}, name="Desk")
    er.async_get(hass).async_get_or_create("sensor", "voip_stack", "kept_duration_sensor",
        config_entry=entry, config_subentry_id=phone.subentry_id, device_id=device.id)
    identities = native_ids(hass, entry)
    untouched = {row.subentry_id: deepcopy(row.as_dict()) for row in (saved, saved_contact, saved_page, phone, queue)}

    assert await async_migrate_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (7, 7)
    assert native_ids(hass, entry) == identities
    assert {identity: entry.subentries[identity].as_dict() for identity in untouched} == untouched
    assert entry.data["unrelated"] == parent["unrelated"]
    assert entry.options == {"unrelated_option": {"keep": True}}
    assert entry.data["default_ringing_timeout"] == 0
    assert entry.data["max_dialog_subscription_expires"] == 420
    assert entry.data["parking"] == parent["parking"]
    assert entry.data["service_timeouts"] == {**INITIAL_SERVICE_TIMEOUTS, **parent["service_timeouts"]}
    assert short.data["policy"] == {"overall_timeout": 4, "step_timeout": 4}
    assert short.data["future"] == {"keep": True}
    assert zero.data["policy"] == {"overall_timeout": 0, "step_timeout": 15}
    # Existing objects retain their historical effective values, independently
    # of a subsequently stored PBX default for future action/contact creation.
    assert missing_contact.data["metadata"] == {
        "virtual_endpoint": "automation", "fallback_destination": "HA", "automation_timeout": 30,
    }
    assert missing_page.data == {
        "paging_group_id": "canonical-paging-id", "name": "Missing paging limit", "members": [],
        "answer_timeout": 0, "future": "kept", "timeout": 120,
    }
    assert [item["metadata"]["automation_timeout"] for item in entry.data[CONF_PHONEBOOK_CONTACTS]] == [30, 17]
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


@pytest.mark.parametrize("parking", [None, {"return_timeout": 0}, {"timeout": 217, "return_timeout": None}])
async def test_upgrade_materializes_parking_even_when_its_form_was_never_opened(hass, parking):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.duration_config import INITIAL_SERVICE_TIMEOUTS

    data = {"default_ringing_timeout": 0}
    if parking is not None:
        data["parking"] = parking
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=6, data=data)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    expected = {**(parking or {})}
    expected.setdefault("timeout", 180)
    assert entry.data["parking"] == expected
    assert entry.data["service_timeouts"] == INITIAL_SERVICE_TIMEOUTS
    assert entry.data["default_ringing_timeout"] == 0


@pytest.mark.parametrize("kind, invalid", [
    ("contact", automation("Invalid", automation_timeout=0)),
    ("paging", {"name": "Invalid", "members": [], "timeout": 0}),
    ("group", {"kind": "ring", "policy": {"step_timeout": -1}}),
    ("group", {"kind": "queue", "policy": {"max_wait": -1}}),
    ("phone", {"kind": "sip_account", "username": "Invalid", "voicemail": {"enabled": True, "max_seconds": -1}}),
])
async def test_invalid_later_record_prevents_every_config_write(hass, monkeypatch, kind, invalid):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=6, data={"unrelated": "keep"})
    entry.add_to_hass(hass)
    add_row(hass, entry, "contact", "first", automation("First"))
    add_row(hass, entry, kind, "invalid", invalid)
    before = deepcopy(entry.as_dict())
    writes = []
    for name in ("async_update_entry", "async_update_subentry", "async_add_subentry", "async_remove_subentry"):
        original = getattr(hass.config_entries, name)
        def observed(*args, _name=name, _original=original, **kwargs):
            writes.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(hass.config_entries, name, observed)
    assert await async_migrate_entry(hass, entry) is False
    assert writes == []
    assert entry.as_dict() == before and entry.minor_version == 6
    assert ir.async_get(hass).async_get_issue("voip_stack", "duration_configuration_invalid_" + entry.entry_id)


async def test_interrupted_subentry_write_keeps_minor_version_and_retries(hass, monkeypatch):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=6, data={"unrelated": "keep"})
    entry.add_to_hass(hass)
    first = add_row(hass, entry, "contact", "first", automation("First"))
    second = add_row(hass, entry, "contact", "second", automation("Second"))
    identities = native_ids(hass, entry)
    original = hass.config_entries.async_update_subentry
    writes = 0
    def interrupted(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("fixture interruption during the second duration write")
        return original(*args, **kwargs)
    monkeypatch.setattr(hass.config_entries, "async_update_subentry", interrupted)
    assert await async_migrate_entry(hass, entry) is False
    assert entry.minor_version == 6 and entry.data == {"unrelated": "keep"}
    assert first.data["metadata"]["automation_timeout"] == 30
    assert "automation_timeout" not in second.data["metadata"]
    assert native_ids(hass, entry) == identities
    issue_id = "duration_configuration_invalid_" + entry.entry_id
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id)
    monkeypatch.setattr(hass.config_entries, "async_update_subentry", original)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 7
    assert second.data["metadata"]["automation_timeout"] == 30
    assert native_ids(hass, entry) == identities
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is None
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


async def test_real_73_identity_conflict_blocks_duration_and_trunk_migrations(hass):
    from custom_components.voip_stack import async_migrate_entry
    from .test_account_migration_diagnostics_ha import home_account

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=3, data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    home = home_account(hass, entry, additional_reference=True)
    missing = add_row(hass, entry, "contact", "pending", automation("Pending"))
    pending_group = add_row(hass, entry, "group", "pending-group", {
        "group_id": "pending-group", "kind": "ring", "name": "Pending group", "members": [],
        "policy": {"overall_timeout": 0},
    })
    pending_queue = add_row(hass, entry, "group", "pending-queue", {
        "group_id": "pending-queue", "kind": "queue", "name": "Pending queue", "members": [],
        "policy": {"ring_timeout": 0},
    })
    pending_trunk = add_row(hass, entry, "trunk", "pending-trunk", {
        "trunk_id": "pending-trunk", "trunk_enabled": True, "trunk_server": "provider.invalid",
        "trunk_inbound_mode": "dtmf", "trunk_dtmf_timeout_ms": 3000,
        "trunk_inbound_default_target": "Tecnodata",
    })
    devices = dr.async_get(hass)
    devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=home.subentry_id,
        identifiers={("voip_stack", "phone_endpoint:default")}, name="Home")
    conflict = devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=home.subentry_id,
        identifiers={("voip_stack", "phone_endpoint:browser:home")}, name="Historical Home")
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry) is False
    assert entry.as_dict() == before and entry.minor_version == 3
    assert "default_ringing_timeout" not in entry.data and "service_timeouts" not in entry.data
    assert "automation_timeout" not in missing.data["metadata"]
    assert "step_timeout" not in pending_group.data["policy"]
    assert "max_wait" not in pending_queue.data["policy"]
    assert pending_trunk.data["trunk_inbound_mode"] == "dtmf"
    assert ir.async_get(hass).async_get_issue("voip_stack", "trunk_dtmf_retired_" + pending_trunk.subentry_id) is None
    devices.async_remove_device(conflict.id)
    await hass.async_block_till_done()
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 7 and missing.data["metadata"]["automation_timeout"] == 30
    assert pending_group.data["policy"] == {"overall_timeout": 0, "step_timeout": 15}
    assert pending_queue.data["policy"]["max_wait"] == 60
    assert "trunk_inbound_mode" not in pending_trunk.data
    assert pending_trunk.data["trunk_inbound_default_target"] == "Tecnodata"
    assert ir.async_get(hass).async_get_issue("voip_stack", "trunk_dtmf_retired_" + pending_trunk.subentry_id)


@pytest.mark.parametrize("kind", ["ring", "queue"])
@pytest.mark.parametrize("legacy_step", [None, 0])
async def test_legacy_identity_upgrade_can_extend_a_group_before_duration_migration(legacy_pair, kind, legacy_step):
    from custom_components.voip_stack import async_migrate_entry
    from .test_legacy_esphome_settings_migration_ha import add_native_setting

    lab = legacy_pair
    data = {key: value for key, value in lab.entry.data.items() if key not in {"service_timeouts", "parking"}}
    lab.hass.config_entries.async_update_entry(lab.entry, data=data)
    if kind == "queue":
        add_native_setting(lab, lab.native, lab.device, "voip_queues", "Support")
    legacy_policy = {"overall_timeout" if kind == "ring" else "ring_timeout": 0}
    if kind == "ring" and legacy_step is not None:
        legacy_policy["step_timeout"] = legacy_step
    group = add_row(lab.hass, lab.entry, "group", "old-group", {
        "group_id": "old-group", "kind": kind, "name": "RG Casa" if kind == "ring" else "Support",
        "members": ["Other"], "policy": legacy_policy,
    })
    assert await async_migrate_entry(lab.hass, lab.entry)
    assert lab.entry.minor_version == 7
    assert group.data["members"] == ["Other", "Box"]
    if kind == "ring":
        assert group.data["policy"]["overall_timeout"] == 0
        assert group.data["policy"]["step_timeout"] == 15
    else:
        assert group.data["policy"]["ring_timeout"] == 0
        assert group.data["policy"]["max_wait"] == 60
        assert group.data["policy"]["no_agent_wait"] == 1
        assert group.data["policy"]["retry_delay"] == 1
        assert group.data["policy"]["wrap_up"] == 0


async def test_queue_and_mailbox_migration_materializes_only_absent_values(hass):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=6, data={
        "esphome_mailboxes": {
            "esphome:missing": {"enabled": True, "pin_hash": "saved-fixture-hash"},
            "esphome:explicit": {"enabled": False, "max_seconds": 219},
        },
        "sip_accounts": [
            {"username": "Legacy missing", "password": "fixture-kept", "voicemail": {"enabled": True}},
            {"username": "Legacy explicit", "voicemail": {"enabled": True, "max_seconds": 245}},
        ],
    })
    entry.add_to_hass(hass)
    missing_queue = add_row(hass, entry, "group", "missing-queue", {
        "group_id": "missing-queue", "kind": "queue", "name": "Support", "members": [],
        "policy": {"max_wait": 81, "ring_timeout": 0}, "future": {"keep": True},
    })
    explicit_queue = add_row(hass, entry, "group", "explicit-queue", {
        "group_id": "explicit-queue", "kind": "queue", "name": "Explicit", "members": [],
        "policy": {"max_wait": 110, "ring_timeout": None, "no_agent_wait": 0, "retry_delay": 2.5, "wrap_up": 0},
    })
    missing_mailbox = add_row(hass, entry, "phone", "missing-mailbox", {
        "kind": "sip_account", "username": "Missing", "name": "Missing", "future": {"keep": True},
        "voicemail": {"enabled": True, "greeting_media": "media-source://kept", "pin_hash": "saved-fixture-hash"},
    })
    disabled_mailbox = add_row(hass, entry, "phone", "disabled-mailbox", {
        "kind": "sip_account", "username": "Disabled", "name": "Disabled", "voicemail": {"enabled": False},
    })
    explicit_mailbox = add_row(hass, entry, "phone", "explicit-mailbox", {
        "kind": "sip_account", "username": "Explicit", "name": "Explicit", "voicemail": {"enabled": True, "max_seconds": 241},
    })
    identities = native_ids(hass, entry)
    explicit = [deepcopy(row.as_dict()) for row in (explicit_queue, explicit_mailbox)]
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 7 and native_ids(hass, entry) == identities
    assert missing_queue.data["policy"] == {
        "max_wait": 81, "ring_timeout": 0, "no_agent_wait": 1, "retry_delay": 1, "wrap_up": 0,
    }
    assert missing_queue.data["future"] == {"keep": True}
    assert missing_mailbox.data["voicemail"] == {
        "enabled": True, "greeting_media": "media-source://kept", "pin_hash": "saved-fixture-hash", "max_seconds": 180,
    }
    assert missing_mailbox.data["future"] == {"keep": True}
    assert disabled_mailbox.data["voicemail"] == {"enabled": False, "max_seconds": 180}
    assert [row.as_dict() for row in (explicit_queue, explicit_mailbox)] == explicit
    assert entry.data["esphome_mailboxes"] == {
        "esphome:missing": {"enabled": True, "pin_hash": "saved-fixture-hash", "max_seconds": 180},
        "esphome:explicit": {"enabled": False, "max_seconds": 219},
    }
    assert entry.data["sip_accounts"] == [
        {"username": "Legacy missing", "password": "fixture-kept", "voicemail": {"enabled": True, "max_seconds": 180}},
        {"username": "Legacy explicit", "voicemail": {"enabled": True, "max_seconds": 245}},
    ]
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


async def test_explicit_unlimited_durations_survive_migration_and_repeat(hass):
    """Approved zero policies stay explicit while finite sibling waits survive."""
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.duration_repair import duration_repair_owners

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=6, data={
        "parking": {"timeout": 0, "return_timeout": 9},
        "service_timeouts": {"recording_timeout": 0, "media_timeout": 125},
        "unrelated": {"keep": "literal"},
    })
    entry.add_to_hass(hass)
    queue = add_row(hass, entry, "group", "unlimited-queue", {
        "group_id": "unlimited-queue", "kind": "queue", "name": "Support", "members": [],
        "policy": {"max_wait": 0, "ring_timeout": 15, "retry_delay": 2,
                   "no_agent_wait": 1, "wrap_up": 0},
    })
    phone = add_row(hass, entry, "phone", "unlimited-mailbox", {
        "kind": "sip_account", "username": "Desk", "password": "fixture-kept",
        "voicemail": {"enabled": True, "max_seconds": 0, "pin_hash": "fixture-pin"},
    })
    identities = native_ids(hass, entry)
    assert await async_migrate_entry(hass, entry)
    assert entry.data["parking"] == {"timeout": 0, "return_timeout": 9}
    assert entry.data["service_timeouts"]["recording_timeout"] == 0
    assert entry.data["service_timeouts"]["media_timeout"] == 125
    assert queue.data["policy"]["max_wait"] == 0
    assert queue.data["policy"]["retry_delay"] == 2
    assert phone.data["voicemail"] == {
        "enabled": True, "max_seconds": 0, "pin_hash": "fixture-pin",
    }
    assert phone.data["password"] == "fixture-kept"
    assert native_ids(hass, entry) == identities
    assert duration_repair_owners(entry) == []
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before
