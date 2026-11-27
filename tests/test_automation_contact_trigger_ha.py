"""Native contact selection keeps the real IVR Script bound to its CallToken."""

import asyncio
import socket

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.trigger import TriggerConfig

from custom_components.voip_stack import automation_call, trigger
from custom_components.voip_stack.automation_contact_device import (
    async_sync_automation_contact_devices,
    automation_contact_device_identifier,
    automation_contact_event_unique_id,
)
from custom_components.voip_stack.const import CALL_EVENT, DOMAIN
from custom_components.voip_stack.core import rtp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.sip_client import SipCallClient, SipDatagramQueueProtocol

from .test_automation_contact_device_ha import contact_entry
from .test_automation_originate_ha import lab as lab, assert_call_resources_released, tts_fixture
from .test_trunk_calls_ha import sip_test_account

pytestmark = pytest.mark.ha


def contact_selection(hass, entry, subentry, field):
    async_sync_automation_contact_devices(hass, entry)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN,
        automation_contact_device_identifier(entry.entry_id, "welcome-id")), entry.entry_id)
    if field == "destination_device":
        return device.id
    entity = er.async_get(hass).async_get_or_create("event", DOMAIN,
        automation_contact_event_unique_id(entry.entry_id, "welcome-id"), config_entry=entry,
        config_subentry_id=subentry.subentry_id, device_id=device.id, suggested_object_id="welcome_call")
    return entity.entity_id


@pytest.mark.parametrize("field", ["destination_device", "destination_entity"])
async def test_selected_contact_filter_keeps_observations_and_same_call_reentry(hass, field):
    from custom_components.voip_stack.automation_contact_device import automation_contact_records
    from custom_components.voip_stack.automation_routing import matches_call
    from custom_components.voip_stack.call_projection import publish_bridge_projection
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    entry, subentry = contact_entry(hass)
    selected = contact_selection(hass, entry, subentry, field)
    endpoints = EndpointRegistry()
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    entry.runtime_data = VoipStackRuntime({}, {}, {}, endpoints, PhoneAdapterRegistry(hass, endpoints), sip=calls)
    contact = automation_contact_records(entry)["welcome-id"]
    session = calls.upsert("welcome-forward-reentry", state="ringing", owner="automation",
        caller="Caller", callee="Welcome", route_kind="automation")
    application = automation_call.attach_automation_application(hass, session, None, contact, "127.0.0.1")
    payload = {"call_id": session.call_id, "generation": session.generation, "direction": "incoming",
        "state": "ringing", "event_type": "automation_requested", "callee": "Welcome", "route_kind": "automation"}
    options = {field: selected}
    runs = []

    def run_action(variables, *_args):
        runs.append(variables["call"])
        completed = hass.loop.create_future()
        completed.set_result(object())
        return completed

    subject = trigger.VoipCallTrigger(hass, TriggerConfig("voip_stack.call_received", options=options))
    remove = await subject.async_attach_runner(run_action)
    try:
        automation_call.queue_automation_request(application)
        await hass.async_block_till_done()
        assert len(runs) == 1
        assert matches_call(payload, options, hass=hass)
        assert not matches_call({**payload, "generation": session.generation + 1}, options, hass=hass)
        calls.transition(session.call_id, owner="router", state="ringing", callee="Test", route_kind="transfer")
        forwarded = {**payload, "callee": "Test", "event_type": "ringing", "route_kind": "transfer"}
        assert not matches_call(forwarded, options, hass=hass)
        publish_bridge_projection(hass, session, direction="incoming", event_type="ringing", route_kind="transfer")
        await hass.async_block_till_done()
        assert len(runs) == 1
        # Observation triggers retain their existing contract during handoff.
        # Event Entity history separately follows the original CallToken.
        for event_type in ("dtmf", "ended", "missed", "failed"):
            assert matches_call({**forwarded, "event_type": event_type}, options, hass=hass)
        # A failed forward may resume Welcome; another actual route may enter
        # it again without changing the call's generation.
        calls.transition(session.call_id, owner="automation", state="ringing", callee="Welcome", route_kind="automation")
        application.require_current(session.generation)
        assert matches_call(payload, options, hass=hass)
        automation_call.queue_automation_request(application)
        await hass.async_block_till_done()
        assert len(runs) == 2 and all(run["call_id"] == session.call_id
            and run["generation"] == session.generation for run in runs)
        application.outgoing = True
        assert not matches_call(payload, options, hass=hass)
    finally:
        remove()
        await calls.shutdown()


