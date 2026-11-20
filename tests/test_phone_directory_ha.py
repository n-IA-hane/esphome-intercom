"""Canonical directory publication through real HA owners and public JSON v2."""

import json
import asyncio
from dataclasses import replace
import logging

import pytest

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
    managed as managed,
    phone_record,
)

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("sensor_present", [False, True])
async def test_phonebook_export_uses_backend_directory_without_reading_sensor(managed, sensor_present):
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack.phonebook_services import build_phonebook_service_handlers

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)

    if sensor_present:
        lab.hass.states.async_set("sensor.voip_phonebook", "unavailable", {"roster_json": "stale"})
    before = len(lab.sent)

    async def no_delivery(_hass):
        raise AssertionError("An export must not provision devices")

    handlers = build_phonebook_service_handlers(no_delivery)
    result = await handlers["export_phonebook"](
        ServiceCall(lab.hass, "voip_stack", "export_phonebook", {})
    )
    rows = json.loads(result["roster_json"])["contacts"]
    own = [row for row in rows if row["metadata"].get("endpoint_id") == phone.endpoint_id]
    assert len(own) == 1
    assert own[0]["id"] == phone.username
    assert own[0]["name"] == "Ingresso"
    assert len(lab.sent) == before


async def test_phone_key_validation_uses_current_contacts_without_mutating_the_directory(managed):
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.physical_phone_validation import physical_validation_runtime
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.store import store_manual_roster_entries

    lab = managed
    store_manual_roster_entries(lab.hass, [RosterEntry("New action", name="New action", ha_bridge=True,
        metadata={"virtual_endpoint": "automation", "automation_entity_id": "automation.new"})])
    stale = PhoneDirectory([
        RosterEntry("Removed action", name="Removed action", ha_bridge=True,
            metadata={"endpoint_id": "service:automation:Removed action", "virtual_endpoint": "automation"}),
        RosterEntry("Shared", name="First group", ha_bridge=True,
            metadata={"endpoint_id": "group:ring:first", "group_type": "ring"}),
        RosterEntry("Shared", name="Second group", ha_bridge=True,
            metadata={"endpoint_id": "group:conference:second", "group_type": "conference"}),
    ])
    lab.runtime.directory = stale
    before = stale.snapshot()
    subentries = dict(lab.entry.subentries)
    view = physical_validation_runtime(lab.hass, lab.entry)
    assert view.directory.get("service:automation:Removed action") is None
    assert view.directory.get("service:automation:New action").display_name == "New action"
    assert view.directory.get("group:ring:first").display_name == "First group"
    assert view.directory.get("group:conference:second").display_name == "Second group"
    assert stale.snapshot() == before and dict(lab.entry.subentries) == subentries


@pytest.mark.parametrize("resolution", ["rename", "remove", "ineligible"])
async def test_live_esp_rename_collision_repair_survives_unrelated_refresh_until_resolved(managed, resolution):
    from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind

    lab = managed
    await lab.controller.async_sync()
    before = phone_record(lab)
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="existing-desk", name="Occupied name",
        username="existing_desk", device_id="existing-desk-device", kind=EndpointKind.SIP_ACCOUNT))
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    devices = dr.async_get(lab.hass)
    devices.async_update_device(lab.device.id, name_by_user="Occupied name")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    issue_id = f"esphome_endpoint_collision_{lab.device.id}"
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is not None
    assert phone_record(lab) == before
    assert lab.runtime.endpoints.get(before.endpoint_id).name == before.display_name
    await async_refresh_directory(lab.hass, peers=[], registered=[])
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is not None
    if resolution == "rename":
        devices.async_update_device(lab.device.id, name_by_user="New distinct name")
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        after = phone_record(lab)
        assert after.endpoint_id == before.endpoint_id and after.device_id == before.device_id
        assert after.mac == before.mac and after.username == before.username and after.password == before.password
        assert after.display_name == "New distinct name"
    elif resolution == "remove":
        devices.async_remove_device(lab.device.id)
        await lab.hass.async_block_till_done(wait_background_tasks=True)
    else:
        registry = er.async_get(lab.hass)
        entity = next(item for item in er.async_entries_for_device(registry, lab.device.id)
                      if item.unique_id.endswith("-voip_state"))
        registry.async_remove(entity.entity_id)
        await async_refresh_directory(lab.hass, peers=[], registered=[])
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is None


