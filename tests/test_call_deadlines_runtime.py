#!/usr/bin/env python3
"""Runtime tests for state-guarded call automation deadlines."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


class ServiceValidationError(ValueError):
    """Minimal Home Assistant service validation error."""


class _Context:
    def __init__(self, state: str, sequence: int) -> None:
        self.state = state
        self.sequence = sequence


class _Registry:
    def __init__(self, state: str = "ringing", sequence: int = 4) -> None:
        self.context = _Context(state, sequence)
        self.sessions = {"call-1": types.SimpleNamespace(state=state)}
        self.pending_invites: dict = {}
        self.preanswered: dict = {}
        self.bridge_clients: dict = {}
        self.softphone_media: dict = {}

    @staticmethod
    def resolve_session_id(call_id: str) -> str:
        return call_id

    def event_context(self, call_id: str) -> _Context | None:
        return self.context if call_id == "call-1" else None


def _load_deadlines(registry: _Registry, events: list[tuple[dict, str]], artifacts):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = ServiceValidationError

    routing = types.ModuleType(f"{PKG_NAME}.automation_routing")
    routing.deadline_is_current = (
        lambda state, sequence, *, armed_state, armed_sequence: (
            state == armed_state and sequence == armed_sequence
        )
    )
    call_registry = types.ModuleType(f"{PKG_NAME}.call_registry")
    call_registry.TERMINAL_STATES = frozenset(
        {"idle", "declined", "busy", "cancelled", "timeout", "error"}
    )
    endpoint_lifecycle = types.ModuleType(f"{PKG_NAME}.endpoint_lifecycle")
    endpoint_lifecycle.call_registry = lambda _hass: registry
    endpoint_lifecycle.create_runtime_task = (
        lambda _hass, coroutine: asyncio.create_task(coroutine)
    )
    websocket_api = types.ModuleType(f"{PKG_NAME}.websocket_api")
    websocket_api._fire_call_event = (
        lambda _hass, payload, source: events.append((payload, source))
    )
    runtime_data = types.ModuleType(f"{PKG_NAME}.runtime_data")
    runtime_data.call_runtime_artifacts = lambda _hass: artifacts

    module_name = f"{PKG_NAME}._test_call_deadlines_runtime"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PKG_DIR / "call_deadlines.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load call_deadlines.py")
    module = importlib.util.module_from_spec(spec)
    dependencies = {
        "homeassistant": homeassistant,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        routing.__name__: routing,
        call_registry.__name__: call_registry,
        endpoint_lifecycle.__name__: endpoint_lifecycle,
        runtime_data.__name__: runtime_data,
        websocket_api.__name__: websocket_api,
    }
    with patch.dict(sys.modules, dependencies):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


class CallDeadlineRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_call_fires_one_scoped_timeout_event(self) -> None:
        registry = _Registry()
        events: list[tuple[dict, str]] = []
        artifacts = types.SimpleNamespace(deadlines={})
        deadlines = _load_deadlines(registry, events, artifacts)
        hass = types.SimpleNamespace(data={})

        await deadlines.async_set_call_deadline(
            hass,
            {
                "call_id": "call-1",
                "phase": "ringing",
                "timeout": 0,
                "expected_state": "ringing",
                "expected_sequence": 4,
            },
        )
        task = artifacts.deadlines["call-1"]
        await task

        self.assertEqual(len(events), 1)
        payload, source = events[0]
        self.assertEqual(source, "sip")
        self.assertEqual(payload["event_type"], "ringing_timeout_requested")
        self.assertEqual(payload["scope"], "automation_deadline")
        self.assertEqual(payload["armed_sequence"], 4)
        self.assertEqual(artifacts.deadlines, {})

    async def test_state_or_sequence_change_suppresses_stale_timeout(self) -> None:
        registry = _Registry()
        events: list[tuple[dict, str]] = []
        artifacts = types.SimpleNamespace(deadlines={})
        deadlines = _load_deadlines(registry, events, artifacts)
        hass = types.SimpleNamespace(data={})

        await deadlines.async_set_call_deadline(
            hass,
            {
                "call_id": "call-1",
                "phase": "ringing",
                "timeout": 0.01,
            },
        )
        task = artifacts.deadlines["call-1"]
        registry.context = _Context("in_call", 5)
        await task

        self.assertEqual(events, [])
        self.assertEqual(artifacts.deadlines, {})

    async def test_replacing_deadline_cancels_previous_owned_task(self) -> None:
        registry = _Registry()
        events: list[tuple[dict, str]] = []
        artifacts = types.SimpleNamespace(deadlines={})
        deadlines = _load_deadlines(registry, events, artifacts)
        hass = types.SimpleNamespace(data={})

        await deadlines.async_set_call_deadline(
            hass,
            {"call_id": "call-1", "phase": "ringing", "timeout": 10},
        )
        previous = artifacts.deadlines["call-1"]
        await deadlines.async_set_call_deadline(
            hass,
            {"call_id": "call-1", "phase": "ringing", "timeout": 0},
        )
        current = artifacts.deadlines["call-1"]
        with self.assertRaises(asyncio.CancelledError):
            await previous
        await current

        self.assertTrue(previous.cancelled())
        self.assertEqual(len(events), 1)

    async def test_unknown_or_wrong_phase_call_is_rejected(self) -> None:
        registry = _Registry(state="in_call")
        events: list[tuple[dict, str]] = []
        artifacts = types.SimpleNamespace(deadlines={})
        deadlines = _load_deadlines(registry, events, artifacts)
        hass = types.SimpleNamespace(data={})

        with self.assertRaisesRegex(ServiceValidationError, "unknown or ended"):
            await deadlines.async_set_call_deadline(
                hass,
                {"call_id": "missing", "phase": "ringing", "timeout": 1},
            )
        with self.assertRaisesRegex(ServiceValidationError, "not in the ringing phase"):
            await deadlines.async_set_call_deadline(
                hass,
                {"call_id": "call-1", "phase": "ringing", "timeout": 1},
            )


if __name__ == "__main__":
    unittest.main()
