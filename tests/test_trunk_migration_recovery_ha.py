"""Stored trunk decisions and failed upgrades retain their original ownership."""

from copy import deepcopy

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

pytestmark = pytest.mark.ha


def add_trunk(hass, entry, identity, **extra):
    row = ConfigSubentry(subentry_type="trunk", title=identity, unique_id="trunk:" + identity,
        data={"trunk_id": identity, "trunk_register": False,
              "trunk_allowed_ips": ["192.0.2.10"], "trunk_inbound_default_target": "Desk", **extra})
    hass.config_entries.async_add_subentry(entry, row)
    return row


@pytest.mark.parametrize("minor", [5, 6, 7])
async def test_saved_unknown_reject_is_preserved_by_projection_and_migration(hass, minor):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.config import normalized_trunk_config
    from custom_components.voip_stack.config_flow import _trunk_target_choices

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=minor,
        data={"default_trunk_id": "provider"})
    entry.add_to_hass(hass)
    row = add_trunk(hass, entry, "provider", trunk_inbound_unknown_target="reject")
    assert normalized_trunk_config(row.data)["trunk_inbound_default_target"] == ""
    assert _trunk_target_choices(hass, entry, row.data)[1] == ""
    assert await async_migrate_entry(hass, entry)
    assert row.data["trunk_inbound_default_target"] == ""
    assert "trunk_inbound_unknown_target" not in row.data
    assert entry.data["default_trunk_id"] == "provider"
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


@pytest.mark.parametrize("minor", [5, 6])
async def test_duration_preflight_precedes_every_trunk_retirement_write(hass, minor):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=minor,
        data={"service_timeouts": {"media_timeout": 0}})
    entry.add_to_hass(hass)
    row = add_trunk(hass, entry, "provider", trunk_inbound_mode="dtmf", trunk_dtmf_timeout_ms=3000)
    before = deepcopy(entry.as_dict())
    assert not await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before
    assert ir.async_get(hass).async_get_issue("voip_stack", "duration_configuration_invalid_" + entry.entry_id)
    assert ir.async_get(hass).async_get_issue("voip_stack", "trunk_dtmf_retired_" + row.subentry_id) is None


async def test_malformed_later_trunk_is_reported_before_mutating_first_trunk(hass):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data={})
    entry.add_to_hass(hass)
    add_trunk(hass, entry, "first", trunk_inbound_mode="dtmf", trunk_dtmf_timeout_ms=3000)
    add_trunk(hass, entry, "second", trunk_inbound_mode="dtmf", trunk_dtmf_timeout_ms="invalid")
    before = deepcopy(entry.as_dict())
    assert not await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before
    issue = ir.async_get(hass).async_get_issue("voip_stack", "duration_configuration_invalid_" + entry.entry_id)
    assert issue and issue.is_fixable


@pytest.mark.parametrize("minor", [6, 7])
async def test_residual_parent_trunk_converges_once_without_resurrecting_legacy_fields(hass, minor):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=minor, data={
        "trunk_enabled": True, "trunk_register": False, "trunk_allowed_ips": ["192.0.2.10"],
        "trunk_inbound_default_target": "Welcome", "trunk_inbound_mode": "dtmf",
        "trunk_dtmf_timeout_ms": 3000, "unrelated": "preserved"})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    row, = entry.subentries.values()
    assert row.data["trunk_inbound_default_target"] == "Welcome"
    assert row.data["trunk_options_interval"] == 30
    assert entry.data["default_trunk_id"] == row.data["trunk_id"]
    assert not any(key.startswith("trunk_") for key in entry.data)
    assert entry.data["unrelated"] == "preserved"
    assert ir.async_get(hass).async_get_issue("voip_stack", "trunk_dtmf_retired_" + row.subentry_id)
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


async def test_interrupted_retirement_recreates_notice_from_saved_journal(hass, monkeypatch):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data={})
    entry.add_to_hass(hass)
    row = add_trunk(hass, entry, "provider", trunk_inbound_mode="dtmf", trunk_dtmf_timeout_ms=3000)
    update_entry = hass.config_entries.async_update_entry
    interrupted = False

    def interrupt_final_write(current, **kwargs):
        nonlocal interrupted
        if current is entry and kwargs.get("minor_version") == 7 and not interrupted:
            interrupted = True
            raise OSError("Interrupted configuration save")
        return update_entry(current, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", interrupt_final_write)
    assert not await async_migrate_entry(hass, entry)
    assert "trunk_inbound_mode" not in row.data
    assert entry.data["trunk_menu_retirement_pending"] == [row.subentry_id]
    issue_id = "trunk_dtmf_retired_" + row.subentry_id
    # Config entries and the issue registry have independent persistence. The
    # retry must recover the notice even if only the config entry was flushed.
    ir.async_delete_issue(hass, "voip_stack", issue_id)
    assert await async_migrate_entry(hass, entry)
    assert "trunk_menu_retirement_pending" not in entry.data
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id)


@pytest.mark.parametrize("legacy", [None, "", " fallback "])
def test_legacy_empty_unknown_policy_keeps_its_existing_fallback(legacy):
    from custom_components.voip_stack.config import normalized_trunk_config

    assert normalized_trunk_config({"trunk_inbound_unknown_target": legacy,
        "trunk_inbound_default_target": "Desk"})["trunk_inbound_default_target"] == "Desk"
