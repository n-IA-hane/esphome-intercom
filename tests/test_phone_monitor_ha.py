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
    }
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
    assert payload(connection) == {"kind": "delta", "revision": 2, "phones": [], "removed": ["kitchen"]}
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
    assert payload(connection)["removed"] == ["kitchen"]
    assert not observer.projection_listeners
    assert connection.subscriptions[20].__self__.closed
    connection.subscriptions.pop(20)()
    assert not observer.unsubscribers and not observer.access_unsubscribers


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
