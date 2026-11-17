"""Native Logbook delivery uses account phases and the existing terminal owner."""

from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
import pytest

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


async def test_account_activity_native_bus_has_one_phase_and_fast_failure_without_global_status(phone_lab):
    from custom_components.voip_stack.const import SIP_CALL_ENDED_EVENT
    from custom_components.voip_stack.logbook import async_setup_account_activity
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sip_account_phone_data, sync_registry_from_entry
    from custom_components.voip_stack.phone_state import PHONE_STATE_CHANGED

    lab = phone_lab
    data = sip_account_phone_data({"username": "desk", "password": "fixture-password",
        "display_name": "Desk"}, endpoint_id="sip:desk")
    _add_phone_subentry(lab.hass, lab.entry, data=data, title="Desk")
    sync_registry_from_entry(lab.hass, lab.entry)
    sensor = er.async_get(lab.hass).async_get_or_create("sensor", "voip_stack",
        "phone_endpoint_sip:desk_call_state", config_entry=lab.entry, suggested_object_id="desk_call")
    async_setup_account_activity(lab.hass, lab.entry)
    captured = []
    unsubscribe = lab.hass.bus.async_listen("logbook_entry", captured.append)
    context = Context()
    row = {"call_id": "call-one", "generation": 1, "state": "early", "direction": "initiator", "leg_id": "one"}
    before, ringing = {"name": "Desk", "calls": []}, {"name": "Desk", "calls": [row, {**row, "leg_id": "two"}]}
    details = {"call_id": "call-one", "generation": 1, "dialed_target": "3511234567"}
    lab.hass.bus.async_fire(PHONE_STATE_CHANGED, {"endpoint_id": "sip:desk", "previous": before,
        "phone": ringing, "call": details}, context=context)
    await lab.hass.async_block_till_done()
    assert [event.data["message"] for event in captured] == ["Calling 3511234567"]
    confirmed = {"name": "Desk", "calls": [{**row, "state": "confirmed", "leg_id": "winner"}]}
    lab.hass.bus.async_fire(PHONE_STATE_CHANGED, {"endpoint_id": "sip:desk", "previous": ringing,
        "phone": confirmed, "call": {**details, "answered_by": "Fanvil"}}, context=context)
    await lab.hass.async_block_till_done()
    terminal = {**details, "type": "ended", "state": "idle", "reason": "remote_hangup",
        "participant_endpoint_ids": ["sip:desk", "sip:desk"], "endpoint_id": "sip:desk"}
    lab.hass.bus.async_fire(PHONE_STATE_CHANGED, {"endpoint_id": "sip:desk", "previous": confirmed,
        "phone": before, "call": terminal}, context=context)
    lab.hass.bus.async_fire(SIP_CALL_ENDED_EVENT, terminal, context=context)
    await lab.hass.async_block_till_done()
    assert [event.data["message"] for event in captured] == ["Calling 3511234567", "Answered by Fanvil", "Call ended · remote hangup"]
    # No preceding phone dialog is needed for the one terminal occurrence.
    lab.hass.bus.async_fire(SIP_CALL_ENDED_EVENT, {"call_id": "failed-fast", "generation": 2,
        "type": "failed", "reason": "busy", "sip_status_code": 486,
        "source_endpoint_id": "sip:desk"}, context=context)
    await lab.hass.async_block_till_done()
    assert captured[-1].data["message"] == "Call failed · busy · SIP 486"
    assert all(event.data["entity_id"] == sensor.entity_id and event.context.id == context.id for event in captured)
    unsubscribe()
