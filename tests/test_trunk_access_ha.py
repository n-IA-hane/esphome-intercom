"""External-call permissions through the real Home Assistant service boundary."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry


DOMAIN = "voip_stack"
SERVICE = "set_external_call_access"
BLOCKED = "external_call_blocked_endpoints"
pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    import custom_components

    original = custom_components.__path__
    custom_components.__path__ = [
        *original,
        str(Path(__file__).parents[1] / "custom_components"),
    ]
    yield
    custom_components.__path__ = original


@pytest.fixture
async def phones(hass):
    """Load real service registration without opening SIP or media sockets."""
    from custom_components.voip_stack.config import entry_transport_config
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    hass.config.components.update({"assist_pipeline", "http", "lovelace", "network"})
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain=DOMAIN, data={"sip_port": 5099})
    entry.add_to_hass(hass)
    registry = EndpointRegistry()
    for kind in EndpointKind:
        registry.register(PhoneEndpoint(
            endpoint_id=f"phone-{kind.value}",
            name=f"Original {kind.value}",
            kind=kind,
            device_id=f"device-{kind.value}",
            username="426" if kind is EndpointKind.SIP_ACCOUNT else "",
        ))
    entry.runtime_data = VoipStackRuntime(
        transport_config=entry_transport_config(entry),
        assist_config={},
        trunk_config={},
        endpoints=registry,
        phones=PhoneAdapterRegistry(hass, registry),
    )
    return entry, registry


async def _set(hass, kind, allowed, *, context=None):
    return await hass.services.async_call(
        DOMAIN, SERVICE,
        {"device_id": f"device-{kind}", "allowed": allowed},
        blocking=True, return_response=True, context=context,
    )


@pytest.mark.parametrize("kind", ["browser", "sip_account", "esphome"])
async def test_default_allow_disable_and_enable_are_persisted(hass, phones, kind):
    from custom_components.voip_stack.config import entry_transport_config
    from custom_components.voip_stack.trunk_policy import external_call_denied

    entry, _registry = phones
    endpoint_id = f"phone-{kind}"
    assert external_call_denied(entry_transport_config(entry), endpoint_id) == ""
    response = await _set(hass, kind, False)
    assert response == {
        "device_id": f"device-{kind}", "endpoint_id": endpoint_id, "allowed": False,
    }
    assert entry.data[BLOCKED] == [endpoint_id]
    assert entry.data["sip_port"] == 5099
    assert external_call_denied(entry.runtime_data.transport_config, endpoint_id) == endpoint_id

    # Reconstruct from serialized config data, not the existing runtime cache.
    restored = MockConfigEntry(domain=DOMAIN, data=dict(entry.data))
    assert external_call_denied(entry_transport_config(restored), endpoint_id) == endpoint_id
    await _set(hass, kind, False)
    assert entry.data[BLOCKED] == [endpoint_id]
    await _set(hass, kind, True)
    assert endpoint_id not in entry.data.get(BLOCKED, [])
    assert external_call_denied(entry.runtime_data.transport_config, endpoint_id) == ""


async def test_rename_preserves_restriction_without_reloading_active_runtime(hass, phones, monkeypatch):
    from custom_components.voip_stack.config_entry_runtime import entry_runtime_signature
    from custom_components.voip_stack.trunk_policy import external_call_denied

    entry, registry = phones
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    runtime = entry.runtime_data
    transport = runtime.transport_config
    signature = entry_runtime_signature(entry)
    registry.claim_call("phone-browser", "existing-call")
    await _set(hass, "browser", False)
    registry.update("phone-browser", name="Renamed phone", extension="999")
    await hass.async_block_till_done()
    assert external_call_denied(transport, "phone-browser") == "phone-browser"
    assert external_call_denied(transport, "phone-sip_account") == ""
    assert registry.get("phone-browser").active_call_id == "existing-call"
    assert entry.runtime_data is runtime
    assert runtime.transport_config is transport
    assert entry_runtime_signature(entry) == signature
    reload.assert_not_awaited()


async def test_only_administrators_and_internal_automations_can_change_access(
    hass, phones, hass_read_only_user, hass_admin_user,
):
    entry, _registry = phones
    for allowed in (False, True):
        with pytest.raises(Unauthorized):
            await _set(hass, "browser", allowed, context=Context(user_id=hass_read_only_user.id))
    assert not entry.data.get(BLOCKED)
    await _set(hass, "browser", False, context=Context(user_id=hass_admin_user.id))
    assert entry.data[BLOCKED] == ["phone-browser"]
    with pytest.raises(Unauthorized):
        await _set(hass, "browser", True, context=Context(user_id=hass_read_only_user.id))
    assert entry.data[BLOCKED] == ["phone-browser"]
    await _set(hass, "browser", True)
    assert not entry.data.get(BLOCKED)


async def test_unknown_phone_is_rejected_without_modifying_configuration(hass, phones):
    entry, _registry = phones
    before = dict(entry.data)
    with pytest.raises(ServiceValidationError):
        await _set(hass, "missing", False)
    assert dict(entry.data) == before


async def test_reenabling_one_phone_preserves_other_phones_restrictions(hass, phones):
    from custom_components.voip_stack.trunk_policy import external_call_denied

    entry, _registry = phones
    await _set(hass, "browser", False)
    await _set(hass, "sip_account", False)
    await _set(hass, "browser", True)
    assert entry.data[BLOCKED] == ["phone-sip_account"]
    assert external_call_denied(entry.runtime_data.transport_config, "phone-browser") == ""
    assert external_call_denied(entry.runtime_data.transport_config, "phone-sip_account") == "phone-sip_account"


async def test_automation_action_does_not_require_response_variable(hass, phones):
    entry, _registry = phones
    result = await hass.services.async_call(
        DOMAIN, SERVICE,
        {"device_id": "device-esphome", "allowed": False},
        blocking=True,
    )
    assert result is None
    assert entry.data[BLOCKED] == ["phone-esphome"]


@pytest.mark.parametrize("allowed", [False, True])
async def test_reconfigure_preserves_latest_permission_changed_during_wizard(
    hass, phones, monkeypatch, allowed,
):
    from homeassistant.config_entries import SOURCE_RECONFIGURE
    from homeassistant.data_entry_flow import FlowResultType
    from custom_components.voip_stack.const import CONF_SIP_VIDEO

    entry, _registry = phones
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    await _set(hass, "browser", not allowed)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    user_input = result["data_schema"]({CONF_SIP_VIDEO: True})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["step_id"] == "video"
    # Another automation may change policy while an administrator has the
    # multi-step form open. Saving the form must retain that latest decision.
    await _set(hass, "browser", allowed)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], result["data_schema"]({}),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert ("phone-browser" in entry.data.get(BLOCKED, ())) is not allowed
    assert entry.data[CONF_SIP_VIDEO] is True


@pytest.mark.parametrize("data", [
    {"allowed": False},
    {"device_id": "device-browser"},
    {"device_id": "device-browser", "allowed": "not-a-boolean"},
    {"device_id": ["device-browser", "device-sip_account"], "allowed": False},
    {"entity_id": "sensor.phone", "allowed": False},
])
async def test_service_requires_one_explicit_phone_and_boolean(hass, phones, data):
    import voluptuous as vol

    entry, _registry = phones
    before = dict(entry.data)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, SERVICE, data, blocking=True)
    assert dict(entry.data) == before
