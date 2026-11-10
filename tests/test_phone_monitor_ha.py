"""Passive phone-monitor subscriptions over the shared authoritative projection."""

from copy import deepcopy
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from homeassistant.auth import EVENT_USER_REMOVED, EVENT_USER_UPDATED
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED

from custom_components.voip_stack import phone_state, websocket_api
from custom_components.voip_stack.dialog_info import document, snapshot
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.phone_state import phone_state_observer, phone_states

from .test_ha_call_transfer import _established
from . import test_phone_state_ha

phones = test_phone_state_ha.phones

pytestmark = pytest.mark.ha


class Permissions:
    def __init__(self, entities):
        self.entities = set(entities)

    def check_entity(self, entity_id, policy):
        return policy == "read" and entity_id in self.entities


class Connection:
    def __init__(self, entities):
        self.user = SimpleNamespace(
            id="monitor-user", is_active=True, permissions=Permissions(entities)
        )
        self.subscriptions, self.events, self.results = {}, [], []

    def send_result(self, msg_id):
        self.results.append(msg_id)

    def send_event(self, msg_id, value):
        self.events.append((msg_id, deepcopy(value)))


@pytest.fixture
async def monitor(hass, phones):
    for identity in ("kitchen", "office"):
        phones.endpoints.update(identity, entity_ids={f"sensor.{identity}_monitor"})
    phones.endpoints.register(PhoneEndpoint(
        endpoint_id="other", name="Other", kind="esphome", device_id="other-device",
        availability="available", entity_ids={"sensor.other_monitor"},
    ))
    return phones


def subscribe(hass, *entities, msg_id=20, connection=None):
    connection = connection or Connection(entities)
    websocket_api.websocket_subscribe_phone_states(hass, connection, {"id": msg_id})
    return connection


def payload(connection):
    return connection.events[-1][1]


def by_id(connection):
    return {phone["endpoint_id"]: phone for phone in payload(connection)["phones"]}


def incoming(monitor, call_id="incoming"):
    with monitor.calls.state_operation():
        call = monitor.calls.upsert(
            call_id, state="ringing", owner="router", caller="Office", callee="Kitchen",
            source_endpoint_id="office", dest_endpoint_id="kitchen",
        )
        monitor.calls.claim_endpoint(call.call_id, "kitchen", role="destination")
        monitor.calls.claim_endpoint(call.call_id, "office", role="source")
    return call


async def test_snapshot_schema_and_subscription_are_passive(hass, monitor, monkeypatch):
    monitor.endpoints.update("kitchen", availability="offline")
    monkeypatch.setattr(websocket_api, "_update_browser_presence", lambda *_: pytest.fail("monitor changed presence"))
    connection = subscribe(hass, "sensor.kitchen_monitor", "sensor.office_monitor")
    assert connection.results == [20]
    assert payload(connection)["kind"] == "snapshot"
    assert payload(connection)["revision"] == 1
    assert set(by_id(connection)) == {"kitchen", "office"}
    kitchen = by_id(connection)["kitchen"]
    assert set(kitchen) == {
        "schema_version", "endpoint_id", "device_id", "name", "extension", "kind",
        "availability", "dnd", "state", "calls", "mailbox",
        "ring_group", "queues", "queue_states",
    }
    assert kitchen["ring_group"] == ""
    assert kitchen["queues"] == []
    assert kitchen["queue_states"] == []
    assert kitchen["availability"] == "offline" and kitchen["state"] == "unavailable"
    assert kitchen["mailbox"] == {"enabled": False, "waiting": False, "new": 0, "old": 0}
    assert not monitor.calls.sessions and not monitor.runtime.softphone_presence
    assert monitor.calls.component("media_archive") is None
    assert not monitor.runtime.media.owners_for("audio")
    connection.subscriptions.pop(20)()
    assert not phone_state_observer(hass).unsubscribers


async def test_metadata_delta_contains_only_changed_phone(hass, monitor):
    connection = subscribe(hass, "sensor.kitchen_monitor", "sensor.office_monitor")
    monitor.endpoints.update("office", name="Workroom", dnd=True, availability="offline")
    await hass.async_block_till_done()
    event = payload(connection)
    assert event["kind"] == "delta" and event["revision"] == 2 and event["removed"] == []
    assert list(by_id(connection)) == ["office"]
    assert by_id(connection)["office"]["name"] == "Workroom"
    assert by_id(connection)["office"]["dnd"] is True
    count = len(connection.events)
    phone_state_observer(hass).refresh(dialogs=phone_state_observer(hass).dialogs)
    assert len(connection.events) == count


