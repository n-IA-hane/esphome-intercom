"""Behavioral tests for shared browser-media call lifetime lookup."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "media_call_lifetime.py"


class _Registry:
    sessions: dict[str, object] = {}


class _Bus:
    def __init__(self) -> None:
        self.listener = None
        self.removed = False

    def async_listen(self, _event_type, listener):
        self.listener = listener

        def remove() -> None:
            self.removed = True

        return remove


@pytest.fixture
def media_call_lifetime(monkeypatch):
    package_name = "voip_stack_media_call_lifetime_test"
    package = ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    dependencies = {
        "call_registry": {"CallRegistry": _Registry},
        "const": {"DOMAIN": "voip_stack"},
        "phone_endpoint": {"DEFAULT_ENDPOINT_ID": "default"},
        "runtime_data": {
            "call_projection": lambda hass: hass.data.get("voip_stack", {}).get(
                "call_registry"
            )
        },
        "websocket_api": {
            "CALL_EVENT": "voip_stack_call_event",
            "_ha_softphone_store": lambda hass, endpoint_id="default": hass.stores[
                endpoint_id
            ],
        },
    }
    for name, values in dependencies.items():
        dependency = ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.media_call_lifetime"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _hass(*, call_id: str = "call-1", state: str = "in_call"):
    registry = _Registry()
    store = {
        "endpoint_id": "default",
        "call_id": call_id,
        "state": state,
    }
    bus = _Bus()
    hass = SimpleNamespace(
        bus=bus,
        data={"voip_stack": {"call_registry": registry}},
        stores={"default": store},
    )
    return hass, registry, bus


def test_active_media_call_requires_active_state_and_registry(
    media_call_lifetime,
) -> None:
    hass, registry, _bus = _hass()

    active = media_call_lifetime.active_media_call(hass, "default")

    assert active is not None
    assert active.call_id == "call-1"
    assert active.registry is registry
    assert active.store is hass.stores["default"]

    active.store["state"] = "ringing"
    assert media_call_lifetime.active_media_call(hass, "default") is None

    active.store["state"] = "connecting"
    hass.data["voip_stack"]["call_registry"] = object()
    assert media_call_lifetime.active_media_call(hass, "default") is None


def test_listener_ignores_other_calls_and_wakes_on_terminal_state(
    media_call_lifetime,
) -> None:
    hass, _registry, bus = _hass()
    ended, remove = media_call_lifetime.listen_for_media_call_end(
        hass, "call-1", "default"
    )
    assert not ended.is_set()

    bus.listener(SimpleNamespace(data={"call_id": "other", "state": "idle"}))
    assert not ended.is_set()
    bus.listener(SimpleNamespace(data={"call_id": "call-1", "state": "in_call"}))
    assert not ended.is_set()
    bus.listener(SimpleNamespace(data={"call_id": "call-1", "state": "idle"}))

    assert ended.is_set()
    remove()
    assert bus.removed


def test_listener_is_already_set_for_stale_projection(media_call_lifetime) -> None:
    hass, _registry, _bus = _hass(call_id="new-call", state="in_call")

    ended, _remove = media_call_lifetime.listen_for_media_call_end(
        hass, "old-call", "default"
    )

    assert ended.is_set()
