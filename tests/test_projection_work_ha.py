"""Bound projection work while retaining canonical configuration and ownership."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.runtime_data import VoipStackRuntime

pytestmark = pytest.mark.ha


def prepare(hass, count):
    entry = MockConfigEntry(domain="voip_stack", data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    registry = EndpointRegistry()
    for i in range(count):
        identity = f"p{i:03d}"
        child = ConfigSubentry(subentry_type="phone", unique_id=f"phone:{identity}",
            title=f"Phone {i}", data={"kind": "sip_account", "name": f"Phone {i}",
                "username": identity, "password": "fixture-only", "enabled": True,
                "voicemail": {"enabled": i % 2 == 0}})
        hass.config_entries.async_add_subentry(entry, child)
        registry.register(PhoneEndpoint(endpoint_id=identity, name=f"Phone {i}",
            kind="sip_account", username=identity, extension=str(1000 + i),
            account_device_id=f"account-{i}", device_id=f"device-{i}",
            entity_ids={f"sensor.phone_{i}"}, availability="available"))
        registry.subentry_ids[identity] = child.subentry_id
    owner = SipEndpointRuntime(allow_dark_sessions=True)
    owner.activate()
    owner.bind_endpoint_registry(registry)
    runtime = entry.runtime_data = VoipStackRuntime(transport_config={},
        assist_config={}, trunk_config={}, endpoints=registry, phones=None, sip=owner)
    return entry, runtime, registry, owner


@pytest.mark.parametrize("count", [20, 100, 256])
async def test_full_phone_projection_reads_each_canonical_subentry_once(hass, monkeypatch, count):
    from custom_components.voip_stack import phone_config, phone_state

    entry, _runtime, registry, owner = prepare(hass, count)
    try:
        original = phone_config.phone_endpoint_id
        visits = []

        def counted(row):
            visits.append(row.subentry_id)
            return original(row)

        with monkeypatch.context() as ctx:
            ctx.setattr(phone_config, "phone_endpoint_id", counted)
            indexed = phone_state.phone_states(hass, dialogs={})
        assert len(visits) == count
        assert len(set(visits)) == count
        # The fallback is the pre-runtime canonical scan, with identical DTOs.
        registry.subentry_ids.clear()
        assert phone_state.phone_states(hass, dialogs={}) == indexed
        assert indexed["p000"]["mailbox"]["enabled"] is True
        assert indexed["p001"]["mailbox"]["enabled"] is False
    finally:
        await owner.shutdown()


async def test_subentry_index_reads_current_config_and_supports_staged_changes(hass):
    from custom_components.voip_stack.phone_config import phone_subentry_by_endpoint_id
    from custom_components.voip_stack.mailbox import mailbox_settings

    entry, _runtime, registry, owner = prepare(hass, 2)
    try:
        child = entry.subentries[registry.subentry_ids["p000"]]
        assert phone_subentry_by_endpoint_id(entry, " P000 ") is child
        previous_data = child.data
        hass.config_entries.async_update_subentry(entry, child,
            data={**child.data, "voicemail": {"enabled": False, "greeting": "changed"}})
        current = entry.subentries[child.subentry_id]
        assert current.data is not previous_data
        assert mailbox_settings(hass, "P000") == {"enabled": False, "greeting": "changed"}
        # An outdated index must not expose another phone's mailbox.
        registry.subentry_ids["p000"] = registry.subentry_ids["p001"]
        assert phone_subentry_by_endpoint_id(entry, "p000") is current
        hass.config_entries.async_remove_subentry(entry, child.subentry_id)
        assert phone_subentry_by_endpoint_id(entry, "P000") is None
        assert mailbox_settings(hass, "p000") == {}
        entry.runtime_data = None
        assert phone_subentry_by_endpoint_id(entry, "P001") is entry.subentries[registry.subentry_ids["p001"]]
    finally:
        await owner.shutdown()


async def test_runtime_claims_keep_identity_indexes_and_current_lookup_objects(hass, monkeypatch):
    _entry, _runtime, registry, owner = prepare(hass, 20)
    events = []
    detach = registry.subscribe(events.append)
    try:
        indexes = {name: deepcopy(getattr(registry, name)) for name in
            ("_device_index", "_entity_index", "_name_index", "_extension_index",
             "_username_index", "_route_index")}
        monkeypatch.setattr(registry, "_rebuild_indexes",
                            lambda: pytest.fail("a call claim changed no indexed identity"))
        claimed = registry.claim_call("P000", "call-one")
        assert registry.claim_call("p000", "call-one") is claimed
        for lookup, value in ((registry.by_device_id, "account-0"),
                              (registry.by_entity_id, "sensor.phone_0"),
                              (registry.by_extension, "1000"),
                              (registry.by_username, "p000"),
                              (registry.by_name, "Phone 0")):
            assert lookup(value) is claimed
        assert registry.release_call("p000", "stale-call") is False
        assert registry.require("p000").active_call_id == "call-one"
        assert registry.release_call("p000", "call-one") is True
        registry.claim_call("p000", "physical:p000")
        adopted = registry.adopt_transport_call("p000", "wire-call")
        assert registry.by_username("p000") is adopted
        assert adopted.active_call_id == "wire-call"
        assert len(events) == 4
        assert all(getattr(registry, key) == value for key, value in indexes.items())
    finally:
        detach()
        await owner.shutdown()


@pytest.mark.parametrize("phase", ["calling", "connecting", "ringing", "in_call", "held"])
async def test_selected_phone_matches_full_projection_from_current_legs(hass, monkeypatch, phase):
    from custom_components.voip_stack import phone_state, dialog_info

    _entry, runtime, registry, owner = prepare(hass, 4)
    try:
        session = owner.upsert("selected", owner="router", state=phase,
            caller="Phone 1", callee="Phone 0", source_endpoint_id="p001", dest_endpoint_id="p000")
        owner.claim_endpoint(session.call_id, "p001", role="source")
        owner.claim_endpoint(session.call_id, "p000", role="destination")
        owner.observe_leg(session.call_id, "source", role="caller", endpoint_id="p001", state=phase)
        owner.observe_leg(session.call_id, "destination", role="callee", endpoint_id="p000", state=phase)
        other = owner.upsert("unrelated", owner="router", state="ringing",
            caller="Phone 3", callee="Phone 2", source_endpoint_id="p003", dest_endpoint_id="p002")
        owner.observe_leg(other.call_id, "other", role="callee", endpoint_id="p002", state="ringing")
        expected = phone_state.phone_states(hass)["p000"]
        all_dialogs = dialog_info.snapshot_all(runtime)
        projected = []
        display = dialog_info.counterpart_display

        def counted(*args, **kwargs):
            projected.append(True)
            return display(*args, **kwargs)

        monkeypatch.setattr(dialog_info, "counterpart_display", counted)
        assert phone_state.phone_state(hass, "P000") == expected
        assert len(projected) == 1
        assert dialog_info.snapshot(runtime, "p000") == all_dialogs["p000"]
        assert phone_state.phone_state(hass, "missing") is None
        registry.update("p000", dnd=True)
        assert phone_state.phone_state(hass, "p000")["dnd"] is True
    finally:
        await owner.shutdown()


@pytest.mark.parametrize("clients", [1, 4])
async def test_dnd_fanout_builds_one_roster_and_one_selected_phone_per_client(hass, monkeypatch, clients):
    from custom_components.voip_stack import phone_state, websocket_api as api
    from custom_components.voip_stack.entity_identity import shared_entity_ids

    _entry, _runtime, registry, owner = prepare(hass, 20)
    er.async_get(hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event")
    shared_entity_ids(hass)
    connections = []
    try:
        for i in range(clients):
            connection = SimpleNamespace(user=SimpleNamespace(id=f"user-{i}", is_active=True,
                permissions=SimpleNamespace(check_entity=lambda *_: True)),
                subscriptions={}, messages=[])
            connection.send_result = lambda identity: None
            connection.send_event = lambda identity, payload, rows=connection.messages: rows.append(deepcopy(payload))
            connection.send_error = lambda *args: pytest.fail(str(args))
            connections.append(connection)
            api.websocket_subscribe_ha_softphone_state(hass, connection,
                {"id": i + 1, "endpoint_id": "p000"})
        await hass.async_block_till_done()
        expected = [{**api._client_phone_state(hass, item, "p000"), "dnd": True} for item in connections]
        projections, selected = [], []
        original = phone_state.phone_states
        original_selected = phone_state.phone_state

        def full(*args, **kwargs):
            projections.append(True)
            return original(*args, **kwargs)

        def one(*args, **kwargs):
            selected.append(True)
            return original_selected(*args, **kwargs)

        sent_before = [len(item.messages) for item in connections]
        monkeypatch.setattr(phone_state, "phone_states", full)
        monkeypatch.setattr(phone_state, "phone_state", one)
        monkeypatch.setattr(phone_state, "snapshot_all", lambda *args: pytest.fail("DND changed no call dialogs"))
        registry.update("p000", dnd=True)
        assert len(projections) == 1
        assert len(selected) == clients
        assert [item.messages[-1] for item in connections] == expected
        assert [len(item.messages) - before for item, before in zip(connections, sent_before)] == [1] * clients
    finally:
        for connection in connections:
            for remove in tuple(connection.subscriptions.values()):
                remove()
        await owner.shutdown()