@pytest.mark.parametrize("field", ["destination_device", "destination_entity"])
async def test_selected_contact_forward_to_card_does_not_retrigger(lab, monkeypatch, field):
    """A retained Welcome application must not select the forwarded card's ringing."""
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.websocket_api import _ha_softphone_state

    entry = lab.hass.config_entries.async_entries(DOMAIN)[0]
    metadata = {"virtual_endpoint": "automation", "automation_timeout": 30}
    subentry = ConfigSubentry(subentry_type="contact", title="Welcome", unique_id="contact:welcome-id",
        data={"id": "welcome-id", "name": "Welcome", "metadata": metadata})
    lab.hass.config_entries.async_add_subentry(entry, subentry)
    contact = RosterEntry("welcome-id", name="Welcome", ha_bridge=True, metadata=metadata)
    endpoint = PhoneEndpoint(endpoint_id="test-card", name="Test", device_id="test-card-device",
        kind=EndpointKind.BROWSER, availability="available", capabilities=frozenset({"audio", "dtmf"}))
    lab.runtime.endpoints.register(endpoint)
    lab.roster.extend([contact, RosterEntry("Test", metadata={"local_ha": True,
        "endpoint_id": endpoint.endpoint_id, "endpoint_kind": endpoint.kind.value})])
    lab.runtime.directory.replace(lab.roster)
    selected = contact_selection(lab.hass, entry, subentry, field)
    config = await trigger.VoipCallTrigger.async_validate_config(lab.hass, {"options": {field: selected}})
    subject = trigger.VoipCallTrigger(lab.hass,
        TriggerConfig("voip_stack.call_received", options=config["options"]))
    tts_fixture(monkeypatch, lab)
    actions = await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA([
        {"action": "voip_stack.tts_say", "data": {"tts_entity_id": "tts.test", "message": "Wake up"}},
        {"action": "voip_stack.forward", "data": {"destination": "Test", "on_failure": "terminate"}},
    ]))
    script = Script(lab.hass, actions, "Welcome forwards to a card", DOMAIN)
    runs, ringing = [], asyncio.Event()
    events = []

    def run_action(variables, _description, context):
        task = lab.hass.async_create_task(script.async_run(variables, context=context))
        runs.append(task)
        return task

    remove_trigger = await subject.async_attach_runner(run_action)

    @callback
    def observe(event):
        data = dict(event.data)
        events.append(data)
        if data.get("state") == "ringing" and data.get("callee") == "Test":
            ringing.set()

    remove_observer = lab.hass.bus.async_listen(CALL_EVENT, observe)
    account = sip_test_account(lab, "bedroom1")
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.2", local_name="Caller", local_uri_user=account.username,
        local_sip_port=0, local_rtp_port=audio.getsockname()[1], username=account.username,
        password=account.password, supported_formats=[AudioFormat(16000, frame_ms=20)])
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0))
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    client.audio_rtp_socket = audio
    call_id = client.dialog_ids.call_id
    try:
        result = await client.invite(target="welcome-id", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert result in {"ringing", "in_call"}
        await asyncio.wait_for(ringing.wait(), 3)
        session = lab.calls.get_session(call_id)
        application = automation_call.automation_call(lab.hass, call_id)
        assert session is not None and application is not None
        assert session.owner != application.owner and session.callee == "Test"
        state = _ha_softphone_state(lab.hass, endpoint.endpoint_id)
        assert state["state"] == "ringing" and state["call_id"] == call_id
        assert len(runs) == 1, events
    finally:
        remove_trigger()
        remove_observer()
        await client.terminate()
        session = lab.calls.get_session(call_id)
        if session is not None:
            await asyncio.wait_for(session.terminated.wait(), 3)
        for task in runs:
            if not task.done():
                task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)
        for session in tuple(lab.calls.sessions.values()):
            barrier = lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
            if barrier is not None:
                await barrier
        await client.close()
        audio.close()
        assert_call_resources_released(lab)


