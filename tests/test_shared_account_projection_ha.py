"""Committed browser-leg ownership changes reach the existing account stream."""

from copy import deepcopy

import pytest

from custom_components.voip_stack import websocket_api
from custom_components.voip_stack.browser_phone_runtime import (
    _set_ha_softphone_call_state,
)
from custom_components.voip_stack.phone_endpoint import EndpointKind
from custom_components.voip_stack.phone_state import phone_state_observer, phone_states
from tests.test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


class Connection:
    def __init__(self, user):
        self.user = user
        self.events = []
        self.subscriptions = {}

    def send_event(self, identity, payload):
        self.events.append(deepcopy(payload))

    def send_result(self, identity):
        pass


@pytest.mark.parametrize("kind", [EndpointKind.BROWSER, EndpointKind.SIP_ACCOUNT])
async def test_duration_projects_the_call_clock_instead_of_a_stale_browser_cache(
    hass, phones, kind
):
    phones.endpoints.update("kitchen", kind=kind)
    with phones.calls.state_operation():
        session = phones.calls.upsert(
            "duration-owner", state="in_call", owner="ha_softphone",
            caller="Office", callee="Kitchen", dest_endpoint_id="kitchen",
        )
        phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
        phones.calls.observe_leg(session.call_id, "browser:kitchen", role="ha_softphone",
            endpoint_id="kitchen", state="in_call")
    websocket_api._ha_softphone_store(hass, "kitchen").update(
        call_id=session.call_id, state="in_call", connected_at=1.0,
    )
    state = websocket_api._ha_softphone_state(hass, "kitchen")
    assert state["connected_at"] == session.event_context.connected_at_epoch
    assert state["connected_at"] > 1.0
    assert state["can_attach_media"]
    await phones.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
    _set_ha_softphone_call_state(
        hass, "idle", endpoint_id="kitchen", call_id=session.call_id,
    )
    assert websocket_api._ha_softphone_state(hass, "kitchen")["connected_at"] == 0


async def test_closing_losing_browser_updates_stream_without_republishing_sip_dialog(
    hass, phones, hass_admin_user
):
    calls = phones.calls
    from homeassistant.helpers import entity_registry as er

    er.async_get(hass).async_get_or_create(
        "event", "voip_stack", "voip_stack_call_event"
    )
    entity = er.async_get(hass).async_get_or_create(
        "sensor", "voip_stack", "kitchen_access"
    )
    phones.endpoints.update(
        "kitchen", kind=EndpointKind.SIP_ACCOUNT, entity_ids={entity.entity_id}
    )
    connection = Connection(hass_admin_user)
    websocket_api.websocket_subscribe_ha_softphone_state(
        hass, connection, {"id": 1, "endpoint_id": "kitchen"}
    )
    observer = phone_state_observer(hass)
    dialog_updates = []
    detach_dialog = observer.subscribe(dialog_updates.append, kind="dialog")
    try:
        with calls.state_operation():
            session = calls.upsert(
                "shared",
                state="ringing",
                owner="router",
                caller="Office",
                callee="Kitchen",
                source_endpoint_id="office",
                dest_endpoint_id="kitchen",
            )
            calls.claim_endpoint(session.call_id, "kitchen", role="destination")
            calls.observe_leg(
                session.call_id,
                "browser:kitchen",
                role="ha_softphone",
                endpoint_id="kitchen",
                state="ringing",
            )
        _set_ha_softphone_call_state(
            hass,
            "ringing",
            endpoint_id="kitchen",
            call_id=session.call_id,
            caller="Office",
            callee="Kitchen",
            direction="incoming",
        )
        calls.observe_leg(
            session.call_id,
            "independent-sip",
            role="callee",
            endpoint_id="kitchen",
            state="in_call",
        )
        await hass.async_block_till_done()
        before = phone_states(hass)["kitchen"]
        assert before["state"] == "in_call"
        assert connection.events[-1]["state"] == "ringing"
        assert connection.events[-1]["can_attach_media"]
        dialog_count = len(dialog_updates)
        calls.observe_leg(
            session.call_id,
            "browser:kitchen",
            role="ha_softphone",
            endpoint_id="kitchen",
            state="cancelled",
        )
        await hass.async_block_till_done()
        assert phone_states(hass)["kitchen"] == before
        assert len(dialog_updates) == dialog_count, (
            "HA media ownership is not a changed SIP dialog"
        )
        assert connection.events[-1]["state"] == "in_call"
        assert not connection.events[-1]["can_attach_media"]
        assert connection.events[-1]["sequence"] == session.event_context.sequence
        assert connection.events[-1]["revision"] == session.revision
        previous = deepcopy(connection.events)
        observer.refresh()
        assert connection.events == previous, (
            "Unchanged snapshots must remain suppressed"
        )
        from custom_components.voip_stack.browser_phone_runtime import (
            _publish_ha_softphone_state,
        )

        _publish_ha_softphone_state(hass, endpoint_id="kitchen")
        await hass.async_block_till_done()
        assert connection.events == previous, (
            "Identical HA cache publication must remain suppressed"
        )
    finally:
        connection.subscriptions[1]()
        detach_dialog()