async def test_calls_use_canonical_ids_and_redact_unreadable_peer(hass, monitor):
    connection = subscribe(hass, "sensor.kitchen_monitor")
    call = incoming(monitor)
    await hass.async_block_till_done()
    kitchen = by_id(connection)["kitchen"]
    assert kitchen["state"] == "ringing"
    observed = kitchen["calls"][0]
    assert set(observed) == {
        "id", "generation", "leg_id", "direction", "state", "held",
        "peer_endpoint_id", "peer_name", "pickup_available",
    }
    assert observed["id"] == call.call_id and observed["generation"] == call.generation
    assert observed["direction"] == "incoming" and observed["state"] == "early"
    assert observed["peer_endpoint_id"] == "" and observed["peer_name"] == "Office"
    assert observed["pickup_available"] is True
    legacy = phone_states(hass)["kitchen"]["calls"][0]
    assert "sip_call_id" in legacy and "local_tag" in legacy and "remote_uri" in legacy
    assert "sip_call_id" not in observed and "remote_uri" not in observed
    blf = ElementTree.fromstring(document("sip:101@127.0.0.1", 1, snapshot(monitor.runtime, "kitchen")))
    ns = {"d": "urn:ietf:params:xml:ns:dialog-info"}
    assert blf.find("d:dialog/d:state", ns).text == observed["state"]


async def test_endpoint_removal_is_explicit_and_disconnect_is_idempotent(hass, monitor):
    connection = subscribe(hass, "sensor.kitchen_monitor", "sensor.office_monitor")
    monitor.endpoints.remove("office")
    await hass.async_block_till_done()
    assert payload(connection) == {"kind": "delta", "revision": 2, "phones": [], "removed": ["office"]}
    detach = connection.subscriptions.pop(20)
    detach()
    detach()
    assert not connection.subscriptions
    observer = phone_state_observer(hass)
    assert not observer.projection_listeners and not observer.access_unsubscribers and not observer.unsubscribers
    previous = len(connection.events)
    monitor.endpoints.update("kitchen", dnd=True)
    assert len(connection.events) == previous


async def test_multiple_subscribers_share_one_projection_and_batch(hass, monitor, monkeypatch):
    scans = []
    original = phone_state.snapshot_all
    monkeypatch.setattr(phone_state, "snapshot_all", lambda runtime: (scans.append(True), original(runtime))[1])
    one = subscribe(hass, "sensor.kitchen_monitor", "sensor.office_monitor")
    two = subscribe(hass, "sensor.kitchen_monitor", "sensor.office_monitor", msg_id=21)
    assert len(scans) == 1
    scans.clear()
    incoming(monitor)
    await hass.async_block_till_done()
    assert len(scans) == 1
    assert len(one.events) == len(two.events) == 2
    assert set(by_id(one)) == {"kitchen", "office"}
    assert payload(one) == payload(two)
    one.subscriptions.pop(20)()
    assert len(phone_state_observer(hass).projection_listeners) == 1
    two.subscriptions.pop(21)()
    assert not phone_state_observer(hass).unsubscribers


async def test_permissions_revoke_and_grant_from_cached_state(hass, monitor, monkeypatch):
    connection = subscribe(hass, "sensor.kitchen_monitor", "sensor.office_monitor")
    incoming(monitor)
    await hass.async_block_till_done()
    assert by_id(connection)["kitchen"]["calls"][0]["peer_endpoint_id"] == "office"
    monkeypatch.setattr(phone_state, "snapshot_all", lambda *_: pytest.fail("permission refresh scanned calls"))
    connection.user.permissions.entities.remove("sensor.office_monitor")
    hass.bus.async_fire(EVENT_USER_UPDATED, {"user_id": connection.user.id})
    await hass.async_block_till_done()
    assert payload(connection)["removed"] == ["office"]
    assert by_id(connection)["kitchen"]["calls"][0]["peer_endpoint_id"] == ""
    assert by_id(connection)["kitchen"]["calls"][0]["peer_name"] == "Office"
    connection.user.permissions.entities.add("sensor.office_monitor")
    hass.bus.async_fire(EVENT_USER_UPDATED, {"user_id": connection.user.id})
    await hass.async_block_till_done()
    assert set(by_id(connection)) == {"kitchen", "office"}
    assert payload(connection)["revision"] == 4


