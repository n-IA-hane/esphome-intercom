"""Account streams retain native HA permission boundaries with diagnostics on."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from homeassistant.auth import EVENT_USER_UPDATED
from homeassistant.auth.models import Group, User
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.helpers import device_registry as dr, entity_registry as er

from tests.test_managed_phones_ha import custom_integration_path as custom_integration_path
from tests.test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


def connection_for(hass, phones, *, control=False, admin=None):
    from custom_components.voip_stack.entity_identity import shared_entity_ids
    from custom_components.voip_stack.phone_endpoint import EndpointKind

    entities = er.async_get(hass)
    own = entities.async_get_or_create("sensor", "voip_stack", "private_kitchen")
    denied = entities.async_get_or_create("sensor", "voip_stack", "private_office")
    entities.async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    for identity, entity in (("kitchen", own), ("office", denied)):
        phones.endpoints.update(identity, kind=EndpointKind.SIP_ACCOUNT,
                               username=identity, entity_ids={entity.entity_id})
    group = Group(name="Account reader", policy={"entities": {"entity_ids": {
        shared_entity_ids(hass)["call_event"]: {"read": True, **({"control": True} if control else {})},
        own.entity_id: {"read": True, **({"control": True} if control else {})},
    }}})
    user = admin or User(name="Account reader", is_active=True,
        perm_lookup=PermissionLookup(entities, dr.async_get(hass)), groups=[group])
    events, results = [], []
    connection = SimpleNamespace(user=user, subscriptions={},
        send_result=lambda *args: results.append(deepcopy(args)),
        send_event=lambda identity, payload: events.append(deepcopy(payload)))
    return connection, events, results, own, denied


def add_dialog(phones, endpoint_id, identity, *, pending=False):
    from custom_components.voip_stack.core.sdp import RtpPcmFormat
    from custom_components.voip_stack.sip_client import SipCallClient, SipDialog

    wire_id = identity + "-wire"
    client = SipCallClient(local_ip="192.0.2.1", local_name="Test", local_sip_port=5060,
                          local_rtp_port=41000, call_id=wire_id)
    if pending:
        client._invite_transaction_active = True
    else:
        fmt = RtpPcmFormat(96, "L16", 16000, 1, 20)
        client.dialog = SipDialog(target=endpoint_id, remote_host="192.0.2.20", remote_sip_port=5060,
            remote_rtp_host="192.0.2.20", remote_rtp_port=42000, local_rtp_port=41000,
            call_id=wire_id, local_uri="sip:Test@192.0.2.1", remote_uri="sip:Peer@192.0.2.20",
            send_format=fmt, recv_format=fmt)
    with phones.calls.state_operation():
        call = phones.calls.upsert(identity, state="calling" if pending else "in_call",
            owner="bridge", caller="Peer", callee=endpoint_id, dest_endpoint_id=endpoint_id)
        phones.calls.claim_endpoint(identity, endpoint_id, role="destination")
        phones.calls.attach_sip_client(identity, wire_id, client, state=call.state)
    return call, wire_id


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("role", ["reader", "controller", "admin"])
@pytest.mark.parametrize("pending", [False, True])
async def test_account_snapshots_and_queued_pushes_exclude_foreign_owners(
    hass, phones, hass_admin_user, debug, role, pending,
):
    from custom_components.voip_stack import websocket_api as api
    from custom_components.voip_stack.const import HA_SOFTPHONE_STATE_EVENT

    connection, events, results, own, denied = connection_for(
        hass, phones, control=role == "controller", admin=hass_admin_user if role == "admin" else None)
    if role != "admin":
        assert connection.user.permissions.check_entity(own.entity_id, "read")
        assert not connection.user.permissions.check_entity(denied.entity_id, "read")
    phones.runtime.debug_mode = debug
    own_call, own_wire = add_dialog(phones, "kitchen", "visible-kitchen", pending=pending)
    api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": 1, "endpoint_id": "kitchen"})
    try:
        # Opening a controller stream adds its own presence after its initial
        # payload. Settle that local update before the foreign-call comparison.
        hass.bus.async_fire(HA_SOFTPHONE_STATE_EVENT, api._ha_softphone_state(hass, "kitchen"))
        await hass.async_block_till_done()
        before = deepcopy(events[-1])
        assert own_wire in before["pending_call_ids" if pending else "active_call_ids"]
        foreign, foreign_wire = add_dialog(phones, "office", "denied-office", pending=pending)
        await hass.async_block_till_done()
        assert api._ha_softphone_state(hass, "kitchen") == {
            key: value for key, value in before.items() if key != "can_control"
        }, "A denied account must not change an account snapshot or its counts"
        assert events[-1] == before
        # A previously constructed bus snapshot must be reprojected too.
        stale = {**before, "active_call_ids": [foreign_wire],
                 "media_debug": {"foreign_owner": foreign.call_id}}
        hass.bus.async_fire(HA_SOFTPHONE_STATE_EVENT, stale)
        await hass.async_block_till_done()
        assert events[-1] == before
        assert foreign.call_id not in json.dumps(events)
        assert foreign_wire not in json.dumps(events)
        api.websocket_ha_softphone_state(hass, connection, {"id": 2, "endpoint_id": "kitchen"})
        await hass.async_block_till_done()
        assert results[-1][1] == before
        assert before["can_control"] == (role != "reader")
        if debug:
            registry = before["media_debug"]["call_registry"]
            assert registry["call_ids"] == [own_call.call_id]
            assert registry["resource_counts"]["sessions"] == 1
    finally:
        connection.subscriptions[1]()


async def test_native_access_revocation_closes_account_stream_before_push(hass, phones):
    from custom_components.voip_stack import websocket_api as api
    from custom_components.voip_stack.const import HA_SOFTPHONE_STATE_EVENT

    connection, events, _, own, _ = connection_for(hass, phones)
    api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": 3, "endpoint_id": "kitchen"})
    connection.user.is_active = False
    hass.bus.async_fire(EVENT_USER_UPDATED, {"user_id": connection.user.id})
    await hass.async_block_till_done()
    assert events[-1]["kind"] == "closed" and events[-1]["reason"] == "permissions_revoked"
    closed = deepcopy(events)
    hass.bus.async_fire(HA_SOFTPHONE_STATE_EVENT, {"endpoint_id": "kitchen", "call_id": "foreign"})
    await hass.async_block_till_done()
    assert events == closed
