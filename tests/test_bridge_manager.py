"""Behavioral tests for B2BUA bridge termination ownership."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "bridge_manager.py"


class _Registry:
    def __init__(self) -> None:
        self.bridge = ("source", "destination")
        self.sip_clients = {"destination": object()}
        self.finished: list[tuple[str, dict[str, object]]] = []

    def bridge_for(self, _call_id: str) -> tuple[str, str]:
        return self.bridge

    async def terminate_call_wait(self, call_id: str, **values) -> None:
        self.finished.append((call_id, values))


@pytest.fixture
def bridge_manager(monkeypatch):
    package_name = "voip_stack_bridge_manager_test"
    package = ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    endpoint_lifecycle = ModuleType(f"{package_name}.endpoint_lifecycle")
    endpoint_lifecycle.call_registry = lambda hass: hass.registry
    monkeypatch.setitem(sys.modules, endpoint_lifecycle.__name__, endpoint_lifecycle)

    fsm = ModuleType(f"{package_name}.fsm")
    fsm.TerminalReason = SimpleNamespace(
        LOCAL_HANGUP=SimpleNamespace(value="local_hangup"),
        REMOTE_HANGUP=SimpleNamespace(value="remote_hangup"),
    )
    fsm.sip_public_state = lambda value: value
    fsm.sip_terminal_reason = lambda value, _state: value
    monkeypatch.setitem(sys.modules, fsm.__name__, fsm)

    module_name = f"{package_name}.bridge_manager"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_bridge_hangup_uses_session_cleanup_barrier(bridge_manager) -> None:
    registry = _Registry()
    hass = SimpleNamespace(registry=registry)
    bye_calls: list[str] = []

    def send_bye(call_id: str) -> bool:
        bye_calls.append(call_id)
        return True

    result = asyncio.run(
        bridge_manager.async_terminate_sip_bridge(
            hass,
            "destination",
            terminal_reason="remote_hangup",
            send_bye=send_bye,
        )
    )

    assert result == (True, "source", "destination", True, True)
    assert bye_calls == ["source"]
    assert registry.finished == [
        ("source", {"reason": "remote_hangup"}),
    ]


def test_unknown_bridge_has_no_hangup_side_effects(bridge_manager) -> None:
    registry = _Registry()
    registry.bridge = ("", "")
    hass = SimpleNamespace(registry=registry)
    bye_calls: list[str] = []

    def send_bye(call_id: str) -> bool:
        bye_calls.append(call_id)
        return True

    result = asyncio.run(
        bridge_manager.async_terminate_sip_bridge(
            hass,
            "missing",
            send_bye=send_bye,
        )
    )

    assert result == (False, "", "", False, False)
    assert bye_calls == []
    assert registry.finished == []