@pytest.mark.parametrize("event_type", [EVENT_USER_REMOVED, EVENT_USER_UPDATED])
async def test_user_revocation_clears_rows_and_releases_subscription(hass, monitor, event_type):
    connection = subscribe(hass, "sensor.kitchen_monitor")
    if event_type == EVENT_USER_UPDATED:
        connection.user.is_active = False
    hass.bus.async_fire(event_type, {"user_id": connection.user.id})
    await hass.async_block_till_done()
    assert payload(connection) == {"kind": "closed", "reason": "permissions_revoked"}
    assert not phone_state_observer(hass).projection_listeners
    # Revocation releases the observer; HA retains ownership of its callback slot.
    assert connection.subscriptions[20].__self__.closed
    connection.subscriptions.pop(20)()


async def test_endpoint_permission_identity_changes_revoke_without_public_state_change(hass, monitor):
    connection = subscribe(hass, "sensor.kitchen_monitor")
    monitor.endpoints.update("kitchen", entity_ids={"sensor.restricted_kitchen"})
    await hass.async_block_till_done()
    assert payload(connection)["removed"] == ["kitchen"]


async def test_entity_permission_event_rechecks_and_unreadable_updates_stay_hidden(hass, monitor):
    connection = subscribe(hass, "sensor.kitchen_monitor", "sensor.office_monitor")
    connection.user.permissions.entities.remove("sensor.office_monitor")
    hass.bus.async_fire(EVENT_ENTITY_REGISTRY_UPDATED, {"action": "update", "entity_id": "sensor.office_monitor"})
    await hass.async_block_till_done()
    assert payload(connection)["removed"] == ["office"]
    count = len(connection.events)
    monitor.endpoints.update("office", dnd=True)
    await hass.async_block_till_done()
    assert len(connection.events) == count


async def test_shared_owner_close_retracts_monitor_data(hass, monitor):
    connection = subscribe(hass, "sensor.kitchen_monitor")
    observer = phone_state_observer(hass)
    await observer.close()
    assert payload(connection) == {"kind": "closed", "reason": "runtime_unloaded"}
    assert not observer.projection_listeners
    assert connection.subscriptions[20].__self__.closed
    connection.subscriptions.pop(20)()
    assert not observer.unsubscribers and not observer.access_unsubscribers


async def test_nonadmin_runtime_ready_uses_the_integration_transport(
    hass, monitor, hass_ws_client, hass_read_only_access_token, hass_read_only_user,
):
    """Exercise HA's real WS command dispatcher and its generic-event denial."""
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    assert not hass_read_only_user.is_admin
    websocket_api.async_register_websocket_api(hass)
    await client.send_json({"id": 1, "type": "subscribe_events", "event_type": "voip_stack.runtime_ready"})
    denied = await client.receive_json()
    assert denied["success"] is False and denied["error"]["code"] == "unauthorized"
    await client.send_json({"id": 2, "type": "voip_stack/subscribe_call_events",
                            "include_runtime_ready": True})
    accepted = await client.receive_json()
    assert accepted["success"] is True
    hass.bus.async_fire("voip_stack.runtime_ready")
    ready = await client.receive_json()
    assert ready == {"id": 2, "type": "event", "event": {"event_type": "voip_stack.runtime_ready", "data": {}}}


async def test_runtime_transport_never_grants_or_retains_call_permissions(hass, monitor):
    from homeassistant.helpers import entity_registry as er
    entry = next(iter(hass.config_entries.async_entries("voip_stack")))
    entity = er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event", config_entry=entry)
    connection = Connection({entity.entity_id})
    websocket_api.websocket_subscribe_call_events(hass, connection, {
        "id": 30, "include_activity": True, "include_runtime_ready": True,
    })
    observer = phone_state_observer(hass)
    assert len(observer.activity_listeners) == 1
    connection.events.clear()
    connection.user.permissions.entities.clear()
    incoming(monitor)
    hass.bus.async_fire(websocket_api.CALL_EVENT, {"private": "unreadable call"})
    hass.bus.async_fire("voip_stack.runtime_ready", {"private": "must not be forwarded"})
    await hass.async_block_till_done()
    assert connection.events == [(30, {"event_type": "voip_stack.runtime_ready", "data": {}})]
    assert not observer.activity_listeners
    connection.user.is_active = False
    hass.bus.async_fire("voip_stack.runtime_ready")
    await hass.async_block_till_done()
    assert len(connection.events) == 1
    connection.subscriptions.pop(30)()


