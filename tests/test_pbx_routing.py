"""Behavioral tests for transport-independent PBX routing rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "pbx_routing.py"


@pytest.fixture
def routing(monkeypatch):
    package_name = "voip_stack_pbx_routing_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    endpoint_routing = types.ModuleType(f"{package_name}.endpoint_routing")
    endpoint_routing.same_route_name = (
        lambda left, right: str(left or "").strip().casefold()
        == str(right or "").strip().casefold()
    )
    endpoint_routing.peer_for_target = lambda target, peers: next(
        (
            peer
            for peer in peers
            if endpoint_routing.same_route_name(peer.name, target)
        ),
        None,
    )
    monkeypatch.setitem(sys.modules, endpoint_routing.__name__, endpoint_routing)

    phone_endpoint = types.ModuleType(f"{package_name}.phone_endpoint")
    phone_endpoint.EndpointAvailability = SimpleNamespace(
        UNAVAILABLE="unavailable"
    )
    monkeypatch.setitem(sys.modules, phone_endpoint.__name__, phone_endpoint)

    module_name = f"{package_name}.pbx_routing"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_group_members_preserve_order_and_remove_case_duplicates(routing) -> None:
    assert routing.unique_group_members(" Casa, Test, casa, , WS3 ") == [
        "Casa",
        "Test",
        "WS3",
    ]




@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (None, True),
        (SimpleNamespace(dnd=False, availability="available"), True),
        (SimpleNamespace(dnd=False, availability="offline"), True),
        (SimpleNamespace(dnd=True, availability="available"), False),
        (SimpleNamespace(dnd=False, availability="unavailable"), False),
    ],
)
def test_browser_ring_eligibility_is_logical_not_presence_based(
    routing,
    endpoint,
    expected,
) -> None:
    assert routing.browser_endpoint_can_ring(endpoint) is expected


def test_originating_endpoint_is_excluded_by_identity_before_transport(routing) -> None:
    peers = [
        SimpleNamespace(name="Casa", host="10.0.0.5", endpoint_id="casa"),
        SimpleNamespace(name="Test", host="10.0.0.5", endpoint_id="test"),
    ]

    assert routing.caller_matches_group_member("Casa", "", "casa", peers)
    assert routing.caller_matches_group_member(
        "anonymous",
        "10.0.0.5",
        "Test",
        peers,
        source_endpoint_id="test",
    )
    assert not routing.caller_matches_group_member(
        "anonymous",
        "10.0.0.5",
        "Test",
        peers,
    )


def test_unique_legacy_host_can_identify_originating_member(routing) -> None:
    peers = [SimpleNamespace(name="WS3", host="10.0.0.7", endpoint_id="")]

    assert routing.caller_matches_group_member(
        "anonymous",
        "10.0.0.7",
        "WS3",
        peers,
    )
