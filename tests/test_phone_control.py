"""Behavioral tests for transport-independent phone action dispatch."""

from __future__ import annotations

import asyncio
from enum import StrEnum
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "phone_control.py"


class _EndpointKind(StrEnum):
    BROWSER = "browser"
    SIP_ACCOUNT = "sip_account"
    ESPHOME = "esphome"


class _ServiceValidationError(Exception):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args)
        self.translation_key = kwargs.get("translation_key")
        self.translation_placeholders = kwargs.get("translation_placeholders")


class _Endpoints:
    def __init__(self, endpoint=None) -> None:
        self.endpoint = endpoint

    def resolve(self, _selector: str):
        return self.endpoint

    def by_device_id(self, _device_id: str):
        return self.endpoint


@pytest.fixture
def phone_control(monkeypatch):
    package_name = "voip_stack_phone_control_test"
    package = ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = ModuleType("homeassistant.core")
    core.Context = type("Context", (), {})
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.ServiceCall = type("ServiceCall", (), {})
    exceptions = ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = _ServiceValidationError
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    dependencies = {
        "const": {"DOMAIN": "voip_stack"},
        "endpoint_registry": {"EndpointRegistry": object},
        "esphome_actions": {
            "async_call_action": AsyncMock(),
            "async_resolve_source_device": AsyncMock(return_value=None),
            "has_action": Mock(return_value=False),
        },
        "phone_endpoint": {
            "EndpointKind": _EndpointKind,
            "PhoneEndpoint": object,
        },
        "service_endpoints": {
            "async_require_phone_service_control": AsyncMock(),
            "service_browser_endpoint": Mock(),
        },
        "softphone_originate": {
            "async_originate_browser_call": AsyncMock(),
        },
        "websocket_api": {
            "_ha_softphone_state": Mock(return_value={}),
        },
    }
    for name, values in dependencies.items():
        dependency = ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.phone_control"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _call(hass, **data):
    return SimpleNamespace(hass=hass, data=data, context=object())


def test_esphome_originate_uses_native_action_and_canonical_response(
    phone_control,
) -> None:
    hass = SimpleNamespace()
    endpoint = SimpleNamespace(
        endpoint_id="esphome:ws3",
        device_id="device-ws3",
        kind=_EndpointKind.ESPHOME,
    )
    endpoints = _Endpoints(endpoint)
    device = {
        "endpoint_id": "esphome:ws3",
        "device_id": "device-ws3",
        "name": "WS3",
        "entities": {"call": "button.ws3_call"},
    }
    phone_control.async_resolve_source_device = AsyncMock(return_value=device)
    phone_control.has_action = Mock(return_value=True)
    phone_control.async_require_phone_service_control = AsyncMock()
    phone_control.async_call_action = AsyncMock()
    call = _call(hass, device_id="device-ws3", destination="P4")
    controller = phone_control.PhoneAdapterRegistry(hass, endpoints)

    result = asyncio.run(
        controller.originate(
            call,
            phone_control.OriginateRequest(
                destination="P4",
                send_video=True,
                context=call.context,
            ),
        )
    )

    phone_control.async_call_action.assert_awaited_once_with(
        hass,
        device,
        "start_call",
        {"dest": "P4"},
        context=call.context,
    )
    response = result.as_service_response()
    assert response["schema_version"] == 2
    assert response["phone"] == {
        "endpoint_id": "esphome:ws3",
        "device_id": "device-ws3",
        "kind": "esphome",
        "name": "WS3",
    }
    assert response["endpoint_id"] == "esphome:ws3"
    assert response["destination"] == "P4"


def test_sip_account_fails_by_capability_without_browser_resolution(
    phone_control,
) -> None:
    endpoint = SimpleNamespace(
        endpoint_id="sip:zoiper",
        device_id="device-zoiper",
        name="Zoiper",
        kind=_EndpointKind.SIP_ACCOUNT,
    )
    hass = SimpleNamespace()
    controller = phone_control.PhoneAdapterRegistry(hass, _Endpoints(endpoint))
    call = _call(hass, device_id="device-zoiper", destination="P4")
    phone_control.service_browser_endpoint = Mock()

    with pytest.raises(_ServiceValidationError) as raised:
        asyncio.run(
            controller.originate(
                call,
                phone_control.OriginateRequest(destination="P4"),
            )
        )

    assert raised.value.translation_key == "phone_operation_not_supported"
    assert raised.value.translation_placeholders == {
        "phone": "Zoiper",
        "operation": "originate",
    }
    phone_control.service_browser_endpoint.assert_not_called()


def test_browser_originate_uses_same_result_type(phone_control) -> None:
    endpoint = SimpleNamespace(
        endpoint_id="casa",
        device_id="device-casa",
        name="Casa",
        kind=_EndpointKind.BROWSER,
    )
    hass = SimpleNamespace()
    phone_control.service_browser_endpoint = Mock(return_value=("casa", endpoint))
    phone_control.async_originate_browser_call = AsyncMock()
    phone_control._ha_softphone_state = Mock(
        return_value={"call_id": "call-1", "state": "calling"}
    )
    call = _call(hass, destination="P4")
    controller = phone_control.PhoneAdapterRegistry(hass, _Endpoints())

    result = asyncio.run(
        controller.originate(
            call,
            phone_control.OriginateRequest(destination="P4"),
        )
    )

    assert isinstance(result, phone_control.PhoneActionResult)
    assert result.call_id == "call-1"
    assert result.state == "calling"
    phone_control.async_originate_browser_call.assert_awaited_once_with(
        call,
        endpoint_id="casa",
        browser_endpoint=endpoint,
        force_ha_bridge=False,
    )
