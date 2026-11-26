"""No-answer defaults share session timers and yield to accepted call changes."""

import asyncio
from types import SimpleNamespace

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.phone_policy_runtime import apply_phone_policy_state
from custom_components.voip_stack.runtime_data import VoipStackRuntime, registration_data


pytestmark = pytest.mark.ha
TIMER = "deadline:phone_policy"
DECISION = "deadline:phone_policy_decision"


@pytest.fixture
async def ringing(hass):
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from custom_components.voip_stack.config import entry_transport_config
    entry = MockConfigEntry(domain="voip_stack", data=initialize_duration_config({}))
    entry.add_to_hass(hass)
    endpoints = EndpointRegistry()
    endpoints.register(PhoneEndpoint(
        endpoint_id="kitchen", name="Kitchen", kind="browser",
        policy=PhonePolicy.from_data({"ringing_timeout": 1, "no_answer": {"kind": "hangup"}}),
    ))
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    entry.runtime_data = VoipStackRuntime(
        transport_config=entry_transport_config(entry), assist_config={}, trunk_config={}, endpoints=endpoints,
        phones=None, sip=calls,
    )
    ended = asyncio.Event()
    calls.bind_termination_observer(lambda *_args: ended.set())
    session = calls.upsert("call-one", state="ringing", owner="router", callee="Kitchen")
    calls.event_fields(session.call_id, "ringing")
    payload = {"call_id": session.call_id, "generation": session.generation,
               "state": "ringing", "callee": "Kitchen", "scope": "sip"}
    yield SimpleNamespace(calls=calls, session=session, endpoints=endpoints, payload=payload, ended=ended)
    for current in tuple(calls.sessions.values()):
        if current.live:
            await calls.request_termination(current.call_id, TerminationIntent("test_done"))


async def test_real_timer_ends_only_its_own_call(hass, ringing):
    other = ringing.calls.upsert("other", state="ringing", owner="router", callee="Other")
    apply_phone_policy_state(hass, ringing.payload)
    await asyncio.wait_for(ringing.ended.wait(), 2)
    assert ringing.session.termination_intent.reason == "no_answer"
    assert other.live
    assert not ringing.session.resources


async def test_unset_policy_owns_no_timer_and_later_edit_does_not_change_active_call(hass, ringing):
    configured = ringing.endpoints.require("kitchen").policy
    ringing.endpoints.update("kitchen", policy=PhonePolicy())
    apply_phone_policy_state(hass, ringing.payload)
    assert not ringing.session.resources
    ringing.endpoints.update("kitchen", policy=configured)
    apply_phone_policy_state(hass, ringing.payload)
    assert not ringing.session.resources


async def test_repeated_ringing_does_not_restart_deadline(hass, ringing):
    apply_phone_policy_state(hass, ringing.payload)
    first = ringing.calls.resource_for("call-one", TIMER)
    assert first is not None
    apply_phone_policy_state(hass, ringing.payload)
    assert ringing.calls.resource_for("call-one", TIMER) is first


async def test_answer_cancels_deadline_and_late_callback_cannot_hang_up(hass, ringing):
    apply_phone_policy_state(hass, ringing.payload)
    first = ringing.calls.resource_for("call-one", TIMER)
    assert first is not None
    ringing.calls.upsert("call-one", owner="bridge", state="in_call")
    apply_phone_policy_state(hass, {**ringing.payload, "state": "in_call"})
    assert first.closed
    first._expired()
    assert not ringing.session.resources
    assert ringing.session.live


async def test_notification_only_automation_does_not_take_call_control(hass, ringing):
    registration_data(hass).unanswered_trigger_filters["notification"] = {"destination": "Kitchen"}
    apply_phone_policy_state(hass, ringing.payload)
    ringing.calls.resource_for("call-one", TIMER)._expired()
    decision = ringing.calls.resource_for("call-one", DECISION)
    assert decision is not None
    assert ringing.session.live
    decision._expired()
    await asyncio.wait_for(ringing.ended.wait(), 2)
    assert ringing.session.termination_intent.reason == "no_answer"


async def test_accepted_automation_handoff_supersedes_fallback(hass, ringing):
    registration_data(hass).unanswered_trigger_filters["handler"] = {"destination": "Kitchen"}
    apply_phone_policy_state(hass, ringing.payload)
    ringing.calls.resource_for("call-one", TIMER)._expired()
    decision = ringing.calls.resource_for("call-one", DECISION)
    ringing.calls.upsert("call-one", owner="automation", state="ringing")
    decision._expired()
    assert ringing.session.live
    assert not ringing.session.resources


async def test_group_does_not_arm_a_members_personal_fallback(hass, ringing):
    ringing.session.route_kind = "ring_group"
    apply_phone_policy_state(hass, ringing.payload)
    assert not ringing.session.resources


async def test_old_generation_does_not_arm_current_call(hass, ringing):
    apply_phone_policy_state(hass, {**ringing.payload, "generation": ringing.session.generation + 1})
    assert not ringing.session.resources


async def test_native_notification_runs_once_and_fallback_still_executes(hass, ringing):
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components.voip_stack.trigger import VoipCallTrigger

    seen = []

    async def notify(data):
        seen.append(data["call"]["call_id"])

    subject = VoipCallTrigger(hass, TriggerConfig(
        "voip_stack.call_unanswered", options={"destination": "Kitchen"},
    ))
    detach = await subject.async_attach_runner(
        lambda data, *_args: hass.async_create_task(notify(data)),
    )
    try:
        apply_phone_policy_state(hass, ringing.payload)
        ringing.calls.resource_for("call-one", TIMER)._expired()
        await hass.async_block_till_done()
        assert seen == ["call-one"]
        ringing.calls.resource_for("call-one", DECISION)._expired()
        await asyncio.wait_for(ringing.ended.wait(), 2)
        assert ringing.session.termination_intent.reason == "no_answer"
    finally:
        detach()
