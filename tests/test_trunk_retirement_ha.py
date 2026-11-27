"""Legacy trunk menu retirement preserves native HA configuration and warnings."""
from copy import deepcopy

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("destination", ["Welcome", "", "Removed account"])
async def test_legacy_menu_migration_keeps_destination_and_persistent_repair(hass, destination):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.config_flow import VoipStackConfigFlow

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5,
        data={"future_parent": "kept"}, options={"future_option": "kept"})
    entry.add_to_hass(hass)
    original = {"trunk_id": "provider", "trunk_inbound_mode": "dtmf",
        "trunk_dtmf_enabled": True, "trunk_dtmf_timeout_ms": 3000,
        "trunk_dtmf_terminator": "#", "trunk_inbound_default_target": destination,
        "trunk_register": False, "trunk_allowed_ips": ["192.0.2.1/32"],
        "future_field": {"keep": True}}
    record = ConfigSubentry(subentry_type="trunk", title="Provider", unique_id="trunk:provider", data=original)
    hass.config_entries.async_add_subentry(entry, record)
    assert await async_migrate_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (7, 7)
    assert VoipStackConfigFlow.MINOR_VERSION == 7
    expected = {key: value for key, value in original.items()
        if key not in {"trunk_inbound_mode", "trunk_dtmf_enabled", "trunk_dtmf_timeout_ms", "trunk_dtmf_terminator"}}
    expected["trunk_options_interval"] = 30
    assert dict(record.data) == expected
    issues = ir.async_get(hass).issues
    issue = issues[("voip_stack", "trunk_dtmf_retired_" + record.subentry_id)]
    assert issue.is_persistent
    assert issue.config_entry_id == entry.entry_id
    assert issue.translation_placeholders["destination"] == destination
    assert issue.translation_key == ("trunk_dtmf_retired" if destination else "trunk_dtmf_retired_reject")
    before = deepcopy(dict(record.data))
    assert await async_migrate_entry(hass, entry)
    assert dict(record.data) == before
    assert len([key for key in issues if key[0] == "voip_stack"]) == 1


async def test_direct_trunk_migration_does_not_create_menu_warning(hass):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data={})
    entry.add_to_hass(hass)
    record = ConfigSubentry(subentry_type="trunk", title="Direct", unique_id="trunk:direct", data={
        "trunk_id": "direct", "trunk_inbound_mode": "direct", "trunk_dtmf_enabled": False,
        "trunk_inbound_default_target": "Office"})
    hass.config_entries.async_add_subentry(entry, record)
    assert await async_migrate_entry(hass, entry)
    assert dict(record.data) == {"trunk_id": "direct", "trunk_inbound_default_target": "Office", "trunk_options_interval": 30}
    assert not ir.async_get(hass).issues


async def test_new_trunk_form_exposes_destination_without_legacy_menu(hass):
    from custom_components.voip_stack.config_flow import TrunkFormMixin, VoipStackConfigFlow
    class Form(TrunkFormMixin, VoipStackConfigFlow):
        pass
    flow = Form()
    flow.hass = hass
    flow._current_entry_data = lambda: (None, {})
    form = await flow.async_step_trunk_mode({"trunk_register": False})
    fields = {key.schema for key in form["data_schema"].schema}
    assert "trunk_inbound_default_target" in fields
    assert not fields.intersection({"trunk_inbound_mode", "trunk_dtmf_timeout_ms", "trunk_dtmf_terminator"})


async def test_parent_trunk_migration_preserves_retirement_notice(hass):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data={
        "trunk_enabled": True, "trunk_inbound_mode": "dtmf", "trunk_dtmf_enabled": True,
        "trunk_dtmf_timeout_ms": 3000, "trunk_inbound_default_target": "Welcome"})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    records = [row for row in entry.subentries.values() if row.subentry_type == "trunk"]
    assert len(records) == 1
    assert records[0].data["trunk_inbound_default_target"] == "Welcome"
    assert ("voip_stack", "trunk_dtmf_retired_" + records[0].subentry_id) in ir.async_get(hass).issues


@pytest.mark.parametrize("settings,warning", [
    ({"trunk_inbound_mode": "dtmf", "trunk_dtmf_enabled": False, "trunk_dtmf_timeout_ms": 3000}, True),
    ({"trunk_inbound_mode": "dtmf", "trunk_dtmf_enabled": True, "trunk_dtmf_timeout_ms": 0}, False),
    ({"trunk_inbound_mode": "direct", "trunk_dtmf_enabled": True, "trunk_dtmf_timeout_ms": 3000}, False),
    ({"trunk_inbound_mode": "dtmf"}, True),
    ({"trunk_dtmf_enabled": True}, True),
    ({"trunk_inbound_mode": "unknown", "trunk_dtmf_enabled": True}, True),
    ({"trunk_dtmf_enabled": False}, False),
    ({}, False),
])
async def test_retirement_warning_requires_enabled_collection(hass, settings, warning):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data={})
    entry.add_to_hass(hass)
    record = ConfigSubentry(subentry_type="trunk", title="Provider", unique_id="trunk:provider", data={
        "trunk_id": "provider", "trunk_inbound_default_target": "", **settings})
    hass.config_entries.async_add_subentry(entry, record)
    assert await async_migrate_entry(hass, entry)
    assert dict(record.data) == {"trunk_id": "provider", "trunk_inbound_default_target": "", "trunk_options_interval": 30}
    issue = ir.async_get(hass).issues.get(("voip_stack", "trunk_dtmf_retired_" + record.subentry_id))
    assert (issue is not None) is warning
    if issue is not None:
        assert "/blob/dev/docs/AUTOMATION_DIALPLAN.md#build-a-small-keypad-menu" in issue.learn_more_url