@pytest.mark.parametrize("field", ["destination_device", "destination_entity"])
@pytest.mark.parametrize("digits", ["1", "111"])
async def test_native_selected_contact_script_speaks_collects_and_forwards_same_call(lab, monkeypatch, field, digits):
    entry = lab.hass.config_entries.async_entries(DOMAIN)[0]
    raw = {"id": "welcome-id", "name": "Welcome", "extension": "665",
        "metadata": {"virtual_endpoint": "automation", "automation_timeout": 30}}
    subentry = ConfigSubentry(subentry_type="contact", title="Welcome", unique_id="contact:welcome-id", data=raw)
    lab.hass.config_entries.async_add_subentry(entry, subentry)
    contact = RosterEntry("welcome-id", name="Welcome", extension="665", ha_bridge=True,
        metadata=raw["metadata"])
    lab.roster.append(contact)
    lab.runtime.directory.replace(lab.roster)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    selected = contact_selection(lab.hass, entry, subentry, field)
    validated = await trigger.VoipCallTrigger.async_validate_config(lab.hass, {"options": {field: selected}})
    subject = trigger.VoipCallTrigger(lab.hass,
        TriggerConfig("voip_stack.call_received", options=validated["options"]))
    stream = tts_fixture(monkeypatch, lab)
    dtmf_results, dtmf_events, context_tokens = [], [], []
    original_say = automation_call.AutomationCall.say
    async def observe_say(application, call):
        execution = automation_call.current_execution.get()
        assert execution is not None
        context_tokens.append(("tts", execution.token, application.session.token))
        return await original_say(application, call)
    monkeypatch.setattr(automation_call.AutomationCall, "say", observe_say)
    original_wait = automation_call.AutomationCall.wait_for_dtmf
    original_forward = automation_call.AutomationCall.forward
    forward_targets = []
    async def observe_forward(application, destination, **kwargs):
        execution = automation_call.current_execution.get()
        assert execution is not None
        context_tokens.append(("forward", execution.token, application.session.token))
        forward_targets.append((application.session.token, destination))
        return await original_forward(application, destination, **kwargs)
    monkeypatch.setattr(automation_call.AutomationCall, "forward", observe_forward)
    async def observe_wait(application, call):
        execution = automation_call.current_execution.get()
        assert execution is not None
        context_tokens.append(("wait", execution.token, application.session.token))
        result = await original_wait(application, call)
        dtmf_results.append((application.session.token, result))
        return result
    monkeypatch.setattr(automation_call.AutomationCall, "wait_for_dtmf", observe_wait)
    from custom_components.voip_stack.const import SIP_DTMF_EVENT
    remove_digits = lab.hass.bus.async_listen(SIP_DTMF_EVENT, lambda event: dtmf_events.append(dict(event.data)))
    actions = [
        {"action": "voip_stack.tts_say", "data": {"tts_entity_id": "tts.test", "message": "Wake up"}},
        {"action": "voip_stack.wait_for_dtmf", "data": {"timeout": 2, "max_digits": 32,
            "inter_digit_timeout": .1}, "response_variable": "selection"},
        {"action": "voip_stack.forward", "data": {
            "destination": "{{ 'bedroom0' if selection.digits in ['1', '111'] else 'invalid-selection' }}",
            "on_failure": "terminate"}},
    ]
    actions = await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(actions))
    script = Script(lab.hass, actions, "Native Welcome IVR", DOMAIN)
    runs = []

    def run_action(variables, _description, context):
        task = lab.hass.async_create_task(script.async_run(variables, context=context))
        runs.append(task)
        return task

    remove = await subject.async_attach_runner(run_action)
    account = sip_test_account(lab, "bedroom1")
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.2", local_name="Caller", local_uri_user=account.username,
        local_sip_port=0, local_rtp_port=audio.getsockname()[1], username=account.username,
        password=account.password, supported_formats=[AudioFormat(16000, frame_ms=20)])
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0))
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    client.audio_rtp_socket = audio
    call_id = client.dialog_ids.call_id
    try:
        result = await client.invite(target="welcome-id", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if result != "in_call":
            assert result == "ringing", result
            assert await client.wait_for_final(timeout=3) == "in_call"
        session = lab.calls.get_session(call_id)
        assert session is not None and session.owner == "automation"
        application = automation_call.automation_call(lab.hass, call_id)
        assert application is not None and application.contact.id == "welcome-id"
        token = session.token
        received = bytearray()
        async with asyncio.timeout(3):
            while len(received) < len(stream.pcm):
                packet = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
                if any(packet.payload):
                    received.extend(packet.payload)
        expected = b"".join(stream.pcm[index:index + 2][::-1] for index in range(0, len(stream.pcm), 2))
        assert bytes(received) == expected
        async with asyncio.timeout(3):
            while application.operation is None or application.operation.get_name() != "automation-wait_for_dtmf":
                await asyncio.sleep(0)
        for digit in digits:
            assert await client.send_dtmf_info(digit)
            await asyncio.sleep(.02)
        async with asyncio.timeout(3):
            while not dtmf_results:
                await asyncio.sleep(0)
        assert dtmf_results == [(token, {"status": "timeout", "digits": digits})], dtmf_events
        async with asyncio.timeout(3):
            while not forward_targets:
                await asyncio.sleep(0)
        assert forward_targets == [(token, "bedroom0")], forward_targets
        await asyncio.wait_for(lab.phones[0].answered.wait(), 3)
        await asyncio.gather(*runs)
        assert len(runs) == 1 and len(stream.calls) == 1
        assert context_tokens == [(phase, token, token) for phase in ("tts", "wait", "forward")]
        assert len(lab.phones[0].invites) == 1
        assert lab.calls.get_session(call_id) is session and session.token == token and session.live
        assert session.callee == "bedroom0"
        await client.terminate()
        await asyncio.wait_for(session.terminated.wait(), 3)
        assert_call_resources_released(lab)
    finally:
        remove()
        remove_digits()
        for task in runs:
            if not task.done():
                task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)
        for session in tuple(lab.calls.sessions.values()):
            barrier = lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
            if barrier is not None:
                await barrier
        await client.close()
        audio.close()


