"""Managed phones through HA registries and their existing native API owner."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from aioesphomeapi import DeviceInfo, UserService, UserServiceArg, UserServiceArgType
from homeassistant.components.esphome.entry_data import RuntimeEntryData
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def custom_integration_path(enable_custom_integrations):
    import custom_components

    previous = custom_components.__path__
    custom_components.__path__ = [*previous, str(Path(__file__).parents[1] / "custom_components")]
    yield
    custom_components.__path__ = previous


def native_services():
    string = UserServiceArgType.STRING
    integer = UserServiceArgType.INT
    boolean = UserServiceArgType.BOOL
    fields = {
        "set_ha_display_name": [("name", string)],
        "set_account": [(name, integer if name in {"port", "proxy_port", "expires"}
                         else boolean if name == "persist" else string)
                        for name in ("account_id", "registrar", "port", "domain", "username",
                                     "password", "auth_username", "proxy", "proxy_port",
                                     "transport", "expires", "persist")],
        "register_account": [("account_id", string)],
    }
    return {key: UserService(name=name, key=key,
                            args=[UserServiceArg(name=arg, type=kind) for arg, kind in args])
            for key, (name, args) in enumerate(fields.items(), 1)}


@pytest.fixture
async def phone_lab(hass):
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.runtime_data import VoipStackRuntime
    from custom_components.voip_stack.sip_registrar import SipRegistrar

    entry = MockConfigEntry(domain="voip_stack", data={
        "initial_phone_created": True, "advertise_host": "127.0.0.1"})
    entry.add_to_hass(hass)
    hass.data.setdefault("voip_stack", {})
    endpoints = EndpointRegistry()
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    registrar = SipRegistrar(enabled=False, accounts=[], local_ip="127.0.0.1", local_sip_port=5060)
    calls.attach_component("registrar", registrar)
    calls.activate()
    entry.runtime_data = VoipStackRuntime(transport_config={"sip_port": 5060,
        "rtp_port": 40000, "advertise_host": "127.0.0.1"}, assist_config={},
        trunk_config={}, endpoints=endpoints, phones=PhoneAdapterRegistry(hass, endpoints), sip=calls)
    native = MockConfigEntry(domain="esphome", title="Technical name", data={
        "host": "127.0.0.2", "device_name": "phone_node"})
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

    native.runtime_data = RuntimeEntryData(entry_id=native.entry_id, title=native.title,
        client=SimpleNamespace(is_connected=True, execute_service=execute_service), store=MagicMock(),
        device_info=DeviceInfo(name="phone_node", friendly_name="Ingresso", mac_address="84:C7:BB:00:00:01"),
        services=native_services(), available=True)
    native.runtime_data.first_connect_done.set()
    unsub = hass.bus.async_listen("call_service", lambda event: events.append(dict(event.data)))
    lab = SimpleNamespace(hass=hass, entry=entry, native=native, device=device,
        registrar=registrar, runtime=entry.runtime_data, sent=sent, events=events, gate=gate)
    yield lab
    if gate.release is not None:
        gate.release.set()
    phone_services = getattr(entry.runtime_data, "phone_services", None)
    if phone_services is not None:
        await phone_services.async_close()
    owner = getattr(entry.runtime_data, "phone_provisioning", None)
    if owner is not None:
        await owner.async_close()
    from custom_components.voip_stack.endpoint_lifecycle import cancel_runtime_tasks
    await cancel_runtime_tasks(hass)
    unsub()


async def test_setup_entry_provisions_mac_identity_on_existing_native_owner(phone_lab, monkeypatch, socket_enabled):
    """The integration setup hook, rather than a test-only provisioner, must run."""
    from custom_components import voip_stack
    from custom_components.voip_stack import mailbox

    lab = phone_lab
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_services": {"listen_host": "127.0.0.1", "ldap_port": 0}})
    for name in ("_async_setup_shared", "_async_apply_assist_intents", "_async_start_sip_trunk",
                 "_deferred_phonebook_sync"):
        monkeypatch.setattr(voip_stack, name, AsyncMock())
    monkeypatch.setattr(mailbox, "async_setup_mailboxes", AsyncMock())
    monkeypatch.setattr(lab.hass.config_entries, "async_forward_entry_setups", AsyncMock())

    async def start(_hass):
        lab.entry.runtime_data.sip = lab.runtime.sip
        return True

    monkeypatch.setattr(voip_stack, "_async_start_sip_endpoint", start)
    assert await voip_stack.async_setup_entry(lab.hass, lab.entry)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    records = lab.entry.data.get("managed_esphome_phones", {})
    assert len(records) == 1
    assert records[lab.device.id]["username"] == "esp_84c7bb000001"
    assert [name for name, _data in lab.sent] == [
        "set_ha_display_name", "set_account", "register_account"]
    assert lab.events == []
    assert len(dr.async_get(lab.hass).devices) == 1


@pytest.fixture
async def managed(phone_lab):
    from custom_components.voip_stack.esphome_provisioning import EspHomeProvisioner
    lab = phone_lab
    lab.controller = EspHomeProvisioner(lab.hass, lab.entry)
    lab.runtime.phone_provisioning = lab.controller
    return lab


def phone_record(lab):
    from custom_components.voip_stack.managed_phones import managed_records
    return managed_records(lab.entry)[lab.device.id]


async def bind_account(lab, phone):
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    uri = "sip:127.0.0.1:5060"
    auth = build_digest_authorization(challenge_header=lab.registrar._challenge()[1],
        username=phone.username, password=phone.password, method="REGISTER", uri=uri)
    request = sip.parse_message(sip.build_request("REGISTER", uri, [
        ("Via", "SIP/2.0/UDP 127.0.0.2:41000;branch=z9hG4bK-managed;rport"),
        ("From", f"<sip:{phone.username}@127.0.0.1>;tag=managed"),
        ("To", f"<sip:{phone.username}@127.0.0.1>"), ("Call-ID", "managed-register"),
        ("CSeq", "1 REGISTER"), ("Contact", f"<sip:{phone.username}@127.0.0.2:41000>"),
        ("Expires", "120"), ("Authorization", auth)]))
    assert (await lab.registrar.handle_register(request, ("127.0.0.2", 41000), "UDP")).status == 200


async def test_identity_idempotence_secret_boundaries_and_sip_readiness(managed, caplog):
    from custom_components.voip_stack.diagnostics import async_get_config_entry_diagnostics
    from custom_components.voip_stack.websocket_api import _get_voip_devices
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    assert phone.username == "esp_84c7bb000001"
    assert lab.registrar.accounts[phone.username].password == phone.password
    assert lab.sent[1][1] == dict(account_id="ha", registrar="127.0.0.1", port=5060,
        domain="127.0.0.1", username=phone.username, password=phone.password,
        auth_username=phone.username, proxy="", proxy_port=5060, transport="auto", expires=3600, persist=True)
    await lab.controller.async_sync()
    assert len(lab.sent) == 3
    assert len(lab.runtime.endpoints.endpoints) == 1
    device, = await _get_voip_devices(lab.hass)
    assert device["managed"] and not device["registered"]
    assert lab.runtime.endpoints.get(phone.endpoint_id).availability.value == "offline"
    await bind_account(lab, phone)
    device, = await _get_voip_devices(lab.hass)
    assert device["registered"]
    assert lab.runtime.endpoints.get(phone.endpoint_id).availability.value == "available"
    diagnostics = await async_get_config_entry_diagnostics(lab.hass, lab.entry)
    for public in (device, diagnostics, lab.events, caplog.text, repr(phone)):
        assert phone.password not in str(public)
    assert len(dr.async_get(lab.hass).devices) == 1


async def test_name_node_and_address_changes_keep_mac_credential_and_endpoint(managed):
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    dr.async_get(lab.hass).async_update_device(lab.device.id, name_by_user="Camera")
    lab.native.runtime_data.device_info = replace(lab.native.runtime_data.device_info, name="renamed_node")
    lab.hass.config_entries.async_update_entry(lab.native, data={**lab.native.data, "host": "127.0.0.3"})
    await lab.controller.async_sync()
    renamed = phone_record(lab)
    assert renamed.username == phone.username and renamed.password == phone.password
    assert renamed.endpoint_id == phone.endpoint_id
    assert renamed.route_id == "renamed_node" and renamed.api_host == "127.0.0.3"
    assert lab.sent[3:] == [("set_ha_display_name", {"name": "Camera"})]
    assert len(lab.runtime.endpoints.endpoints) == 1


@pytest.mark.parametrize("mode", ["incomplete", "wrong-mac", "legacy", "no-register-action"])
async def test_only_complete_mac_verified_real_native_actions_enroll(managed, mode):
    lab = managed
    native = lab.native.runtime_data
    if mode == "incomplete":
        native.first_connect_done.clear()
    elif mode == "wrong-mac":
        native.device_info = replace(native.device_info, mac_address="84:C7:BB:00:00:02")
    elif mode == "legacy":
        native.services = {}
    else:
        native.services = {key: service for key, service in native.services.items() if service.name != "register_account"}
    await lab.controller.async_sync()
    assert not lab.entry.data.get("managed_esphome_phones")
    assert not lab.registrar.accounts and not lab.sent


async def test_manual_account_collision_is_rejected_without_partial_owner(managed, caplog):
    lab = managed
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "sip_accounts": [
        {"username": "esp_84c7bb000001", "password": "manual-fixture-only", "name": "Manual QA"}]})
    await lab.controller.async_sync()
    assert not lab.entry.data.get("managed_esphome_phones") and not lab.sent
    assert not lab.runtime.endpoints.endpoints
    assert "manual-fixture-only" not in caplog.text


async def test_manual_account_cannot_take_a_persisted_managed_username(managed):
    from custom_components.voip_stack.store import update_sip_accounts
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    previous = dict(lab.entry.subentries)
    with pytest.raises(ValueError):
        update_sip_accounts(lab.hass, [{"username": phone.username, "display_name": "Takeover",
                                      "password": "manual-fixture-only"}])
    assert dict(lab.entry.subentries) == previous
    assert lab.registrar.accounts[phone.username].password == phone.password


async def test_live_settings_owner_retains_unknown_but_clears_explicit_empty(managed):
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack.websocket_api import _get_voip_devices
    lab = managed
    entities = {}
    for domain, role, value in (("text", "voip_extension", "301"),
            ("text", "voip_ring_groups", "Casa"), ("text", "voip_conference_groups", "Famiglia"),
            ("switch", "voip_ring_on_conference", "on")):
        entity = er.async_get(lab.hass).async_get_or_create(domain, "esphome", f"84c7bb000001-{role}",
            config_entry=lab.native, device_id=lab.device.id, suggested_object_id=role)
        entities[role] = entity.entity_id
        lab.hass.states.async_set(entity.entity_id, value)
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    phone = phone_record(lab)
    endpoint = lab.runtime.endpoints.get(phone.endpoint_id)
    assert (endpoint.extension, endpoint.ring_group, endpoint.conference_group, endpoint.conference_ring) == (
        "301", "Casa", "Famiglia", True)
    for entity_id in entities.values():
        lab.hass.states.async_set(entity_id, "unavailable")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert phone_record(lab) == phone
    lab.hass.states.async_set(entities["voip_ring_groups"], "")
    lab.hass.states.async_set(entities["voip_conference_groups"], "")
    lab.hass.states.async_set(entities["voip_ring_on_conference"], "off")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    device, = await _get_voip_devices(lab.hass)
    endpoint = lab.runtime.endpoints.get(phone.endpoint_id)
    assert (device["ring_group"], endpoint.ring_group, endpoint.conference_group, endpoint.conference_ring) == ("", "", "", False)
    assert len(lab.sent) == 3


@pytest.mark.parametrize("replacement", ["native", "client", "inventory", "runtime", "close"])
async def test_api_await_cannot_commit_into_replaced_owner(managed, replacement):
    lab = managed
    lab.gate.entered, lab.gate.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(lab.controller.async_sync())
    await asyncio.wait_for(lab.gate.entered.wait(), 1)
    if replacement == "native":
        lab.native.runtime_data = replace(lab.native.runtime_data, services=native_services())
    elif replacement == "client":
        lab.native.runtime_data.client = SimpleNamespace(is_connected=True, execute_service=AsyncMock())
    elif replacement == "inventory":
        lab.native.runtime_data.services = {}
    elif replacement == "runtime":
        lab.entry.runtime_data = replace(lab.runtime, phone_provisioning=None)
    else:
        closing = asyncio.create_task(lab.controller.async_close())
        await asyncio.sleep(0)
    lab.gate.release.set()
    await task
    if replacement == "close":
        await closing
    assert [name for name, _data in lab.sent] == ["set_ha_display_name"]
    assert not lab.controller._delivered


async def test_offline_and_legacy_suspend_without_mailbox_or_credential_purge(managed):
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack.websocket_api import _get_voip_devices
    from custom_components.voip_stack.store import sip_accounts
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "esphome_mailboxes": {phone.endpoint_id: {"enabled": True, "greeting_media": "media-source://fixture"}}})
    await bind_account(lab, phone)
    native = lab.native.runtime_data
    native.available = False
    native.client.is_connected = False
    await lab.controller.async_sync()
    assert phone_record(lab) == phone
    assert lab.registrar.registered_contacts(phone.username)
    native.available = True
    native.client.is_connected = True
    entity = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "84c7bb000001-voip_endpoint",
        config_entry=lab.native, device_id=lab.device.id, suggested_object_id="legacy_voip_endpoint")
    lab.hass.states.async_set(entity.entity_id,
        "Legacy|127.0.0.2|5060|40000|full_duplex|16000:s16le:1:20|16000:s16le:1:20|sip_udp|301")
    native.services = {}
    await lab.controller.async_sync()
    suspended = phone_record(lab)
    assert not suspended.active and suspended.password == phone.password
    assert lab.entry.data["esphome_mailboxes"][phone.endpoint_id]["enabled"]
    assert not sip_accounts(lab.hass) and not lab.registrar.registered_contacts(phone.username)
    device, = await _get_voip_devices(lab.hass)
    assert not device.get("managed") and device["host"] == "127.0.0.2"
    assert lab.runtime.endpoints.get(phone.endpoint_id).username == phone.username
    # Restart/bootstrap with an incomplete API inventory cannot reactivate it.
    native.first_connect_done.clear()
    native.services = native_services()
    assert not sip_accounts(lab.hass)
    native.first_connect_done.set()
    await lab.controller.async_sync()
    assert phone_record(lab).active and phone_record(lab).password == phone.password
    assert len(lab.runtime.endpoints.endpoints) == 1


def varint(value):
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


async def read_varint(reader):
    value, shift = 0, 0
    while True:
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 127) << shift
        if not byte & 128:
            return value
        shift += 7


async def test_private_native_rpc_uses_real_api_client_and_protobuf_wire(managed, socket_enabled):
    from aioesphomeapi import APIClient, api_pb2
    from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
    lab = managed
    messages, completed, actors = [], asyncio.Event(), set()

    async def serve(reader, writer):
        actors.add(asyncio.current_task())
        try:
            while True:
                assert await reader.readexactly(1) == b"\0"
                size, kind = await read_varint(reader), await read_varint(reader)
                raw = await reader.readexactly(size)
                request = MESSAGE_TYPE_TO_PROTO[kind].FromString(raw)
                response = None
                if kind == 1:
                    response = (2, api_pb2.HelloResponse(api_version_major=1, api_version_minor=10, name="phone_node"))
                elif kind == 3:
                    response = (4, api_pb2.AuthenticationResponse())
                elif kind == 7:
                    response = (8, api_pb2.PingResponse())
                elif kind == 5:
                    response = (6, api_pb2.DisconnectResponse())
                elif isinstance(request, api_pb2.ExecuteServiceRequest):
                    messages.append(request)
                    if len(messages) == 3:
                        completed.set()
                if response is not None:
                    payload = response[1].SerializeToString()
                    writer.write(b"\0" + varint(len(payload)) + varint(response[0]) + payload)
                    await writer.drain()
                if kind == 5:
                    return
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            actors.discard(asyncio.current_task())

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    client = APIClient("127.0.0.1", server.sockets[0].getsockname()[1], expected_name="phone_node")
    try:
        await client.connect(login=True)
        lab.native.runtime_data.client = client
        await lab.controller.async_sync()
        await asyncio.wait_for(completed.wait(), 2)
        assert client.is_connected and len(messages) == 3
        phone = phone_record(lab)
        assert [message.key for message in messages] == [1, 2, 3]
        account = {arg.name: value for arg, value in zip(native_services()[2].args, messages[1].args, strict=True)}
        assert account["account_id"].string_ == "ha"
        assert account["username"].string_ == phone.username
        assert account["password"].string_ == phone.password
        assert account["transport"].string_ == "auto" and account["persist"].bool_
        assert account["port"].int_ == 5060 and account["expires"].int_ == 3600
        assert not lab.events
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(actors), return_exceptions=True)


async def test_managed_default_does_not_replace_multiple_manual_qa_accounts(managed):
    lab = managed
    manual = [{"username": f"qa_{index}", "display_name": f"QA {index}", "password": "fixture-only"}
              for index in range(2)]
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data, "sip_accounts": manual})
    await lab.controller.async_sync()
    phone = phone_record(lab)
    assert set(lab.registrar.accounts) == {phone.username, "qa_0", "qa_1"}
    assert lab.entry.data["sip_accounts"] == manual
    assert [data["account_id"] for action, data in lab.sent if action == "set_account"] == ["ha"]


async def test_managed_physical_peer_and_contact_have_one_roster_identity(managed):
    from custom_components.voip_stack.peer_snapshot import async_build_peer_snapshot
    from custom_components.voip_stack.endpoint_routing import roster_from_peers
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    await bind_account(lab, phone)
    peers = await async_build_peer_snapshot(lab.hass)
    entries = roster_from_peers(lab.hass, peers, lab.registrar.registered_roster_entries())
    phones = [entry for entry in entries if entry.metadata.get("endpoint_id") == phone.endpoint_id]
    assert len(phones) == 1
    assert phones[0].id == phone.username and phones[0].sip_uri.startswith(f"sip:{phone.username}@")
    assert len(dr.async_get(lab.hass).devices) == 1


async def test_restart_keeps_credential_and_replays_only_on_current_native_owner(managed):
    from custom_components.voip_stack.esphome_provisioning import EspHomeProvisioner
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    await lab.controller.async_close()
    lab.controller = EspHomeProvisioner(lab.hass, lab.entry)
    lab.runtime.phone_provisioning = lab.controller
    await lab.controller.async_sync()
    assert phone_record(lab) == phone
    assert len(lab.sent) == 6 and len(lab.runtime.endpoints.endpoints) == 1
    await lab.controller.async_sync()
    assert len(lab.sent) == 6


@pytest.mark.parametrize("explicit_identity", [False, True])
async def test_quarantine_record_migration_preserves_matching_mac_origin_identity(managed, explicit_identity):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    lab = managed
    historical_id = "esphome:retained-physical-identity" if explicit_identity else f"esphome:{lab.device.id}"
    record = dict(device_id=lab.device.id, origin_entry_id=lab.native.entry_id,
        mac="84:C7:BB:00:00:01", username="phone_node", password="synthetic-retained-password",
        display_name="Ingresso", route_id="phone_node", api_host="127.0.0.2", extension="301",
        sip_transport="udp")
    if explicit_identity:
        record["endpoint_id"] = historical_id
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "managed_esphome_phones": {lab.device.id: record}})
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=historical_id, device_id=lab.device.id,
        username="phone_node", name="Ingresso", kind=EndpointKind.ESPHOME))
    await lab.controller.async_sync()
    migrated = phone_record(lab)
    assert migrated.username == "phone_node"
    assert migrated.password == "synthetic-retained-password"
    assert migrated.endpoint_id == historical_id
    assert list(lab.registrar.accounts) == ["phone_node"]
    assert len(lab.runtime.endpoints.endpoints) == 1
    assert lab.sent[1][1]["username"] == "phone_node"
    assert lab.sent[1][1]["password"] == "synthetic-retained-password"


async def test_source_ip_await_cannot_send_to_replaced_api_owner(managed, monkeypatch):
    from custom_components.voip_stack import esphome_provisioning as module
    lab = managed
    entered, release = asyncio.Event(), asyncio.Event()

    async def address(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return "127.0.0.1"

    lab.runtime.transport_config["advertise_host"] = ""
    monkeypatch.setattr(module.network, "async_get_source_ip", address)
    task = asyncio.create_task(lab.controller.async_sync())
    await entered.wait()
    lab.native.runtime_data = replace(lab.native.runtime_data, services=native_services())
    release.set()
    await task
    assert not lab.sent and not lab.controller._delivered


async def test_rpc_error_never_logs_secret_parameters(managed, caplog):
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)

    async def fail(_service, _data):
        raise RuntimeError("third-party-error=" + phone.password)

    lab.native.runtime_data.client = SimpleNamespace(is_connected=True, execute_service=fail)
    await lab.controller.async_sync()
    assert "RuntimeError" in caplog.text
    assert phone.password not in caplog.text and "third-party-error" not in caplog.text


@pytest.mark.parametrize("remove", ["device", "origin"])
async def test_permanent_removal_drains_calls_subscriptions_and_media_before_forgetting(managed, tmp_path, remove):
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.endpoint_session import CleanupStage
    from custom_components.voip_stack.mailbox import get_archive
    from custom_components.voip_stack.sip_application import SipApplicationMethods
    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    await bind_account(lab, phone)
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "esphome_mailboxes": {phone.endpoint_id: {"enabled": True}}})
    archive = await get_archive(lab.hass)
    files = []
    for kind in ("voicemail", "recording"):
        writer = await archive.begin(phone.endpoint_id, kind=kind)
        assert writer.offer(b"\x01\x02" * 320, AudioFormat(16000, frame_ms=20))
        await writer.finish()
        files.append(archive.segment_path(writer.identity, phone.endpoint_id))
    assert all(path.exists() for path in files)
    methods = SipApplicationMethods(lab.hass, lab.registrar)
    lab.runtime.sip.adopt_component("sip_application", methods, closer=methods.stop)
    notify_entered, notify_cancelled = asyncio.Event(), asyncio.Event()

    async def sender(_request, _address, _result):
        notify_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            notify_cancelled.set()

    request = sip.parse_message(sip.build_request("SUBSCRIBE", f"sip:{phone.username}@127.0.0.1", [
        ("Via", "SIP/2.0/UDP 127.0.0.2:41000;branch=z9hG4bK-sub"),
        ("From", f"<sip:{phone.username}@127.0.0.1>;tag=subscriber"),
        ("To", f"<sip:{phone.username}@127.0.0.1>"), ("Call-ID", "managed-subscribe"),
        ("CSeq", "1 SUBSCRIBE"), ("Contact", f"<sip:{phone.username}@127.0.0.2:41000>"),
        ("Event", "presence"), ("Expires", "120")]))
    result = await methods.handle(request, ("127.0.0.2", 41000), "UDP", sender)
    assert result.status == 200
    result.follow_up_scheduler()
    await asyncio.wait_for(notify_entered.wait(), 1)
    calls = lab.runtime.sip
    calls._bind_endpoint_registry(lab.runtime.endpoints)
    session = calls.upsert("managed-early-call", owner="router", state="ringing")
    assert calls.claim_endpoint(session.call_id, phone.endpoint_id)
    closed = []

    async def close(reason):
        closed.append(reason)
        assert all(path.exists() for path in files)

    assert calls.own_resource(session.call_id, "managed-fixture", object(), close, stage=CleanupStage.MEDIA)
    lab.controller.start()
    await lab.hass.async_block_till_done(wait_background_tasks=False)
    if remove == "device":
        dr.async_get(lab.hass).async_remove_device(lab.device.id)
    else:
        assert await lab.hass.config_entries.async_remove(lab.native.entry_id)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert closed == ["phone_removed"]
    assert not calls.sessions and not lab.runtime.endpoints.endpoints
    assert not methods.subscriptions and not methods.notifiers
    assert notify_cancelled.is_set()
    assert not lab.entry.data.get("managed_esphome_phones")
    assert not lab.entry.data.get("esphome_mailboxes")
    assert not lab.registrar.accounts and not lab.registrar.registrations
    assert not any(path.exists() for path in files)
    await methods.stop()
    await archive.close()


async def test_removal_during_api_await_cannot_resurrect_account(managed):
    from homeassistant.config_entries import ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
    from homeassistant.helpers.dispatcher import async_dispatcher_send
    lab = managed
    lab.gate.entered, lab.gate.release = asyncio.Event(), asyncio.Event()
    lab.controller.start()
    await asyncio.wait_for(lab.gate.entered.wait(), 1)
    async_dispatcher_send(lab.hass, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.REMOVED, lab.native)
    await asyncio.sleep(0)
    assert not lab.registrar.accounts
    lab.gate.release.set()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert [name for name, _data in lab.sent] == ["set_ha_display_name"]
    assert not lab.entry.data.get("managed_esphome_phones") and not lab.runtime.endpoints.endpoints


@pytest.mark.parametrize("global_registrar,refused", [(False, 405), (True, 401)])
async def test_managed_register_uses_generated_secret_on_real_udp_then_legacy_revokes(
        managed, socket_enabled, global_registrar, refused):
    import socket
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.sip_listener import SipUdpServer
    from custom_components.voip_stack.const import CONF_REGISTRAR_ENABLED
    lab = managed
    lab.runtime.transport_config[CONF_REGISTRAR_ENABLED] = global_registrar
    await lab.controller.async_sync()
    phone = phone_record(lab)
    server = SipUdpServer(host="127.0.0.1", port=0, local_ip="127.0.0.1", local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=AsyncMock(),
        on_register=lab.registrar.handle_register)
    assert await server.start()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.2", 0))
    peer.setblocking(False)
    address = server.transport.get_extra_info("sockname")
    port = peer.getsockname()[1]
    uri = f"sip:127.0.0.1:{address[1]}"
    statuses = []

    async def send(cseq, authorization=""):
        headers = [("Via", f"SIP/2.0/UDP 127.0.0.2:{port};branch=z9hG4bK-{cseq};rport"),
            ("From", f"<sip:{phone.username}@127.0.0.1>;tag=managed"),
            ("To", f"<sip:{phone.username}@127.0.0.1>"), ("Call-ID", "wire-managed"),
            ("CSeq", f"{cseq} REGISTER"), ("Contact", f"<sip:{phone.username}@127.0.0.2:{port}>"),
            ("Expires", "120")]
        if authorization:
            headers.append(("Authorization", authorization))
        await lab.hass.loop.sock_sendto(peer, sip.build_request("REGISTER", uri, headers), address)
        raw, _ = await asyncio.wait_for(lab.hass.loop.sock_recvfrom(peer, 8192), 2)
        response = sip.parse_message(raw)
        statuses.append(response.status_code)
        return response

    try:
        challenge = await send(1)
        assert challenge.status_code == 401
        authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username=phone.username, password=phone.password, method="REGISTER", uri=uri)
        assert (await send(2, authorization)).status_code == 200
        assert lab.registrar.registered_contacts(phone.username)
        lab.native.runtime_data.services = {}
        await lab.controller.async_sync()
        assert not lab.registrar.registered_contacts(phone.username)
        assert (await send(3, authorization)).status_code == refused
        assert statuses == [401, 200, refused]
    finally:
        peer.close()
        await server.stop()
