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
    exceptions.ServiceValidationError = type("ServiceValidationError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    call_state = SimpleNamespace(IDLE=SimpleNamespace(value="idle"))
    terminal_reason = SimpleNamespace(
        LOCAL_HANGUP=SimpleNamespace(value="local_hangup"),
        REMOTE_HANGUP=SimpleNamespace(value="remote_hangup"),
    )
    terminate = AsyncMock(return_value=True)

    class EndpointTerminationHandler:
        def __init__(self, _hass) -> None:
            self.terminate = terminate

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
        "endpoint_session": {
            "SipTerminationDisposition": SimpleNamespace(
                BYE="bye",
                FINAL_RESPONSE="final_response",
            ),
            "TerminationInitiator": SimpleNamespace(LOCAL_USER="local_user"),
            "TerminationIntent": type(
                "TerminationIntent",
                (),
                {
                    "__init__": lambda self, reason, **values: (
                        setattr(self, "reason", reason),
                        self.__dict__.update(values),
                    )[-1],
                    **{
                        name: classmethod(
                            lambda cls, reason, *_args, **values: SimpleNamespace(
                                reason=reason, **values
                            )
                        )
                        for name in ("bye", "final_response")
                    },
                },
            ),
        },
        "endpoint_lifecycle": {"call_registry": Mock()},
        "endpoint_termination": {
            "EndpointTerminationHandler": EndpointTerminationHandler,
        },
        "fsm": {
            "CallState": call_state,
            "TerminalReason": terminal_reason,
            "sip_public_state": Mock(return_value="idle"),
        },
        "media_ports": {"release_media_reservation": Mock()},
        "phone_endpoint": {"DEFAULT_ENDPOINT_ID": "default"},
        "runtime_data": {
            "call_runtime_artifacts": lambda hass: hass.artifacts,
            "conference_component": Mock(return_value=None),
        },
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
        "local_softphone_runtime": {"local_softphone_bridge": Mock(return_value=None)},
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
    module.terminate = terminate
    return module


def test_pending_ring_group_hangup_cancels_only_its_leg(termination) -> None:
    hass = SimpleNamespace(
        data={}, artifacts=SimpleNamespace(task_for=lambda call_id, name: None)
    )
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


def test_outbound_hangup_delegates_to_session_cleanup_owner(termination) -> None:
    hass = SimpleNamespace(
        data={}, artifacts=SimpleNamespace(task_for=lambda call_id, name: None)
    )
    client = SimpleNamespace(terminate=AsyncMock(), close=AsyncMock())
    registry = SimpleNamespace(
        sip_clients={"call-1": client},
        pending_invites={},
        softphone_media={},
        preanswered={},
        relays={},
        bridge_clients={},
        sessions={},
        resolve_session_id=lambda call_id: call_id,
    )
    command = SimpleNamespace(
        endpoint_id="kitchen",
        device_id="device-kitchen",
        call_id="call-1",
        registry=registry,
    )
    termination._ha_softphone_store = Mock(
        return_value={"call_id": "call-1", "direction": "outgoing"}
    )
    asyncio.run(termination.async_hangup_browser_call(hass, command))

    client.terminate.assert_not_awaited()
    client.close.assert_not_awaited()
    termination.terminate.assert_awaited_once()
    call_id, intent = termination.terminate.await_args.args
    assert call_id == "call-1"
    assert intent.reason == "local_hangup"


def test_bridge_termination_delegates_projection_to_session_owner(termination) -> None:
    hass = SimpleNamespace(
        data={}, artifacts=SimpleNamespace(task_for=lambda call_id, name: None)
    )
    termination.call_registry = Mock(
        return_value=SimpleNamespace(
            bridge_for=Mock(return_value=("source-call", "dest-call")),
            sip_clients={"dest-call": object()},
        )
    )

    result = asyncio.run(
        termination.async_terminate_sip_bridge_session(
            hass,
            "source-call",
            endpoint_id="default",
            session_device_id="device-casa",
        )
    )

    assert result[:3] == (True, "source-call", "dest-call")
    assert result[3:] == (True, True)
    termination.terminate.assert_awaited_once()
    call_id, intent = termination.terminate.await_args.args
    assert call_id == "source-call"
    assert intent.reason == "local_hangup"
