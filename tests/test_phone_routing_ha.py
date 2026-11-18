"""Use the real phone registry and dial plan to resolve persisted defaults."""

import logging

import pytest

from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.inbound_routing.targets import resolve_phone_target
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.router import RouteAction, resolve_ha_router


pytestmark = pytest.mark.ha


def resolve(data, *, kind="browser", availability="available", dnd=False,
            active_call_id="", apply_policy=True, second_policy=None,
            call_id="new-call", enabled=True):
    registry = EndpointRegistry()
    first = PhoneEndpoint(
        endpoint_id="first", name="Kitchen", kind=kind,
        availability=availability, dnd=dnd, active_call_id=active_call_id,
        policy=PhonePolicy.from_data(data),
    )
    registry.register(first)
    registry.register(PhoneEndpoint(
        endpoint_id="second", name="Renamed bedroom", kind="browser",
        availability="available", policy=PhonePolicy.from_data(second_policy),
    ))
    entries = [
        RosterEntry(id="Kitchen", name="Kitchen", enabled=enabled,
                    metadata={"endpoint_id": "first", "local_ha": kind == "browser",
                              "endpoint_kind": kind}),
        RosterEntry(id="Renamed bedroom", name="Renamed bedroom",
                    metadata={"endpoint_id": "second", "local_ha": True}),
        RosterEntry(id="Welcome", name="Welcome",
                    metadata={"virtual_endpoint": "automation"}),
    ]
    return resolve_phone_target(
        decision=resolve_ha_router("Kitchen", entries),
        endpoint_registry=registry, roster_entries=entries,
        route_resolver=resolve_ha_router, call_id=call_id,
        apply_policy=apply_policy,
    )


def test_unconditional_forward_follows_stable_identity_after_rename(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.voip_stack.inbound_routing.targets")
    result = resolve({"always": {"target": "second"}})
    assert result.failure is None
    assert result.endpoint.endpoint_id == "second"
    assert result.decision.target == "Renamed bedroom"
    assert result.forwarded_endpoint_ids == ("first",)
    assert "Phone Kitchen redirects to Renamed bedroom: unconditional forwarding policy" in caplog.text
    assert "[call_id=new-call]" in caplog.text
    assert "Automation " not in caplog.text


def test_forwarded_identity_still_limits_later_trunk_legs():
    from types import SimpleNamespace
    from custom_components.voip_stack.trunk_policy import call_external_call_denied

    result = resolve({"always": {"target": "second"}})
    parent = SimpleNamespace(metadata={"policy_endpoint_ids": result.forwarded_endpoint_ids})
    child = SimpleNamespace(metadata={"source_call_id": "parent"})
    runtime = SimpleNamespace(
        transport_config={"external_call_blocked_endpoints": ["first"]},
        sip=SimpleNamespace(get_session={"parent": parent, "child": child}.get),
    )
    assert call_external_call_denied(runtime, "child") == "first"


def test_forwarding_can_select_existing_automation_application():
    result = resolve({"always": {"target": "Welcome"}})
    assert result.failure is None
    assert result.decision.action is RouteAction.AUTOMATION
    assert result.endpoint is None


@pytest.mark.parametrize("changes", [{"dnd": True}, {"active_call_id": "other-call"}])
def test_busy_fallback_before_transport(changes, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.voip_stack.inbound_routing.targets")
    result = resolve({"busy": {"target": "second"}}, **changes)
    assert result.endpoint.endpoint_id == "second"
    cause = "do not disturb" if changes.get("dnd") else "busy"
    assert f"Phone Kitchen redirects to Renamed bedroom: {cause} policy" in caplog.text


def test_same_call_does_not_trigger_own_busy_fallback():
    result = resolve({"busy": {"target": "second"}}, active_call_id="new-call")
    assert result.endpoint.endpoint_id == "first"


def test_browser_offline_is_still_ringable_without_explicit_policy():
    result = resolve({}, availability="offline")
    assert result.failure is None
    assert result.endpoint.endpoint_id == "first"


def test_browser_can_explicitly_forward_when_offline():
    result = resolve({"unavailable": {"target": "second"}}, availability="offline")
    assert result.endpoint.endpoint_id == "second"


def test_explicit_automation_decision_bypasses_phone_defaults():
    result = resolve({"always": {"target": "second"}}, apply_policy=False)
    assert result.endpoint.endpoint_id == "first"


def test_disabled_phone_cannot_be_used_as_forwarding_route():
    result = resolve({"always": {"target": "second"}}, enabled=False)
    assert result.decision.action is RouteAction.REJECT
    assert result.endpoint.endpoint_id == "first"


def test_cycle_terminates_without_creating_transport():
    result = resolve({"always": {"target": "second"}},
                     second_policy={"always": {"target": "first"}})
    assert result.failure.decline_reason == "forward_loop"


def test_hangup_fallback_is_explicit_and_does_not_dial_contact_named_hangup(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.voip_stack.inbound_routing.targets")
    result = resolve({"busy": {"kind": "hangup"}}, dnd=True)
    assert result.failure.decline_reason == "phone_policy"
    assert "Phone Kitchen policy rejects the call: do not disturb" in caplog.text


def test_missing_mailbox_application_never_rings_ordinary_phone():
    result = resolve({"always": {"kind": "voicemail", "target": "second"}})
    assert result.decision.action is RouteAction.REJECT
    assert result.decision.target == "voicemail.second"
    assert result.endpoint is None
