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
    custom_components.__path__.append(component_root)
    yield
    custom_components.__path__.remove(component_root)


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

    originate = AsyncMock(
        return_value={
            "endpoint_id": "esphome:kitchen",
            "device_id": "device-kitchen",
            "name": "Waveshare P4 Touch",
        }
    )
    monkeypatch.setattr(voip_stack, "_originate_call", originate)

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

    assert response == {
        "success": True,
        "endpoint_id": "esphome:kitchen",
        "endpoint_type": "esphome",
        "device_id": "device-kitchen",
        "name": "Waveshare P4 Touch",
        "destination": "Casa",
    }
    originate.assert_awaited_once()


async def test_default_browser_call_response_does_not_require_an_open_card(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    from custom_components import voip_stack

    originate = AsyncMock(return_value=None)
    monkeypatch.setattr(voip_stack, "_originate_call", originate)

    response = await hass.services.async_call(
        DOMAIN,
        "call",
        {"destination": "427"},
        blocking=True,
        return_response=True,
    )

    assert response == {"success": True}
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


async def test_debug_logging_does_not_enable_private_media_capture(
    hass: HomeAssistant,
) -> None:
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    from custom_components.voip_stack.config import (
        debug_mode,
        media_capture_enabled,
    )

    hass.data[DOMAIN]["debug_mode"] = True
    assert debug_mode(hass)
    assert not media_capture_enabled(hass)

    hass.data[DOMAIN]["media_capture"] = True
    assert media_capture_enabled(hass)
