"""Behavioral tests for the browser-phone forwarding service boundary."""

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
MODULE = ROOT / "custom_components" / "voip_stack" / "softphone_forward.py"


class _ServiceValidationError(Exception):
    pass


@pytest.fixture
def forwarding(monkeypatch):
    package_name = "voip_stack_softphone_forward_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.ServiceCall = type("ServiceCall", (), {})
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = _ServiceValidationError
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    def resolve_forward_call_id(requested, routes, invites):
        if requested:
            return requested
        candidates = tuple({**routes, **invites})
        if not candidates:
            raise ValueError("No forwardable SIP call is active")
        return candidates[0]

    dependencies = {
        "automation_routing": {
            "resolve_forward_call_id": resolve_forward_call_id
        },
        "call_scope": {
            "call_belongs_to_endpoint": Mock(return_value=True),
            "pending_routes": Mock(),
        },
        "const": {"DOMAIN": "voip_stack"},
        "endpoint_lifecycle": {"call_registry": Mock()},
        "runtime_data": {
            "call_runtime_artifacts": lambda hass: hass.artifacts,
        },
        "route_decisions": {"set_pending_route_decision": Mock()},
        "service_endpoints": {
            "async_require_phone_service_control": AsyncMock(),
            "service_browser_endpoint": Mock(),
        },
    }
    for name, values in dependencies.items():
        dependency = types.ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.softphone_forward"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _runtime(*, ring_group: bool = False):
    route = {
        "future": object(),
        "ring_group_endpoint_ids": ("casa", "test") if ring_group else (),
    }
    registry = SimpleNamespace(
        pending_routes={"call-1": route},
        pending_invites={},
        event_context=Mock(
            return_value=SimpleNamespace(state="ringing", sequence=4)
        ),
    )
    callback = AsyncMock()
    hass = SimpleNamespace(
        data={"voip_stack": {"async_forward_call": callback}},
        artifacts=SimpleNamespace(
            task_for=lambda call_id, name: None,
            forward_call=callback,
        ),
    )
    return hass, registry, route, callback


def _call(hass, **data):
    return SimpleNamespace(hass=hass, data=data, context=object())


def test_pending_route_is_decided_without_starting_second_forward(
    forwarding,
) -> None:
    hass, registry, route, callback = _runtime()
    forwarding.call_registry = Mock(return_value=registry)
    forwarding.pending_routes = Mock(return_value=registry.pending_routes)
    forwarding.service_browser_endpoint = Mock(
        return_value=("casa", SimpleNamespace())
    )
    forwarding.async_require_phone_service_control = AsyncMock()
    forwarding.set_pending_route_decision = Mock()
    call = _call(hass, destination="Test")

    asyncio.run(forwarding.async_forward_browser_call(call))

    forwarding.set_pending_route_decision.assert_called_once()
    decision = forwarding.set_pending_route_decision.call_args.args[1]
    assert decision["call_id"] == "call-1"
    assert decision["action"] == "forward"
    assert decision["expected_state"] == "ringing"
    assert decision["expected_sequence"] == 4
    callback.assert_not_awaited()
    assert "forward_handoff" not in route


def test_ring_group_handoff_finishes_before_canonical_forward(
    forwarding,
) -> None:
    hass, registry, route, callback = _runtime(ring_group=True)
    forwarding.call_registry = Mock(return_value=registry)
    forwarding.pending_routes = Mock(return_value=registry.pending_routes)
    forwarding.service_browser_endpoint = Mock(
        return_value=("casa", SimpleNamespace())
    )
    forwarding.async_require_phone_service_control = AsyncMock()

    def release_group(_hass, _decision):
        route["forward_handoff"].set_result(None)

    forwarding.set_pending_route_decision = Mock(side_effect=release_group)
    call = _call(hass, call_id="call-1", destination="WS3", on_failure="busy")

    asyncio.run(forwarding.async_forward_browser_call(call))

    callback.assert_awaited_once_with(
        call_id="call-1",
        destination="WS3",
        on_failure="busy",
        expected_state="",
        expected_sequence=0,
    )


def test_foreign_call_is_rejected_before_route_mutation(forwarding) -> None:
    hass, registry, _route, callback = _runtime()
    forwarding.call_registry = Mock(return_value=registry)
    forwarding.pending_routes = Mock(return_value=registry.pending_routes)
    forwarding.service_browser_endpoint = Mock(
        return_value=("test", SimpleNamespace())
    )
    forwarding.async_require_phone_service_control = AsyncMock()
    forwarding.call_belongs_to_endpoint = Mock(return_value=False)
    forwarding.set_pending_route_decision = Mock()

    with pytest.raises(_ServiceValidationError, match="No forwardable SIP call"):
        asyncio.run(
            forwarding.async_forward_browser_call(
                _call(hass, destination="WS3")
            )
        )

    forwarding.set_pending_route_decision.assert_not_called()
    callback.assert_not_awaited()
