"""Native identity choices preserve account IDs and validate peer authority."""

from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigSubentry
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voip_stack.config import normalized_trunk_config, trunk_enabled
from custom_components.voip_stack.trunk_identity_form import validate_trunk_identity_form
from .test_trunk_config_flow import TrunkForm

pytestmark = pytest.mark.ha


@pytest.fixture
def account_entry(hass):
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title="Peer", unique_id="phone:peer-main", data={
            "kind": "sip_account", "username": "peer-main", "password": "fixture-only", "enabled": True,
        },
    ))
    return entry


async def test_authenticated_static_peer_can_be_saved_without_ip_trust_or_fallback(hass, account_entry):
    flow = TrunkForm()
    flow.hass = hass
    flow._current_entry_data = lambda: (account_entry, {})
    form = await flow.async_step_trunk_mode({"trunk_register": False})
    data = form["data_schema"]({
        "trunk_server": "192.0.2.2", "trunk_inbound_account": "peer-main",
        "trunk_inbound_unknown_target": "reject", "trunk_caller_id_policy": "account",
    })
    flow._store_entry = Mock(side_effect=lambda value: value)
    saved = await flow.async_step_trunk(data)
    assert saved["trunk_inbound_account"] == "peer-main"
    assert saved["trunk_allowed_ips"] == []
    assert saved["trunk_caller_id_policy"] == "account"
    assert trunk_enabled(normalized_trunk_config(saved))
    assert next(iter(account_entry.subentries.values())).unique_id == "phone:peer-main"


@pytest.mark.parametrize("enabled", [True, False])
def test_one_enabled_trunk_owns_a_bound_account(hass, account_entry, enabled):
    hass.config_entries.async_add_subentry(account_entry, ConfigSubentry(
        subentry_type="trunk", title="Other", unique_id="trunk:other", data={
            "trunk_id": "other", "trunk_enabled": enabled, "trunk_inbound_account": "peer-main",
        },
    ))
    errors = validate_trunk_identity_form({"trunk_inbound_account": "peer-main"}, {}, account_entry, trunk_id="new")
    assert errors == ({"trunk_inbound_account": "trunk_inbound_account_in_use"} if enabled else {})


@pytest.mark.parametrize("binding", ["missing", "peer-main"])
def test_missing_or_disabled_account_is_an_explicit_form_error(hass, account_entry, binding):
    row = next(iter(account_entry.subentries.values()))
    hass.config_entries.async_update_subentry(account_entry, row, data={**row.data, "enabled": False})
    assert validate_trunk_identity_form({"trunk_inbound_account": binding}, {}, account_entry) == {
        "trunk_inbound_account": "trunk_inbound_account_invalid",
    }


def test_editing_other_settings_keeps_identity_selection(account_entry):
    current = {"trunk_inbound_account": "peer-main", "trunk_caller_id_policy": "caller",
        "trunk_caller_id": "+3906123400", "trunk_caller_name": "Office", "trunk_inbound_unknown_target": "reject"}
    edit = {"trunk_domain": "voice.example"}
    assert not validate_trunk_identity_form(edit, current, account_entry)
    assert all(edit[key] == value for key, value in current.items())
