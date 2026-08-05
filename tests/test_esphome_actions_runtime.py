"""Runtime tests for physical ESPHome phone service selection."""

from __future__ import annotations

import asyncio
from enum import Enum
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


class ServiceValidationError(ValueError):
    def __init__(
        self,
        message: str = "",
        *,
        translation_domain: str = "",
        translation_key: str = "",
        translation_placeholders: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders or {}


class EndpointKind(Enum):
    BROWSER = "browser"
    ESPHOME = "esphome"


def _load_esphome_actions(get_devices: AsyncMock):
    root = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    root.__path__ = [str(ROOT / "custom_components")]
    package = sys.modules.setdefault(PKG_NAME, types.ModuleType(PKG_NAME))
    package.__path__ = [str(PKG_DIR)]

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    dependencies = {
        "homeassistant": homeassistant,
        "homeassistant.core": types.SimpleNamespace(HomeAssistant=object, ServiceCall=object),
        "homeassistant.exceptions": types.SimpleNamespace(
            ServiceValidationError=ServiceValidationError
        ),
        f"{PKG_NAME}.const": types.SimpleNamespace(DOMAIN="voip_stack"),
        f"{PKG_NAME}.device_resolver": types.SimpleNamespace(
            get_resolver=lambda _hass: types.SimpleNamespace(
                resolve_target=AsyncMock(return_value=None)
            )
        ),
        f"{PKG_NAME}.phone_endpoint": types.SimpleNamespace(EndpointKind=EndpointKind),
        f"{PKG_NAME}.runtime_data": types.SimpleNamespace(
            endpoint_directory=lambda hass: hass.data.get("voip_stack", {}).get(
                "endpoint_registry"
            )
        ),
        f"{PKG_NAME}.websocket_api": types.SimpleNamespace(
            _get_voip_devices=get_devices
        ),
    }
    module_name = f"{PKG_NAME}._test_esphome_actions_runtime"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PKG_DIR / "esphome_actions.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, dependencies):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


def _call(device_id: str):
    return types.SimpleNamespace(data={"device_id": device_id})


def _registry(endpoint):
    def resolve(value):
        return endpoint if value == endpoint.device_id else None

    return types.SimpleNamespace(by_device_id=resolve, resolve=resolve)


def test_known_esp_never_falls_through_to_browser_resolver() -> None:
    module = _load_esphome_actions(AsyncMock(return_value=[]))
    endpoint = types.SimpleNamespace(
        device_id="esp-device",
        name="Kitchen",
        kind=EndpointKind.ESPHOME,
    )
    hass = types.SimpleNamespace(
        data={"voip_stack": {"endpoint_registry": _registry(endpoint)}}
    )

    with pytest.raises(ServiceValidationError) as raised:
        asyncio.run(module.async_resolve_command_phone(hass, _call("esp-device")))
    assert raised.value.translation_key == "phone_unavailable"
    assert raised.value.translation_placeholders == {"phone": "Kitchen"}

    endpoint.kind = EndpointKind.BROWSER
    assert asyncio.run(
        module.async_resolve_command_phone(hass, _call("esp-device"))
    ) is None


def test_live_esp_resolves_and_missing_native_action_is_explained() -> None:
    device = {
        "device_id": "esp-device",
        "name": "Kitchen",
        "route_id": "kitchen",
    }
    module = _load_esphome_actions(AsyncMock(return_value=[device]))
    available = set()
    services = types.SimpleNamespace(
        has_service=lambda domain, service: (domain, service) in available,
        async_call=AsyncMock(),
    )
    hass = types.SimpleNamespace(data={"voip_stack": {}}, services=services)

    assert (
        asyncio.run(module.async_resolve_command_phone(hass, _call("esp-device")))
        is device
    )
    with pytest.raises(ServiceValidationError) as raised:
        asyncio.run(module.async_call_action(hass, device, "start_call", {"dest": "667"}))
    assert raised.value.translation_key == "esphome_action_missing"
    assert raised.value.translation_placeholders == {
        "phone": "Kitchen",
        "action": "start_call",
    }

    available.add(("esphome", "kitchen_start_call"))
    asyncio.run(module.async_call_action(hass, device, "start_call", {"dest": "667"}))
    services.async_call.assert_awaited_once_with(
        "esphome",
        "kitchen_start_call",
        {"dest": "667"},
        blocking=True,
        context=None,
    )
