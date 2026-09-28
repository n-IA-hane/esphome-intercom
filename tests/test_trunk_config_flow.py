"""Registration mode forms and saved static trunk configuration."""
from unittest.mock import Mock

import pytest
from custom_components.voip_stack.config_flow import VoipStackConfigFlow

pytestmark = pytest.mark.ha


@pytest.mark.asyncio
async def test_mode_switch_hides_registration_expiry_and_requires_source_acl(hass):
    flow = VoipStackConfigFlow()
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
    values = form["data_schema"]({"trunk_server": "127.0.0.1", "trunk_allowed_ips": ["127.0.0.2"]})
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
    flow = VoipStackConfigFlow()
    flow.hass = hass
    flow._current_entry_data = lambda: (None, {})
    form = await flow.async_step_trunk_mode({"trunk_register": False})
    for ips in ([], ["bad"], ["0.0.0.0/0"]):
        data = form["data_schema"]({"trunk_server": "127.0.0.1", "trunk_allowed_ips": ips})
        result = await flow.async_step_trunk(data)
        assert result["errors"] == {"trunk_allowed_ips": "trunk_allowed_ips_invalid"}
