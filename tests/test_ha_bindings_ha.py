"""Configured HA clients remain declared without cards or media connections."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.lovelace import LovelaceData
from homeassistant.components.lovelace.const import LOVELACE_DATA

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


def runtime_for_bindings():
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind

    endpoints = EndpointRegistry()
    for identity in ("reception", "closed"):
        endpoints.register(PhoneEndpoint(endpoint_id=identity, name=identity, device_id="device-" + identity,
            kind=EndpointKind.SIP_ACCOUNT, username=identity))
    return SimpleNamespace(endpoints=endpoints, ha_bindings=None)


def dashboard(hass, config):
    hass.config.components.add("lovelace")
    export = SimpleNamespace(async_load=AsyncMock(return_value=config))
    hass.data[LOVELACE_DATA] = LovelaceData("storage", {None: export}, None, {})
    return export


async def owner(hass, runtime, entry=None):
    from custom_components.voip_stack.ha_bindings import HaCardBindings
    changed = []
    entry = entry or SimpleNamespace(entry_id="binding-test", subentries={})
    tracker = HaCardBindings(hass, entry, runtime, changed.append)
    runtime.ha_bindings = tracker
    await tracker.async_start()
    return tracker, changed


async def test_saved_hidden_dashboard_declares_account_without_any_client(hass):
    runtime = runtime_for_bindings()
    export = dashboard(hass, {"views": [{"visible": False, "sections": [{"cards": [
        {"type": "conditional", "conditions": [], "card": {"type": "custom:voip-stack-card",
            "mode": "ha_softphone", "device_id": "device-closed"}},
        {"type": "custom:voip-stack-dashboard-card", "device_id": "device-reception"},
        {"type": "custom:voip-stack-card", "mode": "esp_mirror", "device_id": "device-reception"},
    ]}]}]})
    tracker, changes = await owner(hass, runtime)
    try:
        assert tracker.has_binding("closed") and tracker.has_binding("reception")
        assert len(tracker.references("reception")) == 1
        assert tracker.references("closed")[0]["source"] == "dashboard"
        assert changes and set.union(*(set(change) for change in changes)) == {"closed", "reception"}
        export.async_load.return_value = {"views": []}
        hass.bus.async_fire("lovelace_updated", {"url_path": None})
        await hass.async_block_till_done()
        assert not tracker.has_binding("closed") and not tracker.has_binding("reception")
    finally:
        await tracker.async_close()


async def test_unavailable_export_keeps_last_good_declaration_across_owner_restart(hass):
    runtime = runtime_for_bindings()
    export = dashboard(hass, {"views": [{"cards": [{"type": "custom:voip-stack-card",
        "mode": "ha_softphone", "device_id": "device-closed"}]}]})
    tracker, _ = await owner(hass, runtime)
    assert tracker.has_binding("closed")
    await tracker.async_close()
    export.async_load.side_effect = OSError("dashboard unavailable")
    tracker, _ = await owner(hass, runtime)
    try:
        assert tracker.has_binding("closed")
        export.async_load.side_effect = None
        export.async_load.return_value = {"views": []}
        await tracker.async_reload()
        assert not tracker.has_binding("closed")
        export.async_load.assert_awaited_with(True)
    finally:
        await tracker.async_close()


async def test_popup_binding_is_server_persistent_scoped_and_explicit(hass):
    runtime = runtime_for_bindings()
    tracker, _ = await owner(hass, runtime)
    try:
        assert not tracker.has_binding("closed")
        await tracker.async_set_popup_binding("user-a", "browser-1234567890abcdef", "closed", True)
        assert tracker.has_binding("closed")
        assert tracker.popup_binding("user-b", "browser-1234567890abcdef") is None
        await tracker.async_close()
        tracker, _ = await owner(hass, runtime)
        assert tracker.has_binding("closed")
        await tracker.async_set_popup_binding("user-a", "browser-1234567890abcdef", "closed", False)
        assert not tracker.has_binding("closed")
        assert tracker.popup_binding("user-a", "browser-1234567890abcdef")["enabled"] is False
        with pytest.raises(ValueError):
            await tracker.async_set_popup_binding("user-a", "browser-1234567890abcdef", "unknown", True)
        assert not tracker.has_binding("closed")
    finally:
        await tracker.async_close()


async def test_migration_reference_is_declaration_not_an_observed_client(hass):
    runtime = runtime_for_bindings()
    subentry = SimpleNamespace(subentry_type="phone", data={"endpoint_id": "closed", "ha_bindings": [
        {"source": "migration", "reference": "legacy_ha_phone:closed"}]})
    entry = SimpleNamespace(entry_id="binding-test", subentries={"closed": subentry})
    tracker, _ = await owner(hass, runtime, entry)
    try:
        assert tracker.has_binding("closed")
        assert tracker.references("closed")[0]["reference"] == "legacy_ha_phone:closed"
        subentry.data["ha_bindings"] = []
        await tracker.async_reload()
        assert not tracker.has_binding("closed")
        hass.bus.async_fire("voip_stack.ha_softphone_state", {"endpoint_id": "closed", "state": "idle"})
        await hass.async_block_till_done()
        assert not tracker.has_binding("closed")
    finally:
        await tracker.async_close()


async def test_account_subscription_sends_snapshot_after_ack_without_media(hass, phones):
    from copy import deepcopy
    from custom_components.voip_stack import websocket_api
    from custom_components.voip_stack.entity_identity import shared_entity_ids
    from custom_components.voip_stack.phone_endpoint import EndpointKind
    phones.endpoints.update("kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen",
        entity_ids={"sensor.account_kitchen"})
    from homeassistant.helpers import entity_registry as er
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    shared = shared_entity_ids(hass)["call_event"]
    class Connection:
        def __init__(self):
            self.user = SimpleNamespace(id="observer", is_active=True,
                permissions=SimpleNamespace(check_entity=lambda entity, policy: policy == "read" and entity in {shared, "sensor.account_kitchen"}))
            self.subscriptions, self.messages = {}, []
        def send_result(self, identity): self.messages.append(("result", identity))
        def send_event(self, identity, value): self.messages.append(("event", deepcopy(value)))
    with phones.calls.state_operation():
        session = phones.calls.upsert("physical-account-call", state="in_call", owner="bridge", caller="Office", callee="Kitchen",
            source_endpoint_id="office", dest_endpoint_id="kitchen")
        phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
        phones.calls.add_leg(session.call_id, "physical-leg", role="callee", state="in_call", endpoint_id="kitchen")
    connection, second = Connection(), Connection()
    websocket_api._ha_softphone_store(hass, "kitchen")["dnd"] = True
    websocket_api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": 17, "endpoint_id": "kitchen"})
    websocket_api.websocket_subscribe_ha_softphone_state(hass, second, {"id": 18, "endpoint_id": "kitchen"})
    try:
        assert connection.messages[0] == ("result", 17)
        value = connection.messages[1][1]
        assert value["endpoint_id"] == "kitchen" and value["endpoint_type"] == "sip_account"
        assert value["call_id"] == session.call_id and value["state"] == "in_call"
        assert value["generation"] == session.generation
        assert value["can_attach_media"] is False and value["media_owner"] == "other"
        assert not phones.runtime.softphone_presence and not phones.runtime.media.owners_for("audio")
        assert phones.calls.resource_for(session.call_id, "softphone_media") is None
        assert value["dnd"] is False and second.messages[1][1]["dnd"] is False
        phones.endpoints.update("kitchen", dnd=True)
        await hass.async_block_till_done()
        assert connection.messages[-1][1]["dnd"] is True and second.messages[-1][1]["dnd"] is True
        assert not phones.runtime.media.owners_for("audio")
    finally:
        connection.subscriptions.pop(17)()
        second.subscriptions.pop(18)()


async def test_popup_ws_uses_authenticated_user_and_rejects_read_only_control(hass, phones):
    from homeassistant.exceptions import Unauthorized
    from custom_components.voip_stack import websocket_api
    from custom_components.voip_stack.entity_identity import shared_entity_ids
    from custom_components.voip_stack.phone_endpoint import EndpointKind
    from custom_components.voip_stack.store import config_entry
    phones.endpoints.update("kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen", entity_ids={"sensor.account_kitchen"})
    tracker, _ = await owner(hass, phones.runtime, config_entry(hass))
    from homeassistant.helpers import entity_registry as er
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    shared = shared_entity_ids(hass)["call_event"]
    class Connection:
        def __init__(self, control):
            self.user = SimpleNamespace(id="actual-user", is_active=True, permissions=SimpleNamespace(
                check_entity=lambda entity, policy: entity in {shared, "sensor.account_kitchen"} and (policy == "read" or control)))
            self.results = []
        def send_result(self, identity, value): self.results.append(value)
        def send_error(self, *args): pytest.fail(str(args))
    message = {"id": 19, "type": "voip_stack/set_ha_popup_binding", "binding_id": "stable-browser-1234567890", "endpoint_id": "kitchen", "enabled": True}
    try:
        with pytest.raises(Unauthorized):
            await websocket_api.websocket_set_ha_popup_binding.__wrapped__(hass, Connection(False), message)
        assert not tracker.has_binding("kitchen")
        connection = Connection(True)
        await websocket_api.websocket_set_ha_popup_binding.__wrapped__(hass, connection, message)
        assert connection.results[-1]["user_id"] == "actual-user" and tracker.has_binding("kitchen")
        websocket_api.websocket_ha_popup_binding(hass, connection, {"id": 20, "binding_id": message["binding_id"]})
        assert connection.results[-1]["endpoint_id"] == "kitchen"
    finally:
        await tracker.async_close()


async def test_partial_lovelace_bootstrap_preserves_last_good_index_until_loaded(hass):
    runtime = runtime_for_bindings()
    dashboard(hass, {"views": [{"cards": [{"type": "custom:voip-stack-card",
        "mode": "ha_softphone", "device_id": "device-closed"}]}]})
    tracker, _ = await owner(hass, runtime)
    await tracker.async_close()
    hass.config.components.remove("lovelace")
    hass.data[LOVELACE_DATA].dashboards.clear()
    tracker, _ = await owner(hass, runtime)
    try:
        assert tracker.has_binding("closed")
        hass.config.components.add("lovelace")
        hass.bus.async_fire("component_loaded", {"component": "lovelace"})
        await hass.async_block_till_done()
        assert not tracker.has_binding("closed")
    finally:
        await tracker.async_close()


async def test_actual_lovelace_autogenerated_overview_is_not_an_export_failure(hass, caplog):
    from homeassistant.components.lovelace.dashboard import LovelaceStorage
    runtime = runtime_for_bindings()
    export = dashboard(hass, {"views": [{"cards": [{"type": "custom:voip-stack-card",
        "mode": "ha_softphone", "device_id": "device-closed"}]}]})
    hass.data[LOVELACE_DATA].dashboards = {None: LovelaceStorage(hass, None), "calls": export}
    tracker, _ = await owner(hass, runtime)
    try:
        assert tracker.has_binding("closed")
        assert [row["reference"] for row in tracker.references("closed")] == ["dashboard:calls/views/0/cards/0"]
        assert not any("association export unavailable" in record.message for record in caplog.records)
    finally:
        await tracker.async_close()


async def test_closed_browser_branch_cannot_authorize_media_after_native_winner(hass, phones):
    from custom_components.voip_stack import websocket_api
    from custom_components.voip_stack.phone_endpoint import EndpointKind
    from custom_components.voip_stack.phone_state import phone_state_observer
    phones.endpoints.update("kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen")
    with phones.calls.state_operation():
        session = phones.calls.upsert("shared-fork", state="ringing", owner="router", caller="Office", callee="Kitchen",
            source_endpoint_id="office", dest_endpoint_id="kitchen")
        phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
        phones.calls.add_leg(session.call_id, "browser:kitchen", role="ha_softphone", state="ringing", endpoint_id="kitchen")
    unsubscribe = phone_state_observer(hass).subscribe(lambda *_args: None)
    try:
        phones.calls.observe_leg(session.call_id, "browser:kitchen", role="ha_softphone", state="cancelled", endpoint_id="kitchen")
        phones.calls.observe_leg(session.call_id, "sip-winner", role="callee", state="in_call", endpoint_id="kitchen")
        phones.calls.upsert(session.call_id, state="in_call", owner="bridge")
        # A queued browser projection still describes the retired early branch.
        websocket_api._ha_softphone_store(hass, "kitchen").update(call_id=session.call_id, state="ringing", sip_state="ringing")
        snapshot = websocket_api._ha_softphone_state(hass, "kitchen")
        assert snapshot["state"] == "in_call" and snapshot["call_id"] == session.call_id
        assert snapshot["can_attach_media"] is False and snapshot["media_owner"] == "other"
        assert session.legs["browser:kitchen"].phase.value == "closed"
        assert not phones.runtime.media.owners_for("audio")
    finally:
        unsubscribe()


async def test_ended_native_call_cannot_survive_as_active_public_account_cache(hass, phones):
    from custom_components.voip_stack import websocket_api
    from custom_components.voip_stack.phone_endpoint import EndpointKind
    phones.endpoints.update("kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen")
    session = phones.calls.upsert("ended-native", state="in_call", owner="bridge", caller="Office", callee="Kitchen")
    phones.calls.claim_endpoint(session.call_id, "kitchen", role="destination")
    await phones.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
    cache = websocket_api._ha_softphone_store(hass, "kitchen")
    cache.update(call_id=session.call_id, state="in_call", sip_state="in_call")
    snapshot = websocket_api._ha_softphone_state(hass, "kitchen")
    assert snapshot["state"] == "idle" and snapshot["sip_state"] == "idle"
    assert snapshot["call_id"] == session.call_id and snapshot["can_attach_media"] is False
    assert cache["state"] == "in_call", "Reading the public projection must not create a second state owner"
    assert not phones.calls.sessions and not phones.calls.endpoint_claims_snapshot()


async def test_native_caller_idle_stream_carries_ended_generation_without_cache_owner(hass, phones):
    from copy import deepcopy
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack import websocket_api
    from custom_components.voip_stack.entity_identity import shared_entity_ids
    from custom_components.voip_stack.phone_endpoint import EndpointKind
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    shared = shared_entity_ids(hass)["call_event"]
    phones.endpoints.update("kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen", entity_ids={"sensor.account_kitchen"})
    class Connection:
        def __init__(self):
            self.user = SimpleNamespace(id="observer", is_active=True, permissions=SimpleNamespace(
                check_entity=lambda entity, policy: policy == "read" and entity in {shared,"sensor.account_kitchen"}))
            self.subscriptions, self.events = {}, []
        def send_result(self, identity): pass
        def send_event(self, identity, value): self.events.append(deepcopy(value))
    connection = Connection()
    websocket_api.websocket_subscribe_ha_softphone_state(hass, connection, {"id": 31, "endpoint_id": "kitchen"})
    try:
        with phones.calls.state_operation():
            session = phones.calls.upsert("sip-caller-no-cache", state="in_call", owner="bridge", caller="Kitchen", callee="Office",
                source_endpoint_id="kitchen", dest_endpoint_id="office")
            phones.calls.claim_endpoint(session.call_id, "kitchen", role="source")
            phones.calls.observe_leg(session.call_id,"caller",role="caller",state="in_call",endpoint_id="kitchen")
        await hass.async_block_till_done()
        assert connection.events[-1]["state"] == "in_call"
        assert connection.events[-1]["call_id"] == session.call_id
        await phones.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
        await hass.async_block_till_done()
        assert connection.events[-1]["state"] == "idle"
        assert connection.events[-1]["call_id"] == session.call_id
        assert connection.events[-1]["generation"] == session.generation
        assert connection.events[-1]["can_attach_media"] is False
        assert not phones.calls.sessions and not phones.calls.endpoint_claims_snapshot()
        with phones.calls.state_operation():
            successor = phones.calls.upsert("new-call-B", state="in_call", owner="bridge", caller="Kitchen", callee="Office",
                source_endpoint_id="kitchen", dest_endpoint_id="office")
            phones.calls.claim_endpoint(successor.call_id, "kitchen", role="source")
            phones.calls.observe_leg(successor.call_id,"new-caller",role="caller",state="in_call",endpoint_id="kitchen")
        # A delayed occurrence for A must read the current B snapshot, never
        # decorate that snapshot with A's terminal identity.
        from custom_components.voip_stack.phone_state import phone_state_observer
        listener = next(fn for fn in phone_state_observer(hass).listeners if fn.__name__ == "forward_projection")
        listener({"endpoint_id":"kitchen", "previous":{"calls":[{"call_id":session.call_id,"generation":session.generation}]},
            "phone":{"calls":[]}, "call":{"call_id":session.call_id,"generation":session.generation,"sequence":22,"revision":22}})
        assert connection.events[-1]["state"] == "in_call"
        assert connection.events[-1]["call_id"] == successor.call_id
    finally:
        connection.subscriptions.pop(31)()