@pytest.mark.parametrize("resolution", ["rename", "remove"])
async def test_duplicate_esp_name_does_not_block_other_directory_identities(managed, resolution):
    from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
    from pytest_homeassistant.common import MockConfigEntry
    from custom_components.voip_stack.directory_runtime import async_refresh_directory

    lab = managed
    lab.native.runtime_data.available = False
    lab.native.runtime_data.client.is_connected = False
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    devices, entities = dr.async_get(lab.hass), er.async_get(lab.hass)
    additions = []
    for index, name in ((2, "Ingresso"), (3, "Studio")):
        origin = MockConfigEntry(domain="esphome", title=f"Node {index}",
                                 data={"device_name": f"node_{index}"})
        origin.add_to_hass(lab.hass)
        device = devices.async_get_or_create(config_entry_id=origin.entry_id,
            identifiers={("esphome", f"84c7bb00000{index}")}, name=name)
        entity = entities.async_get_or_create("sensor", "esphome", f"84c7bb00000{index}-voip_state",
            config_entry=origin, device_id=device.id, suggested_object_id=f"node_{index}_voip_state")
        lab.hass.states.async_set(entity.entity_id, "unavailable")
        additions.append(device)
    before = dict(lab.entry.data)
    await async_refresh_directory(lab.hass, peers=[], registered=[])
    duplicate, third = additions
    first_id, third_id = f"esphome:{lab.device.id}", f"esphome:{third.id}"
    assert lab.runtime.directory.get(first_id) is not None
    assert lab.runtime.directory.get(third_id) is not None
    assert lab.runtime.endpoints.by_device_id(duplicate.id) is None
    issue_id = f"esphome_endpoint_collision_{duplicate.id}"
    issue = ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id)
    assert issue is not None
    assert issue.learn_more_url == f"/config/devices/device/{duplicate.id}"
    if resolution == "rename":
        devices.async_update_device(duplicate.id, name_by_user="Ingresso upstairs")
    else:
        devices.async_remove_device(duplicate.id)
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is None
    await async_refresh_directory(lab.hass, peers=[], registered=[])
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is None
    assert lab.runtime.directory.get(first_id).name == "Ingresso"
    assert lab.runtime.directory.get(third_id).name == "Studio"
    if resolution == "rename":
        restored = lab.runtime.endpoints.by_device_id(duplicate.id)
        assert restored.endpoint_id == f"esphome:{duplicate.id}"
        assert restored.name == "Ingresso upstairs"
    assert lab.entry.data == before, "Repair must not rewrite identities or credentials"


async def test_esp_restore_collision_never_reassigns_a_stable_endpoint_owner(phone_lab):
    from homeassistant.helpers import device_registry as dr, issue_registry as ir
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind

    lab = phone_lab
    other = dr.async_get(lab.hass).async_get_or_create(config_entry_id=lab.native.entry_id,
        identifiers={("esphome", "84c7bb009999")}, name="Existing owner")
    identity = f"esphome:{lab.device.id}"
    owner = PhoneEndpoint(endpoint_id=identity, device_id=other.id, name="Existing owner",
                          username="existing_owner", kind=EndpointKind.ESPHOME)
    lab.runtime.endpoints.register(owner)
    await async_refresh_directory(lab.hass, peers=[], registered=[])
    assert lab.runtime.endpoints.get(identity) == owner
    assert lab.runtime.endpoints.by_device_id(lab.device.id) is None
    issue = ir.async_get(lab.hass).async_get_issue("voip_stack", f"esphome_endpoint_collision_{lab.device.id}")
    assert issue is not None and issue.translation_placeholders["field"] == "endpoint_id"
    assert lab.runtime.directory.get(identity).id == "existing_owner"


