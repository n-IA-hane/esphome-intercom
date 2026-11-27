"""Legacy trunk data and invalid probe settings remain owned and recoverable."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.components.repairs.issue_handler import async_setup
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from .test_duration_migration_repair_ha import edit_durations, owner_choice
from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


def legacy_data(**changes):
    return {
        "trunk_enabled": False, "trunk_server": "provider.example",
        "trunk_domain": "realm.example", "trunk_username": "office",
        "trunk_auth_username": "office-auth", "trunk_password": " fixture-secret ",
        "trunk_register": True, "trunk_inbound_default_target": "Desk",
        "trunk_options_interval": 37, "unrelated": {"literal": "preserved"},
        **changes,
    }


def assert_preserved(entry, *, probe=37):
    row, = [row for row in entry.subentries.values() if row.subentry_type == "trunk"]
    for key, value in legacy_data().items():
        if key.startswith("trunk_") and key != "trunk_options_interval":
            assert row.data[key] == value
    assert row.data["trunk_options_interval"] == probe
    assert entry.data["default_trunk_id"] == row.data["trunk_id"]
    assert entry.data["unrelated"] == {"literal": "preserved"}
    assert not any(key.startswith("trunk_") for key in entry.data)
    return row


@pytest.mark.parametrize("version,minor", [(6, 2), (7, 5), (7, 6), (7, 7)])
async def test_disabled_legacy_trunk_survives_real_migration_once(hass, version, minor):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor,
        data=legacy_data())
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (7, 7)
    assert_preserved(entry)
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


@pytest.mark.parametrize("version,minor", [(6, 2), (7, 5), (7, 7)])
async def test_disabled_legacy_trunk_keeps_existing_native_identity(hass, version, minor):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor,
        data=legacy_data(default_trunk_id="provider"))
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type="trunk", title="Office provider", unique_id="trunk:provider",
        data={"trunk_id": "provider", "trunk_enabled": True, "trunk_options_interval": 37})
    hass.config_entries.async_add_subentry(entry, row)
    identity = (row.subentry_id, row.unique_id, row.title)
    assert await async_migrate_entry(hass, entry)
    assert assert_preserved(entry) is row
    assert (row.subentry_id, row.unique_id, row.title) == identity
    assert entry.data["default_trunk_id"] == "provider"
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


@pytest.mark.parametrize("version,minor", [(6, 2), (7, 5)])
async def test_retaining_disabled_trunk_keeps_explicit_empty_default(hass, version, minor):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor,
        data=legacy_data(default_trunk_id=""))
    entry.add_to_hass(hass)
    unrelated = ConfigSubentry(subentry_type="trunk", title="Other provider", unique_id="trunk:other",
        data={"trunk_id": "other", "trunk_enabled": True, "trunk_server": "other.example",
              "trunk_options_interval": 37})
    hass.config_entries.async_add_subentry(entry, unrelated)
    before_other = deepcopy(unrelated.data)
    assert await async_migrate_entry(hass, entry)
    assert entry.data["default_trunk_id"] == ""
    assert dict(unrelated.data) == before_other
    saved, = [row for row in entry.subentries.values() if row is not unrelated]
    assert saved.data["trunk_enabled"] is False
    assert saved.data["trunk_password"] == " fixture-secret "
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


@pytest.mark.parametrize("version,minor", [(6, 2), (7, 5), (7, 7)])
async def test_invalid_legacy_probe_is_correctable_in_native_repair(hass, monkeypatch,
        version, minor):
    from custom_components import voip_stack

    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor,
        data=legacy_data(trunk_options_interval=-1))
    entry.add_to_hass(hass)
    setup = AsyncMock(return_value=True)

    async def guarded_setup(hass, entry):
        # Keep the real current-version setup guard: HA skips migration when
        # the saved numeric version already matches, even for invalid content.
        if not voip_stack.async_ensure_duration_policies(hass, entry):
            raise ConfigEntryError("Correct the saved duration policy through Repairs")
        return await setup(hass, entry)

    monkeypatch.setattr(voip_stack, "async_setup_entry", guarded_setup)
    hass.config.components.add("voip_stack")
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is (ConfigEntryState.SETUP_ERROR if (version, minor) == (7, 7)
                           else ConfigEntryState.MIGRATION_ERROR)
    issue_id = "duration_configuration_invalid_" + entry.entry_id
    issue = ir.async_get(hass).async_get_issue("voip_stack", issue_id)
    assert issue and issue.is_persistent and issue.is_fixable
    hass.data.setdefault("repairs", {})
    async_setup(hass)
    manager = repairs_flow_manager(hass)
    initial = await manager.async_init("voip_stack", context={"issue_id": issue_id})
    assert initial["step_id"] == "init"
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    assert form["step_id"] == "edit_durations"
    assert "fixture-secret" not in repr(form)
    fields = next(iter(form["data_schema"].schema.values())).schema.schema
    assert {key.schema for key in fields} == {"trunk_options_interval"}
    before = deepcopy(entry.as_dict())
    rejected = await edit_durations(manager, form["flow_id"], {"trunk_options_interval": -2})
    assert rejected["errors"] == {"base": "invalid_duration"}
    assert entry.as_dict() == before
    result = await edit_durations(manager, form["flow_id"], {"trunk_options_interval": 0})
    assert result["type"] == "create_entry"
    assert entry.state is ConfigEntryState.LOADED
    setup.assert_awaited_once()
    assert_preserved(entry, probe=0)
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is None


def test_legacy_probe_and_advanced_policy_are_distinct_repair_owners(hass):
    from custom_components.voip_stack.duration_repair import duration_repair_owners, duration_repair_update

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5,
        data=legacy_data(trunk_options_interval=-1, registrar_contact_limit=0))
    entry.add_to_hass(hass)
    owners = duration_repair_owners(entry)
    assert {owner.kind for owner in owners} == {"advanced", "trunk"}
    assert len({owner.identity for owner in owners}) == 2
    trunk = next(owner for owner in owners if owner.kind == "trunk")
    assert trunk.subentry_id is None and trunk.path == ()
    row, corrected = duration_repair_update(entry, trunk, {"trunk_options_interval": 19})
    assert row is None
    assert corrected == {**entry.data, "trunk_options_interval": 19}
    assert entry.data["trunk_options_interval"] == -1
    with pytest.raises(ValueError, match="Unknown duration field"):
        duration_repair_update(entry, trunk, {"trunk_password": "forbidden"})
    hass.config_entries.async_update_entry(entry,
        data={**entry.data, "trunk_inbound_default_target": "Changed"})
    with pytest.raises(RuntimeError, match="configuration_changed"):
        duration_repair_update(entry, trunk, {"trunk_options_interval": 19})


async def test_existing_native_probe_survives_early_legacy_move_until_repair(hass, monkeypatch):
    from custom_components import voip_stack

    parent = legacy_data(default_trunk_id="provider")
    parent.pop("trunk_options_interval")
    entry = MockConfigEntry(domain="voip_stack", version=6, minor_version=2, data=parent)
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type="trunk", title="Office provider", unique_id="trunk:provider",
        data={"trunk_id": "provider", "trunk_options_interval": -1,
              "trunk_enabled": False, "trunk_server": "provider.example"})
    hass.config_entries.async_add_subentry(entry, row)
    identity = (row.subentry_id, row.unique_id, row.title)
    setup = AsyncMock(return_value=True)
    monkeypatch.setattr(voip_stack, "async_setup_entry", setup)
    hass.config.components.add("voip_stack")
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    issue_id = "duration_configuration_invalid_" + entry.entry_id
    issue = ir.async_get(hass).async_get_issue("voip_stack", issue_id)
    assert issue and issue.is_fixable
    assert row.data["trunk_options_interval"] == -1
    assert row.data["trunk_enabled"] is False
    assert row.data["trunk_password"] == " fixture-secret "
    assert (row.subentry_id, row.unique_id, row.title) == identity
    assert entry.data["default_trunk_id"] == "provider"
    assert len(entry.subentries) == 1
    hass.data.setdefault("repairs", {})
    async_setup(hass)
    manager = repairs_flow_manager(hass)
    initial = await manager.async_init("voip_stack", context={"issue_id": issue_id})
    assert initial["step_id"] == "init"
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    assert form["step_id"] == "edit_durations"
    assert "fixture-secret" not in repr(form)
    fields = next(iter(form["data_schema"].schema.values())).schema.schema
    assert {key.schema for key in fields} == {"trunk_options_interval"}
    result = await edit_durations(manager, form["flow_id"], {"trunk_options_interval": 0})
    assert result["type"] == "create_entry"
    assert entry.state is ConfigEntryState.LOADED
    setup.assert_awaited_once()
    assert assert_preserved(entry, probe=0) is row
    assert (row.subentry_id, row.unique_id, row.title) == identity
    before = deepcopy(entry.as_dict())
    assert await voip_stack.async_migrate_entry(hass, entry)
    assert entry.as_dict() == before
