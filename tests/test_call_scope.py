"""Behavioral tests for logical PBX call scope."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_call_scope():
    full_name = f"{PKG_NAME}.call_scope"
    temporary_names = (
        "homeassistant",
        "homeassistant.core",
        "custom_components",
        PKG_NAME,
        f"{PKG_NAME}.endpoint_lifecycle",
        f"{PKG_NAME}.phone_endpoint",
        full_name,
    )
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in temporary_names}
    try:
        if "homeassistant" not in sys.modules:
            package = types.ModuleType("homeassistant")
            package.__path__ = []
            sys.modules["homeassistant"] = package
        if "homeassistant.core" not in sys.modules:
            core = types.ModuleType("homeassistant.core")
            core.HomeAssistant = type("HomeAssistant", (), {})
            sys.modules["homeassistant.core"] = core
        if "custom_components" not in sys.modules:
            root_package = types.ModuleType("custom_components")
            root_package.__path__ = [str(ROOT / "custom_components")]
            sys.modules["custom_components"] = root_package
        if PKG_NAME not in sys.modules:
            package = types.ModuleType(PKG_NAME)
            package.__path__ = [str(PKG_DIR)]
            sys.modules[PKG_NAME] = package

        lifecycle = types.ModuleType(f"{PKG_NAME}.endpoint_lifecycle")
        lifecycle.call_registry = lambda hass: hass.registry
        sys.modules[lifecycle.__name__] = lifecycle
        phone_endpoint = types.ModuleType(f"{PKG_NAME}.phone_endpoint")
        phone_endpoint.DEFAULT_ENDPOINT_ID = "default"
        sys.modules[phone_endpoint.__name__] = phone_endpoint

        spec = importlib.util.spec_from_file_location(
            full_name,
            PKG_DIR / "call_scope.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {full_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in previous.items():
            if original is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


call_scope = _load_call_scope()


class _Registry:
    def __init__(self) -> None:
        self.sessions: dict[str, SimpleNamespace] = {}
        self.aliases: dict[str, str] = {}
        self.pending_routes: dict[str, dict] = {}

    def resolve_session_id(self, call_id: str) -> str:
        return self.aliases.get(call_id, call_id)


class CallScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _Registry()

    def add(self, call_id: str, **metadata) -> None:
        self.registry.sessions[call_id] = SimpleNamespace(metadata=metadata)

    def test_unscoped_call_does_not_invent_a_master_endpoint(self) -> None:
        self.add("legacy")
        self.assertEqual(
            call_scope.call_endpoint_ids(self.registry, "legacy"),
            frozenset(),
        )

    def test_local_call_is_controllable_by_both_endpoints(self) -> None:
        self.add(
            "local",
            endpoint_id="casa",
            source_endpoint_id="casa",
            dest_endpoint_id="test",
        )
        self.assertEqual(
            call_scope.call_endpoint_ids(self.registry, "local"),
            frozenset({"casa", "test"}),
        )
        self.assertTrue(
            call_scope.call_belongs_to_endpoint(self.registry, "local", "test")
        )

    def test_ring_group_exposes_every_ringing_endpoint(self) -> None:
        self.add(
            "group",
            endpoint_id="casa",
            ring_endpoint_ids=("casa", "test", ""),
        )
        self.assertEqual(
            call_scope.call_endpoint_ids(self.registry, "group"),
            frozenset({"casa", "test"}),
        )

    def test_leg_alias_resolves_to_authoritative_session(self) -> None:
        self.add("session", endpoint_id="test")
        self.registry.aliases["leg"] = "session"
        self.assertEqual(call_scope.call_endpoint_id(self.registry, "leg"), "test")

    def test_single_pending_route_is_scoped_and_must_be_unambiguous(self) -> None:
        self.add("casa-call", endpoint_id="casa")
        self.add("test-call", endpoint_id="test")
        self.registry.pending_routes = {"casa-call": {}, "test-call": {}}
        hass = SimpleNamespace(registry=self.registry)

        self.assertEqual(
            call_scope.single_pending_route_call_id(hass, "casa"),
            "casa-call",
        )
        self.add("casa-call-2", endpoint_id="casa")
        self.registry.pending_routes["casa-call-2"] = {}
        self.assertEqual(call_scope.single_pending_route_call_id(hass, "casa"), "")


if __name__ == "__main__":
    unittest.main()
