"""Behavioral tests for ring-group preflight and candidate settlement."""

from __future__ import annotations

from enum import Enum
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "ring_group.py"


class _Disposition(Enum):
    BUSY = "busy"
    DND = "dnd"
    UNAVAILABLE = "unavailable"


class _Availability(Enum):
    AVAILABLE = "available"
    OFFLINE = "offline"
    UNAVAILABLE = "unavailable"


class _Kind(Enum):
    BROWSER = "browser"
    ESPHOME = "esphome"


class _CallState(Enum):
    RINGING = "ringing"


@pytest.fixture
def ring_group(monkeypatch):
    package_name = "voip_stack_ring_group_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    dependencies = {
        "call_projection": {"observe_phone_leg_projection": Mock()},
        "dial_fork": {"DialDisposition": _Disposition},
        "endpoint_lifecycle": {"call_registry": Mock()},
        "fsm": {"CallState": _CallState},
        "outbound_attempts": {"BrowserLeg": object},
        "phone_endpoint": {
            "EndpointAvailability": _Availability,
            "EndpointKind": _Kind,
    }
        }
    for name, values in dependencies.items():
        dependency = types.ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.ring_group"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("browser", "availability", "dnd", "active_call", "expected"),
    [
        (True, _Availability.AVAILABLE, False, "", None),
        (True, _Availability.OFFLINE, False, "", None),
        (True, _Availability.UNAVAILABLE, False, "", _Disposition.UNAVAILABLE),
        (False, _Availability.OFFLINE, False, "", _Disposition.UNAVAILABLE),
        (False, _Availability.AVAILABLE, True, "", _Disposition.DND),
        (False, _Availability.AVAILABLE, False, "other", _Disposition.BUSY),
        (False, _Availability.AVAILABLE, False, "call-1", None),
    ],
)
def test_endpoint_preflight_matrix(
    ring_group,
    browser,
    availability,
    dnd,
    active_call,
    expected,
) -> None:
    endpoint = SimpleNamespace(
        availability=availability,
        dnd=dnd,
        active_call_id=active_call,
    )
    assert (
        ring_group.endpoint_preflight_disposition(
            endpoint,
            call_id="call-1",
            browser=browser,
        )
        is expected
    )


def test_settlement_releases_every_loser_even_if_one_observer_fails(
    ring_group,
) -> None:
    registry = SimpleNamespace(
        get_session=Mock(return_value=SimpleNamespace(generation=1)),
        release_endpoint_claim=Mock(),
    )
    legs = [
        SimpleNamespace(endpoint_id="casa", device_id="device-casa"),
        SimpleNamespace(endpoint_id="test", device_id="device-test"),
        SimpleNamespace(endpoint_id="ws3", device_id="device-ws3"),
    ]

    def publish(_hass, _registry, _session, endpoint_id, *_args, **_kwargs):
        if endpoint_id == "test":
            raise RuntimeError("observer failed")

    ring_group.observe_phone_leg_projection.side_effect = publish
    ring_group.settle_browser_candidates(
        SimpleNamespace(),
        registry,
        legs,
        call_id="call-1",
        caller="Door",
        callee="RG Casa",
        state="cancelled",
        reason="cancelled",
        route_kind="ring",
        keep_endpoint_id="casa",
    )

    assert registry.release_endpoint_claim.call_args_list == [
        (("call-1", "test"),),
        (("call-1", "ws3"),),
    ]
    assert ring_group.observe_phone_leg_projection.call_count == 2


def test_browser_ringing_projection_publishes_each_candidate(
    ring_group,
) -> None:
    session = SimpleNamespace(generation=1)
    registry = SimpleNamespace(upsert=Mock(return_value=session), add_leg=Mock())
    legs = [
        SimpleNamespace(endpoint_id="casa", device_id="device-casa"),
        SimpleNamespace(endpoint_id="test", device_id="device-test"),
    ]
    send_format = SimpleNamespace(
        audio_format=SimpleNamespace(wire_token=lambda: "L16/16000/1"),
        wire_token=lambda: "pt=96:L16/16000/1/20ms",
    )
    recv_format = SimpleNamespace(
        audio_format=SimpleNamespace(wire_token=lambda: "L16/16000/1"),
        wire_token=lambda: "pt=96:L16/16000/1/20ms",
    )
    invite = SimpleNamespace(
        call_id="call-1",
        caller="Door",
        send_format=send_format,
        recv_format=recv_format,
    )

    ring_group.publish_browser_candidates_ringing(
        SimpleNamespace(),
        registry,
        legs,
        invite=invite,
        callee="RG Casa",
        route_kind="ring",
        origin_endpoint_id="hall",
        source_endpoint_id="hall",
        origin_media_client_id="browser-owner",
    )

    registry.upsert.assert_called_once_with(
        "call-1",
        state="ringing",
        owner="ha_softphone",
        caller="Door",
        callee="RG Casa",
        route_kind="ring",
        endpoint_id="hall",
        source_endpoint_id="hall",
        ring_endpoint_ids=("casa", "test"),
        media_client_id="browser-owner",
    )
    assert ring_group.observe_phone_leg_projection.call_count == 2
    published = ring_group.observe_phone_leg_projection.call_args_list[0]
    assert published.args[3] == "casa"
    assert published.args[4] == "ringing"
    assert published.kwargs["selected_tx_rtp_format"].endswith("/20ms")
    assert published.kwargs["sip_status_code"] == 180


def test_origin_projection_is_gated_and_preserves_terminal_metadata(
    ring_group,
) -> None:
    common = {
        "state": "transport_unreachable",
        "endpoint_id": "hall",
        "peer_name": "RG Casa",
        "call_id": "call-1",
        "reason": "protocol_error",
        "origin": "self",
        "route_kind": "ring",
        "last_sip_event": "SIP_RESPONSE",
        "sip_status_code": 500,
    }

    ring_group.publish_ring_group_origin_state(
        SimpleNamespace(),
        enabled=False,
        **common,
    )
    ring_group.observe_phone_leg_projection.assert_not_called()

    ring_group.publish_ring_group_origin_state(
        SimpleNamespace(),
        enabled=True,
        **common,
    )

    ring_group.observe_phone_leg_projection.assert_called_once_with(
        ANY,
        ANY,
        ANY,
        "hall",
        "transport_unreachable",
        leg_id="browser-origin:hall",
        peer_name="RG Casa",
        direction="outgoing",
        reason="protocol_error",
        terminal_reason="protocol_error",
        origin="self",
        last_sip_event="SIP_RESPONSE",
        route_kind="ring",
        sip_status_code=500,
    )


def test_esphome_transport_adoption_is_explicit(ring_group) -> None:
    assert ring_group.endpoint_is_esphome(
        SimpleNamespace(kind=_Kind.ESPHOME)
    )
    assert not ring_group.endpoint_is_esphome(
        SimpleNamespace(kind=_Kind.BROWSER)
    )
