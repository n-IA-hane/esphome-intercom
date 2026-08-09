"""Behavioral tests for inbound browser answer routing."""

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
MODULE = ROOT / "custom_components" / "voip_stack" / "softphone_answer.py"


class _ServiceValidationError(Exception):
    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message)
        self.translation_domain = kwargs.get("translation_domain")
        self.translation_key = kwargs.get("translation_key")
        self.translation_placeholders = kwargs.get("translation_placeholders")


@pytest.fixture
def softphone_answer(monkeypatch):
    package_name = "voip_stack_softphone_answer_test"
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
        "call_projection": {},
        "call_scope": {
            "endpoint_call_ids": Mock(return_value=[]),
            "pending_routes": Mock(return_value={}),
        },
        "config": {"transport_config": Mock(return_value={"video_camera_send": True})},
        "const": {"CONF_VIDEO_CAMERA_SEND": "video_camera_send", "DOMAIN": "voip_stack"},
        "fsm": {
            "CallState": SimpleNamespace(
                IN_CALL=SimpleNamespace(value="in_call"),
                CANCELLED=SimpleNamespace(value="cancelled"),
            ),
            "TerminalReason": SimpleNamespace(
                PROTOCOL_ERROR=SimpleNamespace(value="protocol_error")
            ),
        },
        "endpoint_session": {
            "TerminationIntent": type(
                "TerminationIntent",
                (),
                {
                    "__init__": lambda self, reason: setattr(self, "reason", reason),
                    "bye": classmethod(lambda cls, reason: cls(reason)),
                },
            ),
        },
        "endpoint_termination": {
            "EndpointTerminationHandler": lambda _hass: SimpleNamespace(
                terminate=AsyncMock(return_value=True)
            )
        },
        "inbound_answer": {"AnswerTransaction": object},
        "media_ports": {
            "allocate_sip_rtp_port": Mock(return_value=40000),
            "release_media_reservation": Mock(),
            "reserve_sip_video_media": Mock(),
        },
        "peer_snapshot": {"async_advertise_host": AsyncMock(return_value="127.0.0.1")},
        "route_decisions": {"set_pending_route_decision": Mock()},
        "runtime_data": {
            "call_runtime_artifacts": lambda hass: hass.artifacts,
            "conference_component": Mock(return_value=None),
            "sip_endpoint_runtime": Mock(return_value=None),
        },
        "sip_runtime": {
            "send_bye": Mock(return_value=True),
            "send_final_response": Mock(return_value=True),
        },
        "softphone_commands": {
            "BrowserCallCommand": object,
            "bind_service_call_controller": Mock(),
        },
        "video_rtp": {"RtpSenderState": object},
        "websocket_api": {"_set_ha_softphone_call_state": Mock()},
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

    call_projection = sys.modules[f"{package_name}.call_projection"]

    def publish_phone_projection(hass, session, endpoint_id, **details) -> bool:
        sys.modules[f"{package_name}.websocket_api"]._set_ha_softphone_call_state(
            hass,
            session.state,
            endpoint_id=endpoint_id,
            caller=session.caller,
            callee=session.callee,
            call_id=session.call_id,
            **details,
        )
        return True

    call_projection.publish_phone_projection = publish_phone_projection

    module_name = f"{package_name}.softphone_answer"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _command():
    registry = SimpleNamespace(
        sessions={},
        resolve_session_id=lambda call_id: call_id,
    )
    return SimpleNamespace(
        endpoint_id="kitchen",
        endpoint=SimpleNamespace(supports=lambda capability: capability == "video"),
        endpoint_name="Cucina",
        device_id="device-kitchen",
        call_id="call-1",
        registry=registry,
    )


def test_ring_group_answer_is_submitted_to_fork_controller(softphone_answer) -> None:
    hass = SimpleNamespace(
        data={},
        artifacts=SimpleNamespace(
            task_for=lambda call_id, name: None,
            artifacts_for=lambda call_id: SimpleNamespace(
                forward_claim=False, answer_commit=False
            ),
        ),
    )
    call = SimpleNamespace(
        data={"media_client_id": "browser-1", "send_video": True},
        context=object(),
    )
    routes = {"call-1": {"future": object()}}
    softphone_answer.pending_routes = Mock(return_value=routes)
    softphone_answer.set_pending_route_decision = Mock()

    asyncio.run(
        softphone_answer.async_answer_browser_call(hass, call, _command())
    )

    softphone_answer.set_pending_route_decision.assert_called_once_with(
        hass,
        {
            "call_id": "call-1",
            "action": "answer_ha",
            "endpoint_id": "kitchen",
            "media_client_id": "browser-1",
            "send_video": True,
        },
    )


def test_generic_forward_owner_rejects_direct_answer(softphone_answer) -> None:
    hass = SimpleNamespace(
        data={"voip_stack": {}},
        artifacts=SimpleNamespace(
            task_for=lambda call_id, name: None,
            artifacts_for=lambda call_id: SimpleNamespace(
                forward_claim=True, answer_commit=False
            ),
        ),
    )
    call = SimpleNamespace(data={}, context=object())
    softphone_answer.pending_routes = Mock(return_value={})

    with pytest.raises(_ServiceValidationError, match="being forwarded"):
        asyncio.run(
            softphone_answer.async_answer_browser_call(hass, call, _command())
        )