async def test_terminal_commit_replay_preserves_native_idle_scope_and_successor(
    hass, phones, hass_admin_user
):
    from homeassistant.helpers import entity_registry as er

    er.async_get(hass).async_get_or_create(
        "event", "voip_stack", "voip_stack_call_event"
    )
    entity = er.async_get(hass).async_get_or_create(
        "sensor", "voip_stack", "native_kitchen_access"
    )
    phones.endpoints.update(
        "kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen",
        entity_ids={entity.entity_id},
    )
    connection = Connection(hass_admin_user)
    websocket_api.websocket_subscribe_ha_softphone_state(
        hass, connection, {"id": 2, "endpoint_id": "kitchen"}
    )
    try:
        calls = phones.calls
        with calls.state_operation():
            session = calls.upsert(
                "native-terminal-A", state="in_call", owner="bridge",
                caller="Kitchen", callee="Office", source_endpoint_id="kitchen",
                dest_endpoint_id="office",
            )
            calls.claim_endpoint(session.call_id, "kitchen", role="source")
            calls.observe_leg(session.call_id, "caller-A", role="caller",
                state="in_call", endpoint_id="kitchen")
        await hass.async_block_till_done()
        assert connection.events[-1]["connected_at"] > 0
        await calls.terminate_call_wait(session.call_id, reason="remote_hangup")
        await hass.async_block_till_done()
        terminal = session.committed_state
        assert terminal.phase == "terminated"
        assert connection.events[-1]["state"] == "idle"
        assert connection.events[-1]["call_id"] == session.call_id
        assert connection.events[-1]["generation"] == session.generation
        assert connection.events[-1]["connected_at"] == 0
        ended_events = deepcopy(connection.events)
        observer = phone_state_observer(hass)
        observer._committed(terminal)
        await hass.async_block_till_done()
        assert connection.events == ended_events
        assert not calls.sessions and not calls.endpoint_claims_snapshot()

        with calls.state_operation():
            successor = calls.upsert(
                "native-current-B", state="in_call", owner="bridge",
                caller="Kitchen", callee="Office", source_endpoint_id="kitchen",
                dest_endpoint_id="office",
            )
            calls.claim_endpoint(successor.call_id, "kitchen", role="source")
            calls.observe_leg(successor.call_id, "caller-B", role="caller",
                state="in_call", endpoint_id="kitchen")
        await hass.async_block_till_done()
        successor_events = deepcopy(connection.events)
        observer._committed(terminal)
        await hass.async_block_till_done()
        assert connection.events == successor_events
        assert connection.events[-1]["state"] == "in_call"
        assert connection.events[-1]["call_id"] == successor.call_id
        assert connection.events[-1]["connected_at"] == successor.event_context.connected_at_epoch
    finally:
        connection.subscriptions[2]()
