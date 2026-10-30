"""Registration mode forms and saved static trunk configuration."""
from unittest.mock import Mock

import pytest
from custom_components.voip_stack.config_flow import TrunkFormMixin, VoipStackConfigFlow


class TrunkForm(TrunkFormMixin, VoipStackConfigFlow):
    """Isolate form schema tests from network registration validation."""

pytestmark = pytest.mark.ha


@pytest.mark.asyncio
async def test_mode_switch_hides_registration_expiry_and_requires_source_acl(hass):
    flow = TrunkForm()
    flow.hass = hass
    flow._current_entry_data = lambda: (None, {})
    form = await flow.async_step_trunk_mode()
    assert form["data_schema"]({})["trunk_register"] is True
    form = await flow.async_step_trunk_mode({"trunk_register": False})
    fields = {key.schema for key in form["data_schema"].schema}
    assert "trunk_allowed_ips" in fields
    assert "trunk_register_expires" not in fields
    transport = next(value for key, value in form["data_schema"].schema.items() if key.schema == "trunk_transport")
    assert transport.config["options"] == ["udp", "tcp"]
    values = form["data_schema"]({"trunk_server": "127.0.0.1", "trunk_allowed_ips": ["127.0.0.2"], "trunk_inbound_default_target": "Kitchen"})
    flow._store_entry = Mock(side_effect=lambda data: data)
    flow.async_set_unique_id = __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    stored = await flow.async_step_trunk(values)
    assert stored["trunk_register"] is False
    assert stored["trunk_allowed_ips"] == ["127.0.0.2/32"]
    assert stored["trunk_password"] == ""
    form = await flow.async_step_trunk_mode({"trunk_register": True})
    fields = {key.schema for key in form["data_schema"].schema}
    assert "trunk_allowed_ips" not in fields
    assert "trunk_register_expires" in fields


@pytest.mark.asyncio
async def test_empty_or_unrestricted_acl_cannot_be_saved(hass):
    flow = TrunkForm()
    flow.hass = hass
    flow._current_entry_data = lambda: (None, {})
    form = await flow.async_step_trunk_mode({"trunk_register": False})
    for ips in ([], ["bad"], ["0.0.0.0/0"]):
        data = form["data_schema"]({"trunk_server": "127.0.0.1", "trunk_allowed_ips": ips, "trunk_inbound_default_target": "Kitchen"})
        result = await flow.async_step_trunk(data)
        assert result["errors"] == {"trunk_allowed_ips": "trunk_allowed_ips_invalid"}


@pytest.fixture
def phone_services_flow(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain="voip_stack", version=6, data={"sip_port": 15060, "existing_setting": "preserve"})
    entry.add_to_hass(hass)
    flow = VoipStackConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    return flow, entry


async def test_phone_services_defaults_and_conflicting_ports(phone_services_flow):
    flow, entry = phone_services_flow
    form = await flow.async_step_phone_services()
    values = form["data_schema"]({})
    assert values["ldap_port"] == 1389 and values["imap_port"] == 1143
    assert values["tls"] is False and values["allow_anonymous"] is False
    values["imap_port"] = values["ldap_port"]
    result = await flow.async_step_phone_services(values)
    assert result["type"] == "form" and result["errors"] == {"base": "phone_service_ports_conflict"}
    assert "phone_services" not in entry.data


@pytest.mark.parametrize("certificate,private_key", [("", ""), ("/config/test.crt", ""), ("", "/config/test.key")])
async def test_phone_services_tls_requires_both_files(phone_services_flow, certificate, private_key):
    flow, entry = phone_services_flow
    form = await flow.async_step_phone_services()
    values = form["data_schema"]({"tls": True, "certificate": certificate, "private_key": private_key})
    result = await flow.async_step_phone_services(values)
    assert result["errors"] == {"base": "phone_service_certificate_required"}
    assert "phone_services" not in entry.data


async def test_phone_services_normal_configuration_persists_without_replacing_other_settings(phone_services_flow):
    flow, entry = phone_services_flow
    form = await flow.async_step_phone_services()
    values = form["data_schema"]({"ldap_port": 2389, "imap_port": 2143, "allow_anonymous": True,
        "base_dn": "ou=contacts,dc=home", "listen_host": "127.0.0.1"})
    result = await flow.async_step_phone_services(values)
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert dict(entry.data["phone_services"]) == values
    assert entry.data["sip_port"] == 15060 and entry.data["existing_setting"] == "preserve"
