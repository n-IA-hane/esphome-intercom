"""Incoming trunk calls use one native route-window matcher."""

from types import SimpleNamespace

import pytest

from custom_components.voip_stack.automation_routing import has_native_route_window, route_window_snapshot
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
