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
