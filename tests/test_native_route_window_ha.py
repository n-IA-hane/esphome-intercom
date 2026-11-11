"""Direct and DTMF no-digits ingress use one native route-window matcher."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import trunk_inbound_router as router
from custom_components.voip_stack.automation_routing import has_native_route_window, route_window_snapshot
from custom_components.voip_stack.const import CONF_AUTOMATION_ROUTING_ENABLED
from custom_components.voip_stack.router import RouteAction
from custom_components.voip_stack.runtime_data import registration_data

pytestmark = pytest.mark.ha


def _invite():
    return SimpleNamespace(call_id="native-dtmf", trunk_id="provider-a", caller="Public caller", target="Registration account",
        routing_target="account-contact", caller_number="001111", caller_name="Public caller",
        called_number="0009900042", called_number_raw="+0009900042", called_number_source="header:X-Called",
        inbound_trunk_id="provider-a", outbound_trunk_id="", source_host="192.0.2.10")


@pytest.mark.parametrize("options,match", [
    ({"trunk": "provider-a", "called_number": "0009900042"}, True),
    ({"trunk": "provider-a", "destination": "Fallback"}, True),
    ({"trunk": "another"}, False), ({"caller": "different"}, False),
])
def test_shared_gate_uses_original_identity_and_the_projected_fallback(hass, options, match):
    registration_data(hass).route_trigger_filters[object()] = options
    invite = _invite()
    direct = SimpleNamespace(target="Fallback", entry=None)
    assert has_native_route_window(hass, invite, direct, trunk_invite=True) is match
    assert has_native_route_window(hass, invite, trunk_invite=True, default_destination="Fallback") is match
    snapshot = route_window_snapshot(invite, direct, trunk_invite=True)
    assert snapshot["called_number_raw"] == "+0009900042"
    assert snapshot["called_number_source"] == "header:X-Called"
    assert snapshot["called_extension"] == "account-contact"


@pytest.mark.parametrize("digits,closed,matching,expected", [
    ("", False, True, True), ("123", False, True, False),
    ("", True, True, False), ("", False, False, False),
])
async def test_dtmf_native_trigger_opens_only_the_no_digits_live_window(hass, monkeypatch, digits, closed, matching, expected):
    invite = _invite()
    registration_data(hass).route_trigger_filters[object()] = {
        "trunk": "provider-a" if matching else "another", "called_number": "0009900042"}
    artifacts = SimpleNamespace(trunk_closed=False, trunk_info_queue=None)
    registry = Mock()
    registry.resource_for.return_value = {"final_response_sent": False}
    ports = SimpleNamespace(ports=(40000, 40002), audio_socket=Mock(), release=Mock())
    config = {"trunk_dtmf_enabled": True, "trunk_dtmf_timeout_ms": 1,
              "trunk_inbound_default_target": "Fallback", CONF_AUTOMATION_ROUTING_ENABLED: False}
    window = AsyncMock(return_value={"action": "decline"})
    termination = AsyncMock(return_value=True)
    async def collect(*_args, **_kwargs):
        artifacts.trunk_closed = closed
        return SimpleNamespace(digits=digits, destination="Selected" if digits else "")
    monkeypatch.setattr(router, "call_runtime_artifacts", lambda _: SimpleNamespace(artifacts_for=lambda _: artifacts))
    monkeypatch.setattr(router, "call_registry", lambda _: registry)
    monkeypatch.setattr(router, "trunk_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(router, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(router, "registered_roster_entries", lambda _: [])
    monkeypatch.setattr(router, "roster_from_peers", lambda *_args: [])
    monkeypatch.setattr(router, "dtmf_extension_routes", lambda _: {})
    monkeypatch.setattr(router, "collect_trunk_dtmf", collect)
    monkeypatch.setattr(router, "async_request_inbound_destination", window)
    monkeypatch.setattr(router, "route_inbound_trunk", Mock(return_value=SimpleNamespace(action=RouteAction.REJECT, target="")))
    monkeypatch.setattr(router, "EndpointTerminationHandler", lambda _: SimpleNamespace(terminate=termination))
    runtime = router.TrunkInboundRuntime(hass=hass, config={}, local_ip="192.0.2.1", ha_peer_name="HA",
        route_resolver=Mock(), forward_existing_call=AsyncMock(), defer_invite_to_softphone=Mock(),
        start_local_assist_bridge=AsyncMock())
    await router.async_route_trunk_invite(runtime, invite, bridge_ports=ports)
    assert window.await_count == int(expected)
    if expected:
        actual = window.await_args.args[1]
        assert actual is invite and actual.called_number_raw == "+0009900042"
    if closed:
        termination.assert_not_awaited()
