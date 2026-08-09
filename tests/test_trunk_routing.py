"""Behavioral tests for the bounded inbound trunk automation decision."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "trunk_routing.py"


@pytest.fixture
def trunk_routing(monkeypatch):
    package_name = "voip_stack_trunk_routing_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    routes: dict = {}
    publish_bridge_projection = Mock()
    dependencies = {
        "call_scope": {
            "set_pending_route": Mock(
                side_effect=lambda _hass, call_id, route: routes.__setitem__(
                    call_id, route
                )
            ),
            "take_pending_route": Mock(
                side_effect=lambda _hass, call_id: routes.pop(call_id, None)
            ),
        },
        "const": {"CONF_TRUNK_INBOUND_DEFAULT_TARGET": "fallback"},
        "call_projection": {"publish_bridge_projection": publish_bridge_projection},
        "fsm": {
            "CallState": SimpleNamespace(
                CONNECTING=SimpleNamespace(value="connecting")
            )
        },
    }
    for name, values in dependencies.items():
        dependency = types.ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.trunk_routing"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    module.test_routes = routes
    module.test_publish_bridge_projection = publish_bridge_projection
    return module


def _invite():
    return SimpleNamespace(
        call_id="call-1",
        caller="Alice",
        source_host="10.0.0.2",
    )


def _registry_and_session():
    session = SimpleNamespace(generation=1, state="connecting")
    registry = SimpleNamespace(transition=Mock(return_value=session))
    return registry, session


def test_explicit_automation_decision_wins_and_future_is_removed(
    trunk_routing,
) -> None:
    def answer_route(*_args, **_kwargs):
        trunk_routing.test_routes["call-1"]["future"].set_result(
            {"action": "forward", "destination": "RG Casa"}
        )

    trunk_routing.test_publish_bridge_projection.side_effect = answer_route
    registry, session = _registry_and_session()

    result = asyncio.run(
        trunk_routing.async_request_inbound_destination(
            SimpleNamespace(),
            _invite(),
            registry=registry,
            session=session,
            trunk_config={"fallback": "Casa"},
            timeout=1.0,
        )
    )

    assert result == {"action": "forward", "destination": "RG Casa"}
    assert trunk_routing.test_routes == {}
    state = trunk_routing.test_publish_bridge_projection.call_args
    assert state.args[1] is session
    assert state.kwargs["fallback_destination"] == "Casa"
    assert state.kwargs["ingress"] == "trunk"


def test_default_or_timeout_preserves_configured_fallback(trunk_routing) -> None:
    def select_default(*_args, **_kwargs):
        trunk_routing.test_routes["call-1"]["future"].set_result(
            {"action": "default", "destination": "ignored"}
        )

    trunk_routing.test_publish_bridge_projection.side_effect = select_default
    registry, session = _registry_and_session()
    result = asyncio.run(
        trunk_routing.async_request_inbound_destination(
            SimpleNamespace(),
            _invite(),
            registry=registry,
            session=session,
            trunk_config={"fallback": "  Test  "},
            timeout=1.0,
        )
    )
    assert result == {}
    assert trunk_routing.trunk_default_target({"fallback": "  Test  "}) == "Test"
    assert trunk_routing.trunk_default_target({}) == "HA"
    assert trunk_routing.test_routes == {}


def test_timeout_always_removes_pending_route(trunk_routing) -> None:
    registry, session = _registry_and_session()
    result = asyncio.run(
        trunk_routing.async_request_inbound_destination(
            SimpleNamespace(),
            _invite(),
            registry=registry,
            session=session,
            trunk_config={},
            timeout=0.001,
        )
    )

    assert result == {}
    assert trunk_routing.test_routes == {}
