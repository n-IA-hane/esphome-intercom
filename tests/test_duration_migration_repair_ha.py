"""A failed duration migration is recoverable through HA Repairs while unloaded."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.components.repairs.issue_handler import async_setup
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


async def blocked_repair(hass, monkeypatch, data, rows=(), *, minor=6):
    from custom_components import voip_stack

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=minor, data=deepcopy(data),
        options={"preserved_option": "literal"})
    entry.add_to_hass(hass)
    for kind, content in rows:
        row = ConfigSubentry(subentry_type=kind, title=kind + " fixture", unique_id=kind + ":fixture", data=deepcopy(content))
        hass.config_entries.async_add_subentry(entry, row)
    setup = AsyncMock(return_value=True)
    monkeypatch.setattr(voip_stack, "async_setup_entry", setup)
    hass.config.components.add("voip_stack")
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    prefix = "duration_configuration_invalid_"
    issue_id = prefix + entry.entry_id
    issue = ir.async_get(hass).async_get_issue("voip_stack", issue_id)
    assert issue and issue.is_persistent and issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}
    hass.data.setdefault("repairs", {})
    async_setup(hass)
    manager = repairs_flow_manager(hass)
    initial = await manager.async_init("voip_stack", context={"issue_id": issue_id})
    return entry, setup, manager, initial, issue_id


async def edit_durations(manager, flow_id, values):
    return await manager.async_configure(flow_id, {"advanced": values})


def owner_choice(form):
    selector = next(iter(form["data_schema"].schema.values()))
    return selector.config["options"][0]["value"]


@pytest.mark.parametrize("data, rows, correction, verify", [
    pytest.param({"service_timeouts": {"media_timeout": 10**1000}}, (),
                 {"media_timeout": 125}, (None, ("service_timeouts", "media_timeout"), 125),
                 id="huge-integer"),
    pytest.param({"parking": {"timeout": 10**1000}}, (), {"timeout": 250},
                 (None, ("parking", "timeout"), 250), id="huge-parking"),
    pytest.param({}, [("phone", {"kind": "sip_account", "username": "Desk", "password": "fixture-secret",
        "voicemail": {"enabled": True, "max_seconds": 10**1000, "pin_hash": "fixture-pin"}})], {"max_seconds": 193},
        ("phone", ("voicemail", "max_seconds"), 193), id="huge-mailbox"),
    ({"service_timeouts": {"media_timeout": 0, "future_key": "literal"}}, (),
     {"media_timeout": 125}, (None, ("service_timeouts", "media_timeout"), 125)),
    ({"parking": {"timeout": -1, "return_timeout": 0, "hold_media": "media-source://preserved"}}, (),
     {"timeout": 250, "return_timeout": 0}, (None, ("parking", "timeout"), 250)),
    ({}, [("contact", {"id": "auto", "name": "Gate", "metadata": {
        "virtual_endpoint": "automation", "automation_timeout": 0, "custom": "literal"}})],
     {"automation_timeout": 47}, ("contact", ("metadata", "automation_timeout"), 47)),
    ({}, [("group", {"kind": "ring", "policy": {"step_timeout": 25, "overall_timeout": 10}, "members": ["Desk"]})],
     {"step_timeout": 8, "overall_timeout": 10}, ("group", ("policy", "step_timeout"), 8)),
    ({}, [("group", {"kind": "queue", "policy": {"max_wait": -1, "ring_timeout": 0}, "members": ["Desk"]})],
     {"max_wait": 72, "ring_timeout": 0}, ("group", ("policy", "max_wait"), 72)),
    ({}, [("phone", {"kind": "sip_account", "username": "Desk", "password": "fixture-secret",
                       "voicemail": {"enabled": True, "max_seconds": -1, "pin_hash": "fixture-pin"}})],
     {"max_seconds": 193}, ("phone", ("voicemail", "max_seconds"), 193)),
    ({}, [("paging", {"name": "Page", "members": ["Desk"], "timeout": 0, "answer_timeout": None})],
     {"timeout": 85}, ("paging", ("timeout",), 85)),
    ({"roster_contacts": [{"id": "Legacy", "metadata": {"virtual_endpoint": "automation", "automation_timeout": 0}}]}, (),
     {"automation_timeout": 25}, (None, ("roster_contacts", 0, "metadata", "automation_timeout"), 25)),
    ({"esphome_mailboxes": {"endpoint": {"enabled": True, "max_seconds": -1, "pin_hash": "fixture-pin"}}}, (),
     {"max_seconds": 149}, (None, ("esphome_mailboxes", "endpoint", "max_seconds"), 149)),
    ({"sip_accounts": [{"username": "Legacy", "password": "fixture-secret", "voicemail": {"max_seconds": -1}}]}, (),
     {"max_seconds": 174}, (None, ("sip_accounts", 0, "voicemail", "max_seconds"), 174)),
])
async def test_repair_edits_invalid_duration_and_runs_real_migration(hass, monkeypatch, data, rows, correction, verify):
    from custom_components.voip_stack.const import CONF_PHONEBOOK_CONTACTS

    if "roster_contacts" in data:
        data = {**data, CONF_PHONEBOOK_CONTACTS: data["roster_contacts"]}
        data.pop("roster_contacts")
        verify = (verify[0], (CONF_PHONEBOOK_CONTACTS, *verify[1][1:]), verify[2])
    entry, setup, manager, initial, issue_id = await blocked_repair(hass, monkeypatch, data, rows)
    assert initial["step_id"] == "init"
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    assert form["step_id"] == "edit_durations"
    exposed = repr(form)
    assert "fixture-secret" not in exposed and "fixture-pin" not in exposed
    assert set(str(key) for key in next(iter(form["data_schema"].schema.values())).schema.schema) == set(correction)
    assert form["description_placeholders"]["error"]
    before = deepcopy(entry.as_dict())
    result = await edit_durations(manager, form["flow_id"], correction)
    assert result["type"] == "create_entry"
    assert (entry.version, entry.minor_version) == (7, 7)
    assert entry.state is ConfigEntryState.LOADED
    setup.assert_awaited_once()
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is None
    assert entry.options == {"preserved_option": "literal"}
    assert [(row.subentry_id, row.unique_id, row.title) for row in entry.subentries.values()] == [
        (row["subentry_id"], row["unique_id"], row["title"]) for row in before["subentries"]]
    record = (next(row.data for row in entry.subentries.values() if row.subentry_type == verify[0])
              if verify[0] else entry.data)
    for key in verify[1]:
        record = record[key]
    assert record == verify[2]
    if rows and rows[0][0] == "phone":
        phone = next(iter(entry.subentries.values()))
        assert phone.data["password"] == "fixture-secret"
        assert phone.data["voicemail"]["pin_hash"] == "fixture-pin"
    if "sip_accounts" in data:
        assert entry.data["sip_accounts"][0]["password"] == "fixture-secret"
    if "esphome_mailboxes" in data:
        assert entry.data["esphome_mailboxes"]["endpoint"]["pin_hash"] == "fixture-pin"


async def test_invalid_submission_and_multiple_owners_preserve_other_saved_values(hass, monkeypatch):
    entry, setup, manager, initial, _ = await blocked_repair(hass, monkeypatch,
        {"service_timeouts": {"media_timeout": 0, "dtmf_timeout": 0}, "parking": {"timeout": -1},
         "unrelated": {"password": "fixture-secret"}})
    first = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    before = deepcopy(entry.as_dict())
    invalid = await edit_durations(manager, first["flow_id"], {"media_timeout": 70, "dtmf_timeout": 0})
    assert invalid["errors"] == {"base": "invalid_duration"}
    assert entry.as_dict() == before
    second = await edit_durations(manager, first["flow_id"], {"media_timeout": 70, "dtmf_timeout": 12})
    assert second["step_id"] == "init"
    assert entry.minor_version == 6 and entry.state is ConfigEntryState.MIGRATION_ERROR
    assert entry.data["parking"] == {"timeout": -1}
    assert entry.data["service_timeouts"] == {"media_timeout": 70, "dtmf_timeout": 12}
    setup.assert_not_awaited()
    form = await manager.async_configure(second["flow_id"], {"owner": owner_choice(second)})
    result = await edit_durations(manager, form["flow_id"], {"timeout": 225})
    assert result["type"] == "create_entry"
    assert entry.minor_version == 7 and entry.state is ConfigEntryState.LOADED
    assert entry.data["unrelated"] == {"password": "fixture-secret"}
    setup.assert_awaited_once()


async def test_stale_form_does_not_overwrite_concurrent_configuration(hass, monkeypatch):
    entry, setup, manager, initial, _ = await blocked_repair(hass, monkeypatch,
        {"service_timeouts": {"media_timeout": 0}})
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    hass.config_entries.async_update_entry(entry, data={"service_timeouts": {"media_timeout": -2}, "other": "new"})
    before = deepcopy(entry.as_dict())
    result = await edit_durations(manager, form["flow_id"], {"media_timeout": 80})
    assert result["errors"] == {"base": "configuration_changed"}
    assert entry.as_dict() == before and entry.minor_version == 6
    setup.assert_not_awaited()


@pytest.mark.parametrize("nested", [False, True])
async def test_new_zero_group_step_is_rejected_instead_of_materializing_legacy_default(hass, monkeypatch, nested):
    policy = {"step_timeout": 25, "overall_timeout": 10}
    if nested:
        policy = {"step_timeout": 15, "ring_policy": policy}
    entry, setup, manager, initial, _ = await blocked_repair(hass, monkeypatch, {},
        [("group", {"kind": "ring", "policy": policy})])
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    submitted = ({"step_timeout": 15, "nested_step_timeout": 0, "nested_overall_timeout": 10} if nested
                 else {"step_timeout": 0, "overall_timeout": 10})
    before = deepcopy(entry.as_dict())
    result = await edit_durations(manager, form["flow_id"], submitted)
    assert result["errors"] == {"base": "invalid_duration"}
    assert entry.as_dict() == before and entry.minor_version == 6
    assert "positive" in result["description_placeholders"]["error"]
    setup.assert_not_awaited()


async def test_reordered_legacy_accounts_with_equal_mailboxes_cannot_receive_each_others_edit(hass, monkeypatch):
    accounts = [{"name": "Same label", "username": username, "password": "secret-" + username,
                 "voicemail": {"max_seconds": -1}} for username in ("first", "second")]
    entry, setup, manager, initial, _ = await blocked_repair(hass, monkeypatch, {"sip_accounts": accounts})
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    hass.config_entries.async_update_entry(entry, data={"sip_accounts": list(reversed(accounts))})
    before = deepcopy(entry.as_dict())
    result = await edit_durations(manager, form["flow_id"], {"max_seconds": 80})
    assert result["errors"] == {"base": "configuration_changed"}
    assert entry.as_dict() == before
    assert "secret-first" not in repr(result) and "secret-second" not in repr(result)
    setup.assert_not_awaited()


async def test_structural_failure_stays_open_without_exposing_or_replacing_raw_storage(hass, monkeypatch):
    entry, setup, manager, initial, issue_id = await blocked_repair(hass, monkeypatch,
        {"parking": ["fixture-secret"], "unrelated": {"password": "fixture-secret"}})
    assert initial["step_id"] == "confirm"
    assert initial["description_placeholders"]["entry"] == entry.title
    assert "structure" in initial["description_placeholders"]["error"]
    assert "Logs" in initial["description_placeholders"]["error"]
    assert "ValueError" not in repr(initial)
    before = deepcopy(entry.as_dict())
    result = await manager.async_configure(initial["flow_id"], {})
    assert result["type"] == "form" and result["errors"] == {"base": "migration_retry_failed"}
    assert "fixture-secret" not in repr(result)
    assert entry.as_dict() == before
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id)
    setup.assert_not_awaited()


async def test_schema_76_trunk_repair_can_fix_remaining_invalid_duration(hass, monkeypatch):
    entry, setup, manager, initial, _ = await blocked_repair(hass, monkeypatch,
        {"service_timeouts": {"media_timeout": 0}}, minor=6)
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    result = await edit_durations(manager, form["flow_id"], {"media_timeout": 91})
    assert result["type"] == "create_entry" and entry.minor_version == 7
    assert entry.data["service_timeouts"]["media_timeout"] == 91
    from custom_components.voip_stack.duration_policy import SERVICE_TIMEOUT_LIMITS
    assert set(SERVICE_TIMEOUT_LIMITS) <= entry.data["service_timeouts"].keys()
    setup.assert_awaited_once()
