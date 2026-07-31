"""Behavioral tests for transport-owned SIP call termination."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "endpoint_termination.py"


class _Registry:
    def __init__(self) -> None:
        self.begin_result = True
        self.begin_calls: list[str] = []
        self.pending_invites: dict[str, object] = {}
        self.sessions: dict[str, object] = {}
        self.preanswered: object = {"reservation": "early"}
        self.active_media: object = {"reservation": "active"}
        self.detach_result = ("", "", None, None, None, False)
        self.finished: list[tuple[str, dict[str, object]]] = []

    def begin_termination(self, call_id: str) -> bool:
        self.begin_calls.append(call_id)
        return self.begin_result

    @staticmethod
    def resolve_session_id(call_id: str) -> str:
        return call_id

    def take_media(
        self,
        _call_id: str,
        *,
        provisional: bool = False,
        default=None,
    ):
        if provisional:
            value = self.preanswered
            self.preanswered = None
            return value
        value = self.active_media
        self.active_media = default
        return value

    def detach_bridge(self, _call_id: str):
        return self.detach_result

    def finish_and_pop(self, call_id: str, **values) -> None:
        self.finished.append((call_id, values))


def _hass(registry: _Registry):
    return SimpleNamespace(
        data={"voip_stack": {}},
        registry=registry,
        routes={},
        released=[],
        cleanups=[],
        events=[],
        softphone_stores={},
    )


@pytest.fixture
def endpoint_termination(monkeypatch):
    package_name = "voip_stack_endpoint_termination_test"
    package = ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    async def cleanup(**values) -> None:
        values["relay"].hass.cleanups.append(values)

    hass_holder = {"hass": None}

    def release(value) -> None:
        if value is not None:
            hass_holder["hass"].released.append(value)

    def project_softphone(hass, state, **values) -> None:
        hass.events.append(("softphone", state, values))

    def project_bridge(hass, state, **values) -> None:
        hass.events.append(("bridge", state, values))

    dependencies = {
        "call_scope": {
            "pending_routes": lambda hass: hass.routes,
        },
        "const": {
            "DOMAIN": "voip_stack",
            "HA_SOFTPHONE_DEVICE_ID": "ha-device",
        },
        "endpoint_lifecycle": {
            "call_registry": lambda hass: hass.registry,
        },
        "fsm": {
            "CallState": SimpleNamespace(
                CANCELLED=SimpleNamespace(value="cancelled"),
                IDLE=SimpleNamespace(value="idle"),
            ),
            "TerminalReason": SimpleNamespace(
                CANCELLED=SimpleNamespace(value="cancelled"),
            ),
        },
        "media_ports": {
            "release_media_reservation": release,
        },
        "phone_endpoint": {
            "DEFAULT_ENDPOINT_ID": "default",
        },
        "session_cleanup": {
            "async_cleanup_sip_runtime": cleanup,
        },
        "websocket_api": {
            "_ha_softphone_store": (
                lambda hass, endpoint_id: hass.softphone_stores.setdefault(
                    endpoint_id,
                    {},
                )
            ),
            "_set_ha_softphone_call_state": project_softphone,
            "_set_sip_bridge_call_state": project_bridge,
        },
    }
    for name, values in dependencies.items():
        dependency = ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.endpoint_termination"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    module.hass_holder = hass_holder
    return module


def test_duplicate_transport_termination_has_no_side_effects(
    endpoint_termination,
) -> None:
    registry = _Registry()
    registry.begin_result = False
    hass = _hass(registry)
    endpoint_termination.hass_holder["hass"] = hass
    handler = endpoint_termination.EndpointTerminationHandler(
        hass,
        lambda _hass: "Home Assistant",
    )

    asyncio.run(handler.handle("call-1"))

    assert registry.begin_calls == ["call-1"]
    assert not registry.finished
    assert not hass.released
    assert not hass.events


def test_bridge_termination_releases_media_before_final_projection(
    endpoint_termination,
) -> None:
    registry = _Registry()
    invite = SimpleNamespace(caller="Kitchen", target="Desk")
    registry.pending_invites["call-1"] = invite
    registry.sessions["call-1"] = SimpleNamespace(
        caller="Kitchen",
        callee="Desk",
        metadata={},
        route_kind="bridge",
    )
    relay = SimpleNamespace(hass=None)
    client = object()
    watcher = object()
    registry.detach_result = (
        "call-1",
        "dest-1",
        relay,
        client,
        watcher,
        False,
    )
    hass = _hass(registry)
    relay.hass = hass
    endpoint_termination.hass_holder["hass"] = hass
    handler = endpoint_termination.EndpointTerminationHandler(
        hass,
        lambda _hass: "Home Assistant",
    )

    async def run() -> None:
        future = asyncio.get_running_loop().create_future()
        hass.routes["call-1"] = {"future": future}
        await handler.handle("call-1", "remote_hangup")
        assert future.result()["action"] == "cancel"

    asyncio.run(run())

    assert hass.released == [
        {"reservation": "early"},
        {"reservation": "active"},
    ]
    assert hass.cleanups == [
        {
            "relay": relay,
            "client": client,
            "watcher": watcher,
            "terminate_client": True,
            "relay_first": False,
        }
    ]
    assert registry.finished == [
        ("call-1", {"reason": "remote_hangup", "state": "idle"})
    ]
    assert hass.events == [
        (
            "bridge",
            "idle",
            {
                "call_id": "call-1",
                "dest_call_id": "dest-1",
                "caller": "Kitchen",
                "callee": "Desk",
                "peer_name": "Desk",
                "target": "Desk",
                "reason": "remote_hangup",
                "terminal_reason": "remote_hangup",
                "origin": "remote",
                "last_sip_event": "BYE",
            },
        )
    ]


def test_pending_softphone_termination_uses_owning_endpoint(
    endpoint_termination,
) -> None:
    registry = _Registry()
    invite = SimpleNamespace(caller="Door", target="HA")
    registry.pending_invites["call-2"] = invite
    registry.sessions["call-2"] = SimpleNamespace(
        caller="Door",
        callee="HA",
        metadata={
            "endpoint_id": "wall-tablet",
            "session_device_id": "browser-1",
        },
        route_kind="answer_ha",
    )
    registry.preanswered = None
    registry.active_media = {}
    hass = _hass(registry)
    hass.softphone_stores["wall-tablet"] = {"call_id": "call-2"}
    endpoint_termination.hass_holder["hass"] = hass
    handler = endpoint_termination.EndpointTerminationHandler(
        hass,
        lambda _hass: "Home Assistant",
    )

    asyncio.run(handler.handle("call-2", "cancelled"))

    assert hass.events == [
        (
            "softphone",
            "cancelled",
            {
                "endpoint_id": "wall-tablet",
                "session_device_id": "browser-1",
                "caller": "Door",
                "callee": "HA",
                "peer_name": "Door",
                "direction": "incoming",
                "call_id": "call-2",
                "reason": "cancelled",
                "origin": "remote",
            },
        )
    ]
    assert registry.finished == [
        ("call-2", {"reason": "cancelled", "state": "cancelled"})
    ]
