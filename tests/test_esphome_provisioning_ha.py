"""Native ESP configuration, canonical SIP identity and permanent removal."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.voip_stack.esphome_provisioning import EspHomeProvisioner
from custom_components.voip_stack.managed_phones import CONF_MANAGED_PHONES, managed_records
from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.runtime_data import VoipStackRuntime
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar
from custom_components.voip_stack.store import sip_accounts

pytestmark = pytest.mark.ha


@pytest.fixture
async def phone_lab(hass):
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    endpoints = EndpointRegistry()
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    registrar = SipRegistrar(enabled=False, accounts=[], local_ip="192.0.2.1", local_sip_port=5060)
    calls.attach_component("registrar", registrar)
    calls.activate()
    entry.runtime_data = VoipStackRuntime(
        transport_config={"sip_port": 5060, "advertise_host": "192.0.2.1"},
        assist_config={}, trunk_config={}, endpoints=endpoints,
        phones=PhoneAdapterRegistry(hass, endpoints), sip=calls,
    )
    native = MockConfigEntry(domain="esphome", title="Technical name", data={
        "host": "192.0.2.10", "device_name": "phone_node",
    })
    native.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=native.entry_id,
        identifiers={("esphome", "84c7bb000001")},
        connections={(dr.CONNECTION_NETWORK_MAC, "84:c7:bb:00:00:01")}, name="Ingresso")
    sent, events = [], []
    gate = SimpleNamespace(entered=None, release=None)

    async def execute_service(service, data):
        sent.append((service.name, dict(data)))
        if gate.entered is not None:
            gate.entered.set()
            await gate.release.wait()

    services = {i: SimpleNamespace(name=name) for i, name in enumerate(("set_ha_display_name", "set_account"))}
    native.runtime_data = SimpleNamespace(client=SimpleNamespace(is_connected=True, execute_service=execute_service),
        device_info=SimpleNamespace(name="phone_node", friendly_name="Ingresso", mac_address="84:C7:BB:00:00:01"),
        services=services)
    unsub = hass.bus.async_listen("call_service", lambda event: events.append(dict(event.data)))
    controller = EspHomeProvisioner(hass, entry)
    entry.runtime_data.phone_provisioning = controller
    yield SimpleNamespace(hass=hass, entry=entry, native=native, device=device,
        registrar=registrar, runtime=entry.runtime_data, sent=sent, events=events, gate=gate, controller=controller)
    gate.release and gate.release.set()
    await controller.async_close()
    unsub()


async def test_one_account_same_password_and_no_secret_event(phone_lab, caplog):
    lab = phone_lab
    await lab.controller.async_sync()
    first = managed_records(lab.entry)[lab.device.id]
    assert first.username == "phone_node"
    assert first.display_name == "Ingresso"
    assert len(lab.runtime.endpoints.endpoints) == 1
    assert lab.runtime.endpoints.by_username("phone_node").device_id == lab.device.id
    assert lab.registrar.accounts["phone_node"].endpoint_id == first.endpoint_id
    assert lab.registrar.enabled
    assert lab.sent[0] == ("set_ha_display_name", {"name": "Ingresso"})
    assert lab.sent[1][0] == "set_account"
    assert lab.sent[1][1]["password"] == first.password
    assert lab.sent[1][1]["transport"] == "auto"
    assert lab.sent[1][1]["persist"] is True
    assert lab.events == []
    assert first.password not in caplog.text
    assert first.password not in repr(first)
    await lab.controller.async_sync()
    assert len(lab.sent) == 2
    assert managed_records(lab.entry)[lab.device.id].password == first.password


async def test_managed_native_groups_reach_canonical_directory_without_endpoint_sensor(phone_lab, monkeypatch):
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack.device_resolver import get_resolver
    from custom_components.voip_stack.directory_runtime import async_refresh_directory, directory_uri
    from custom_components.voip_stack.ldap_server import LdapDirectoryServer
    from custom_components.voip_stack.sensor import VoipPhonebookSensor

    lab = phone_lab
    fields = {}
    for domain, role, value in (
        ("text", "voip_ring_groups", "RG Casa"),
        ("text", "voip_conference_groups", "CG Casa"),
        ("switch", "voip_ring_on_conference", "on"),
    ):
        entity = er.async_get(lab.hass).async_get_or_create(
            domain, "esphome", f"84c7bb000001-{domain}-{role}",
            config_entry=lab.native, device_id=lab.device.id,
            suggested_object_id=f"phone_{role}",
        )
        fields[role] = entity.entity_id
        lab.hass.states.async_set(entity.entity_id, value)
    await lab.controller.async_sync()
    phone = managed_records(lab.entry)[lab.device.id]
    monkeypatch.setattr(lab.registrar, "registered_contacts", lambda username: (
        SimpleNamespace(contact_uri="sip:phone_node@192.0.2.10:5060", transport="udp"),
    ) if username == phone.username else ())

    devices = await get_resolver(lab.hass).list_devices()
    device, = devices
    assert "voip_endpoint" not in device["entities"]
    assert (device["ring_group"], device["conference_group"], device["conference_ring"]) == ("RG Casa", "CG Casa", True)
    rows = await async_refresh_directory(lab.hass, registered=[])
    assert {entry.name for entry in rows if entry.metadata.get("group_type")} == {"RG Casa", "CG Casa"}
    server = LdapDirectoryServer(lab.runtime.directory, lab.controller.authenticate,
        uri_for=lambda row: directory_uri(lab.hass, row))
    exported = {attrs["cn"][0]: attrs for _dn, attrs in server.records() if "uid" in attrs}
    assert exported[b"RG Casa"]["ou"] == exported[b"CG Casa"]["ou"] == (b"group",)
    assert b"RG%20Casa@" in exported[b"RG Casa"]["labeleduri"][0]

    sensor = VoipPhonebookSensor(lab.hass)
    sensor.entity_id = None
    await sensor.async_added_to_hass()
    try:
        # Temporarily missing native states keep the existing endpoint owner,
        # without adding another group cache or polling task.
        for entity_id in fields.values():
            lab.hass.states.async_remove(entity_id)
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        device, = await get_resolver(lab.hass).list_devices()
        assert (device["ring_group"], device["conference_group"], device["conference_ring"]) == ("RG Casa", "CG Casa", True)

        # Empty and off are authoritative. Exercise the real state bus so the
        # ordinary phonebook observer removes groups without a manual refresh.
        lab.hass.states.async_set(fields["voip_ring_groups"], "")
        lab.hass.states.async_set(fields["voip_conference_groups"], "")
        lab.hass.states.async_set(fields["voip_ring_on_conference"], "off")
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        device, = await get_resolver(lab.hass).list_devices()
        assert (device["ring_group"], device["conference_group"], device["conference_ring"]) == ("", "", False)
        assert not [entry for entry in lab.runtime.directory.snapshot() if entry.metadata.get("group_type")]
    finally:
        await sensor.async_will_remove_from_hass()


async def test_mailbox_event_runs_provisioning_on_ha_loop_and_unsubscribes(phone_lab, monkeypatch):
    """The real event dispatcher must not send native API work to its executor."""
    lab = phone_lab
    loop, thread = asyncio.get_running_loop(), threading.get_ident()
    completed = asyncio.Event()
    observed = []

    async def sync():
        observed.append((asyncio.get_running_loop(), threading.get_ident()))
        completed.set()

    monkeypatch.setattr(lab.controller, "async_sync", sync)
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    observed.clear()
    completed.clear()

    lab.hass.bus.async_fire("voip_stack_voicemail_changed", {"endpoint_id": "browser:mailbox"})
    await asyncio.wait_for(completed.wait(), 1)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert observed == [(loop, thread)]
    assert lab.controller._task.done()
    assert not lab.runtime.tasks

    await lab.controller.async_close()
    observed.clear()
    lab.hass.bus.async_fire("voip_stack_voicemail_changed", {"endpoint_id": "browser:mailbox"})
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert observed == []
    assert not lab.runtime.tasks


async def test_friendly_rename_changes_presented_name_only(phone_lab):
    lab = phone_lab
    await lab.controller.async_sync()
    first = managed_records(lab.entry)[lab.device.id]
    dr.async_get(lab.hass).async_update_device(lab.device.id, name_by_user="Camera")
    await lab.controller.async_sync()
    changed = managed_records(lab.entry)[lab.device.id]
    assert changed.password == first.password
    assert changed.username == first.username
    assert changed.endpoint_id == first.endpoint_id
    assert changed.display_name == "Camera"
    assert lab.sent[2] == ("set_ha_display_name", {"name": "Camera"})
    assert len(lab.sent) == 3
    assert len(lab.runtime.endpoints.endpoints) == 1


async def test_device_registry_rename_refreshes_the_canonical_directory(phone_lab):
    from custom_components.voip_stack.directory_runtime import async_refresh_directory

    lab = phone_lab
    await lab.controller.async_sync()
    phone = managed_records(lab.entry)[lab.device.id]
    await async_refresh_directory(lab.hass, peers=[], registered=[])
    assert lab.runtime.directory.get(phone.endpoint_id).display_name == "Ingresso"
    lab.controller.start()
    dr.async_get(lab.hass).async_update_device(lab.device.id, name_by_user="Camera")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert lab.runtime.endpoints.get(phone.endpoint_id).name == "Camera"
    assert lab.runtime.directory.get(phone.endpoint_id).display_name == "Camera"


async def test_directory_refresh_is_once_per_changed_batch_and_skips_identical_rpcs(phone_lab, monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import config_entry_runtime
    from custom_components.voip_stack.mailbox import CONF_ESPHOME_MAILBOXES

    lab = phone_lab
    refresh = AsyncMock(wraps=config_entry_runtime.async_refresh_and_push_phonebook)
    monkeypatch.setattr(config_entry_runtime, "async_refresh_and_push_phonebook", refresh)
    await lab.controller.async_sync()
    assert refresh.await_count == 1
    await lab.controller.async_sync()
    assert refresh.await_count == 1
    phone = managed_records(lab.entry)[lab.device.id]
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        CONF_ESPHOME_MAILBOXES: {phone.endpoint_id: {"enabled": True}}})
    await lab.controller.async_sync()
    assert refresh.await_count == 1
    dr.async_get(lab.hass).async_update_device(lab.device.id, name_by_user="Camera")
    await lab.controller.async_sync()
    assert refresh.await_count == 2


@pytest.mark.parametrize("legacy_identity", ["username", "device_id"])
async def test_legacy_peer_and_registry_are_one_directory_identity(phone_lab, legacy_identity):
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.peer import Peer
    from custom_components.voip_stack.roster import dump_roster_json, parse_roster_json

    lab = phone_lab
    await lab.controller.async_sync()
    phone = managed_records(lab.entry)[lab.device.id]
    peer = Peer(name=phone.username, host="192.0.2.10", endpoint_kind="esphome",
        device={"device_id": lab.device.id} if legacy_identity == "device_id" else None)
    rows = await async_refresh_directory(lab.hass, peers=[peer], registered=[])
    assert [entry.id for entry in rows].count(phone.username) == 1
    assert next(entry for entry in rows if entry.id == phone.username).metadata["endpoint_id"] == phone.endpoint_id
    assert len(parse_roster_json(dump_roster_json(rows))) == len(rows)


async def test_directory_identity_does_not_merge_unrelated_manual_or_virtual_destinations(phone_lab, monkeypatch):
    from custom_components.voip_stack import endpoint_routing
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.roster import RosterEntry

    lab = phone_lab
    await lab.controller.async_sync()
    phone = managed_records(lab.entry)[lab.device.id]
    manual = RosterEntry("manual-contact", name=phone.username, sip_uri="sip:external@example.org")
    virtual = RosterEntry("automation", name="Open door", metadata={"virtual_endpoint": "automation"})
    monkeypatch.setattr(endpoint_routing, "manual_roster_entries", lambda _hass: [manual, virtual])
    rows = await async_refresh_directory(lab.hass, peers=[], registered=[])
    assert len(rows) == 3
    assert lab.runtime.directory.get("manual-contact") == manual
    assert lab.runtime.directory.get("automation") == virtual
    assert lab.runtime.directory.get(phone.endpoint_id).id == phone.username


async def test_device_name_change_reuses_owner_and_mailbox_identity(phone_lab):
    lab = phone_lab
    await lab.controller.async_sync()
    first = managed_records(lab.entry)[lab.device.id]
    lab.native.runtime_data.device_info.name = "renamed_node"
    await lab.controller.async_sync()
    changed = managed_records(lab.entry)[lab.device.id]
    assert changed.username == "renamed_node"
    assert changed.endpoint_id == first.endpoint_id and changed.password == first.password
    assert "phone_node" not in lab.registrar.accounts
    assert "renamed_node" in lab.registrar.accounts


@pytest.mark.parametrize("extension", ["102", ""])
async def test_online_extension_change_updates_shared_alias_without_reregistering(phone_lab, extension):
    from homeassistant.helpers import entity_registry as er

    lab = phone_lab
    entity = er.async_get(lab.hass).async_get_or_create(
        "text", "esphome", "84c7bb000001-voip_extension",
        config_entry=lab.native, device_id=lab.device.id,
        suggested_object_id="phone_voip_extension",
    )
    lab.native.runtime_data.services[2] = SimpleNamespace(name="register_account")
    lab.hass.states.async_set(entity.entity_id, "101")
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    previous = managed_records(lab.entry)[lab.device.id]
    sent = list(lab.sent)
    assert lab.runtime.endpoints.by_extension("101").endpoint_id == previous.endpoint_id

    lab.hass.states.async_set(entity.entity_id, extension)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    current = managed_records(lab.entry)[lab.device.id]
    assert current.extension == extension
    assert current.password == previous.password
    assert lab.registrar.accounts[current.username].extension == extension
    assert lab.runtime.endpoints.get(current.endpoint_id).extension == extension
    assert lab.runtime.directory.get(current.endpoint_id).extension == extension
    assert lab.runtime.endpoints.by_extension("101") is None
    if extension:
        assert lab.runtime.endpoints.by_extension(extension).endpoint_id == current.endpoint_id
    assert lab.sent == sent


async def test_extension_collision_preserves_the_existing_account_and_endpoint(phone_lab):
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind

    lab = phone_lab
    entity = er.async_get(lab.hass).async_get_or_create(
        "text", "esphome", "84c7bb000001-voip_extension",
        config_entry=lab.native, device_id=lab.device.id,
    )
    lab.hass.states.async_set(entity.entity_id, "101")
    await lab.controller.async_sync()
    previous = managed_records(lab.entry)[lab.device.id]
    other = PhoneEndpoint(endpoint_id="sip:other", name="Other phone", username="reserved",
                          kind=EndpointKind.SIP_ACCOUNT)
    lab.runtime.endpoints.register(other)
    lab.hass.states.async_set(entity.entity_id, "reserved")
    await lab.controller.async_sync()
    assert managed_records(lab.entry)[lab.device.id] == previous
    assert lab.registrar.accounts[previous.username].extension == "101"
    assert lab.runtime.endpoints.get(previous.endpoint_id).extension == "101"
    assert lab.runtime.endpoints.resolve("reserved") == other


async def test_duplicate_extension_uses_existing_ambiguity_and_repair(phone_lab):
    from homeassistant.helpers import entity_registry as er, issue_registry as ir
    from custom_components.voip_stack.const import DOMAIN
    from custom_components.voip_stack.endpoint_registry import EndpointAmbiguousError
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.sensor import VoipPhonebookSensor

    lab = phone_lab
    sensor = VoipPhonebookSensor(lab.hass)
    sensor.entity_id = None  # Exercise its projection without adding an HA entity.
    lab.runtime.phonebook_sensor = sensor
    entity = er.async_get(lab.hass).async_get_or_create(
        "text", "esphome", "84c7bb000001-voip_extension",
        config_entry=lab.native, device_id=lab.device.id,
    )
    other = PhoneEndpoint(endpoint_id="sip:other", name="Other phone", username="other",
                          kind=EndpointKind.SIP_ACCOUNT, extension="102")
    lab.runtime.endpoints.register(other)
    lab.hass.states.async_set(entity.entity_id, "101")
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    lab.hass.states.async_set(entity.entity_id, "102")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    with pytest.raises(EndpointAmbiguousError):
        lab.runtime.endpoints.resolve("102")
    assert ir.async_get(lab.hass).async_get_issue(DOMAIN, "duplicate_extensions") is not None
    assert lab.runtime.endpoints.resolve("other") == other
    lab.hass.states.async_set(entity.entity_id, "103")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert lab.runtime.endpoints.resolve("102") == other
    assert ir.async_get(lab.hass).async_get_issue(DOMAIN, "duplicate_extensions") is None


async def test_offline_preserves_account_and_reconnect_reuses_password(phone_lab):
    lab = phone_lab
    await lab.controller.async_sync()
    first = managed_records(lab.entry)[lab.device.id]
    lab.native.runtime_data.client.is_connected = False
    await lab.controller.async_sync()
    assert sip_accounts(lab.hass)[0].password == first.password
    assert lab.runtime.endpoints.get(first.endpoint_id) is not None
    lab.native.runtime_data.client.is_connected = True
    await lab.controller.async_sync()
    assert managed_records(lab.entry)[lab.device.id].password == first.password


async def test_new_backend_bootstrap_refreshes_registration_without_new_credentials(phone_lab):
    lab = phone_lab
    lab.native.runtime_data.services[2] = SimpleNamespace(name="register_account")
    await lab.controller.async_sync()
    first = managed_records(lab.entry)[lab.device.id]
    assert [data for name, data in lab.sent if name == "register_account"] == [{"account_id": "ha"}]
    assert lab.events == []
    await lab.controller.async_sync()
    assert sum(name == "register_account" for name, _ in lab.sent) == 1
    await lab.controller.async_close()
    new_registrar = SipRegistrar(enabled=True, accounts=[], local_ip="192.0.2.1", local_sip_port=5060)
    assert lab.runtime.sip.release_component("registrar", lab.registrar)
    lab.runtime.sip.adopt_component("registrar", new_registrar)
    lab.controller = EspHomeProvisioner(lab.hass, lab.entry)
    lab.runtime.phone_provisioning = lab.controller
    await lab.controller.async_sync()
    assert sum(name == "register_account" for name, _ in lab.sent) == 2
    restored = managed_records(lab.entry)[lab.device.id]
    assert restored.password == first.password and restored.endpoint_id == first.endpoint_id
    assert new_registrar.accounts[first.username].endpoint_id == first.endpoint_id
    assert not new_registrar.registrations
    await lab.controller.async_close()


async def test_manual_account_collision_is_not_overwritten(phone_lab):
    lab = phone_lab
    manual = SipAccount("phone_node", "Existing phone", "fixture-password")
    lab.registrar.update_accounts([manual])
    await lab.controller.async_sync()
    assert lab.registrar.accounts["phone_node"] is manual
    assert not managed_records(lab.entry)
    assert not lab.sent


async def test_permanent_removal_revokes_and_purges_without_native_api(phone_lab, monkeypatch):
    from custom_components.voip_stack import mailbox
    from unittest.mock import AsyncMock

    lab = phone_lab
    await lab.controller.async_sync()
    first = managed_records(lab.entry)[lab.device.id]
    remove = AsyncMock()
    monkeypatch.setattr(mailbox, "async_remove_mailbox", remove)
    lab.controller.start()
    async_dispatcher_send(lab.hass, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.REMOVED, lab.native)
    assert "phone_node" not in lab.registrar.accounts
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert lab.entry.data[CONF_MANAGED_PHONES] == {}
    assert lab.runtime.endpoints.get(first.endpoint_id) is None
    remove.assert_awaited_once_with(lab.hass, first.endpoint_id)
    await lab.controller.async_sync()
    assert not managed_records(lab.entry)


async def test_removal_during_native_call_cannot_recreate_account(phone_lab, monkeypatch):
    from custom_components.voip_stack import mailbox
    from unittest.mock import AsyncMock

    lab = phone_lab
    monkeypatch.setattr(mailbox, "async_remove_mailbox", AsyncMock())
    lab.gate.entered, lab.gate.release = asyncio.Event(), asyncio.Event()
    lab.controller.start()
    await asyncio.wait_for(lab.gate.entered.wait(), 2)
    async_dispatcher_send(lab.hass, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.REMOVED, lab.native)
    assert "phone_node" not in lab.registrar.accounts
    lab.gate.release.set()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert not managed_records(lab.entry)
    assert "phone_node" not in lab.registrar.accounts


async def test_permanent_removal_releases_protocols_files_uids_and_subscriptions(phone_lab, socket_enabled, tmp_path):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.media_archive import MediaArchive
    from custom_components.voip_stack.imap_server import ImapMailboxServer
    from custom_components.voip_stack.ldap_server import LdapDirectoryServer
    from custom_components.voip_stack.mailbox import CONF_ESPHOME_MAILBOXES
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.sip_application import SipApplicationMethods, SipPresencePublication

    lab = phone_lab
    await lab.controller.async_sync()
    phone = managed_records(lab.entry)[lab.device.id]
    owner, other = phone.endpoint_id, "esphome:another-phone"
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=other, name="Other phone", username="other", kind=EndpointKind.SIP_ACCOUNT))
    archive = MediaArchive(tmp_path / "archive", lab.hass.async_add_executor_job)
    await archive.load()
    lab.runtime.sip.adopt_component("media_archive", archive, closer=archive.close)
    records = {}
    for identity in (owner, other):
        writer = await archive.begin(identity, caller="Controlled caller")
        assert writer.offer(b"\x10\0" * 1600, AudioFormat(16000, frame_ms=20))
        records[identity] = await writer.finish()
        await archive.imap_snapshot(identity)
    preserved = archive.segment_path(records[other]["id"], other)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        CONF_ESPHOME_MAILBOXES: {owner: {"enabled": True}, other: {"enabled": True}}})
    lab.runtime.directory = PhoneDirectory([
        RosterEntry(id=phone.username, name=phone.display_name, metadata={"endpoint_id": owner}),
        RosterEntry(id="other", name="Other phone", metadata={"endpoint_id": other}),
    ])
    methods = SipApplicationMethods(lab.hass, lab.registrar)
    lab.runtime.sip.adopt_component("sip_application", methods, closer=methods.stop)
    request = sip.parse_message((
        "SUBSCRIBE sip:phone_node@192.0.2.1 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bK-removal\r\n"
        "From: <sip:phone_node@192.0.2.1>;tag=phone\r\n"
        "To: <sip:phone_node@192.0.2.1>\r\nCall-ID: removal-subscription\r\n"
        "CSeq: 1 SUBSCRIBE\r\nContact: <sip:phone_node@192.0.2.10:5060>\r\n"
        "Event: presence\r\nExpires: 300\r\nContent-Length: 0\r\n\r\n"
    ).encode())
    notification_entered = asyncio.Event()
    async def blocked_notification(_request, _address, _result):
        notification_entered.set()
        await asyncio.Event().wait()
    response = methods._subscribe(request, ("192.0.2.10", 5060), phone.username, blocked_notification)
    assert response.status == 200 and len(methods.subscriptions) == 1
    methods.publications["publication"] = SipPresencePublication("publication", b"", 9999999999, target=phone.username)
    ldap = LdapDirectoryServer(lab.runtime.directory, lab.controller.authenticate)
    imap = ImapMailboxServer(AsyncMock(return_value=archive), lab.controller.authenticate_verifier)
    lab.runtime.ldap_server, lab.runtime.imap_server = ldap, imap
    await ldap.async_start("127.0.0.1", 0)
    await imap.async_start("127.0.0.1", 0)
    ldap_reader, ldap_writer = await asyncio.open_connection("127.0.0.1", ldap.port)
    imap_reader, imap_writer = await asyncio.open_connection("127.0.0.1", imap.port)

    def tlv(tag, body):
        return bytes((tag, len(body))) + body

    def bind():
        # Independent short-form BER fixture, with this test account only.
        body = b"\x02\x01\x03" + tlv(4, phone.username.encode()) + tlv(0x80, phone.password.encode())
        return tlv(0x30, b"\x02\x01\x01" + tlv(0x60, body))

    try:
        ldap_writer.write(bind())
        await ldap_writer.drain()
        assert b"\x0a\x01\x00" in await asyncio.wait_for(ldap_reader.read(128), 1)
        await imap_reader.readline()
        imap_writer.write(f"A1 LOGIN {phone.username} {phone.password}\r\n".encode())
        await imap_writer.drain()
        assert (await asyncio.wait_for(imap_reader.readline(), 1)).startswith(b"A1 OK")
        response.follow_up_scheduler()
        await asyncio.wait_for(notification_entered.wait(), 1)
        assert methods.notifiers
        lab.controller.start()
        async_dispatcher_send(lab.hass, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.REMOVED, lab.native)
        assert phone.username not in lab.registrar.accounts
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        await asyncio.wait_for(ldap_reader.read(), 1)
        await asyncio.wait_for(imap_reader.read(), 1)
        assert ldap_reader.at_eof() and imap_reader.at_eof()
        assert not methods.subscriptions and not methods.subscription_timers and not methods.publications and not methods.notifiers
        assert not managed_records(lab.entry) and lab.runtime.endpoints.get(owner) is None
        assert lab.runtime.directory.get(owner) is None and lab.runtime.directory.get(other) is not None
        assert not archive.list(owner) and preserved.exists() and len(archive.list(other)) == 1
        assert owner not in (archive.root / ".protocol" / "imap-index.json").read_text()
        assert owner not in lab.entry.data.get(CONF_ESPHOME_MAILBOXES, {})
        assert await lab.controller.authenticate(phone.username, phone.password.encode()) is None
        assert await lab.controller.authenticate_verifier(phone.username, lambda secret: secret == phone.password) is None
        rejected_reader, rejected_writer = await asyncio.open_connection("127.0.0.1", ldap.port)
        try:
            rejected_writer.write(bind())
            await rejected_writer.drain()
            assert b"\x0a\x01\x31" in await asyncio.wait_for(rejected_reader.read(128), 1)
        finally:
            rejected_writer.close()
            await rejected_writer.wait_closed()
        rejected_reader, rejected_writer = await asyncio.open_connection("127.0.0.1", imap.port)
        try:
            await rejected_reader.readline()
            rejected_writer.write(f"A1 LOGIN {phone.username} {phone.password}\r\n".encode())
            await rejected_writer.drain()
            assert (await asyncio.wait_for(rejected_reader.readline(), 1)).startswith(b"A1 NO")
        finally:
            rejected_writer.close()
            await rejected_writer.wait_closed()
        await lab.controller.async_sync()
        assert not managed_records(lab.entry)
    finally:
        ldap_writer.close()
        imap_writer.close()
        await ldap_writer.wait_closed()
        await imap_writer.wait_closed()
        await ldap.async_close()
        await imap.async_close()
        await methods.stop()
        await archive.close()
