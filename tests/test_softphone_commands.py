"""Behavioral tests for the shared Home Assistant call-command boundary."""

from __future__ import annotations

import importlib.util
import asyncio
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "softphone_commands.py"


class _ServiceValidationError(Exception):
    pass


@pytest.fixture
def softphone_commands(monkeypatch):
    package_name = "voip_stack_softphone_commands_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.ServiceCall = type("ServiceCall", (), {})
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = _ServiceValidationError
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    dependencies = {
        "call_scope": {
            "call_belongs_to_endpoint": Mock(return_value=True),
            "endpoint_call_ids": Mock(return_value=[]),
            "pending_routes": Mock(return_value={}),
            "single_pending_route_call_id": Mock(return_value=""),
        },
        "const": {"DOMAIN": "voip_stack", "HA_SOFTPHONE_DEVICE_ID": "ha-device"},
        "endpoint_lifecycle": {"call_registry": Mock()},
        "esphome_actions": {
            "async_call_action": AsyncMock(),
            "async_press_device_button": AsyncMock(return_value=True),
            "async_resolve_command_phone": AsyncMock(return_value=None),
            "has_action": Mock(return_value=False),
        },
        "service_endpoints": {
            "async_require_phone_service_control": AsyncMock(),
            "browser_endpoint_name": Mock(return_value="Casa"),
            "service_browser_endpoint": Mock(return_value=("default", None)),
        },
        "fsm": {
            "TerminalReason": SimpleNamespace(
                BUSY=SimpleNamespace(value="busy"),
                CANCELLED=SimpleNamespace(value="cancelled"),
                DECLINED=SimpleNamespace(value="declined"),
            )
        },
        "media_ports": {"release_media_reservation": Mock()},
        "local_softphone_bridge": {
            "LocalBridgeError": type("LocalBridgeError", (Exception,), {})
        },
        "local_softphone_runtime": {
            "local_softphone_bridge": Mock(return_value=None)
        },
            "route_decisions": {"set_pending_route_decision": Mock()},
            "runtime_data": {
                "call_runtime_artifacts": Mock(),
                "conference_component": Mock(return_value=None),
            },
        "sip_runtime": {
            "send_bye": Mock(return_value=True),
            "send_final_response": Mock(return_value=True),
        },
        "websocket_api": {
            "_ha_softphone_store": Mock(return_value={}),
            "_set_ha_softphone_call_state": Mock(),
        },
    }
    for name, values in dependencies.items():
        dependency = types.ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.softphone_commands"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _call(**data):
    return SimpleNamespace(
        hass=SimpleNamespace(data={}, config=SimpleNamespace(location_name="Casa")),
        data=data,
        context=object(),
    )


def test_browser_command_resolves_pending_call_and_authorizes(
    softphone_commands,
) -> None:
    call = _call(endpoint_id="kitchen")
    endpoint = SimpleNamespace(device_id="device-kitchen", name="Cucina")
    registry = SimpleNamespace()
    authorize = AsyncMock()
    softphone_commands.service_browser_endpoint = Mock(
        return_value=("kitchen", endpoint)
    )
    softphone_commands.async_require_phone_service_control = authorize
    softphone_commands.single_pending_route_call_id = Mock(return_value="call-1")
    softphone_commands.call_registry = Mock(return_value=registry)
    softphone_commands.call_belongs_to_endpoint = Mock(return_value=True)
    softphone_commands.browser_endpoint_name = Mock(return_value="Cucina")

    result = asyncio.run(
        softphone_commands.async_resolve_browser_call_command(call.hass, call)
    )

    assert result.endpoint_id == "kitchen"
    assert result.call_id == "call-1"
    assert result.endpoint_name == "Cucina"
    assert result.device_id == "device-kitchen"
    assert result.registry is registry
    authorize.assert_awaited_once_with(call.hass, call, endpoint=endpoint)


def test_browser_command_rejects_foreign_call(softphone_commands) -> None:
    call = _call(endpoint_id="kitchen", call_id="foreign")
    endpoint = SimpleNamespace(device_id="device-kitchen", name="Cucina")
    softphone_commands.service_browser_endpoint = Mock(
        return_value=("kitchen", endpoint)
    )
    softphone_commands.call_registry = Mock(return_value=SimpleNamespace())
    softphone_commands.call_belongs_to_endpoint = Mock(return_value=False)

    with pytest.raises(_ServiceValidationError, match="another phone endpoint"):
        asyncio.run(
            softphone_commands.async_resolve_browser_call_command(call.hass, call)
        )


def test_bind_controller_converts_registry_conflict(softphone_commands) -> None:
    registry = Mock()
    registry.bind_controller.side_effect = ValueError("stale generation")
    call = _call()
    with pytest.raises(_ServiceValidationError, match="stale generation"):
        softphone_commands.bind_service_call_controller(
            registry,
            "call-1",
            call,
            endpoint_id="kitchen",
        )


def test_ring_group_decline_is_a_leg_decision(softphone_commands) -> None:
    call = _call(status=486, reason="Busy Here")
    registry = SimpleNamespace()
    command = softphone_commands.BrowserCallCommand(
        endpoint_id="kitchen",
        endpoint=None,
        endpoint_name="Cucina",
        device_id="device-kitchen",
        call_id="call-1",
        registry=registry,
    )
    route = {"call-1": {"future": object()}}
    softphone_commands.pending_routes = Mock(return_value=route)
    softphone_commands.set_pending_route_decision = Mock()

    asyncio.run(
        softphone_commands.async_decline_browser_call(call.hass, call, command)
    )

    softphone_commands.set_pending_route_decision.assert_called_once_with(
        call.hass,
        {
            "call_id": "call-1",
            "action": "busy",
            "status": 486,
            "reason": "Busy Here",
            "decline_reason": "busy",
            "endpoint_id": "kitchen",
        },
    )