async def test_entry_reload_announces_ready_after_new_runtime_can_snapshot(
    hass, monkeypatch, hass_ws_client, hass_read_only_access_token, hass_read_only_user,
):
    """Actual setup/unload and command authorization on one non-admin socket."""
    from unittest.mock import AsyncMock
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components import voip_stack
    from custom_components.voip_stack import phone_services
    from custom_components.voip_stack.endpoint_lifecycle import call_registry

    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    hass.data["voip_stack"] = {}
    monkeypatch.setattr(voip_stack, "_async_setup_shared", AsyncMock())
    monkeypatch.setattr(voip_stack, "_async_start_sip_trunk", AsyncMock())
    monkeypatch.setattr(voip_stack, "_deferred_phonebook_sync", AsyncMock())
    monkeypatch.setattr(phone_services, "async_setup_phone_services", AsyncMock())
    async def forward_platforms(entry, platforms):
        # Give the platform boundary its normal endpoint permission identity.
        for endpoint in entry.runtime_data.endpoints.endpoints:
            entry.runtime_data.endpoints.update(endpoint.endpoint_id, entity_ids={"sensor.reload_phone"})

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward_platforms)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True))

    async def start_transport(hass):
        call_registry(hass).activate()
        return True

    monkeypatch.setattr(voip_stack, "_async_start_sip_endpoint", start_transport)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    assert not hass_read_only_user.is_admin
    websocket_api.async_register_websocket_api(hass)
    try:
        assert await voip_stack.async_setup_entry(hass, entry)
        await hass.async_block_till_done(wait_background_tasks=True)
        first_runtime = entry.runtime_data.sip
        await client.send_json({"id": 1, "type": "voip_stack/subscribe_call_events",
                                "include_activity": True, "include_runtime_ready": True})
        assert (await client.receive_json())["success"] is True
        await client.send_json({"id": 2, "type": "voip_stack/subscribe_phone_states"})
        assert (await client.receive_json())["success"] is True
        initial = (await client.receive_json())["event"]
        assert initial["kind"] == "snapshot" and initial["revision"] == 1
        assert len(initial["phones"]) == 1
        phone = initial["phones"][0]
        assert phone["state"] == "idle" and phone["availability"] == "available"
        assert not entry.runtime_data.softphone_presence
        assert await voip_stack.async_unload_entry(hass, entry)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert (await client.receive_json())["event"] == {"kind": "closed", "reason": "runtime_unloaded"}
        assert await voip_stack.async_setup_entry(hass, entry)
        await hass.async_block_till_done(wait_background_tasks=True)
        ready = await client.receive_json()
        assert ready == {"id": 1, "type": "event", "event": {"event_type": "voip_stack.runtime_ready", "data": {}}}
        assert entry.runtime_data.sip is not first_runtime and not client.closed
        await client.send_json({"id": 3, "type": "voip_stack/subscribe_phone_states"})
        assert (await client.receive_json())["success"] is True
        new_snapshot = (await client.receive_json())["event"]
        assert new_snapshot["revision"] == 1
        restored, = new_snapshot["phones"]
        assert (restored["endpoint_id"], restored["device_id"], restored["state"]) == (
            phone["endpoint_id"], phone["device_id"], "idle")
        assert not entry.runtime_data.softphone_presence
        observer = phone_state_observer(hass)
        assert len(observer.projection_listeners) == 1
        # Endpoint-only readers do not inherit global call or activity access.
        assert not observer.activity_listeners
        hass.bus.async_fire(websocket_api.CALL_EVENT, {"private": "unreadable call"})
        await hass.async_block_till_done()
        await client.send_json({"id": 4, "type": "ping"})
        assert await client.receive_json() == {"id": 4, "type": "pong"}
        await hass.auth.async_update_user(hass_read_only_user, group_ids=[])
        await hass.async_block_till_done()
        revoked = (await client.receive_json())["event"]
        assert revoked["kind"] == "delta" and revoked["phones"] == []
        assert revoked["removed"] == [phone["endpoint_id"]]
        hass.bus.async_fire("voip_stack.runtime_ready")
        await hass.async_block_till_done()
        assert (await client.receive_json())["event"] == {"event_type": "voip_stack.runtime_ready", "data": {}}
        await client.send_json({"id": 5, "type": "unsubscribe_events", "subscription": 3})
        assert (await client.receive_json())["success"] is True
        await client.send_json({"id": 6, "type": "voip_stack/subscribe_phone_states"})
        assert (await client.receive_json())["success"] is True
        assert (await client.receive_json())["event"] == {"kind": "snapshot", "revision": 1, "phones": []}
    finally:
        await client.close()
        await hass.async_block_till_done()
        if entry.runtime_data.sip is not None:
            await voip_stack.async_unload_entry(hass, entry)