@pytest.mark.parametrize("field", ["destination_device", "destination_entity"])
async def test_contact_trigger_rejects_conflicting_disabled_and_stale_selection(hass, field):
    entry, subentry = contact_entry(hass)
    selected = contact_selection(hass, entry, subentry, field)
    other_field = "destination_entity" if field == "destination_device" else "destination_device"
    for conflict in ({"destination": "Welcome"}, {other_field: "another"}, {"destination_account": "phone"},
                     {"destination_match": "prefix"}):
        with pytest.raises(vol.Invalid):
            await trigger.VoipCallTrigger.async_validate_config(hass, {"options": {field: selected, **conflict}})
    hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, "enabled": False})
    with pytest.raises(vol.Invalid):
        await trigger.VoipCallTrigger.async_validate_config(hass, {"options": {field: selected}})
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    async_sync_automation_contact_devices(hass, entry)
    with pytest.raises(vol.Invalid):
        await trigger.VoipCallTrigger.async_validate_config(hass, {"options": {field: selected}})


@pytest.mark.parametrize("field", ["destination_device", "destination_entity"])
async def test_contact_trigger_resolves_current_identity_without_stale_name_fallback(hass, field):
    from custom_components.voip_stack.automation_routing import matches_call

    entry, subentry = contact_entry(hass)
    selected = contact_selection(hass, entry, subentry, field)
    options = {field: selected}
    payload = {"state": "ringing", "direction": "incoming", "route_kind": "automation", "callee": "Welcome"}
    assert matches_call(payload, options, hass=hass)
    hass.config_entries.async_update_subentry(entry, subentry, title="Renamed IVR",
        data={**subentry.data, "name": "Renamed IVR"})
    assert matches_call({**payload, "callee": "Renamed IVR"}, options, hass=hass)
    assert not matches_call(payload, options, hass=hass)
    current = entry.subentries[subentry.subentry_id]
    hass.config_entries.async_update_subentry(entry, current, data={**current.data, "enabled": False})
    assert not matches_call({**payload, "callee": "Renamed IVR"}, options, hass=hass)
    # Ordinary destination matching remains the established, independent path.
    assert matches_call({"callee": "Desk", "route_kind": "direct"}, {"destination": "Desk"}, hass=hass)
