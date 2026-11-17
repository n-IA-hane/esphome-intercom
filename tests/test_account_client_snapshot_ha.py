"""Account state and per-user controls stay distinct from global diagnostics."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from homeassistant.helpers import entity_registry as er

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


async def test_subscription_projects_each_users_control_without_mutating_account(hass, phones):
    from custom_components.voip_stack import websocket_api as api
    from custom_components.voip_stack.entity_identity import shared_entity_ids
    from custom_components.voip_stack.phone_endpoint import EndpointKind

    phones.endpoints.update("kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen",
                            entity_ids={"sensor.account_kitchen"})
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    shared = shared_entity_ids(hass)["call_event"]

    class Connection:
        def __init__(self, control):
            self.user = SimpleNamespace(id="controller" if control else "observer", is_active=True,
                permissions=SimpleNamespace(check_entity=lambda entity, policy:
                    entity in {shared, "sensor.account_kitchen"} and (policy == "read" or control)))
            self.subscriptions, self.messages = {}, []

        def send_result(self, identity):
            self.messages.append(("ack", identity))

        def send_event(self, identity, state):
            self.messages.append(("snapshot", deepcopy(state)))

    connections = [Connection(False), Connection(True)]
    try:
        for identity, connection in enumerate(connections, 1):
            api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": identity, "endpoint_id": "kitchen"})
            assert connection.messages[0] == ("ack", identity)
            assert connection.messages[1][1]["can_control"] is (identity == 2)
        phones.endpoints.update("kitchen", dnd=True)
        await hass.async_block_till_done()
        assert connections[0].messages[-1][1]["can_control"] is False
        assert connections[1].messages[-1][1]["can_control"] is True
        assert "can_control" not in api._ha_softphone_store(hass, "kitchen")
    finally:
        for connection in connections:
            for unsubscribe in connection.subscriptions.values():
                unsubscribe()


async def test_idle_account_never_inherits_another_trunks_sip_outcome(hass, phones, monkeypatch):
    from custom_components.voip_stack import websocket_api as api

    original = api._sip_runtime_snapshot

    def foreign_diagnostics(hass, **kwargs):
        return {**original(hass, **kwargs), "last_sip_status_code": 503,
                "last_sip_event": "foreign_trunk_failed", "last_sip_reason": "Service Unavailable"}

    monkeypatch.setattr(api, "_sip_runtime_snapshot", foreign_diagnostics)
    state = api._ha_softphone_state(hass, "kitchen")
    assert state["sip_status_code"] == ""
    assert state["last_sip_event"] == ""
    assert state["last_sip_reason"] == ""
    api._ha_softphone_store(hass, "kitchen").update(sip_status_code=486,
        last_sip_event="account_response", last_sip_reason="Busy Here")
    state = api._ha_softphone_state(hass, "kitchen")
    assert (state["sip_status_code"], state["last_sip_event"], state["last_sip_reason"]) == (486, "account_response", "Busy Here")


@pytest.mark.parametrize("revoked_entity", ["account", "shared"])
@pytest.mark.parametrize("producer", ["state_event", "projection"])
@pytest.mark.parametrize("notice", ["user", "entity", "none"])
async def test_account_subscription_retracts_revoked_read_access(hass, phones, producer, notice, revoked_entity):
    from homeassistant.auth import EVENT_USER_UPDATED
    from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
    from custom_components.voip_stack import websocket_api as api
    from custom_components.voip_stack.entity_identity import shared_entity_ids
    from custom_components.voip_stack.phone_state import phone_state_observer

    phones.endpoints.update("kitchen", entity_ids={"sensor.account_kitchen"})
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    shared = shared_entity_ids(hass)["call_event"]
    readable = {shared, "sensor.account_kitchen"}
    messages = []
    connection = SimpleNamespace(
        user=SimpleNamespace(id="account-user", is_active=True,
            permissions=SimpleNamespace(check_entity=lambda entity, policy: entity in readable)),
        subscriptions={}, send_result=lambda identity: None,
        send_event=lambda identity, state: messages.append(deepcopy(state)),
    )
    api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": 4, "endpoint_id": "kitchen"})
    assert phones.runtime.softphone_presence["kitchen"] == 1
    readable.remove("sensor.account_kitchen" if revoked_entity == "account" else shared)
    if notice == "user":
        hass.bus.async_fire(EVENT_USER_UPDATED, {"user_id": connection.user.id})
    elif notice == "entity":
        hass.bus.async_fire(EVENT_ENTITY_REGISTRY_UPDATED,
            {"action": "update", "entity_id": "sensor.account_kitchen"})
    await hass.async_block_till_done()
    if producer == "state_event":
        hass.bus.async_fire(api.HA_SOFTPHONE_STATE_EVENT,
            {"endpoint_id": "kitchen", "state": "ringing", "call_id": "private-call", "caller": "private-number"})
    else:
        with phones.calls.state_operation():
            call = phones.calls.upsert("private-call", state="ringing", owner="router",
                caller="private-number", dest_endpoint_id="kitchen")
            phones.calls.claim_endpoint(call.call_id, "kitchen", role="destination")
    await hass.async_block_till_done()
    try:
        assert messages[-1].get("kind") == "closed"
        assert messages[-1]["reason"] == "permissions_revoked"
        assert not any(item.get("call_id") == "private-call" for item in messages)
        assert not phones.runtime.softphone_presence.get("kitchen")
        observer = phone_state_observer(hass)
        assert not observer.listeners and not observer.projection_listeners
        count = len(messages)
        phones.endpoints.update("kitchen", dnd=True)
        await hass.async_block_till_done()
        assert len(messages) == count
    finally:
        connection.subscriptions[4]()
        connection.subscriptions[4]()


@pytest.mark.parametrize("action", ["control_only", "removed", "unload"])
async def test_account_subscription_access_lifecycle(hass, phones, action):
    from homeassistant.auth import EVENT_USER_REMOVED, EVENT_USER_UPDATED
    from custom_components.voip_stack import websocket_api as api
    from custom_components.voip_stack.entity_identity import shared_entity_ids
    from custom_components.voip_stack.phone_state import phone_state_observer

    phones.endpoints.update("kitchen", entity_ids={"sensor.account_kitchen"})
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    shared = shared_entity_ids(hass)["call_event"]
    policies = {"read", "control"}
    messages = []
    connection = SimpleNamespace(
        user=SimpleNamespace(id="account-user", is_active=True,
            permissions=SimpleNamespace(check_entity=lambda entity, policy:
                entity in {shared, "sensor.account_kitchen"} and policy in policies)),
        subscriptions={}, send_result=lambda identity: None,
        send_event=lambda identity, state: messages.append(deepcopy(state)),
    )
    api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": 4, "endpoint_id": "kitchen"})
    # Closing a view never releases someone else's live phone claim.
    call = phones.calls.upsert("other-owner", state="in_call", owner="router", dest_endpoint_id="kitchen")
    phones.calls.claim_endpoint(call.call_id, "kitchen", role="destination")
    if action == "control_only":
        policies.remove("control")
        hass.bus.async_fire(EVENT_USER_UPDATED, {"user_id": "account-user"})
    elif action == "removed":
        hass.bus.async_fire(EVENT_USER_REMOVED, {"user_id": "account-user"})
    else:
        await phone_state_observer(hass).close()
    await hass.async_block_till_done()
    if action == "control_only":
        assert messages[-1]["can_control"] is False
        assert messages[-1]["call_id"] == "other-owner"
    else:
        assert messages[-1]["kind"] == "closed"
        assert messages[-1]["reason"] == ("runtime_unloaded" if action == "unload" else "permissions_revoked")
    assert phones.calls.get_session("other-owner") is call
    assert phones.endpoints.get("kitchen").active_call_id == "other-owner"
    assert not phones.runtime.softphone_presence.get("kitchen")
    connection.subscriptions[4]()
    connection.subscriptions[4]()
