"""Keep native owners and distinguish migration conflicts from failed storage."""

from copy import deepcopy
from dataclasses import replace
import logging

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_legacy_esphome_settings_migration_ha import (
    legacy_pair as legacy_pair,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


def home_account(hass, entry, *, kind="sip_account", additional_reference=False):
    data = {
        "kind": kind, "name": "Home", "username": "Home", "username_source": "user",
        "password": "fixture-password-must-not-appear-in-log", "extension": "home",
        "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:default"}],
    }
    if additional_reference:
        data["ha_bindings"].append({"source": "migration", "reference": "legacy_ha_phone:browser:home"})
    row = ConfigSubentry(subentry_type="phone", title="Home", unique_id="phone:default", data=data)
    hass.config_entries.async_add_subentry(entry, row)
    return row


def native_snapshot(hass, entry):
    return (
        deepcopy(entry.as_dict()),
        [(row.id, row.identifiers, row.config_subentry_id, row.name, row.name_by_user)
         for row in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)],
        [(row.id, row.entity_id, row.unique_id, row.device_id, row.config_subentry_id, row.name)
         for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)],
    )


@pytest.mark.parametrize("old_status", ["offline", "removed", "available"])
async def test_home_and_replacement_esp_distinguish_native_ownership_from_reused_extension(legacy_pair, old_status):
    """A reported name/extension alone cannot identify the user's stored conflict."""
    from custom_components.voip_stack import async_migrate_entry

    lab = legacy_pair
    old, replacement = lab.legacy_phones
    for phone, name in ((old, "Meldestelle-Audio"), (replacement, "Meldestelle-Audio-Ersatz")):
        native = phone.origin.runtime_data
        native.device_info = replace(native.device_info, friendly_name=name)
        lab.hass.states.async_set(phone.settings["voip_extension"].entity_id, "meldestelle-audio")
    if old_status == "offline":
        old.origin.runtime_data = None
    elif old_status == "removed":
        dr.async_get(lab.hass).async_remove_device(old.device.id)
        await lab.hass.async_block_till_done()

    home = home_account(lab.hass, lab.entry, kind="browser")
    devices, entities = dr.async_get(lab.hass), er.async_get(lab.hass)
    device = devices.async_get_or_create(config_entry_id=lab.entry.entry_id,
        config_subentry_id=home.subentry_id, identifiers={("voip_stack", "phone_endpoint:default")},
        name="Home")
    state = entities.async_get_or_create("sensor", "voip_stack", "phone_endpoint_default_call_state",
        config_entry=lab.entry, config_subentry_id=home.subentry_id, device_id=device.id)
    state = entities.async_update_entity(state.entity_id, name="My existing Home state")
    original = home.subentry_id, device.id, state.id, state.entity_id
    # HA has no running VoIP owner during the blocked config-entry migration.
    lab.entry.runtime_data = None
    try:
        result = await async_migrate_entry(lab.hass, lab.entry)
        issues = ir.async_get(lab.hass)
        if old_status == "available":
            assert result is False
            route = issues.async_get_issue("voip_stack", "account_route_conflict_" + lab.entry.entry_id)
            assert route is not None and route.translation_placeholders["route"] == "meldestelle-audio"
            assert issues.async_get_issue("voip_stack", "phone_identity_conflict_" + lab.entry.entry_id) is None
            assert home.unique_id == "phone:default"
            assert entities.async_get(state.entity_id).unique_id == "phone_endpoint_default_call_state"
            return
        assert result is True
        assert lab.entry.version == 7 and lab.entry.minor_version == 7
        assert (home.subentry_id, device.id, entities.async_get(state.entity_id).id, state.entity_id) == original
        assert devices.async_get(device.id).identifiers == {("voip_stack", "phone_endpoint:Home")}
        assert entities.async_get(state.entity_id).unique_id == "phone_endpoint_Home_call_state"
        assert entities.async_get(state.entity_id).name == "My existing Home state"
        assert home.data["password"] == "fixture-password-must-not-appear-in-log"
        migrated = [row for row in lab.entry.subentries.values() if row.data.get("managed_esphome")]
        assert len(migrated) == 1 and migrated[0].data["device_id"] == replacement.device.id
        assert migrated[0].data["username"] == "Meldestelle-Audio-Ersatz"
        assert migrated[0].data["extension"] == "meldestelle-audio"
        assert lab.entry.data["legacy_phone_references"][replacement.old] == "Meldestelle-Audio-Ersatz"
        # A reused extension is not permission to redirect the removed hardware's references.
        assert old.old not in lab.entry.data["legacy_phone_references"]
        assert old.old in lab.entry.data["esphome_phone_policies"]
        after = native_snapshot(lab.hass, lab.entry)
        assert await async_migrate_entry(lab.hass, lab.entry)
        assert native_snapshot(lab.hass, lab.entry) == after
    finally:
        lab.entry.runtime_data = lab.runtime


