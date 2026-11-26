"""Trunk routing preserves configured choices and the selected route's outcome."""

import pytest

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.router import CallContext, RouteAction, RouteReason, route_inbound_trunk
from custom_components.voip_stack.trunk_routing import trunk_default_target

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("settings,expected", [
    ({}, "HA"),
    ({"trunk_inbound_default_target": ""}, ""),
    ({"trunk_inbound_default_target": "   "}, ""),
    ({"trunk_inbound_default_target": "Desk"}, "Desk"),
])
def test_normalization_keeps_initial_destination_and_explicit_empty_choice(settings, expected):
    normalized = normalized_trunk_config(settings)
    assert normalized["trunk_inbound_default_target"] == expected
    assert trunk_default_target(normalized) == expected
    assert trunk_default_target(settings) == (expected if settings else "")


@pytest.mark.parametrize("enabled,status,reason", [
    (False, 403, RouteReason.TARGET_DISABLED),
    (True, 480, RouteReason.TARGET_UNREACHABLE),
])
def test_incoming_account_route_retains_actual_failure(enabled, status, reason):
    entry = RosterEntry("desk", enabled=enabled,
                        metadata={"endpoint_kind": "sip_account", "endpoint_id": "desk"})
    decision = route_inbound_trunk(CallContext(call_id="incoming", direction="inbound", origin="trunk",
                                              route_hint="desk"), [entry])
    assert decision.action is RouteAction.REJECT
    assert decision.status == status
    assert decision.reason is reason
    assert decision.entry is entry