async def test_cold_reload_publishes_managed_identity_without_api_or_binding(managed):
    from custom_components.voip_stack.phone_config import async_setup_endpoint_registry
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook
    from custom_components.voip_stack.sensor import VoipPhonebookSensor

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    await lab.controller.async_close()
    lab.native.runtime_data.available = False
    lab.native.runtime_data.client.is_connected = False
    lab.runtime.endpoints = async_setup_endpoint_registry(lab.hass, lab.entry)
    lab.runtime.phone_provisioning = None
    coordinator = entry_phonebook(lab.hass, lab.runtime)
    await coordinator.async_refresh()
    sensor = VoipPhonebookSensor(coordinator)
    rows = json.loads(sensor.extra_state_attributes["roster_json"])["contacts"]
    matching = [row for row in rows if row["metadata"].get("endpoint_id") == phone.endpoint_id]
    assert len(matching) == 1
    assert matching[0]["id"] == phone.username
    assert matching[0]["name"] == "Ingresso"
    assert matching[0]["metadata"]["endpoint_kind"] == "esphome"
    assert matching[0]["metadata"]["account_id"] == "ha"
    assert len(lab.sent) == 3


def add_phone(lab, *, identity, name, kind="browser", username="", extension="", group=""):
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    data = dict(endpoint_id=identity, name=name, kind=kind, username=username, password="synthetic-phone-secret",
                extension=extension, ring_group=group, enabled=True)
    lab.hass.config_entries.async_add_subentry(lab.entry, ConfigSubentry(data=data, title=name,
        subentry_type="phone", unique_id=f"phone:{identity}"))
    sync_registry_from_entry(lab.hass, lab.entry)


