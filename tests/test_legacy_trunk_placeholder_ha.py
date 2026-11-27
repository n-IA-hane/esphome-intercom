"""Empty historical trunk forms are not lines; configured disabled lines remain."""

from copy import deepcopy

import pytest
from homeassistant.config_entries import ConfigSubentry
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


# Persisted shape of the former disabled-trunk form, including its defaults.
EMPTY_FORM = {
    "trunk_enabled": False, "trunk_register": True, "trunk_allowed_ips": [],
    "trunk_transport": "udp", "trunk_server": "", "trunk_port": 5060,
    "trunk_domain": "", "trunk_username": "", "trunk_auth_username": "",
    "trunk_password": "", "trunk_register_expires": 300, "trunk_outbound_proxy": "",
    "trunk_inbound_default_target": "HA", "trunk_inbound_unknown_target": "fallback",
    "trunk_inbound_mode": "direct", "trunk_dtmf_enabled": False,
    "trunk_dtmf_timeout_ms": 3000, "trunk_dtmf_terminator": "#",
}


@pytest.mark.parametrize("version,minor", [(6, 2), (7, 5), (7, 7)])
@pytest.mark.parametrize("legacy", [
    {"trunk_enabled": False},
    {"trunk_enabled": False, "trunk_server": "", "trunk_username": "",
     "trunk_password": "", "trunk_inbound_default_target": ""},
    {"trunk_enabled": False, "trunk_server": None, "trunk_username": None,
     "trunk_password": None, "trunk_inbound_default_target": None},
    EMPTY_FORM,
], ids=["flag-only", "blank-fields", "null-fields", "disabled-form-defaults"])
async def test_empty_legacy_parent_does_not_create_trunk(hass, version, minor, legacy):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor,
        data={**deepcopy(legacy), "unrelated": {"value": "keep"}})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (7, 7)
    assert not [row for row in entry.subentries.values() if row.subentry_type == "trunk"]
    assert not any(key.startswith("trunk_") for key in entry.data)
    assert "default_trunk_id" not in entry.data
    assert entry.data["unrelated"] == {"value": "keep"}
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


@pytest.mark.parametrize("version,minor", [(6, 2), (7, 5), (7, 7)])
@pytest.mark.parametrize("selected", [None, "provider", ""], ids=["implicit", "selected", "none"])
async def test_empty_legacy_parent_does_not_overwrite_native_trunk(hass, version, minor, selected):
    from custom_components.voip_stack import async_migrate_entry

    data = deepcopy(EMPTY_FORM)
    if selected is not None:
        data["default_trunk_id"] = selected
    entry = MockConfigEntry(domain="voip_stack", version=version, minor_version=minor, data=data)
    entry.add_to_hass(hass)
    native = ConfigSubentry(subentry_type="trunk", title="Existing provider",
        unique_id="trunk:provider", data={
            "trunk_id": "provider", "trunk_enabled": True,
            "trunk_server": "provider.example", "trunk_username": "office",
            "trunk_password": "fixture-password", "trunk_inbound_default_target": "Desk",
            "trunk_options_interval": 37,
        })
    hass.config_entries.async_add_subentry(entry, native)
    before = deepcopy(native.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert list(entry.subentries.values()) == [native]
    assert native.as_dict() == before
    assert not any(key.startswith("trunk_") for key in entry.data)
    if selected is None:
        assert "default_trunk_id" not in entry.data
    else:
        assert entry.data["default_trunk_id"] == selected


@pytest.mark.parametrize("choice", [
    {"trunk_server": "provider.example"},
    {"trunk_domain": "realm.example"},
    {"trunk_username": "office"},
    {"trunk_auth_username": "office-auth"},
    {"trunk_password": " "},
    {"trunk_inbound_account": "office-account"},
    {"trunk_inbound_default_target": "Desk"},
    {"trunk_inbound_unknown_target": "reject"},
    {"trunk_allowed_ips": ["192.0.2.42"]},
    {"trunk_outbound_proxy": "proxy.example"},
    {"trunk_transport": "tcp"},
    {"trunk_register": False},
    {"trunk_options_interval": 0},
    {"trunk_options_interval": 37},
    {"trunk_caller_id_policy": "caller"},
    {"trunk_inbound_mode": "dtmf", "trunk_dtmf_timeout_ms": 3000},
], ids=["server", "domain", "username", "auth-user", "password", "inbound-account",
        "destination", "reject", "acl", "proxy", "transport", "static", "probe-disabled",
        "probe-custom", "caller-policy", "collector"])
async def test_disabled_partial_legacy_configuration_is_preserved(hass, choice):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5,
        data={**deepcopy(EMPTY_FORM), **choice})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    native, = [row for row in entry.subentries.values() if row.subentry_type == "trunk"]
    assert native.data["trunk_enabled"] is False
    for key, value in choice.items():
        if key == "trunk_inbound_unknown_target":
            assert native.data["trunk_inbound_default_target"] == ""
        elif key not in {"trunk_inbound_mode", "trunk_dtmf_timeout_ms"}:
            assert native.data[key] == value
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


async def test_enabled_incomplete_legacy_trunk_is_not_a_placeholder(hass):
    from custom_components.voip_stack import async_migrate_entry

    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5,
        data={**deepcopy(EMPTY_FORM), "trunk_enabled": True})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    native, = [row for row in entry.subentries.values() if row.subentry_type == "trunk"]
    assert native.data["trunk_enabled"] is True