async def test_multi_call_and_hold_preserve_the_blf_projection(hass, monitor):
    monitor.endpoints.update("office", kind="esphome")
    connection = subscribe(hass, "sensor.office_monitor", "sensor.kitchen_monitor")
    first = monitor.calls.upsert("held-call", state="in_call", owner="bridge", caller="Kitchen", callee="Office", dest_endpoint_id="office")
    client = _established(first.call_id, "private-tag", "office")
    client.dialog.remote_audio_direction = "sendonly"
    monitor.calls.observe_leg(first.call_id, first.call_id, role="callee", state="in_call", endpoint_id="office", dialog=client)
    second = monitor.calls.upsert("consultation", state="in_call", owner="router", caller="Office", callee="Other", source_endpoint_id="office")
    monitor.calls.claim_endpoint(second.call_id, "office", role="source")
    await hass.async_block_till_done()
    office = by_id(connection)["office"]
    assert len(office["calls"]) == 2 and office["state"] == phone_states(hass)["office"]["state"]
    calls = {value["id"]: value for value in office["calls"]}
    assert calls[first.call_id]["held"] is True and calls[first.call_id]["direction"] == "incoming"
    assert calls[second.call_id]["held"] is False and calls[second.call_id]["direction"] == "outgoing"
    assert [value["state"] for value in office["calls"]] == [value.state for value in snapshot(monitor.runtime, "office")]
    assert "private-tag" not in str(office) and "sip:office@" not in str(office)


async def test_new_subscription_starts_with_fresh_snapshot_revision(hass, monitor):
    one = subscribe(hass, "sensor.kitchen_monitor")
    monitor.endpoints.update("kitchen", dnd=True)
    await hass.async_block_till_done()
    assert payload(one)["revision"] == 2
    one.subscriptions.pop(20)()
    two = subscribe(hass, "sensor.kitchen_monitor", msg_id=21)
    assert payload(two)["kind"] == "snapshot" and payload(two)["revision"] == 1
    assert by_id(two)["kitchen"]["dnd"] is True


async def test_inactive_or_missing_user_cannot_start_projection(hass, monitor):
    connection = Connection({"sensor.kitchen_monitor"})
    connection.user.is_active = False
    with pytest.raises(Unauthorized):
        subscribe(hass, connection=connection)
    assert not connection.subscriptions and not connection.events


async def test_real_HA_connection_close_owns_the_subscription_dictionary(hass, monitor):
    """Execute HA's real values()-iteration, rather than copying its entries."""
    import logging
    from homeassistant.components.websocket_api.connection import ActiveConnection

    connection = subscribe(hass, "sensor.kitchen_monitor")
    subscribe(hass, msg_id=21, connection=connection)
    remaining = []
    connection.subscriptions[22] = lambda: remaining.append("other subscription")
    connection.logger = logging.getLogger(__name__)
    connection._connect_closed_error = lambda *_: None
    ActiveConnection.async_handle_close(connection)
    assert not connection.subscriptions
    assert remaining == ["other subscription"]
    observer = phone_state_observer(hass)
    assert not observer.projection_listeners and not observer.unsubscribers
    assert not observer.access_unsubscribers
