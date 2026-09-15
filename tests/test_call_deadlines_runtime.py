"""Deadline replacement and stale guards using the real call resource owner."""

import asyncio

import pytest

from custom_components.voip_stack import call_deadlines as deadlines
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

pytestmark = pytest.mark.ha


@pytest.fixture
def deadline_call(hass, monkeypatch):
    registry = SipEndpointRuntime(allow_dark_sessions=True)
    registry.activate()
    session = registry.upsert("call-1", state="ringing", owner="router")
    registry.event_fields(session.call_id, "ringing")
    events = []
    fired = asyncio.Event()

    def emit(_hass, payload, source):
        events.append((payload, source))
        fired.set()

    monkeypatch.setattr(deadlines, "call_registry", lambda _: registry)
    monkeypatch.setattr(deadlines, "_fire_call_event", emit)
    return registry, session, events, fired


async def test_unchanged_call_fires_one_scoped_timeout_event(hass, deadline_call):
    registry, session, events, fired = deadline_call
    sequence = registry.event_context(session.call_id).sequence
    await deadlines.async_set_call_deadline(hass, {
        "call_id": session.call_id, "phase": "ringing", "timeout": 0,
        "expected_state": "ringing", "expected_sequence": sequence,
    })
    async with asyncio.timeout(2):
        await fired.wait()
    assert len(events) == 1
    payload, source = events[0]
    assert source == "sip"
    assert payload["event_type"] == "ringing_timeout_requested"
    assert payload["armed_sequence"] == sequence
    assert not session.resources


async def test_state_or_sequence_change_suppresses_stale_timeout(hass, deadline_call):
    registry, session, events, _fired = deadline_call
    await deadlines.async_set_call_deadline(hass, {
        "call_id": session.call_id, "phase": "ringing", "timeout": 0.01,
    })
    registry.upsert(session.call_id, state="in_call", owner="bridge")
    registry.event_fields(session.call_id, "in_call")
    await asyncio.sleep(0.03)
    assert events == []
    assert not session.resources


async def test_replacing_deadline_cancels_previous_owned_timer(hass, deadline_call):
    registry, session, events, fired = deadline_call
    await deadlines.async_set_call_deadline(hass, {
        "call_id": session.call_id, "phase": "ringing", "timeout": 10,
    })
    previous = registry.resource_for(session.call_id, "deadline")
    assert previous is not None
    await deadlines.async_set_call_deadline(hass, {
        "call_id": session.call_id, "phase": "ringing", "timeout": 0,
    })
    assert previous.closed
    async with asyncio.timeout(2):
        await fired.wait()
    assert len(events) == 1
    assert not session.resources


async def test_call_cleanup_cancels_its_deadline(hass, deadline_call):
    registry, session, events, _fired = deadline_call
    await deadlines.async_set_call_deadline(hass, {
        "call_id": session.call_id, "phase": "ringing", "timeout": 10,
    })
    deadline = registry.resource_for(session.call_id, "deadline")
    result = await registry.request_termination(session.call_id, TerminationIntent.bye("local_hangup"))
    assert result.errors == ()
    assert deadline.closed
    assert events == []
