"""Behavioral tests for inbound browser softphone routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "inbound_routing"
    / "softphone.py"
)
PACKAGE = "voip_inbound_softphone_test"


def _module(name: str, **values):
    if "." in name:
        parent = name.rsplit(".", 1)[0]
        parent_name = f"{PACKAGE}.{parent}"
        if parent_name not in sys.modules:
            package = types.ModuleType(parent_name)
            package.__path__ = []
            sys.modules[parent_name] = package
    module = types.ModuleType(f"{PACKAGE}.{name}")
    for key, value in values.items():
        setattr(module, key, value)
    sys.modules[module.__name__] = module
    return module


@dataclass(frozen=True)
class _Result:
    status: int
    reason: str
    answer_sdp: str = ""
    to_tag: str = ""
    defer_final: bool = False
    decline_reason: str = ""


class _EndpointKind(StrEnum):
    BROWSER = "browser"
    ESPHOME = "esphome"


class _Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _Endpoint:
    endpoint_id: str
    kind: _EndpointKind
    device_id: str = ""
    availability: _Availability = _Availability.AVAILABLE
    capabilities: frozenset[str] = frozenset({"audio", "video"})
    dnd: bool = False
    active_call_id: str = ""

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


class _BusyError(Exception):
    pass


class _Registry:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.upserts: list[tuple[str, dict]] = []
        self.claims: list[tuple[str, str, dict]] = []
        self.finished: list[tuple[str, dict]] = []
        self.media: dict[str, dict] = {}
        self.legs: list[tuple[str, str, dict]] = []

    def upsert(self, call_id: str, **values) -> None:
        self.upserts.append((call_id, values))

    def claim_endpoint(self, call_id: str, endpoint_id: str, **values) -> None:
        if self.busy:
            raise _BusyError(endpoint_id)
        self.claims.append((call_id, endpoint_id, values))

    def terminate_call(self, call_id: str, **values) -> None:
        self.finished.append((call_id, values))

    def attach_media(self, call_id: str, media: dict) -> None:
        self.media[call_id] = media

    def add_leg(self, call_id: str, leg_id: str, **values) -> None:
        self.legs.append((call_id, leg_id, values))


class _EndpointRegistry:
    def __init__(self, endpoint: _Endpoint | None) -> None:
        self.endpoint = endpoint

    def get(self, endpoint_id: str):
        return self.endpoint if endpoint_id == "default" else None


def _load_module():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE.parents[1])]
    sys.modules[PACKAGE] = package
    inbound = types.ModuleType(f"{PACKAGE}.inbound_routing")
    inbound.__path__ = [str(SOURCE.parent)]
    sys.modules[inbound.__name__] = inbound

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.core", core)

    class CallState(StrEnum):
        RINGING = "ringing"
        CONNECTING = "connecting"
        IN_CALL = "in_call"
        BUSY = "busy"

    class TerminalReason(StrEnum):
        BUSY = "busy"

    class RouteReason(StrEnum):
        TARGET_UNREACHABLE = "target_unreachable"

    state_updates: list[dict] = []
    answer_calls: list[dict] = []
    port_allocations: list[object] = []

    _module("const", HA_SOFTPHONE_DEVICE_ID="ha-device")
    _module("endpoint_registry", EndpointBusyError=_BusyError)
    _module("fsm", CallState=CallState, TerminalReason=TerminalReason)
    _module(
        "media_ports",
        allocate_sip_rtp_port=lambda hass: port_allocations.append(hass) or 40000,
        take_delayed_offer_ports=lambda _hass, _call_id: None,
        reserve_sip_video_media=lambda _hass: (_ for _ in ()).throw(
            AssertionError("video reservation is not expected")
        ),
    )
    _module(
        "phone_endpoint",
        DEFAULT_ENDPOINT_ID="default",
        EndpointAvailability=_Availability,
        EndpointKind=_EndpointKind,
        PhoneEndpoint=_Endpoint,
    )
    _module("router", RouteReason=RouteReason)

    def build_answer_directional(*args, **kwargs):
        answer_calls.append({"args": args, "kwargs": kwargs})
        return "v=0\r\nm=audio 40000 RTP/AVP 96\r\n"

    _module(
        "core.sdp",
        build_answer_directional=build_answer_directional,
        constrained_video_direction=lambda *_args, **_kwargs: "inactive",
    )
    _module("sip_listener", SipInviteResult=_Result)
    _module(
        "websocket_api",
        _set_ha_softphone_call_state=lambda _hass, state, **values: (
            state_updates.append({"state": state, **values})
        ),
    )

    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.inbound_routing.softphone",
        SOURCE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, state_updates, answer_calls, port_allocations


def _invite():
    audio = SimpleNamespace(
        wire_token=lambda: "16000:s16le:1:20",
        audio_format=SimpleNamespace(wire_token=lambda: "16000:s16le:1:20"),
    )
    return SimpleNamespace(
        call_id="call-1",
        caller="ESP",
        send_format=audio,
        recv_format=audio,
        remote_sdp=b"offer",
        video_format=None,
        answer_video_format=None,
        send_video_format=None,
        recv_video_format=None,
        local_audio_direction="sendrecv",
        remote_audio_connection_held=False,
    )


def _decision():
    return SimpleNamespace(
        action=SimpleNamespace(value="answer_ha"),
        sip_uri="sip:ha@example.invalid",
    )


def test_deferred_browser_answer_rings_and_claims_source() -> None:
    module, _states, _answers, _ports = _load_module()
    registry = _Registry()
    source = _Endpoint("esp", _EndpointKind.ESPHOME)
    browser = _Endpoint("browser", _EndpointKind.BROWSER, device_id="browser-device")
    deferred: list[tuple[object, dict]] = []

    result = module.defer_browser_softphone_invite(
        registry=registry,
        invite=_invite(),
        decision=_decision(),
        resolved_callee="HA",
        source_endpoint=source,
        target_endpoint=browser,
        defer_invite=lambda invite, **values: deferred.append((invite, values)),
    )

    assert result.status == 180
    assert result.defer_final is True
    assert registry.claims == [
        ("call-1", "esp", {"role": "source", "adopt_transport": True})
    ]
    assert deferred[0][1]["endpoint_id"] == "browser"
    assert deferred[0][1]["endpoint_device_id"] == "browser-device"


def test_deferred_browser_answer_releases_busy_call() -> None:
    module, _states, _answers, _ports = _load_module()
    registry = _Registry(busy=True)
    source = _Endpoint("esp", _EndpointKind.ESPHOME)
    browser = _Endpoint("browser", _EndpointKind.BROWSER, device_id="browser-device")

    result = module.defer_browser_softphone_invite(
        registry=registry,
        invite=_invite(),
        decision=_decision(),
        resolved_callee="HA",
        source_endpoint=source,
        target_endpoint=browser,
        defer_invite=lambda *_args, **_kwargs: None,
    )

    assert result.status == 486
    assert result.decline_reason == "busy"
    assert registry.finished == [("call-1", {"reason": "busy"})]


def test_immediate_answer_rejects_unavailable_browser() -> None:
    module, _states, _answers, _ports = _load_module()
    browser = _Endpoint(
        "default",
        _EndpointKind.BROWSER,
        availability=_Availability.UNAVAILABLE,
    )

    result = module.answer_inbound_ha_softphone(
        hass=SimpleNamespace(),
        local_ip="192.0.2.10",
        registry=_Registry(),
        invite=_invite(),
        decision=_decision(),
        resolved_callee="HA",
        source_endpoint=None,
        target_endpoint=browser,
        dtmf_format=None,
    )

    assert result.status == 480
    assert result.decline_reason == "target_unreachable"


def test_immediate_audio_answer_owns_media_and_publishes_state() -> None:
    module, states, answers, ports = _load_module()
    registry = _Registry()
    hass = SimpleNamespace()
    browser = _Endpoint(
        "default",
        _EndpointKind.BROWSER,
        device_id="browser-device",
        capabilities=frozenset({"audio"}),
    )

    result = module.answer_inbound_ha_softphone(
        hass=hass,
        local_ip="192.0.2.10",
        registry=registry,
        invite=_invite(),
        decision=_decision(),
        resolved_callee="HA",
        source_endpoint=None,
        target_endpoint=browser,
        dtmf_format=None,
    )

    assert result.status == 200
    assert result.answer_sdp.startswith("v=0")
    assert ports == [hass]
    assert answers[0]["args"][2] == 40000
    assert registry.claims == [
        ("call-1", "default", {"role": "destination"})
    ]
    assert registry.media["call-1"]["local_rtp_port"] == 40000
    assert states[0]["state"] == "in_call"
    assert states[0]["endpoint_id"] == "default"
    assert states[0]["video_status"] == "inactive"
