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
    # A completed part of the same move contains the actual definition.
    # Selecting this record as default is not, by itself, identity evidence.
    row = ConfigSubentry(subentry_type="trunk", title="Office provider", unique_id="trunk:provider",
        data={**{key: value for key, value in entry.data.items() if key.startswith("trunk_")},
              "trunk_id": "provider"})
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
        data={**{key: value for key, value in parent.items() if key.startswith("trunk_")},
              "trunk_id": "provider", "trunk_options_interval": -1})
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


@pytest.mark.parametrize("version,minor", [(6, 2), (7, 5), (7, 7)])
@pytest.mark.parametrize("selected", [None, "native-provider", ""])
@pytest.mark.parametrize("enabled", [False, True])
async def test_distinct_legacy_provider_never_replaces_native_default(hass, version, minor, selected, enabled):
    from custom_components.voip_stack import async_migrate_entry

    parent = legacy_data(trunk_enabled=enabled)
    if selected is not None:
        parent["default_trunk_id"] = selected
    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor, data=parent)
    entry.add_to_hass(hass)
    native = ConfigSubentry(subentry_type="trunk", title="Native provider", unique_id="trunk:native-provider",
        data={"trunk_id": "native-provider", "trunk_enabled": True,
              "trunk_server": "native.example", "trunk_domain": "native-realm.example",
              "trunk_username": "native-user", "trunk_password": " native-secret ",
              "trunk_inbound_default_target": "Native desk", "trunk_options_interval": 43,
              "native_metadata": {"keep": True}})
    hass.config_entries.async_add_subentry(entry, native)
    native_before = deepcopy(native.as_dict())

    assert await async_migrate_entry(hass, entry)
    assert native.as_dict() == native_before
    moved, = [row for row in entry.subentries.values() if row is not native]
    for key, value in parent.items():
        if key.startswith("trunk_"):
            assert moved.data[key] == value
    assert moved.data["trunk_id"] != "native-provider"
    assert entry.data["default_trunk_id"] == ("native-provider" if selected is None else selected)
    assert entry.data["unrelated"] == parent["unrelated"]
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


@pytest.mark.parametrize("version,minor", [(6, 2), (7, 5), (7, 7)])
@pytest.mark.parametrize("disable", ["legacy", "native"])
async def test_overlapping_legacy_sources_are_preserved_and_repairable(hass, monkeypatch, version, minor, disable):
    from custom_components import voip_stack
    from homeassistant.exceptions import ConfigEntryError

    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor,
        data=legacy_data(trunk_enabled=True, trunk_register=False,
                         trunk_allowed_ips=["192.0.2.0/24"], default_trunk_id="native"))
    entry.add_to_hass(hass)
    native = ConfigSubentry(subentry_type="trunk", title="Native provider", unique_id="trunk:native",
        data={"trunk_id": "native", "trunk_enabled": True, "trunk_register": False,
              "trunk_server": "native.example", "trunk_allowed_ips": ["192.0.2.42"],
              "trunk_password": " native-secret ", "trunk_options_interval": 37})
    hass.config_entries.async_add_subentry(entry, native)
    parent_before, native_before = deepcopy(dict(entry.data)), deepcopy(native.as_dict())
    setup = AsyncMock(return_value=True)

    async def guarded_setup(hass, entry):
        if not voip_stack.async_ensure_duration_policies(hass, entry):
            raise ConfigEntryError("Resolve the saved trunk source conflict through Repairs")
        return await setup(hass, entry)

    monkeypatch.setattr(voip_stack, "async_setup_entry", guarded_setup)
    hass.config.components.add("voip_stack")
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert dict(entry.data) == parent_before
    assert native.as_dict() == native_before
    issue_id = "trunk_migration_source_conflict_" + entry.entry_id
    issue = ir.async_get(hass).async_get_issue("voip_stack", issue_id)
    assert issue and issue.is_fixable and issue.is_persistent
    hass.data.setdefault("repairs", {})
    async_setup(hass)
    manager = repairs_flow_manager(hass)
    initial = await manager.async_init("voip_stack", context={"issue_id": issue_id})
    assert initial["step_id"] == "init"
    assert "secret" not in repr(initial)
    result = await manager.async_configure(initial["flow_id"], {
        "line": "legacy" if disable == "legacy" else native.subentry_id})
    assert result["type"] == "create_entry"
    assert entry.state is ConfigEntryState.LOADED
    setup.assert_awaited_once()
    moved, = [row for row in entry.subentries.values() if row is not native]
    for key, value in parent_before.items():
        if key.startswith("trunk_"):
            assert moved.data[key] == (False if key == "trunk_enabled" and disable == "legacy" else value)
    native_expected = deepcopy(native_before["data"])
    if disable == "native":
        native_expected["trunk_enabled"] = False
    assert native.data == native_expected
    assert entry.data["default_trunk_id"] == "native"
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is None


