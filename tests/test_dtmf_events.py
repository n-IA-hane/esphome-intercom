"""Behavioural tests for canonical in-dialog DTMF projection."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "dtmf_events.py"


def _load_dtmf_events(monkeypatch):
    homeassistant = types.ModuleType("homeassistant")
    homeassistant_core = types.ModuleType("homeassistant.core")
    homeassistant_core.HomeAssistant = object
    package = types.ModuleType("custom_components")
    package.__path__ = []
    voip_stack = types.ModuleType("custom_components.voip_stack")
    voip_stack.__path__ = [str(MODULE.parent)]
    automation = types.ModuleType("custom_components.voip_stack.automation_routing")
    automation.CALL_EVENT_SCHEMA_VERSION = 1
    automation.canonical_call_origin = lambda origin, _route: origin or "extension"
    const = types.ModuleType("custom_components.voip_stack.const")
    const.DOMAIN = "voip_stack"
    dtmf = types.ModuleType("custom_components.voip_stack.dtmf")
    dtmf.parse_sip_info_digit = lambda _content_type, body: body.decode("ascii").strip()
    lifecycle = types.ModuleType("custom_components.voip_stack.endpoint_lifecycle")
    lifecycle.call_registry = lambda _hass: None
    fsm = types.ModuleType("custom_components.voip_stack.fsm")
    fsm.CallState = SimpleNamespace(IN_CALL=SimpleNamespace(value="in_call"))
    runtime_data = types.ModuleType("custom_components.voip_stack.runtime_data")
    runtime_data.call_runtime_artifacts = lambda hass: hass.artifacts
    websocket = types.ModuleType("custom_components.voip_stack.websocket_api")
    websocket.SIP_DTMF_EVENT = "voip_stack.dtmf"
    for name, module in {
        "homeassistant": homeassistant,
        "homeassistant.core": homeassistant_core,
        "custom_components": package,
        "custom_components.voip_stack": voip_stack,
        "custom_components.voip_stack.automation_routing": automation,
        "custom_components.voip_stack.const": const,
        "custom_components.voip_stack.dtmf": dtmf,
        "custom_components.voip_stack.endpoint_lifecycle": lifecycle,
        "custom_components.voip_stack.fsm": fsm,
        "custom_components.voip_stack.runtime_data": runtime_data,
        "custom_components.voip_stack.websocket_api": websocket,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    module_name = "custom_components.voip_stack.dtmf_events"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, object | None]] = []

    def async_fire(self, event_type, payload, *, context=None) -> None:
        self.events.append((event_type, payload, context))


def test_bridge_publishes_canonical_dtmf_and_translates_info(monkeypatch) -> None:
    dtmf_events = _load_dtmf_events(monkeypatch)
    bus = _Bus()
    hass = SimpleNamespace(bus=bus)
    session = SimpleNamespace(
        metadata={"ingress": "trunk", "direction": "incoming"},
        route_kind="ring_group",
    )
    registry = SimpleNamespace(
        event_context=lambda _call_id: SimpleNamespace(state="in_call"),
        sessions={"source": session},
        resolve_session_id=lambda _call_id: "source",
        event_fields=lambda call_id, state: {
            "session_id": "source",
            "revision": 4,
            "canonical_call_id": call_id,
            "canonical_state": state,
        },
        ha_context=lambda _call_id: "ha-context",
    )
    monkeypatch.setattr(dtmf_events, "call_registry", lambda _hass: registry)

    relayed: list[tuple[str, str]] = []
    relay = SimpleNamespace(
        on_dtmf=None,
        relay_dtmf=lambda side, digit: relayed.append((side, digit)),
    )
    client = SimpleNamespace(on_info_dtmf=None)
    dtmf_events.attach_dtmf_event_bridge(
        hass,
        relay,
        call_id="source-call",
        dest_call_id="dest-call",
        caller="428",
        callee="Casa",
        client=client,
    )

    relay.on_dtmf("left", "6", "rfc4733")
    client.on_info_dtmf("7")

    assert relayed == [("right", "7")]
    assert [event[0] for event in bus.events] == [
        dtmf_events.SIP_DTMF_EVENT,
        dtmf_events.SIP_DTMF_EVENT,
    ]
    left = bus.events[0][1]
    assert left["source"] == "428"
    assert left["source_leg"] == "caller"
    assert left["side"] == "left"
    assert left["transport"] == "rfc4733"
    assert left["ingress"] == "trunk"
    assert left["origin"] == "trunk"
    assert left["route_kind"] == "ring_group"
    assert left["automation_control"] == "ha_anchored"
    assert bus.events[0][2] == "ha-context"

    right = bus.events[1][1]
    assert right["source"] == "Casa"
    assert right["source_leg"] == "callee"
    assert right["side"] == "right"
    assert right["transport"] == "sip_info"


def test_direct_outbound_client_projects_info_from_callee(monkeypatch) -> None:
    dtmf_events = _load_dtmf_events(monkeypatch)
    bus = _Bus()
    hass = SimpleNamespace(bus=bus)
    session = SimpleNamespace(
        metadata={"ingress": "extension", "direction": "outgoing"},
        route_kind="direct",
    )
    registry = SimpleNamespace(
        event_context=lambda _call_id: SimpleNamespace(state="in_call"),
        sessions={"outbound": session},
        resolve_session_id=lambda _call_id: "outbound",
        event_fields=lambda call_id, state: {
            "session_id": "outbound",
            "canonical_call_id": call_id,
            "canonical_state": state,
        },
        ha_context=lambda _call_id: None,
    )
    monkeypatch.setattr(dtmf_events, "call_registry", lambda _hass: registry)
    client = SimpleNamespace(on_info_dtmf=None)

    dtmf_events.attach_direct_client_dtmf_events(
        hass,
        client,
        call_id="outbound-call",
        caller="Casa",
        callee="Codex",
    )
    client.on_info_dtmf("8")

    payload = bus.events[0][1]
    assert payload["digit"] == "8"
    assert payload["source"] == "Codex"
    assert payload["source_leg"] == "callee"
    assert payload["side"] == "right"
    assert payload["transport"] == "sip_info"
    assert payload["direction"] == "outgoing"


class _InfoRequest:
    def __init__(self, call_id: str, digit: str) -> None:
        self.call_id = call_id
        self.body = digit.encode()

    def header(self, name: str) -> str:
        if name == "Call-ID":
            return self.call_id
        if name == "Content-Type":
            return "application/dtmf-relay"
        return ""


def test_sip_info_prefers_the_live_relay_callback(monkeypatch) -> None:
    dtmf_events = _load_dtmf_events(monkeypatch)
    received: list[tuple[str, str, str]] = []
    relay = SimpleNamespace(
        on_dtmf=lambda side, digit, transport: received.append((side, digit, transport))
    )
    registry = SimpleNamespace(
        relays={"call-1": relay},
        softphone_media={},
        sessions={},
        bridge_clients={},
        resolve_session_id=lambda call_id: call_id,
    )
    monkeypatch.setattr(dtmf_events, "call_registry", lambda _hass: registry)
    hass = SimpleNamespace(
        data={"voip_stack": {}},
        artifacts=SimpleNamespace(trunk_info_queues={}),
    )

    asyncio.run(
        dtmf_events.handle_sip_info(
            hass,
            _InfoRequest("call-1", "5"),
            ("192.0.2.10", 5060),
            "udp",
        )
    )

    assert received == [("left", "5", "sip_info")]


def test_sip_info_queues_trunk_digits_without_touching_the_relay(
    monkeypatch,
) -> None:
    dtmf_events = _load_dtmf_events(monkeypatch)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    hass = SimpleNamespace(
        data={"voip_stack": {}},
        artifacts=SimpleNamespace(trunk_info_queues={"call-2": queue}),
    )

    asyncio.run(
        dtmf_events.handle_sip_info(
            hass,
            _InfoRequest("call-2", "9"),
            ("198.51.100.20", 5060),
            "tcp",
        )
    )

    assert queue.get_nowait() == "9"
