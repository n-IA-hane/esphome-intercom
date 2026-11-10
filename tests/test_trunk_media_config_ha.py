"""HA form/normalization contracts for per-trunk media NAT selection."""

from unittest.mock import Mock

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.config_flow import TrunkFormMixin, VoipStackConfigFlow
from custom_components.voip_stack.trunk_runtime import trunk_client_config


pytestmark = pytest.mark.ha


class _TrunkForm(TrunkFormMixin, VoipStackConfigFlow):
    """Exercise the real HA schema without opening a registration transport."""


def _form(hass, existing=None):
    existing = dict(existing or {})
    flow = _TrunkForm()
    flow.hass = hass
    entry = MockConfigEntry(domain="voip_stack", data=existing)
    flow._current_entry_data = lambda: (entry, existing)
    flow._store_entry = Mock(side_effect=lambda data: data)
    return flow


def _required_values(**extra):
    return {
        "trunk_server": "pbx.example.org",
        "trunk_username": "line-user",
        "trunk_password": " fixture-only opaque password ",
        "trunk_inbound_default_target": "Kitchen",
        **extra,
    }


@pytest.mark.parametrize("existing,expected_mode", [
    ({}, "auto"),
    ({"trunk_outbound_media_address": "198.51.100.22"}, "manual"),
    ({"trunk_media_nat": "direct", "trunk_outbound_media_address": "198.51.100.22"}, "direct"),
])
async def test_media_mode_defaults_preserve_legacy_override(hass, existing, expected_mode):
    flow = _form(hass, existing)
    result = await flow.async_step_trunk()
    schema = result["data_schema"]
    values = schema(_required_values())
    assert values["trunk_media_nat"] == expected_mode
    stored = await flow.async_step_trunk(values)
    normalized = normalized_trunk_config(stored)
    client = trunk_client_config(normalized)
    assert normalized["trunk_media_nat"] == expected_mode
    assert client.media_nat_mode == expected_mode
    assert client.outbound_media_address == existing.get("trunk_outbound_media_address", "")


@pytest.mark.parametrize("mode", ["auto", "direct", "manual"])
async def test_media_mode_reconfigure_preserves_signaling_accounts_and_opaque_password(hass, mode):
    accounts = [{"username": "local-phone", "password": "fixture-local-only"}]
    contacts = [{"id": "kitchen", "name": "Kitchen", "extension": "202"}]
    existing = {"sip_accounts": accounts, "phonebook_contacts": contacts}
    flow = _form(hass, existing)
    form = await flow.async_step_trunk()
    values = form["data_schema"](_required_values(
        trunk_media_nat=mode,
        trunk_outbound_media_address="198.51.100.22" if mode == "manual" else "",
        trunk_transport="tcp", trunk_port=5070, trunk_domain="realm.example.org",
        trunk_auth_username="digest-user", trunk_register_expires=600,
        trunk_outbound_proxy="sip:proxy.example.org:5080",
    ))
    stored = await flow.async_step_trunk(values)
    assert stored["sip_accounts"] == accounts
    assert stored["phonebook_contacts"] == contacts
    client = trunk_client_config(normalized_trunk_config(stored))
    assert client.media_nat_mode == mode
    assert client.register is True
    assert client.transport == "tcp"
    assert client.server == "pbx.example.org"
    assert client.port == 5070
    assert client.domain == "realm.example.org"
    assert client.username == "line-user"
    assert client.auth_username == "digest-user"
    assert client.password == " fixture-only opaque password "
    assert client.expires == 600
    assert client.outbound_proxy == "sip:proxy.example.org:5080"


async def test_manual_requires_explicit_unicast_ipv4_before_store(hass):
    flow = _form(hass)
    form = await flow.async_step_trunk()
    values = form["data_schema"](_required_values(trunk_media_nat="manual"))
    result = await flow.async_step_trunk(values)
    assert result["type"] == "form"
    assert result["errors"] == {"trunk_outbound_media_address": "trunk_media_address_invalid"}
    flow._store_entry.assert_not_called()


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "224.0.0.1", "::1", "https://example.org"])
async def test_manual_rejects_invalid_media_address(hass, address):
    flow = _form(hass)
    form = await flow.async_step_trunk()
    values = form["data_schema"](_required_values(trunk_media_nat="manual", trunk_outbound_media_address=address))
    result = await flow.async_step_trunk(values)
    assert result["errors"]["trunk_outbound_media_address"] == "trunk_media_address_invalid"
    flow._store_entry.assert_not_called()


async def test_unknown_mode_rejected_by_selector_and_handler(hass):
    flow = _form(hass)
    form = await flow.async_step_trunk()
    with pytest.raises(vol.Invalid):
        form["data_schema"](_required_values(trunk_media_nat="invented"))
    # The handler also validates persisted/direct inputs rather than relying on
    # the selector to protect all call sites.
    values = form["data_schema"](_required_values())
    values["trunk_media_nat"] = "invented"
    result = await flow.async_step_trunk(values)
    assert result["errors"] == {"trunk_media_nat": "trunk_media_nat_invalid"}
    flow._store_entry.assert_not_called()


@pytest.mark.parametrize("mode", ["direct", "manual"])
async def test_explicit_modes_do_not_consult_stun_or_change_signaling(hass, mode):
    from types import SimpleNamespace
    from custom_components.voip_stack.trunk_media import prepare_trunk_media

    config = trunk_client_config(normalized_trunk_config({
        "trunk_media_nat": mode, "trunk_outbound_media_address": "198.51.100.22",
        "trunk_server": "pbx.example.org", "trunk_password": "fixture-only",
    }))
    client = SimpleNamespace(local_ip="192.0.2.10", media_ip="198.51.100.22")
    servers = Mock(side_effect=AssertionError("explicit media mode must not query STUN"))
    await prepare_trunk_media(client, config, "pbx.example.org", servers)
    assert client.media_ip == ("192.0.2.10" if mode == "direct" else "198.51.100.22")
    assert config.server == "pbx.example.org"
    assert config.password == "fixture-only"
    servers.assert_not_called()


def test_normalization_without_form_keeps_legacy_override_and_default_auto():
    assert normalized_trunk_config({})["trunk_media_nat"] == "auto"
    legacy = normalized_trunk_config({"trunk_outbound_media_address": "198.51.100.22"})
    assert legacy["trunk_media_nat"] == "manual"
    assert trunk_client_config(legacy).outbound_media_address == "198.51.100.22"


@pytest.mark.parametrize("mode,address", [
    ("auto", ""), ("auto", "198.51.100.22"), ("direct", ""), ("manual", "198.51.100.22"),
])
def test_disabling_trunk_preserves_media_mode_and_account_credentials(mode, address):
    from custom_components.voip_stack.config_flow import _disabled_trunk_data

    accounts = [{"username": "local-phone", "password": "fixture-local-only"}]
    previous = {
        "trunk_media_nat": mode,
        "trunk_outbound_media_address": address,
        "trunk_password": " fixture-only opaque password ",
        "sip_accounts": accounts,
    }
    disabled = _disabled_trunk_data({}, previous)
    client = trunk_client_config(normalized_trunk_config(disabled))
    assert client.media_nat_mode == mode
    assert client.outbound_media_address == previous["trunk_outbound_media_address"]
    assert client.password == previous["trunk_password"]
    assert disabled["sip_accounts"] == accounts
