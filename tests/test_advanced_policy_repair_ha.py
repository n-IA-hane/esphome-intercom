"""Saved technical policies remain editable while they prevent PBX setup."""

import pytest

from .test_duration_migration_repair_ha import blocked_repair, edit_durations, owner_choice
from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("data,rows,correction,kind", [
    ({"registrar_contact_limit": 0}, (), {"registrar_contact_limit": 8}, None),
    ({"registrar_min_expires": 120, "registrar_max_expires": 60}, (),
        {"registrar_min_expires": 120, "registrar_max_expires": 180}, None),
    ({"audio_rtcp_min_interval": 4}, (), {"audio_rtcp_min_interval": 7}, None),
    ({"local_redirect_max_hops": 0}, (), {"local_redirect_max_hops": 6}, None),
    ({}, [("trunk", {"trunk_id": "provider", "trunk_options_interval": -1,
                    "trunk_password": "fixture-secret"})], {"trunk_options_interval": 0}, "trunk"),
])
async def test_invalid_stored_advanced_policy_is_repairable_before_setup(hass, monkeypatch,
        data, rows, correction, kind):
    entry, setup, manager, initial, _ = await blocked_repair(hass, monkeypatch, data, rows)
    assert initial["step_id"] == "init"
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    assert form["step_id"] == "edit_durations"
    assert "fixture-secret" not in repr(form)
    fields = next(iter(form["data_schema"].schema.values())).schema.schema
    assert {key.schema for key in fields} == set(correction)
    for key, selector in fields.items():
        if key.schema in {"registrar_contact_limit", "local_redirect_max_hops"}:
            assert "unit_of_measurement" not in selector.config
    result = await edit_durations(manager, form["flow_id"], correction)
    assert result["type"] == "create_entry"
    actual = next(row.data for row in entry.subentries.values() if row.subentry_type == kind) if kind else entry.data
    assert all(actual[key] == value for key, value in correction.items())
    setup.assert_awaited_once()


async def test_rejected_nested_repair_keeps_the_other_submitted_values_visible(hass, monkeypatch):
    _entry, _setup, manager, initial, _ = await blocked_repair(hass, monkeypatch,
        {"service_timeouts": {"media_timeout": 0, "dtmf_timeout": 0}})
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    invalid = await edit_durations(manager, form["flow_id"], {"media_timeout": 70, "dtmf_timeout": 0})
    assert invalid["errors"] == {"base": "invalid_duration"}
    fields = next(iter(invalid["data_schema"].schema.values())).schema.schema
    assert {key.schema: key.default() for key in fields} == {"media_timeout": 70, "dtmf_timeout": 0}
