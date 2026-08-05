#!/usr/bin/env python3
"""Privacy and runtime contracts for Home Assistant diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "voip_stack"


def _redact(data, keys):
    if isinstance(data, list):
        return [_redact(item, keys) for item in data]
    if not isinstance(data, Mapping):
        return data
    return {
        key: "**REDACTED**" if key in keys and value not in (None, "") else _redact(value, keys)
        for key, value in data.items()
    }


def _load_module(monkeypatch):
    homeassistant = ModuleType("homeassistant")
    components = ModuleType("homeassistant.components")
    diagnostics = ModuleType("homeassistant.components.diagnostics")
    config_entries = ModuleType("homeassistant.config_entries")
    core = ModuleType("homeassistant.core")
    helpers = ModuleType("homeassistant.helpers")
    device_registry = ModuleType("homeassistant.helpers.device_registry")

    diagnostics.async_redact_data = _redact
    config_entries.ConfigEntry = object
    core.HomeAssistant = object
    device_registry.DeviceEntry = object

    for name, module in {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.diagnostics": diagnostics,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    package_name = "voip_stack_diagnostics_test"
    package = ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    monkeypatch.setitem(sys.modules, package_name, package)
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.diagnostics",
        COMPONENT / "diagnostics.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load diagnostics")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.runtime_data = lambda hass: getattr(hass, "runtime", None)
    module.call_projection = lambda hass: getattr(
        getattr(hass, "runtime", None), "calls", None
    )
    module.sip_endpoint_manager = lambda hass: getattr(
        getattr(hass, "runtime", None), "endpoint", None
    )
    module.sip_trunk = lambda hass: getattr(
        getattr(hass, "runtime", None), "trunk", None
    )
    module.endpoint_directory = lambda hass: getattr(
        getattr(hass, "runtime", None), "endpoints", _Registry([])
    )
    return module


class _Registry:
    def __init__(self, endpoints) -> None:
        self.endpoints = tuple(endpoints)

    def by_device_id(self, device_id):
        return next(
            (endpoint for endpoint in self.endpoints if endpoint.device_id == device_id),
            None,
        )

    def get(self, endpoint_id):
        return next(
            (endpoint for endpoint in self.endpoints if endpoint.endpoint_id == endpoint_id),
            None,
        )


class _CallRegistry:
    def snapshot(self):
        return {
            "resource_counts": {"sessions": 1, "legs": 2},
            "call_ids": ["private-call-id"],
            "pending_call_ids": ["private-pending-id"],
            "media_call_ids": ["private-media-id"],
        }


class _SipEndpoint:
    def snapshot(self):
        return SimpleNamespace(
            udp_ready=True,
            tcp_ready=True,
            pending_transactions=1,
            active_dialogs=1,
            pending_call_ids=("private-pending-id",),
            active_call_ids=("private-call-id",),
            last_sip_event="private-peer-event",
            last_sip_status_code=200,
            last_sip_reason="private-peer-reason",
        )


class _Trunk:
    def snapshot(self):
        return {
            "trunk_enabled": True,
            "trunk_registered": True,
            "trunk_status_code": 200,
            "trunk_transport": "tcp",
            "trunk_expires_at": 1234.5,
            "trunk_server": "private-pbx.example",
            "trunk_last_sip_event": "private-event",
        }


def _endpoint(**changes):
    values = {
        "endpoint_id": "private-endpoint",
        "name": "Private Kitchen",
        "kind": "browser",
        "availability": "available",
        "capabilities": frozenset({"audio", "video"}),
        "device_id": "private-device",
        "entity_ids": frozenset({"sensor.private_phone"}),
        "active_call_id": "private-call-id",
        "dnd": True,
        "offline_policy": "wait",
        "ring_group": "Private Ring Group",
        "conference_group": "Private Conference",
        "conference_ring": True,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _entry():
    raw = {
        "entry_id": "private-entry-id",
        "unique_id": "private-entry-unique-id",
        "title": "Private PBX",
        "domain": "voip_stack",
        "version": 3,
        "minor_version": 1,
        "data": {
            "sip_port": 5060,
            "experimental_sip_video": True,
            "trunk_server": "private-pbx.example",
            "trunk_domain": "private-domain.example",
            "trunk_username": "private-trunk-user",
            "trunk_password": "private-password",
            "phonebook_contacts": [
                {"name": "Private Person", "number": "private-number"}
            ],
        },
        "options": {"assist_pipeline": "private-assist-pipeline"},
        "subentries": [
            {
                "subentry_id": "private-subentry-id",
                "unique_id": "private-subentry-unique-id",
                "title": "Private Kitchen",
                "subentry_type": "phone",
                "data": {
                    "endpoint_id": "private-endpoint",
                    "kind": "browser",
                    "name": "Private Kitchen",
                    "extension": "667",
                    "ring_group": "Private Ring Group",
                    "video_enabled": True,
                },
            }
        ],
    }
    return SimpleNamespace(
        state=SimpleNamespace(value="loaded"),
        as_dict=lambda: raw,
    )


def test_config_entry_diagnostics_are_bounded_and_private(monkeypatch) -> None:
    diagnostics = _load_module(monkeypatch)
    endpoint = _endpoint()
    runtime = SimpleNamespace(
        endpoints=_Registry([endpoint]),
        calls=_CallRegistry(),
        endpoint=_SipEndpoint(),
        trunk=_Trunk(),
        sip=SimpleNamespace(forward_tasks={}, deadlines={}),
        rtp_port_pool={"used": {40000, 40002}},
    )
    hass = SimpleNamespace(
        data={
            "voip_stack": {
                "active_audio_sessions": {"private-call-id": object()},
            }
        },
        runtime=runtime,
    )

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass, _entry())
    )
    serialized = json.dumps(result, sort_keys=True)

    for private_value in (
        "private-entry-id",
        "private-entry-unique-id",
        "Private PBX",
        "private-pbx.example",
        "private-domain.example",
        "private-trunk-user",
        "private-password",
        "Private Person",
        "private-number",
        "private-assist-pipeline",
        "private-subentry-id",
        "private-endpoint",
        "Private Kitchen",
        "667",
        "Private Ring Group",
        "private-call-id",
        "private-pending-id",
        "private-media-id",
        "private-peer-event",
        "private-peer-reason",
    ):
        assert private_value not in serialized

    assert "**REDACTED**" in serialized
    assert result["runtime"]["endpoints"] == {
        "total": 1,
        "active": 1,
        "by_kind": {"browser": 1},
        "by_availability": {"available": 1},
        "by_capability": {"audio": 1, "video": 1},
    }
    assert result["runtime"]["signaling"] == {
        "configured": True,
        "udp_ready": True,
        "tcp_ready": True,
        "pending_transactions": 1,
        "active_dialogs": 1,
        "last_status_code": 200,
    }
    assert result["runtime"]["trunk"]["registered"] is True
    assert result["runtime"]["resources"]["resource_counts"]["sessions"] == 1
    assert "call_ids" not in result["runtime"]["resources"]
    assert "allocated_rtp_ports" not in result["runtime"]["resources"]


def test_config_entry_keeps_the_public_integration_domain(monkeypatch) -> None:
    diagnostics = _load_module(monkeypatch)
    hass = SimpleNamespace(data={"voip_stack": {}})

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass, _entry())
    )

    assert result["config_entry"]["domain"] == "voip_stack"
    assert result["config_entry"]["data"]["trunk_domain"] == "**REDACTED**"


def test_device_diagnostics_expose_behavior_not_identity(monkeypatch) -> None:
    diagnostics = _load_module(monkeypatch)
    endpoint = _endpoint()
    hass = SimpleNamespace(
        data={"voip_stack": {}},
        runtime=SimpleNamespace(
            endpoints=_Registry([endpoint]),
            calls=_CallRegistry(),
            endpoint=None,
            trunk=None,
            sip=None,
            rtp_port_pool={},
        ),
    )
    device = SimpleNamespace(id="private-device", identifiers=frozenset())

    result = asyncio.run(
        diagnostics.async_get_device_diagnostics(hass, _entry(), device)
    )
    serialized = json.dumps(result, sort_keys=True)

    for private_value in (
        "private-device",
        "private-endpoint",
        "Private Kitchen",
        "sensor.private_phone",
        "Private Ring Group",
        "Private Conference",
        "private-call-id",
    ):
        assert private_value not in serialized
    assert result["phone"] == {
        "found": True,
        "kind": "browser",
        "availability": "available",
        "capabilities": ["audio", "video"],
        "dnd": True,
        "active": True,
        "entity_count": 1,
        "ring_group_configured": True,
        "conference_group_configured": True,
        "conference_ring": True,
    }
    assert "call_ids" not in result["resources"]


def test_device_diagnostics_are_safe_when_device_is_not_an_endpoint(monkeypatch) -> None:
    diagnostics = _load_module(monkeypatch)
    hass = SimpleNamespace(
        data={"voip_stack": {}},
        runtime=SimpleNamespace(
            endpoints=_Registry([]),
            calls=None,
            endpoint=None,
            trunk=None,
            sip=None,
            rtp_port_pool={},
        ),
    )
    device = SimpleNamespace(
        id="private-unrelated-device",
        identifiers={("other", "private-identifier")},
    )

    result = asyncio.run(
        diagnostics.async_get_device_diagnostics(hass, _entry(), device)
    )

    assert result["phone"] == {"found": False}
    assert result["resources"]["call_scoped_quiescent"] is True
