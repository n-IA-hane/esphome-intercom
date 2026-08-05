"""Behavioral tests for browser-phone call termination ownership."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "softphone_termination.py"


@pytest.fixture
def termination(monkeypatch):
    package_name = "voip_stack_softphone_termination_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = type(
        "ServiceValidationError", (Exception,), {}
    )
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    call_state = SimpleNamespace(IDLE=SimpleNamespace(value="idle"))
    terminal_reason = SimpleNamespace(
        LOCAL_HANGUP=SimpleNamespace(value="local_hangup"),
        REMOTE_HANGUP=SimpleNamespace(value="remote_hangup"),
    )
    dependencies = {
        "bridge_manager": {"async_terminate_sip_bridge": AsyncMock()},
            "call_scope": {
                "endpoint_call_ids": Mock(return_value=[]),
                "pending_routes": Mock(return_value={}),
                "take_pending_route": Mock(),
            },
            "const": {
                "DOMAIN": "voip_stack",
                "HA_SOFTPHONE_DEVICE_ID": "ha-device",
            },
        "fsm": {
            "CallState": call_state,
            "TerminalReason": terminal_reason,
            "sip_public_state": Mock(return_value="idle"),
        },
            "media_ports": {"release_media_reservation": Mock()},
            "phone_endpoint": {"DEFAULT_ENDPOINT_ID": "default"},
        "runtime_data": {"conference_component": Mock(return_value=None)},
        "route_decisions": {"set_pending_route_decision": Mock()},
        "session_cleanup": {"async_cleanup_sip_runtime": AsyncMock()},
        "sip_runtime": {
            "send_bye": Mock(return_value=True),
            "send_final_response": Mock(return_value=True),
            "sip_servers": Mock(return_value=[]),
        },
        "softphone_commands": {"BrowserCallCommand": object},
        "websocket_api": {
            "_ha_softphone_store": Mock(return_value={}),
            "_set_ha_softphone_call_state": Mock(),
            "_set_sip_bridge_call_state": Mock(),
            "_sip_bridge_store": Mock(return_value={}),
        },
        "local_softphone_bridge": {
            "LocalBridgeError": type("LocalBridgeError", (Exception,), {})
        },
        "local_softphone_runtime": {
            "local_softphone_bridge": Mock(return_value=None)
        },
    }
    for name, values in dependencies.items():
        dependency = types.ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.softphone_termination"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_pending_ring_group_hangup_cancels_only_its_leg(termination) -> None:
    hass = SimpleNamespace(data={})
    future = Mock()
    future.done.return_value = False
    routes = {"call-1": {"future": future}}
    termination.pending_routes = Mock(return_value=routes)
    termination.set_pending_route_decision = Mock()
    command = SimpleNamespace(
        endpoint_id="kitchen",
        device_id="device-kitchen",
        call_id="call-1",
        registry=SimpleNamespace(),
    )

    asyncio.run(termination.async_hangup_browser_call(hass, command))

    termination.set_pending_route_decision.assert_called_once_with(
        hass,
        {
            "call_id": "call-1",
            "action": "cancel",
            "reason": "Request Terminated",
            "decline_reason": "local_hangup",
            "endpoint_id": "kitchen",
        },
    )


def test_bridge_projection_is_scoped_to_matching_softphone(termination) -> None:
    hass = SimpleNamespace(data={})
    termination._ha_softphone_store = Mock(
        return_value={
            "call_id": "source-call",
            "caller": "Alice",
            "callee": "Casa",
            "peer_name": "Alice",
            "direction": "incoming",
        }
    )
    termination._sip_bridge_store = Mock(
        return_value={
            "state": "in_call",
            "call_id": "source-call",
            "dest_call_id": "dest-call",
            "caller": "Alice",
            "callee": "Casa",
            "peer_name": "Alice",
            "direction": "incoming",
            "route_kind": "sip",
        }
    )
    termination.async_terminate_sip_bridge = AsyncMock(
        return_value=(True, "source-call", "dest-call", True, True)
    )
    termination._set_ha_softphone_call_state = Mock()
    termination._set_sip_bridge_call_state = Mock()

    result = asyncio.run(
        termination.async_terminate_sip_bridge_session(
            hass,
            "source-call",
            endpoint_id="default",
            session_device_id="device-casa",
        )
    )

    assert result[:3] == (True, "source-call", "dest-call")
    termination._set_ha_softphone_call_state.assert_called_once()
    assert termination._set_ha_softphone_call_state.call_args.kwargs["call_id"] == (
        "source-call"
    )
    termination._set_sip_bridge_call_state.assert_called_once()
    bridge = termination._set_sip_bridge_call_state.call_args
    assert bridge.args[1] == "idle"
    assert bridge.kwargs["call_id"] == "source-call"
    assert bridge.kwargs["dest_call_id"] == "dest-call"
    assert bridge.kwargs["terminal_reason"] == "local_hangup"
    assert bridge.kwargs["route_kind"] == "sip"


def test_bridge_projection_does_not_overwrite_another_active_call(termination) -> None:
    hass = SimpleNamespace(data={})
    termination._ha_softphone_store = Mock(return_value={})
    termination._sip_bridge_store = Mock(
        return_value={
            "state": "in_call",
            "call_id": "other-source",
            "dest_call_id": "other-dest",
        }
    )
    termination.async_terminate_sip_bridge = AsyncMock(
        return_value=(True, "source-call", "dest-call", True, True)
    )
    termination._set_sip_bridge_call_state = Mock()

    asyncio.run(
        termination.async_terminate_sip_bridge_session(
            hass,
            "source-call",
        )
    )

    termination._set_sip_bridge_call_state.assert_not_called()


def test_remote_bridge_bye_publishes_remote_terminal_reason(termination) -> None:
    hass = SimpleNamespace(data={})
    termination._ha_softphone_store = Mock(return_value={})
    termination._sip_bridge_store = Mock(
        return_value={
            "state": "in_call",
            "call_id": "source-call",
            "dest_call_id": "dest-call",
            "caller": "Alice",
            "callee": "Kitchen",
        }
    )
    termination.async_terminate_sip_bridge = AsyncMock(
        return_value=(True, "source-call", "dest-call", True, True)
    )
    termination._set_sip_bridge_call_state = Mock()

    asyncio.run(
        termination.async_terminate_sip_bridge_session(
            hass,
            "dest-call",
            terminal_reason="remote_hangup",
        )
    )

    bridge = termination._set_sip_bridge_call_state.call_args
    assert bridge.kwargs["terminal_reason"] == "remote_hangup"
    assert bridge.kwargs["origin"] == "remote"
    assert bridge.kwargs["last_sip_event"] == "SIP_BYE"