async def test_equivalent_native_is_reused_even_when_another_provider_is_default(hass):
    from custom_components.voip_stack import async_migrate_entry

    parent = legacy_data(default_trunk_id="other")
    parent.pop("trunk_options_interval")
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data=parent)
    entry.add_to_hass(hass)
    other = ConfigSubentry(subentry_type="trunk", title="Other", unique_id="trunk:other",
        data={"trunk_id": "other", "trunk_enabled": True, "trunk_server": "other.example",
              "trunk_password": " other-secret ", "trunk_options_interval": 19})
    matching = ConfigSubentry(subentry_type="trunk", title="Existing legacy owner", unique_id="trunk:matching",
        data={**{key: value for key, value in parent.items() if key.startswith("trunk_")},
              "trunk_id": "matching", "trunk_options_interval": 37,
              "native_metadata": {"preserved": True}})
    for row in (other, matching):
        hass.config_entries.async_add_subentry(entry, row)
    originals = [deepcopy(row.as_dict()) for row in (other, matching)]
    assert await async_migrate_entry(hass, entry)
    assert len(entry.subentries) == 2
    assert [row.as_dict() for row in (other, matching)] == originals
    assert entry.data["default_trunk_id"] == "other"


async def test_legacy_id_collision_preserves_native_and_materializes_distinct_provider(hass):
    from custom_components.voip_stack import async_migrate_entry

    parent = legacy_data(trunk_id="occupied", default_trunk_id="occupied")
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data=parent)
    entry.add_to_hass(hass)
    native = ConfigSubentry(subentry_type="trunk", title="Native", unique_id="trunk:occupied",
        data={"trunk_id": "occupied", "trunk_enabled": True, "trunk_server": "native.example",
              "trunk_password": " native-secret ", "trunk_options_interval": 37})
    hass.config_entries.async_add_subentry(entry, native)
    before = deepcopy(native.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert native.as_dict() == before
    moved, = [row for row in entry.subentries.values() if row is not native]
    assert moved.data["trunk_id"] != "occupied"
    assert moved.data["legacy_trunk_id"] == "occupied"
    assert moved.data["trunk_password"] == parent["trunk_password"]
    assert entry.data["default_trunk_id"] == "occupied"
    snapshot = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == snapshot


@pytest.mark.parametrize("failure", ["parent-changed", "native-changed", "save-failed", "retry-failed"])
async def test_source_conflict_repair_preserves_concurrent_edits_and_failed_retry(hass, monkeypatch, failure):
    from custom_components import voip_stack

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5,
        data=legacy_data(trunk_enabled=True, trunk_register=False,
                         trunk_allowed_ips=["192.0.2.0/24"], default_trunk_id="native"))
    entry.add_to_hass(hass)
    native = ConfigSubentry(subentry_type="trunk", title="Native", unique_id="trunk:native",
        data={"trunk_id": "native", "trunk_enabled": True, "trunk_register": False,
              "trunk_allowed_ips": ["192.0.2.42"], "trunk_server": "native.example",
              "trunk_password": " native-secret ", "trunk_options_interval": 37})
    hass.config_entries.async_add_subentry(entry, native)
    setup = AsyncMock(return_value=True)
    monkeypatch.setattr(voip_stack, "async_setup_entry", setup)
    hass.config.components.add("voip_stack")
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    issue_id = "trunk_migration_source_conflict_" + entry.entry_id
    hass.data.setdefault("repairs", {})
    async_setup(hass)
    manager = repairs_flow_manager(hass)
    initial = await manager.async_init("voip_stack", context={"issue_id": issue_id})
    if failure == "parent-changed":
        hass.config_entries.async_update_entry(entry, data={**entry.data, "trunk_password": "changed-parent"})
    elif failure == "native-changed":
        hass.config_entries.async_update_subentry(entry, native,
            data={**native.data, "trunk_password": "changed-native"})
    elif failure == "save-failed":
        def fail_save(*args, **kwargs):
            raise OSError("Storage unavailable")
        monkeypatch.setattr(hass.config_entries, "async_update_entry", fail_save)
    else:
        monkeypatch.setattr(hass.config_entries, "async_retry_migration", AsyncMock(return_value=None))
    before = deepcopy(entry.as_dict())
    result = await manager.async_configure(initial["flow_id"], {"line": "legacy"})
    expected = ("configuration_changed" if failure.endswith("changed") else
                "save_failed" if failure == "save-failed" else "migration_retry_failed")
    assert result["errors"] == {"base": expected}
    assert "secret" not in repr(result)
    assert native.as_dict() == before["subentries"][0]
    assert entry.data == {**before["data"], **({"trunk_enabled": False} if failure == "retry-failed" else {})}
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is not None
    setup.assert_not_awaited()