@pytest.mark.parametrize("publish_sensor", [True, False], ids=["sensor", "disabled-sensor"])
async def test_public_ha_sensor_and_native_json_share_offline_phones_groups_and_services(managed, tmp_path, publish_sensor):
    from datetime import timedelta
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_platform import EntityPlatform
    from custom_components.voip_stack.store import store_manual_roster_entries
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook
    from custom_components.voip_stack.sensor import VoipPhonebookSensor
    from custom_components.voip_stack.directory_runtime import directory_uri
    from custom_components.voip_stack.config_entry_runtime import async_refresh_and_push_phonebook
    from custom_components.voip_stack.const import CONF_ASSIST_ENDPOINT_ENABLED, CONF_ASSIST_EXTENSION
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    add_phone(lab, identity="browser-primary", name="Casa", extension="100", group="Family")
    add_phone(lab, identity="sip-qa", name="QA", kind="sip_account", username="qa", extension="200", group="Family")
    store_manual_roster_entries(lab.hass, [RosterEntry("External", name="External", number="12345",
        metadata={"nested": {"password": "synthetic-contact-secret"}}),
        RosterEntry("Door action", name="Door action", ha_bridge=True,
            metadata={"virtual_endpoint": "automation", "automation_entity_id": "automation.door"})])
    lab.runtime.assist_config.update({CONF_ASSIST_ENDPOINT_ENABLED: True, CONF_ASSIST_EXTENSION: "777", "name": "Assist"})
    sent = []

    async def receive(call):
        sent.append(json.loads(call.data["roster_json"]))

    lab.hass.services.async_register("esphome", "phone_node_set_roster_json", receive)
    coordinator = entry_phonebook(lab.hass, lab.runtime)
    await coordinator.async_start()
    coordinator.enable_delivery()
    sensor = VoipPhonebookSensor(coordinator)
    platform = EntityPlatform(hass=lab.hass, logger=logging.getLogger(__name__), domain="sensor",
        platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = lab.entry
    if not publish_sensor:
        er.async_get(lab.hass).async_get_or_create("sensor", "voip_stack", "voip_stack_phonebook",
            config_entry=lab.entry, disabled_by=er.RegistryEntryDisabler.USER)
    await platform.async_add_entities([sensor])
    try:
        await async_refresh_and_push_phonebook(lab.hass)
        state = lab.hass.states.get(sensor.entity_id)
        if publish_sensor:
            assert state is not None
            payload = json.loads(state.attributes["roster_json"])
            assert state.attributes["count"] == len(payload["contacts"])
        else:
            assert state is None
            assert sent
            payload = sent[-1]
        assert payload["version"] == 2
        assert sent and sent[-1] == payload
        rows = {row["name"]: row for row in payload["contacts"]}
        assert {"Ingresso", "Casa", "QA", "Family", "Assist", "Door action", "External"} <= rows.keys()
        for name, kind in (("Ingresso", "esphome"), ("Casa", "browser"), ("QA", "sip_account"),
                           ("Family", "group"), ("Assist", "service"), ("Door action", "service"),
                           ("External", "contact")):
            assert rows[name]["metadata"]["endpoint_kind"] == kind
        # This fixture declares no saved HA association or SIP registration.
        assert rows["Casa"]["metadata"]["availability"] == "offline"
        assert not lab.runtime.softphone_presence
        assert rows["QA"]["metadata"]["availability"] == "offline"
        assert rows["Family"]["metadata"]["members"] == ["Casa", "qa"]
        assert lab.runtime.directory.resolve("100").metadata["endpoint_id"] == "browser-primary"
        directory = lab.runtime.directory
        assert directory.resolve("Casa").metadata["endpoint_id"] == "browser-primary"
        assert directory.resolve("200").metadata["endpoint_id"] == "sip-qa"
        assert directory.get(phone.endpoint_id).id == phone.username
        assert directory_uri(lab.hass, directory.get(phone.endpoint_id)) == f"sip:{phone.username}@127.0.0.1:5060"
        public = json.dumps(payload)
        assert phone.password not in public
        assert "synthetic-phone-secret" not in public and "synthetic-contact-secret" not in public
        (tmp_path / "public-roster-v2.json").write_text(public)
    finally:
        await platform.async_reset()
        await coordinator.async_close()


async def test_directory_snapshot_owns_metadata_and_rejects_ambiguous_aliases(phone_lab):
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.roster import RosterEntry, RosterError
    first = RosterEntry("phone", name="Phone", extension="100", metadata={
        "endpoint_id": "stable-phone", "members": ["original"], "password": "fixture-only"})
    directory = PhoneDirectory([first])
    first.metadata["members"].append("producer-mutated")
    row = directory.snapshot()[0]
    row.metadata["members"].append("consumer-mutated")
    assert directory.get("stable-phone").metadata == {"endpoint_id": "stable-phone", "members": ["original"]}
    observed = []
    unsubscribe = directory.subscribe(lambda: observed.append(directory.generation))
    assert not directory.upsert(directory.get("stable-phone"))
    directory.upsert(RosterEntry("other", name="Other", extension="100"))
    with pytest.raises(RosterError, match="Ambiguous"):
        directory.resolve("100")
    assert len(observed) == 1
    unsubscribe()
    directory.close()
    assert not directory.upsert(first) and not directory.snapshot()


async def test_routing_uses_current_contact_and_instance_after_directory_publication(managed):
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.endpoint_routing import roster_from_peers
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)

    async def register(index):
        uri = "sip:127.0.0.1:5060"
        auth = build_digest_authorization(challenge_header=lab.registrar._challenge()[1], username=phone.username,
            password=phone.password, method="REGISTER", uri=uri)
        request = sip.parse_message(sip.build_request("REGISTER", uri, [
            ("Via", f"SIP/2.0/UDP 127.0.0.2:{41000+index};branch=z9hG4bK-dir{index};rport"),
            ("From", f"<sip:{phone.username}@127.0.0.1>;tag=dir"),
            ("To", f"<sip:{phone.username}@127.0.0.1>"), ("Call-ID", f"directory-{index}"),
            ("CSeq", "1 REGISTER"), ("Supported", "gruu"), ("Expires", "120"),
            ("Contact", f'<sip:{phone.username}@127.0.0.2:{41000+index}>;+sip.instance="<urn:uuid:00000000-0000-0000-0000-00000000000{index}>"'),
            ("Authorization", auth)]))
        assert (await lab.registrar.handle_register(request, ("127.0.0.2", 41000+index), "UDP")).status == 200
        return next(binding for binding in lab.registrar.registered_contacts(phone.username) if binding.call_id == f"directory-{index}")

    first = await register(1)
    first.capabilities = {"sdp_features": ["directional_audio_v1"], "sip_audio_tx_formats": ["L16/16000/1/10"]}
    await async_refresh_directory(lab.hass)
    published = lab.runtime.directory.get(phone.endpoint_id)
    assert published.sip_uri == first.contact_uri
    second = await register(2)
    assert second.public_gruu
    selected = lab.registrar.registered_roster_entries(instance_uri=sip.parse_sip_uri(second.public_gruu))
    routed = roster_from_peers(lab.hass, [], selected)
    row = next(entry for entry in routed if entry.metadata.get("endpoint_id") == phone.endpoint_id)
    assert row.sip_uri == second.contact_uri
    assert len(row.metadata["sip_contacts"]) == 1
    assert row.metadata["sip_capabilities_known"] is False
    assert not row.metadata.get("sdp_features")
    assert lab.runtime.directory.get(phone.endpoint_id) == published
    lab.registrar.remove_registration(phone.username)
    offline = next(entry for entry in roster_from_peers(lab.hass, [], [])
                   if entry.metadata.get("endpoint_id") == phone.endpoint_id)
    assert not offline.address and not offline.sip_uri
    assert offline.metadata["registered"] is False
    assert not offline.metadata.get("sdp_features")


