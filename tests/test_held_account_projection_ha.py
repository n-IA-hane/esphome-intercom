"""Hold keeps the selected account call live across native HA projections."""

from copy import deepcopy
from xml.etree import ElementTree

import pytest

from tests.test_account_projection_privacy_ha import connection_for
from tests.test_managed_phones_ha import custom_integration_path as custom_integration_path
from tests.test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("browser", [False, True])
@pytest.mark.parametrize("source", [False, True])
async def test_hold_resume_keeps_call_clock_controls_and_blf_live(hass, phones, browser, source):
    from custom_components.voip_stack import websocket_api as api
    from custom_components.voip_stack.const import CALL_EVENT
    from custom_components.voip_stack.dialog_info import document, snapshot
    from custom_components.voip_stack.phone_state import phone_states, phone_state_dto

    connection, events, _, _, _ = connection_for(hass, phones, control=True)
    role = "ha_softphone" if browser else "caller" if source else "callee"
    leg_id = "browser:kitchen" if browser else "native-kitchen"
    with phones.calls.state_operation():
        call = phones.calls.upsert("held-selected", state="in_call", owner="ha_softphone" if browser else "bridge",
            caller="Kitchen" if source else "Office", callee="Office" if source else "Kitchen",
            source_endpoint_id="kitchen" if source else "office",
            dest_endpoint_id="office" if source else "kitchen")
        phones.calls.claim_endpoint(call.call_id, "kitchen", role="source" if source else "destination")
        phones.calls.observe_leg(call.call_id, leg_id, endpoint_id="kitchen", role=role, state="in_call")
    api._ha_softphone_store(hass, "kitchen").update(call_id=call.call_id, state="in_call",
        direction="outgoing" if source else "incoming")
    api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": 1, "endpoint_id": "kitchen"})
    await hass.async_block_till_done()
    clock = events[-1]["connected_at"]
    assert clock > 0
    occurrence_events = []
    detach = hass.bus.async_listen(CALL_EVENT, lambda event: occurrence_events.append(dict(event.data)))
    try:
        for state in ("held", "in_call", "held", "in_call"):
            phones.calls.observe_leg(call.call_id, leg_id, endpoint_id="kitchen", role=role, state=state)
            await hass.async_block_till_done()
            assert call.live and call.state == "in_call"
            assert events[-1]["call_id"] == call.call_id
            assert events[-1]["state"] == state
            assert events[-1]["connected_at"] == clock
            assert events[-1]["can_control"]
            assert events[-1]["can_attach_media"] is browser
            phone = phone_states(hass)["kitchen"]
            assert phone["state"] == state
            dto = phone_state_dto(phone, peer_readable=lambda _identity: False)
            assert dto["calls"][0]["held"] is (state == "held")
            dialogs = snapshot(phones.runtime, "kitchen")
            assert dialogs[0].state == "confirmed" and dialogs[0].held is (state == "held")
            root = ElementTree.fromstring(document("sip:kitchen@example.test", 1, dialogs))
            ns = {"d": "urn:ietf:params:xml:ns:dialog-info"}
            assert root.find("d:dialog/d:state", ns).text == "confirmed"
        assert not [event for event in occurrence_events if event.get("event_type") in {"ended", "answered", "failed"}]
    finally:
        detach()
        connection.subscriptions[1]()


async def test_selected_held_call_does_not_inherit_another_calls_ringing_state(hass, phones):
    from custom_components.voip_stack import websocket_api as api
    from custom_components.voip_stack.phone_state import phone_states

    connection, events, _, _, _ = connection_for(hass, phones)
    with phones.calls.state_operation():
        held = phones.calls.upsert("held-selected", state="in_call", owner="bridge",
            caller="Kitchen", callee="Office", source_endpoint_id="kitchen")
        phones.calls.observe_leg(held.call_id, "native-held", endpoint_id="kitchen", role="caller", state="held")
        incoming = phones.calls.upsert("other-ringing", state="ringing", owner="router",
            caller="Another caller", callee="Kitchen", dest_endpoint_id="kitchen")
        phones.calls.observe_leg(incoming.call_id, "native-incoming", endpoint_id="kitchen", role="callee", state="ringing")
    api._ha_softphone_store(hass, "kitchen").update(call_id=held.call_id, state="in_call")
    api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": 2, "endpoint_id": "kitchen"})
    try:
        assert phone_states(hass)["kitchen"]["state"] == "ringing"
        selected = deepcopy(events[-1])
        assert selected["call_id"] == held.call_id and selected["state"] == "held"
        assert selected["connected_at"] == held.event_context.connected_at_epoch
        assert selected["can_answer"] is False
        assert selected["caller"] == "Kitchen" and selected["callee"] == "Office"
        assert selected["direction"] == "outgoing"
    finally:
        connection.subscriptions[2]()
