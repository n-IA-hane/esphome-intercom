"""Behavioral tests for outbound softphone call routing."""

from __future__ import annotations

import asyncio
from enum import Enum
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "softphone_originate.py"


class _ServiceValidationError(Exception):
    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message)
        self.translation_domain = kwargs.get("translation_domain")
        self.translation_key = kwargs.get("translation_key")
        self.translation_placeholders = kwargs.get("translation_placeholders")


class _RouteAction(Enum):
    ANSWER_HA = "answer_ha"
    TRUNK = "trunk"
    REJECT = "reject"
    GROUP = "group"
    ASSIST = "assist"
    DIRECT = "direct"
    FORWARD = "forward"
    BRIDGE = "bridge"


class _Availability(Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    OFFLINE = "offline"


class _EndpointKind(Enum):
    BROWSER = "browser"
    ESPHOME = "esphome"


class _OfflinePolicy(Enum):
    WAIT = "wait"
    FORWARD = "forward"


@pytest.fixture
def softphone_originate(monkeypatch):
    package_name = "voip_stack_softphone_originate_test"
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
        "audio_format": {"HA_TRUNK_AUDIO_FORMATS": []},
        "authorization": {"async_require_service_admin": AsyncMock()},
        "config": {
            "transport_config": Mock(return_value={}),
            "trunk_config": Mock(return_value={}),
            "trunk_enabled": Mock(return_value=False),
        },
        "const": {
            "CONF_SIP_VIDEO": "sip_video",
            "CONF_TRUNK_AUTH_USERNAME": "trunk_auth_username",
            "CONF_TRUNK_DOMAIN": "trunk_domain",
            "CONF_TRUNK_OUTBOUND_PROXY": "trunk_outbound_proxy",
            "CONF_TRUNK_PASSWORD": "trunk_password",
            "CONF_TRUNK_PORT": "trunk_port",
            "CONF_TRUNK_SERVER": "trunk_server",
            "CONF_TRUNK_TRANSPORT": "trunk_transport",
            "CONF_TRUNK_USERNAME": "trunk_username",
            "CONF_VIDEO_CAMERA_SEND": "video_camera_send",
            "DOMAIN": "voip_stack",
            "HA_PEER_FALLBACK_NAME": "Home Assistant",
            "HA_SOFTPHONE_DEVICE_ID": "ha-device",
        },
        "endpoint_lifecycle": {
            "call_registry": Mock(),
            "create_runtime_task": Mock(),
        },
        "endpoint_termination": {"EndpointTerminationHandler": Mock()},
        "endpoint_session": {
            "TerminationInitiator": SimpleNamespace(
                LOCAL_USER="local_user",
                RUNTIME="runtime",
            )
        },
        "endpoint_registry": {
            "EndpointBusyError": type("EndpointBusyError", (Exception,), {})
        },
        "endpoint_routing": {
            "device_formats": Mock(return_value=[]),
            "roster_entry_formats": Mock(return_value=[]),
            "sip_target_audio_profile": Mock(return_value=([], [])),
        },
        "esphome_actions": {
            "async_call_action": AsyncMock(),
            "async_resolve_source_device": AsyncMock(return_value=None),
            "async_resolve_target_device": AsyncMock(return_value=None),
        },
        "fsm": {
            "CallState": SimpleNamespace(
                IDLE=SimpleNamespace(value="idle"),
                IN_CALL=SimpleNamespace(value="in_call"),
            ),
            "TerminalReason": SimpleNamespace(),
            "sip_public_state": Mock(),
            "sip_terminal_reason": Mock(),
        },
        "media_ports": {
            "allocate_sip_rtp_port": Mock(return_value=40000),
            "reserve_sip_video_media": Mock(),
        },
            "outbound_lifecycle": {
                "HA_SOFTPHONE_ACTIVE_STATES": frozenset({"calling", "in_call"}),
                "attach_outbound_connected_identity_state": Mock(),
                "async_prepare_ha_outbound_call": AsyncMock(),
                "async_track_outbound_sip_client": AsyncMock(),
            },
        "peer_snapshot": {"async_advertise_host": AsyncMock(return_value="127.0.0.1")},
        "phone_endpoint": {
            "DEFAULT_ENDPOINT_ID": "default",
            "EndpointAvailability": _Availability,
            "EndpointKind": _EndpointKind,
            "OfflinePolicy": _OfflinePolicy,
        },
        "router": {
            "RouteAction": _RouteAction,
            "RouteReason": SimpleNamespace(DIRECT_URI="direct_uri"),
            "ha_uri_for": Mock(),
            "resolve_ha_router": Mock(),
        },
            "runtime_data": {
                "call_runtime_artifacts": lambda hass: hass.artifacts,
            "endpoint_directory": lambda hass: hass.data.get("voip_stack", {}).get(
                "endpoint_registry",
                SimpleNamespace(get=lambda _endpoint_id: None),
            ),
            "sip_registrar": lambda hass: hass.data.get("voip_stack", {}).get(
                "sip_registrar"
            ),
            "sip_trunk": lambda hass: hass.data.get("voip_stack", {}).get(
                "sip_trunk"
            ),
            "require_runtime_data": lambda hass: hass.runtime,
        },
        "service_endpoints": {
            "async_require_phone_service_control": AsyncMock(),
            "browser_endpoint_name": Mock(return_value="Casa"),
            "service_browser_endpoint": Mock(return_value=("default", None)),
        },
        "sip_runtime": {
            "enable_reused_tcp_connection": Mock(),
            "uri_transport": Mock(),
        },
        "softphone_commands": {"bind_service_call_controller": Mock()},
        "websocket_api": {
            "_fire_call_event": Mock(),
            "_ha_softphone_store": Mock(return_value={}),
            "_set_ha_softphone_call_state": Mock(),
        },
        "roster": {"parse_roster_json": Mock(return_value=[])},
        "sip": {"parse_sip_uri": Mock()},
        "sip_client": {"SIP_TIMER_B": 32.0, "SipCallClient": object},
        "local_softphone_bridge": {
            "LocalBridgeError": type("LocalBridgeError", (Exception,), {})
        },
        "local_softphone_runtime": {"start_local_softphone_call": Mock()},
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

    module_name = f"{package_name}.softphone_originate"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _call(hass, **data):
    return SimpleNamespace(hass=hass, data=data, context=object())


def test_browser_to_browser_uses_local_bridge_before_network(
    softphone_originate,
) -> None:
    hass = SimpleNamespace(
        data={"voip_stack": {}},
        config=SimpleNamespace(location_name="Casa"),
        states=SimpleNamespace(get=Mock(return_value=None)),
    )
    source_endpoint = SimpleNamespace(
        endpoint_id="casa",
        device_id="device-casa",
        name="Casa",
        availability=_Availability.AVAILABLE,
        supports=Mock(return_value=True),
    )
    destination_endpoint = SimpleNamespace(endpoint_id="test", name="Test")
    route = SimpleNamespace(action=_RouteAction.ANSWER_HA, entry=object())
    softphone_originate._require_phone_service_control = AsyncMock()
    softphone_originate._get_transport_config = Mock(
        return_value={"video_camera_send": True}
    )
    softphone_originate.resolve_ha_router = Mock(return_value=route)
    softphone_originate._async_resolve_browser_destination = AsyncMock(
        return_value=(route, "Test", destination_endpoint)
    )
    softphone_originate._async_prepare_ha_outbound_call = AsyncMock()
    snapshot = SimpleNamespace(call_id="local-1", video_enabled=True)
    local_runtime = sys.modules[
        f"{softphone_originate.__package__}.local_softphone_runtime"
    ]
    local_runtime.start_local_softphone_call = Mock(return_value=snapshot)
    call = _call(
        hass,
        destination="Test",
        media_client_id="browser-casa",
        send_video=True,
    )

    asyncio.run(
        softphone_originate.async_originate_browser_call(
            call,
            endpoint_id="casa",
            browser_endpoint=source_endpoint,
        )
    )

    local_runtime.start_local_softphone_call.assert_called_once_with(
        hass,
        "casa",
        "test",
        request_video=True,
        enable_caller_video_send=True,
        caller_owner_id="browser-casa",
        context=call.context,
    )
    softphone_originate._ha_advertise_host.assert_not_awaited()


def test_offline_browser_phone_remains_a_local_ringing_destination(
    softphone_originate,
) -> None:
    destination = SimpleNamespace(
        endpoint_id="casa",
        kind=_EndpointKind.BROWSER,
        name="Casa",
        availability=_Availability.OFFLINE,
        dnd=False,
        active_call_id="",
    )
    endpoint_registry = SimpleNamespace(get=Mock(return_value=destination))
    hass = SimpleNamespace(data={"voip_stack": {"endpoint_registry": endpoint_registry}})
    route = SimpleNamespace(
        action=_RouteAction.ANSWER_HA,
        entry=SimpleNamespace(metadata={"endpoint_id": "casa"}),
    )

    resolved = asyncio.run(
        softphone_originate._async_resolve_browser_destination(
            hass,
            route=route,
            target="Casa",
            source_endpoint_id="test",
        )
    )

    assert resolved == (route, "Casa", destination)
