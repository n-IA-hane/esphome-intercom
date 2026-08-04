"""Home Assistant level service tests through supported public APIs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry


DOMAIN = "voip_stack"
INTEGRATION_DEPENDENCIES = {
    "assist_pipeline",
    "http",
    "lovelace",
    "network",
}
pytestmark = pytest.mark.ha


def _prepare_integration_dependencies(hass: HomeAssistant) -> None:
    """Provide dependency surfaces without starting unrelated HA services."""
    hass.config.components.update(INTEGRATION_DEPENDENCIES)
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations) -> Iterator[None]:
    """Allow loading the checked-out custom integration."""
    import custom_components

    component_root = str(Path(__file__).parents[1] / "custom_components")
    original_path = custom_components.__path__
    custom_components.__path__ = [*original_path, component_root]
    yield
    custom_components.__path__ = original_path


async def test_physical_phone_call_response_uses_the_selected_esp_endpoint(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    components = await loader.async_get_custom_components(hass)
    assert DOMAIN in components
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    from custom_components import voip_stack

    service_response = {
        "schema_version": 2,
        "success": True,
        "operation": "originate",
        "phone": {
            "endpoint_id": "esphome:kitchen",
            "device_id": "device-kitchen",
            "kind": "esphome",
            "name": "Waveshare P4 Touch",
        },
        "call": {
            "call_id": "",
            "state": "accepted",
            "destination": "Casa",
        },
        "endpoint_id": "esphome:kitchen",
        "endpoint_type": "esphome",
        "device_id": "device-kitchen",
        "name": "Waveshare P4 Touch",
        "call_id": "",
        "state": "accepted",
        "destination": "Casa",
    }
    action_result = MagicMock()
    action_result.as_service_response.return_value = service_response
    originate = AsyncMock(return_value=action_result)
    monkeypatch.setattr(voip_stack, "_originate_phone_action", originate)

    response = await hass.services.async_call(
        DOMAIN,
        "call",
        {
            "device_id": "device-kitchen",
            "destination": "Casa",
            "send_video": True,
        },
        blocking=True,
        return_response=True,
    )

    assert response == service_response
    originate.assert_awaited_once()
    action_result.as_service_response.assert_called_once_with(
        include_legacy_fields=True
    )


async def test_default_browser_call_response_does_not_require_an_open_card(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    from custom_components import voip_stack

    action_result = MagicMock()
    action_result.as_service_response.return_value = {
        "schema_version": 2,
        "success": True,
    }
    originate = AsyncMock(return_value=action_result)
    monkeypatch.setattr(voip_stack, "_originate_phone_action", originate)

    response = await hass.services.async_call(
        DOMAIN,
        "call",
        {"destination": "427"},
        blocking=True,
        return_response=True,
    )

    assert response == {"schema_version": 2, "success": True}
    originate.assert_awaited_once()


async def test_purge_devices_is_disabled_before_resolving_or_removing_devices(
    hass: HomeAssistant,
) -> None:
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "purge_devices",
            {},
            blocking=True,
        )


async def test_entry_runtime_exposes_configuration_without_global_mirrors(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack.config import (
        assist_config,
        debug_mode,
        media_capture_enabled,
        transport_config,
        trunk_config,
    )
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    runtime = VoipStackRuntime(
        transport_config={"sip_port": 5099, "rtp_port": 45000},
        assist_config={"assist_extension": "900"},
        trunk_config={"trunk_enabled": True},
        endpoints=MagicMock(),
        phones=MagicMock(),
        debug_mode=True,
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime
    assert debug_mode(hass)
    assert not media_capture_enabled(hass)
    assert transport_config(hass)["sip_port"] == 5099
    assert assist_config(hass)["assist_extension"] == "900"
    assert trunk_config(hass)["trunk_enabled"] is True

    runtime.media_capture = True
    assert media_capture_enabled(hass)
    assert not {
        "transport_config",
        "assist_config",
        "trunk_config",
        "debug_mode",
        "media_capture",
    }.intersection(hass.data.get(DOMAIN, {}))