@pytest.mark.parametrize("retired", ["runtime", "directory", "later-refresh", "close"])
async def test_directory_await_does_not_publish_for_retired_owner(managed, monkeypatch, retired):
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    lab = managed
    await lab.controller.async_sync()
    original = lab.runtime.directory
    before = original.snapshot()
    entered, release = asyncio.Event(), asyncio.Event()

    async def peers(_hass):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", peers)
    task = asyncio.create_task(async_refresh_directory(lab.hass))
    await entered.wait()
    if retired == "runtime":
        lab.entry.runtime_data = replace(lab.runtime, directory=PhoneDirectory())
    elif retired == "directory":
        lab.runtime.directory = PhoneDirectory()
    elif retired == "later-refresh":
        await async_refresh_directory(lab.hass, peers=[], registered=[])
    else:
        original.close()
    generation = original.generation
    release.set()
    await task
    assert original.generation == generation
    if retired not in {"later-refresh", "close"}:
        assert original.snapshot() == before


async def test_stale_coordinator_refresh_cannot_deliver_into_new_runtime(managed, monkeypatch):
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook
    lab = managed
    await lab.controller.async_sync()
    coordinator = entry_phonebook(lab.hass, lab.runtime)
    coordinator.enable_delivery()
    entered, release = asyncio.Event(), asyncio.Event()

    async def peers(_hass):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", peers)
    task = asyncio.create_task(coordinator.async_refresh(deliver=True))
    await entered.wait()
    lab.entry.runtime_data = replace(lab.runtime, directory=None, phonebook=None)
    release.set()
    await task
    assert lab.entry.runtime_data.directory is None


@pytest.mark.parametrize("legacy_record", [False, True])
async def test_legacy_offline_identity_is_restored_without_becoming_a_managed_account(managed, legacy_record):
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack.phone_config import async_setup_endpoint_registry
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    lab = managed
    if legacy_record:
        await lab.controller.async_sync()
        phone = phone_record(lab)
        lab.native.runtime_data.services = {}
        await lab.controller.async_sync()
        identity, username = phone.endpoint_id, phone.username
    else:
        identity, username = f"esphome:{lab.device.id}", "phone_node"
    entity = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "84c7bb000001-voip_endpoint",
        config_entry=lab.native, device_id=lab.device.id, suggested_object_id="legacy_voip_endpoint")
    lab.hass.states.async_set(entity.entity_id, "unavailable")
    lab.native.runtime_data.available = False
    lab.native.runtime_data.client.is_connected = False
    lab.runtime.endpoints = async_setup_endpoint_registry(lab.hass, lab.entry)
    await async_refresh_directory(lab.hass)
    row = lab.runtime.directory.get(identity)
    assert row is not None and row.id == username
    assert row.metadata["endpoint_kind"] == "esphome" and row.metadata["availability"] == "offline"
    assert not lab.registrar.accounts


async def test_name_extension_and_group_events_update_one_canonical_identity(managed):
    from homeassistant.helpers import entity_registry as er, device_registry as dr
    lab = managed
    values = {}
    for role, value in (("voip_extension", "301"), ("voip_ring_groups", "Old group")):
        entity = er.async_get(lab.hass).async_get_or_create("text", "esphome", f"84c7bb000001-{role}",
            config_entry=lab.native, device_id=lab.device.id, suggested_object_id=role)
        values[role] = entity.entity_id
        lab.hass.states.async_set(entity.entity_id, value)
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    phone = phone_record(lab)
    directory = lab.runtime.directory
    observed = []
    unsubscribe = directory.subscribe(lambda: observed.append(directory.generation))
    try:
        dr.async_get(lab.hass).async_update_device(lab.device.id, name_by_user="Camera")
        lab.hass.states.async_set(values["voip_extension"], "302")
        lab.hass.states.async_set(values["voip_ring_groups"], "New group")
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        assert directory.resolve("302").metadata["endpoint_id"] == phone.endpoint_id
        assert directory.resolve("301") is None and directory.resolve("Old group") is None
        assert directory.resolve("Camera").id == phone.username
        assert directory.resolve("New group").metadata["members"] == [phone.username]
        assert len([row for row in directory.snapshot() if row.metadata["endpoint_id"] == phone.endpoint_id]) == 1
        assert observed
    finally:
        unsubscribe()


