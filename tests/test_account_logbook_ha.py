"""Native Logbook delivery uses account phases and the existing terminal owner."""

from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from unittest.mock import Mock
from pathlib import Path
import pytest

from .test_managed_phones_ha import (
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def custom_integration_path(recorder_db_url, enable_custom_integrations):
    """Configure Recorder before the custom integration fixture creates HA."""
    import custom_components

    previous = custom_components.__path__
    custom_components.__path__ = [*previous, str(Path(__file__).parents[1] / "custom_components")]
    yield
    custom_components.__path__ = previous


async def test_account_activity_native_bus_has_one_phase_and_fast_failure_without_global_status(phone_lab):
    from custom_components.voip_stack.const import SIP_CALL_ENDED_EVENT
    from custom_components.voip_stack.logbook import async_setup_account_activity
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sip_account_phone_data, sync_registry_from_entry
    from custom_components.voip_stack.phone_state import PHONE_STATE_CHANGED

    lab = phone_lab
    lab.hass.config.language = "it"  # Persisted custom Logbook messages do not follow a user's UI locale.
    data = sip_account_phone_data({"username": "desk", "password": "fixture-password",
        "display_name": "Desk"}, endpoint_id="desk")
    _add_phone_subentry(lab.hass, lab.entry, data=data, title="Desk")
    sync_registry_from_entry(lab.hass, lab.entry)
    sensor = er.async_get(lab.hass).async_get_or_create("sensor", "voip_stack",
        "phone_endpoint_desk_call_state", config_entry=lab.entry, suggested_object_id="desk_call")
    async_setup_account_activity(lab.hass, lab.entry)
    captured = []
    unsubscribe = lab.hass.bus.async_listen("logbook_entry", captured.append)
    context = Context()
    row = {"call_id": "call-one", "generation": 1, "state": "early", "direction": "initiator", "leg_id": "one"}
    before, ringing = {"name": "Desk", "calls": []}, {"name": "Desk", "calls": [row, {**row, "leg_id": "two"}]}
    details = {"call_id": "call-one", "generation": 1, "dialed_target": "3511234567"}
    lab.hass.bus.async_fire(PHONE_STATE_CHANGED, {"endpoint_id": "desk", "previous": before,
        "phone": ringing, "call": details}, context=context)
    await lab.hass.async_block_till_done()
    assert [event.data["message"] for event in captured] == ["Calling 3511234567"]
    confirmed = {"name": "Desk", "calls": [{**row, "state": "confirmed", "leg_id": "winner"}]}
    lab.hass.bus.async_fire(PHONE_STATE_CHANGED, {"endpoint_id": "desk", "previous": ringing,
        "phone": confirmed, "call": {**details, "answered_by": "Fanvil"}}, context=context)
    await lab.hass.async_block_till_done()
    terminal = {**details, "type": "ended", "state": "idle", "reason": "remote_hangup",
        "participant_endpoint_ids": ["desk", "desk"], "endpoint_id": "desk"}
    lab.hass.bus.async_fire(PHONE_STATE_CHANGED, {"endpoint_id": "desk", "previous": confirmed,
        "phone": before, "call": terminal}, context=context)
    lab.hass.bus.async_fire(SIP_CALL_ENDED_EVENT, terminal, context=context)
    await lab.hass.async_block_till_done()
    assert [event.data["message"] for event in captured] == [
        "Calling 3511234567", "Outgoing call to Fanvil answered", "Call with 3511234567 ended · remote hangup"]
    # No preceding phone dialog is needed for the one terminal occurrence.
    lab.hass.bus.async_fire(SIP_CALL_ENDED_EVENT, {"call_id": "failed-fast", "generation": 2,
        "type": "failed", "reason": "busy", "sip_status_code": 486,
        "source_endpoint_id": "desk"}, context=context)
    await lab.hass.async_block_till_done()
    assert captured[-1].data["message"] == "Outgoing call failed · busy · SIP 486"
    assert all(event.data["entity_id"] == sensor.entity_id and event.context.id == context.id for event in captured)
    unsubscribe()


async def test_native_call_entities_keep_state_and_capture_account_relative_summary(phone_lab):
    from homeassistant.core import Event
    from custom_components.voip_stack.event import PhoneEndpointCallEvent, VoipStackCallEvent
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    lab = phone_lab
    source = PhoneEndpoint(endpoint_id="test", name="Test", kind=EndpointKind.SIP_ACCOUNT)
    destination = PhoneEndpoint(endpoint_id="desk", name="Desk", kind=EndpointKind.SIP_ACCOUNT)
    context = Context()
    original = {"call_id": "original-call", "generation": 7, "sequence": 9,
        "type": "ended", "state": "idle", "pbx_phase": "terminated", "direction": "outgoing",
        "caller": "Test", "callee": "430", "connected_party": "Desk", "reason": "remote_hangup",
        "source_endpoint_id": source.endpoint_id, "dest_endpoint_id": destination.endpoint_id}
    global_entity = VoipStackCallEvent(lab.hass, lab.entry)
    global_entity.async_write_ha_state = Mock()
    source_entity = PhoneEndpointCallEvent(lab.hass, source, lab.runtime.endpoints)
    source_entity.hass = lab.hass
    source_entity.async_write_ha_state = Mock()
    destination_entity = PhoneEndpointCallEvent(lab.hass, destination, lab.runtime.endpoints)
    destination_entity.hass = lab.hass
    destination_entity.async_write_ha_state = Mock()
    event = Event(CALL_EVENT, original, context=context)
    for entity in (global_entity, source_entity, destination_entity):
        entity._on_call_event(event)
        attrs = entity.state_attributes
        assert attrs["event_type"] == "ended" and attrs["state"] == "idle"
        assert (attrs["call_id"], attrs["generation"], attrs["sequence"]) == ("original-call", 7, 9)
        assert entity._context == context
        assert "ended by Desk" not in attrs["call_summary"]
    assert global_entity.state_attributes["call_summary"] == "Test called Desk · remote hangup"
    assert source_entity.state_attributes["call_summary"] == "Outgoing call to Desk ended · remote hangup"
    assert destination_entity.state_attributes["call_summary"] == "Incoming call from Test ended · remote hangup"
    assert destination_entity.state_attributes["account_direction"] == "incoming"
    assert destination_entity.state_attributes["direction"] == "outgoing"  # Original automation envelope is preserved.
    assert destination_entity.state_attributes["peer_name"] == "Test"
    assert original.get("call_summary") is None and original.get("account_direction") is None
    global_entity._on_call_event(Event(CALL_EVENT, {**original, "pbx_phase": "established",
        "state": "cancelled", "reason": "forwarded"}, context=context))
    assert "call_summary" not in global_entity.state_attributes


async def test_pbx_call_activity_is_selected_by_real_recorder_and_local_accounts_do_not_leak(
    recorder_mock, phone_lab
):
    """Exercise terminal publication, Recorder's SQL filter and native Logbook formatting."""
    from datetime import timedelta
    from homeassistant.components import logbook as ha_logbook
    from homeassistant.components.logbook.helpers import async_determine_event_types
    from homeassistant.components.logbook.processor import EventProcessor
    from homeassistant.helpers import device_registry as dr
    from homeassistant.setup import async_setup_component
    from homeassistant.util import dt as dt_util
    from custom_components.voip_stack import logbook
    from custom_components.voip_stack.const import DOMAIN, SIP_CALL_ENDED_EVENT
    from custom_components.voip_stack.endpoint_lifecycle import call_registry
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sip_account_phone_data, sync_registry_from_entry

    lab = phone_lab
    accounts = [("test", "Test"), ("other", "Other"), ("desk", "Desk")]
    for identity, name in accounts:
        data = sip_account_phone_data({"username": name.lower(), "password": "fixture-only-password",
            "display_name": name}, endpoint_id=identity)
        _add_phone_subentry(lab.hass, lab.entry, data=data, title=name)
    sync_registry_from_entry(lab.hass, lab.entry)
    entities = er.async_get(lab.hass)
    devices = dr.async_get(lab.hass)
    pbx = devices.async_get_or_create(config_entry_id=lab.entry.entry_id,
        identifiers={(DOMAIN, lab.entry.entry_id)}, name="VoIP Stack", model="PBX")
    global_call = entities.async_get_or_create("event", DOMAIN, "voip_stack_call_event",
        config_entry=lab.entry, device_id=pbx.id, suggested_object_id="renamed_pbx_call")
    local = {identity: entities.async_get_or_create("sensor", DOMAIN,
        f"phone_endpoint_{identity}_call_state", config_entry=lab.entry,
        device_id=lab.runtime.endpoints.get(identity).device_id) for identity, _name in accounts}
    from .support.ha_frontend import prepare_frontend_dependency

    prepare_frontend_dependency(lab.hass)
    assert await async_setup_component(lab.hass, "logbook", {})
    # This fixture owns an I/O-free runtime, so register the normal integration
    # platform through Core's real registration hook rather than loading SIP I/O.
    ha_logbook._process_logbook_platform(lab.hass, DOMAIN, logbook)
    logbook.async_setup_account_activity(lab.hass, lab.entry)
    captured = []
    unsubscribe = lab.hass.bus.async_listen(SIP_CALL_ENDED_EVENT, captured.append)
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    native_terminal = []
    remove_native = lab.hass.bus.async_listen(CALL_EVENT, lambda event: native_terminal.append(event)
        if event.data.get("type") in {"ended", "failed", "missed"} else None)
    registry = call_registry(lab.hass)
    start = dt_util.utcnow() - timedelta(seconds=1)
    cases = [
        ("first-call", "test", "Test", "esp:p4", "430", "Waveshare P4 Touch"),
        ("other-pair", "other", "Other", "desk", "Desk", "Desk"),
        ("next-call", "test", "Test", "desk", "Desk", "Desk"),
    ]
    for call_id, source_id, caller, dest_id, callee, connected in cases:
        context = Context()
        session = registry.upsert(call_id, state="in_call", owner="ha_softphone",
            caller=caller, callee=callee, endpoint_id=source_id, source_endpoint_id=source_id,
            dest_endpoint_id=dest_id, connected_party=connected, direction="outgoing", ha_context=context)
        task = registry.request_termination(call_id, TerminationIntent.bye("local_hangup"), generation=session.generation)
        assert task is not None
        await task
        await lab.hass.async_block_till_done()
    await recorder_mock.async_block_till_done()
    end = dt_util.utcnow() + timedelta(seconds=1)

    async def activity(entity_id):
        processor = EventProcessor(lab.hass,
            async_determine_event_types(lab.hass, [entity_id], None), entity_ids=[entity_id], timestamp=True)
        return await recorder_mock.async_add_executor_job(processor.get_events, start, end)

    global_rows = [row for row in await activity(global_call.entity_id) if row.get("message")]
    assert len(global_rows) == 3
    assert [row["call_id"] for row in global_rows] == [case[0] for case in cases]
    assert [row["context_id"] for row in global_rows] == [event.context.id for event in captured]
    assert all(row["entity_id"] == global_call.entity_id and "state" not in row for row in global_rows)
    assert global_rows[0]["message"].startswith("Test called Waveshare P4 Touch")
    assert all("local hangup" in row["message"] for row in global_rows)
    assert all(row["generation"] > 0 for row in global_rows)
    assert len(captured) == 3 and all(event.data["entity_id"] == global_call.entity_id for event in captured)
    for event in captured:
        matching = [native for native in native_terminal if native.data.get("call_id") == event.data["call_id"]]
        assert matching and all(native.context.id == event.context.id for native in matching)
    assert all(event.data["device_id"] != pbx.id for event in captured if event.data.get("device_id"))
    test_rows = [row for row in await activity(local["test"].entity_id) if row.get("message")]
    assert len(test_rows) == 2 and all("Other" not in row["message"] for row in test_rows)
    assert test_rows[0]["message"] == "Outgoing call to Waveshare P4 Touch ended · local hangup"
    desk_rows = [row for row in await activity(local["desk"].entity_id) if row.get("message")]
    assert len(desk_rows) == 2
    assert desk_rows[0]["message"] == "Incoming call from Other ended · local hangup"
    assert desk_rows[1]["message"] == "Incoming call from Test ended · local hangup"
    unsubscribe()
    remove_native()
