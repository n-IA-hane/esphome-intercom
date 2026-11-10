"""HA and SIP consumers read the same phone dialogs and mailbox state."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.helpers.trigger import TriggerConfig
from homeassistant.helpers.condition import ConditionConfig
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.runtime_data import VoipStackRuntime
from custom_components.voip_stack.phone_state import phone_states, phone_state_observer
from custom_components.voip_stack.trigger import PhoneStateTrigger, CALL_EVENT
from custom_components.voip_stack.condition import (
    PhoneStateCondition,
    HasRingingCallCondition,
)
from custom_components.voip_stack.automation_context import current_execution
from custom_components.voip_stack.automation_routing import CALL_EVENT_SCHEMA_VERSION

pytestmark = pytest.mark.ha


@pytest.fixture
async def phones(hass):
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    endpoints = EndpointRegistry()
    for identity, extension in (("kitchen", "101"), ("office", "102")):
        endpoints.register(
            PhoneEndpoint(
                endpoint_id=identity,
                name=identity.title(),
                kind="browser",
                device_id=identity + "-device",
                extension=extension,
                availability="available",
            )
        )
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    calls.bind_endpoint_registry(endpoints)
    entry.runtime_data = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=endpoints,
        phones=None,
        sip=calls,
    )
    yield SimpleNamespace(calls=calls, endpoints=endpoints, runtime=entry.runtime_data)
    await calls.shutdown()


async def test_phone_state_service_returns_same_calls_as_conditions(hass, phones):
    from custom_components.voip_stack import _async_register_services
    from custom_components.voip_stack.dialog_info import snapshot

    await _async_register_services(hass)
    session = phones.calls.upsert(
        "one",
        state="ringing",
        owner="router",
        caller="Office",
        callee="Kitchen",
        source_endpoint_id="office",
        dest_endpoint_id="kitchen",
    )
    phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
    response = await hass.services.async_call(
        "voip_stack",
        "get_phone_state",
        {"device_id": "kitchen-device"},
        blocking=True,
        return_response=True,
    )
    assert (
        response["state"] == "ringing" and response["calls"][0]["peer_name"] == "Office"
    )
    assert response["calls"][0]["call_id"] == session.call_id
    assert response["calls"][0]["generation"] == session.generation
    assert snapshot(phones.runtime, "kitchen")[0].peer_user == "102"
    for kind, options in (
        (PhoneStateCondition, {"phone": "kitchen-device", "state": "ringing"}),
        (HasRingingCallCondition, {"phone": "kitchen-device"}),
    ):
        condition = kind(hass, ConditionConfig(options=options))
        assert condition._async_check()
    response["calls"].clear()
    assert phone_states(hass)["kitchen"]["calls"]


async def test_state_trigger_keeps_exact_call_context_and_suppresses_duplicate_state(
    hass, phones
):
    seen, tasks = [], []
    subject = PhoneStateTrigger(
        hass,
        TriggerConfig(
            "voip_stack.phone_state_changed",
            options={"phone": "kitchen-device", "to": "ringing"},
        ),
    )

    def runner(payload, description, context):
        async def action():
            await asyncio.sleep(0)
            execution = current_execution.get()
            seen.append((execution.token, payload["phone"]["state"]))

        task = asyncio.create_task(action())
        tasks.append(task)
        return task

    detach = await subject.async_attach_runner(runner)
    try:
        session = phones.calls.upsert(
            "one",
            state="ringing",
            owner="router",
            caller="Office",
            callee="Kitchen",
            source_endpoint_id="office",
            dest_endpoint_id="kitchen",
        )
        phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
        await hass.async_block_till_done()
        hass.bus.async_fire(
            CALL_EVENT, {"call_id": session.call_id, "generation": session.generation}
        )
        await hass.async_block_till_done()
        await asyncio.gather(*tasks)
        assert seen == [(session.token, "ringing")]
        assert current_execution.get() is None
    finally:
        detach()
    assert not phone_state_observer(hass).listeners


async def test_one_observer_fans_out_and_retains_identity_after_rename(
    hass, phones, monkeypatch
):
    from custom_components.voip_stack import phone_state
    from custom_components.voip_stack.call_projection import publish_bridge_projection

    scans = []
    original = phone_state.snapshot_all

    def scan(runtime):
        scans.append(True)
        return original(runtime)

    monkeypatch.setattr(phone_state, "snapshot_all", scan)
    observer = phone_state_observer(hass)
    changes, dialogs, activity = [], [], []
    detach = [
        observer.subscribe(lambda payload, _: changes.append(payload)),
        observer.subscribe(dialogs.append, kind="dialog"),
        observer.subscribe(activity.append, kind="activity"),
    ]
    assert len(scans) == 1
    try:
        session = phones.calls.upsert(
            "one",
            state="ringing",
            owner="router",
            caller="Office",
            callee="Kitchen",
            source_endpoint_id="office",
            dest_endpoint_id="kitchen",
        )
        phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
        publish_bridge_projection(hass, session)
        await hass.async_block_till_done()
        scans.clear()
        phones.endpoints.update("kitchen", name="Dining room")
        await hass.async_block_till_done()
        assert len(scans) == 0
        assert observer.previous["kitchen"]["name"] == "Dining room"
        assert observer.previous["kitchen"]["calls"][0]["call_id"] == session.call_id
        assert changes[-1]["endpoint_id"] == "kitchen"
        assert dialogs and activity
        scans.clear()
        hass.bus.async_fire(
            CALL_EVENT, {"call_id": session.call_id, "generation": session.generation}
        )
        await hass.async_block_till_done()
        assert len(scans) == 0
    finally:
        for unsubscribe in detach:
            unsubscribe()
    scans.clear()
    hass.bus.async_fire(CALL_EVENT, {"call_id": "one"})
    await hass.async_block_till_done()
    assert scans == []
    assert not observer.unsubscribers


async def test_complete_registry_operation_publishes_without_a_manual_call_event(
    hass, phones
):
    from custom_components.voip_stack.call_projection import publish_bridge_projection

    changes = []
    observer = phone_state_observer(hass)
    detach = observer.subscribe(
        lambda payload, _: changes.append(
            (payload, phones.calls.event_context("one").state)
        )
    )
    try:
        session = phones.calls.upsert(
            "one",
            state="connecting",
            owner="router",
            caller="Office",
            callee="Kitchen",
            source_endpoint_id="office",
            dest_endpoint_id="kitchen",
        )
        publish_bridge_projection(hass, session)
        with phones.calls.state_operation():
            phones.calls.transition(session.call_id, state="ringing")
            phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
            assert changes == []
        await hass.async_block_till_done()
        assert len(changes) == 1
        assert changes[0][0]["phone"]["state"] == changes[0][1] == "ringing"
        assert changes[0][0]["call"]["caller"] == "Office"
        assert changes[0][0]["call"]["callee"] == "Kitchen"
        assert changes[0][0]["call"]["schema_version"] == CALL_EVENT_SCHEMA_VERSION
        phones.endpoints.update("kitchen", name="Dining room")
        await hass.async_block_till_done()
        assert changes[-1][0]["phone"]["state"] == "ringing"
        changes.clear()
        publish_bridge_projection(hass, session)
        await hass.async_block_till_done()
        kitchen = [
            (payload, state)
            for payload, state in changes
            if payload["endpoint_id"] == "kitchen"
        ]
        assert kitchen == [], "Replaying a presentation event must not recommit phone state"
    finally:
        detach()


@pytest.mark.parametrize("outer_context", [False, True])
async def test_mailbox_change_does_not_bind_an_unrelated_active_call(
    hass, phones, monkeypatch, outer_context
):
    from custom_components.voip_stack import phone_state
    from custom_components.voip_stack.mailbox import EVENT_VOICEMAIL_CHANGED

    count = 0
    monkeypatch.setattr(
        phone_state,
        "mailbox_state",
        lambda _hass, _endpoint: {
            "enabled": True,
            "waiting": bool(count),
            "new": count,
            "old": 0,
        },
    )
    session = phones.calls.upsert(
        "active",
        state="in_call",
        owner="router",
        source_endpoint_id="office",
        dest_endpoint_id="kitchen",
    )
    phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
    seen, tasks = [], []
    subject = PhoneStateTrigger(
        hass,
        TriggerConfig(
            "voip_stack.mailbox_changed", options={"phone": "kitchen-device"}
        ),
    )

    def runner(payload, description, context):
        async def action():
            seen.append((current_execution.get(), payload["phone"]["mailbox"]["new"]))

        task = asyncio.create_task(action())
        tasks.append(task)
        return task

    detach = await subject.async_attach_runner(runner)
    try:
        count = 1
        from custom_components.voip_stack.automation_context import CallExecution

        binding = (
            current_execution.set(CallExecution(session.token, {}))
            if outer_context
            else None
        )
        try:
            hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": "kitchen"})
        finally:
            if binding is not None:
                current_execution.reset(binding)
        await hass.async_block_till_done()
        await asyncio.gather(*tasks)
        assert seen == [(None, 1)]
    finally:
        detach()


@pytest.mark.parametrize('owned', [True, False])
async def test_esp_observation_cannot_release_a_pbx_owned_phone(hass, phones, monkeypatch, owned):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import esphome_state_bridge

    phones.endpoints.register(PhoneEndpoint(endpoint_id='esp', name='ESP', kind='esphome',
                                           device_id='esp-device', availability='available'))
    monkeypatch.setattr(esphome_state_bridge, '_get_voip_devices', AsyncMock(return_value=[{
        'device_id': 'esp-device', 'name': 'ESP', 'entities': {'voip_state': 'sensor.esp_state'},
    }]))
    changes = []
    remove = phone_state_observer(hass).subscribe(lambda data, _context: changes.append(data))
    try:
        if owned:
            session = phones.calls.upsert('sip-call', state='in_call', owner='bridge', source_endpoint_id='esp')
            phones.calls.claim_endpoint(session.call_id, 'esp', role='source')
            identity = session.call_id
            before = (session.event_context.state, session.event_context.sequence)
        else:
            identity = 'physical:esp'
            await esphome_state_bridge.async_emit_state_event(
                hass, 'sensor.esp_state', 'in_call', 'calling',
                expected_endpoint_id='esp', expected_call_id=identity,
            )
            assert phone_states(hass)['esp']['state'] == 'in_call'
        await esphome_state_bridge.async_emit_state_event(
            hass, 'sensor.esp_state', 'idle', 'in_call',
            expected_endpoint_id='esp', expected_call_id=identity,
        )
        if owned:
            assert phones.endpoints.require('esp').active_call_id == identity
            assert (session.event_context.state, session.event_context.sequence) == before
            assert phone_states(hass)['esp']['state'] == 'in_call'
        else:
            assert phones.endpoints.require('esp').active_call_id == ''
            assert phone_states(hass)['esp']['state'] == 'idle'
            assert changes[-1]['phone']['state'] == 'idle'
    finally:
        remove()


async def test_registry_commit_updates_card_and_releases_losing_browser_without_manual_events(hass, phones):
    from custom_components.voip_stack.endpoint_lifecycle import call_registry
    from custom_components.voip_stack.websocket_api import _ha_softphone_state
    from custom_components.voip_stack.dialog_info import snapshot

    calls = call_registry(hass)
    with calls.state_operation():
        session = calls.upsert('automatic', state='ringing', owner='ha_softphone',
                               caller='Office', callee='Kitchen',
                               dest_endpoint_id='kitchen', endpoint_id='kitchen')
        calls.claim_endpoint(session.call_id, 'kitchen', role='destination')
    await hass.async_block_till_done()
    assert _ha_softphone_state(hass, 'kitchen')['state'] == 'ringing'
    calls.transition(session.call_id, state='in_call', expected_generation=session.generation)
    await hass.async_block_till_done()
    assert _ha_softphone_state(hass, 'kitchen')['state'] == 'in_call'
    assert phone_states(hass)['kitchen']['state'] == 'in_call'
    assert all(dialog.state == 'confirmed' for dialog in snapshot(phones.runtime, 'kitchen'))
    calls.release_endpoint_claim(session.call_id, 'kitchen')
    await hass.async_block_till_done()
    assert phone_states(hass)['kitchen']['state'] == 'idle'
    # The public envelope retains its terminal Call-ID for provenance.
    assert not phones.runtime.softphones['kitchen'].get('call_id')
    assert _ha_softphone_state(hass, 'kitchen')['state'] == 'idle'
    assert not calls.event_context(session.call_id).terminal_summary_claimed
    assert session.live
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    await calls.request_termination(session.call_id, TerminationIntent('test_done'))
    await hass.async_block_till_done()
    publisher = calls.component('state_publisher')
    assert not publisher.pending and not publisher.delivered


async def test_retained_answered_caller_does_not_answer_new_browser_destination(hass, phones):
    calls = phones.calls
    session = calls.upsert('retained', state='in_call', owner='router', caller='SIP caller')
    calls.transition(session.call_id, owner='ha_softphone', state='ringing',
                     endpoint_id='kitchen', dest_endpoint_id='kitchen', callee='Kitchen')
    calls.claim_endpoint(session.call_id, 'kitchen', role='destination')
    assert session.state == 'in_call'
    assert session.legs['browser:kitchen'].state == 'ringing'
    assert phone_states(hass)['kitchen']['state'] == 'ringing'


async def test_outgoing_browser_state_never_creates_browser_leg_for_sip_destination(hass, phones):
    phones.endpoints.register(PhoneEndpoint(endpoint_id='desk', name='Desk', kind='sip_account'))
    session = phones.calls.upsert('outgoing', state='remote_ringing', owner='ha_softphone',
                                  endpoint_id='office', source_endpoint_id='office',
                                  dest_endpoint_id='desk')
    assert 'browser:desk' not in session.legs
    assert session.legs['browser:office'].state == 'remote_ringing'


async def test_control_precondition_uses_the_selected_leg_not_the_last_published_state(hass, phones):
    from homeassistant.exceptions import ServiceValidationError
    from custom_components.voip_stack.call_state import CallPrecondition, validate_call_snapshot

    calls = phones.calls
    session = calls.upsert('retained-guard', state='in_call', owner='router',
                           source_endpoint_id='office', dest_endpoint_id='kitchen')
    calls.observe_leg(session.call_id, 'browser:office', role='ha_softphone', endpoint_id='office', state='in_call')
    calls.observe_leg(session.call_id, 'browser:kitchen', role='ha_softphone', endpoint_id='kitchen', state='ringing')
    guard = CallPrecondition.capture(session)
    calls.event_fields(session.call_id, 'remote_ringing')
    assert guard.matches(session)
    validate_call_snapshot(calls, session.call_id, endpoint_id='kitchen', expected_state='ringing')
    validate_call_snapshot(calls, session.call_id, endpoint_id='office', expected_state='in_call')
    with pytest.raises(ServiceValidationError):
        validate_call_snapshot(calls, session.call_id, endpoint_id='kitchen', expected_state='in_call')
    calls.observe_leg(session.call_id, 'browser:kitchen', role='ha_softphone', endpoint_id='kitchen', state='in_call')
    assert not guard.matches(session)


async def test_batched_independent_calls_keep_each_phone_trigger_context(hass, phones):
    changes = []
    remove = phone_state_observer(hass).subscribe(lambda payload, _: changes.append(payload))
    try:
        with phones.calls.state_operation():
            for name in ('kitchen', 'office'):
                session = phones.calls.upsert('call-' + name, state='ringing', owner='router',
                                               callee=name, dest_endpoint_id=name)
                phones.calls.claim_endpoint(session.call_id, name, role='destination')
        assert {p['endpoint_id']: p['call']['call_id'] for p in changes} == {
            'kitchen': 'call-kitchen', 'office': 'call-office',
        }
        assert all(p['call']['sequence'] == phones.calls.event_context(p['call']['call_id']).sequence
                   for p in changes)
    finally:
        remove()


async def test_queue_agent_change_pushes_phone_projection_without_call_or_entity_change(hass, phones):
    from custom_components.voip_stack.call_queues import EVENT_QUEUE_CHANGED

    state = {"value": "available"}
    phones.runtime.queues = SimpleNamespace(phone_summary=lambda endpoint_id: [
        {"queue_id": "stable-queue", "name": "Support", "state": state["value"],
         "logged_in": state["value"] != "logged_out", "paused": state["value"] == "paused",
         "waiting": 0, "busy": False}] if endpoint_id == "kitchen" else [])
    changes = []
    remove = phone_state_observer(hass).subscribe(changes.append, kind="projection")
    try:
        state["value"] = "paused"
        hass.bus.async_fire(EVENT_QUEUE_CHANGED)
        await hass.async_block_till_done()
        assert len(changes) == 1
        assert changes[0].phones["kitchen"]["queue_states"][0]["state"] == "paused"
        state["value"] = "logged_out"
        hass.bus.async_fire(EVENT_QUEUE_CHANGED)
        await hass.async_block_till_done()
        assert changes[-1].phones["kitchen"]["queue_states"][0]["logged_in"] is False
    finally:
        remove()