async def test_assist_service_keeps_identity_when_display_name_changes(managed):
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.const import CONF_ASSIST_ENDPOINT_ENABLED, CONF_ASSIST_EXTENSION
    lab = managed
    lab.runtime.assist_config.update({CONF_ASSIST_ENDPOINT_ENABLED: True, CONF_ASSIST_EXTENSION: "777", "name": "Assist"})
    await async_refresh_directory(lab.hass)
    first = lab.runtime.directory.resolve("777")
    lab.runtime.assist_config["name"] = "Updated assistant"
    await async_refresh_directory(lab.hass)
    current = lab.runtime.directory.resolve("777")
    assert current.metadata["endpoint_id"] == first.metadata["endpoint_id"]
    assert current.name == "Updated assistant" and current.metadata["endpoint_kind"] == "service"


async def test_contacts_and_virtual_destinations_never_gain_phone_kind_from_uri_or_label(managed):
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.store import store_manual_roster_entries
    from custom_components.voip_stack.roster import RosterEntry
    lab = managed
    store_manual_roster_entries(lab.hass, [
        RosterEntry("looks-like-phone", name="Browser phone", sip_uri="sip:browser@127.0.0.1:5060"),
        RosterEntry("group", name="Explicit group", metadata={"endpoint_kind": "browser", "group_type": "ring"}),
        RosterEntry("service", name="Explicit service", metadata={"endpoint_kind": "sip_account", "virtual_endpoint": "automation"}),
    ])
    await async_refresh_directory(lab.hass)
    for name, kind in (("Browser phone", "contact"), ("Explicit group", "group"), ("Explicit service", "service")):
        row = lab.runtime.directory.resolve(name)
        assert row.metadata["endpoint_kind"] == kind


async def test_roster_delivery_does_not_mark_stale_generation_as_delivered(managed):
    from custom_components.voip_stack.phonebook_runtime import push_roster_json_to_esps
    from custom_components.voip_stack.roster import RosterEntry
    lab = managed
    await lab.controller.async_sync()
    entered, release = asyncio.Event(), asyncio.Event()

    async def receive(_call):
        entered.set()
        await release.wait()

    service = "phone_node_set_roster_json"
    lab.hass.services.async_register("esphome", service, receive)
    task = asyncio.create_task(push_roster_json_to_esps(lab.hass, '{"version":2,"contacts":[]}'))
    await entered.wait()
    lab.runtime.directory.upsert(RosterEntry("new", name="New", metadata={"endpoint_id": "new"}))
    release.set()
    await task
    assert service not in lab.runtime.phonebook_delivered_roster


async def test_cold_legacy_record_without_optional_name_uses_existing_ha_name(managed):
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    lab = managed
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "managed_esphome_phones": {lab.device.id: dict(device_id=lab.device.id,
            origin_entry_id=lab.native.entry_id, mac="84:c7:bb:00:00:01",
            username="legacy_node", password="synthetic-record-secret")}})
    lab.native.runtime_data.available = False
    lab.native.runtime_data.client.is_connected = False
    await async_refresh_directory(lab.hass)
    row = lab.runtime.directory.get(f"esphome:{lab.device.id}")
    assert row.id == "legacy_node" and row.name == "Ingresso"


async def test_call_state_without_directory_change_does_not_notify_subscribers(managed):
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    directory = lab.runtime.directory
    generation = directory.generation
    changed = []
    unsubscribe = directory.subscribe(lambda: changed.append(directory.generation))
    try:
        lab.runtime.endpoints.sync_transport_call(phone.endpoint_id, active=True, fallback_call_id="physical:fixture")
        await async_refresh_directory(lab.hass)
        lab.runtime.endpoints.sync_transport_call(phone.endpoint_id, active=False)
        await async_refresh_directory(lab.hass)
        assert directory.generation == generation and changed == []
    finally:
        unsubscribe()
        lab.runtime.endpoints.sync_transport_call(phone.endpoint_id, active=False)