@pytest.mark.parametrize("collision", ["device", "entity"])
async def test_two_native_records_converging_on_one_identity_fail_during_preflight(hass, monkeypatch, caplog, collision):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.phone_config import PhoneIdentityConflict
    from custom_components.voip_stack.sip_account_identity import account_identity_migration_plan

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=3,
        data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    home = home_account(hass, entry, additional_reference=True)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    first = devices.async_get_or_create(config_entry_id=entry.entry_id,
        config_subentry_id=home.subentry_id, identifiers={("voip_stack", "phone_endpoint:default")}, name="Home")
    if collision == "device":
        other = devices.async_get_or_create(config_entry_id=entry.entry_id,
            config_subentry_id=home.subentry_id, identifiers={("voip_stack", "phone_endpoint:browser:home")},
            name="Previous Home device")
        expected = "phone_endpoint:Home"
        owners = first.id, other.id
    else:
        first = entities.async_get_or_create("sensor", "voip_stack", "voip_stack_ha_softphone_call_state",
            config_entry=entry, config_subentry_id=home.subentry_id, device_id=first.id)
        other = entities.async_get_or_create("sensor", "voip_stack", "phone_endpoint_default_call_state",
            config_entry=entry, config_subentry_id=home.subentry_id, device_id=first.device_id)
        expected = "phone_endpoint_Home_call_state"
        owners = first.entity_id, other.entity_id
    before = native_snapshot(hass, entry)
    with pytest.raises(PhoneIdentityConflict) as caught:
        account_identity_migration_plan(hass, entry)
    assert caught.value.canonical == expected
    writes = []
    for owner, method in ((hass.config_entries, "async_update_subentry"),
            (hass.config_entries, "async_update_entry"), (devices, "async_update_device"),
            (entities, "async_update_entity")):
        original = getattr(owner, method)
        def observed(*args, _method=method, _original=original, **kwargs):
            writes.append(_method)
            return _original(*args, **kwargs)
        monkeypatch.setattr(owner, method, observed)
    with caplog.at_level(logging.ERROR, logger="custom_components.voip_stack"):
        assert await async_migrate_entry(hass, entry) is False
    assert writes == []
    assert native_snapshot(hass, entry) == before
    assert expected in caplog.text and all(owner in caplog.text for owner in owners)
    assert home.data["password"] not in caplog.text
    issue = ir.async_get(hass).async_get_issue("voip_stack", "phone_identity_conflict_" + entry.entry_id)
    assert issue is not None and issue.translation_placeholders["canonical"] == expected


async def test_multiple_historical_keys_on_one_native_owner_can_be_consolidated(hass):
    """Equivalent aliases on a single record do not create competing HA IDs."""
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=3,
        data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    home = home_account(hass, entry, additional_reference=True)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id,
        config_subentry_id=home.subentry_id, identifiers={
            ("voip_stack", "phone_endpoint:default"),
            ("voip_stack", "phone_endpoint:browser:home"),
        }, name="Home")
    state = entities.async_get_or_create("sensor", "voip_stack", "voip_stack_ha_softphone_call_state",
        config_entry=entry, config_subentry_id=home.subentry_id, device_id=device.id)
    before = home.subentry_id, device.id, state.id, state.entity_id
    assert await async_migrate_entry(hass, entry)
    assert (home.subentry_id, device.id, entities.async_get(state.entity_id).id, state.entity_id) == before
    assert devices.async_get(device.id).identifiers == {("voip_stack", "phone_endpoint:Home")}
    assert entities.async_get(state.entity_id).unique_id == "phone_endpoint_Home_call_state"
    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 1
    assert len(er.async_entries_for_config_entry(entities, entry.entry_id)) == 1
    assert entry.data["legacy_phone_references"]["default"] == "Home"
    assert entry.data["legacy_phone_references"]["browser:home"] == "Home"


@pytest.mark.parametrize("error_type", [OSError, KeyError, RuntimeError])
async def test_nonidentity_failure_reports_safe_origin_and_retry_without_editing_accounts(hass, monkeypatch, caplog, error_type):
    from unittest.mock import AsyncMock
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.components.repairs.issue_handler import async_setup
    from homeassistant.config_entries import ConfigEntryState
    from custom_components import voip_stack
    from custom_components.voip_stack import sip_account_identity

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=3,
        data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    home = home_account(hass, entry)
    before = native_snapshot(hass, entry)
    secret = "unrelated-exception-may-include-a-secret"

    async def unavailable_storage(*args, **kwargs):
        raise error_type(secret)

    original = sip_account_identity.async_migrate_account_identities
    monkeypatch.setattr(sip_account_identity, "async_migrate_account_identities", unavailable_storage)
    setup = AsyncMock(return_value=True)
    monkeypatch.setattr(voip_stack, "async_setup_entry", setup)
    hass.config.components.add("voip_stack")
    with caplog.at_level(logging.ERROR, logger="custom_components.voip_stack"):
        assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert native_snapshot(hass, entry) == before
    assert error_type.__name__ in caplog.text and "unavailable_storage" in caplog.text
    assert "native identity conflict" not in caplog.text
    assert secret not in caplog.text and home.data["password"] not in caplog.text
    issues = ir.async_get(hass)
    assert issues.async_get_issue("voip_stack", "phone_identity_conflict_" + entry.entry_id) is None
    issue_id = "account_migration_failed_" + entry.entry_id
    issue = issues.async_get_issue("voip_stack", issue_id)
    assert issue is not None and issue.translation_placeholders["error_type"] == error_type.__name__
    hass.data.setdefault("repairs", {})
    async_setup(hass)
    manager = repairs_flow_manager(hass)
    initial = await manager.async_init("voip_stack", context={"issue_id": issue_id})
    assert initial["step_id"] == "confirm"
    # A failed retry retains both the blocked version and every native owner.
    retry = await manager.async_configure(initial["flow_id"], {})
    assert retry["errors"] == {"base": "migration_retry_failed"}
    assert native_snapshot(hass, entry) == before
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    setup.assert_not_awaited()
    monkeypatch.setattr(sip_account_identity, "async_migrate_account_identities", original)
    result = await manager.async_configure(initial["flow_id"], {})
    assert result["type"] == "create_entry" and entry.state is ConfigEntryState.LOADED
    setup.assert_awaited_once()
    assert issues.async_get_issue("voip_stack", issue_id) is None
