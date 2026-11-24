"""Native ESP telemetry cannot own the shared account's PBX call state."""

import asyncio
import json
import re
import socket
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

from aioesphomeapi import DeviceInfo
from homeassistant.components.esphome.entry_data import RuntimeEntryData
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant.common import MockConfigEntry
import pytest

from custom_components.voip_stack import esphome_state_bridge
from custom_components.voip_stack.const import CALL_EVENT
from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.phone_config import endpoint_from_subentry, sync_registry_from_entry
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.store import config_entry
from .test_automation_originate_ha import PHONE_PASSWORD, assert_call_resources_released, lab as lab

pytestmark = pytest.mark.ha


async def register_second_contact(lab):
    """Add this independent UA to the same account through real UDP REGISTER."""
    peer = lab.phones[1]
    address = peer.transport.get_extra_info("sockname")
    identity = "shared-second-wire-registration"
    responses = asyncio.Queue()
    previous = peer.datagram_received
    def receive(raw, remote):
        message = sip.parse_message(raw)
        if message.is_response and message.header("Call-ID") == identity:
            responses.put_nowait(message)
        else:
            previous(raw, remote)
    peer.datagram_received = receive
    uri = f"sip:127.0.0.1:{lab.runtime.transport_config['sip_port']}"
    async def exchange(sequence, authorization=""):
        headers = [("Via", f"SIP/2.0/UDP {address[0]}:{address[1]};branch=z9hG4bK-shared-wire-{sequence};rport"),
            ("From", "<sip:bedroom0@127.0.0.1>;tag=shared-second"), ("To", "<sip:bedroom0@127.0.0.1>"),
            ("Call-ID", identity), ("CSeq", f"{sequence} REGISTER"),
            ("Contact", f"<sip:bedroom0@{address[0]}:{address[1]}>"), ("Expires", "120")]
        if authorization:
            headers.append(("Authorization", authorization))
        peer.transport.sendto(sip.build_request("REGISTER", uri, headers),
            ("127.0.0.1", lab.runtime.transport_config["sip_port"]))
        return await asyncio.wait_for(responses.get(), 2)
    challenge = await exchange(1)
    assert challenge.status_code == 401
    authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
        username="bedroom0", password=PHONE_PASSWORD, method="REGISTER", uri=uri)
    assert (await exchange(2, authorization)).status_code == 200


@pytest.fixture
async def native_shared_account(lab):
    from custom_components.voip_stack.directory_runtime import compose_directory_entries

    entry = config_entry(lab.hass)
    origin = MockConfigEntry(domain="esphome", title="Shared ESP hardware",
        data={"host": "127.0.0.1", "device_name": "shared_esp"})
    origin.add_to_hass(lab.hass)
    hardware = dr.async_get(lab.hass).async_get_or_create(config_entry_id=origin.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, "84:c7:bb:00:00:21")}, name="Shared ESP hardware")
    origin.runtime_data = RuntimeEntryData(entry_id=origin.entry_id, title=origin.title,
        client=SimpleNamespace(is_connected=True), store=MagicMock(), services={}, available=True,
        device_info=DeviceInfo(name="shared_esp", friendly_name="Shared ESP hardware", mac_address="84:C7:BB:00:00:21"))
    origin.runtime_data.first_connect_done.set()
    state = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "84c7bb000021-voip_state",
        config_entry=origin, device_id=hardware.id, suggested_object_id="shared_esp_voip_state")
    destination = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "84c7bb000021-destination",
        config_entry=origin, device_id=hardware.id, suggested_object_id="shared_esp_destination")
    lab.hass.states.async_set(state.entity_id, "idle")
    lab.hass.states.async_set(destination.entity_id, "Foreign PBX conversation")
    account = next(row for row in entry.subentries.values() if row.data.get("username") == "bedroom0")
    lab.hass.config_entries.async_update_subentry(entry, account, data={**account.data,
        "device_id": hardware.id, "origin_entry_id": origin.entry_id, "managed_esphome": True,
        "mac": "84c7bb000021", "route_id": "shared_esp", "active": True})
    # Establish the hardware association through the canonical account DTO.
    # Normal reconciliation preserves an existing runtime device association.
    lab.runtime.endpoints.upsert(endpoint_from_subentry(entry, account))
    sync_registry_from_entry(lab.hass, entry)
    await register_second_contact(lab)
    registrar = lab.calls.component("registrar")
    contacts = registrar.registered_contacts("bedroom0")
    assert len(contacts) == 2 and len({contact.source_port for contact in contacts}) == 2
    automation = [row for row in lab.roster if row.metadata.get("virtual_endpoint") == "automation"]
    lab.roster[:] = [*compose_directory_entries(lab.hass, [], registrar.registered_roster_entries()), *automation]
    lab.runtime.directory.replace(lab.roster)
    inventory = await esphome_state_bridge._get_voip_devices(lab.hass)
    assert len(inventory) == 1 and inventory[0]["endpoint_id"] == "bedroom0"
    assert inventory[0]["entities"]["voip_state"] == state.entity_id
    events = []
    remove = lab.hass.bus.async_listen(CALL_EVENT, lambda event: events.append(dict(event.data)))
    esphome_state_bridge.register_state_event_bridge(lab.hass)
    lab.hardware, lab.native_state, lab.native_account, lab.native_events = hardware, state, account, events
    try:
        yield lab
    finally:
        lab.hass.states.async_set(state.entity_id, "idle")
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        if lab.runtime.esp_state_event_bridge_unsub is not None:
            lab.runtime.esp_state_event_bridge_unsub()
        remove()


async def native_state(lab, value):
    lab.hass.states.async_set(lab.native_state.entity_id, value)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert lab.hass.states.get(lab.native_state.entity_id).state == value


async def caller(lab):
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.1", 0))
    media.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.1", local_name="Independent caller", local_uri_user="bedroom1",
        local_sip_port=0, local_rtp_port=media.getsockname()[1], signaling_transport="UDP",
        username="bedroom1", password=PHONE_PASSWORD, supported_formats=[AudioFormat(16000, "s16le", 1, 20)])
    await client.start()
    return client, media


async def connect(lab, client):
    result = await client.invite(target="bedroom0", remote_host="127.0.0.1",
        remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
    return await client.wait_for_final(timeout=3) if result == "ringing" else result


async def terminate_call(lab, client):
    session = lab.calls.get_session(client.dialog_ids.call_id)
    established = client.dialog is not None
    try:
        await client.terminate()
    finally:
        await client.close()
    # Ingress BYE/200 does not mean the destination dialog has already closed.
    if established:
        await asyncio.wait_for(lab.phones[1].bye.wait(), 2)
    if session is not None:
        await asyncio.wait_for(session.terminated.wait(), 2)


async def duplex_pcm(lab, client, media, *, sequence):
    peer = lab.phones[1]
    loop = asyncio.get_running_loop()
    sent = struct.pack("!320h", *([1500 + sequence] * 320))
    await loop.sock_sendto(media, rtp.build_packet(rtp.RtpPacket(client.dialog.send_format.payload_type,
        sequence, sequence * 320, 0x1234, sent)), (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
    async with asyncio.timeout(2):
        while rtp.parse_packet((await peer.audio.get())[0]).payload != sent:
            pass
    returned = struct.pack("!320h", *([-2100 - sequence] * 320))
    target = int(re.search(rb"m=audio (\d+)", peer.last_request.body)[1])
    await loop.sock_sendto(peer.rtp_socket, rtp.build_packet(rtp.RtpPacket(peer.payload_type,
        sequence, sequence * 320, 0x5678, returned)), ("127.0.0.1", target))
    async with asyncio.timeout(2):
        while rtp.parse_packet(await loop.sock_recv(media, 4096)).payload != returned:
            pass


async def test_foreign_native_call_cannot_block_second_registered_contact(native_shared_account, tmp_path):
    lab = native_shared_account
    lab.phones[0].final_status = 486
    await native_state(lab, "calling")
    await native_state(lab, "in_call")
    client, media = await caller(lab)
    try:
        result = await connect(lab, client)
        evidence = {"result": result, "contacts": len(lab.calls.component("registrar").registered_contacts("bedroom0")),
            "active_call_id": lab.runtime.endpoints.require("bedroom0").active_call_id,
            "sessions": list(lab.calls.sessions), "ua_invite_counts": [len(peer.invites) for peer in lab.phones]}
        (tmp_path / "foreign-native-call.json").write_text(json.dumps(evidence, indent=2))
        assert result == "in_call", evidence
        assert all(peer.invites for peer in lab.phones)
        assert lab.runtime.endpoints.require("bedroom0").active_call_id == client.dialog_ids.call_id
        assert not any(identity.startswith("physical:") for identity in lab.calls.sessions)
        await duplex_pcm(lab, client, media, sequence=1)
    finally:
        await terminate_call(lab, client)
        media.close()
        await native_state(lab, "idle")
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert_call_resources_released(lab)
    assert lab.phones[1].bye.is_set()
    before = [len(peer.invites) for peer in lab.phones]
    lab.phones[1].bye.clear()
    following, following_media = await caller(lab)
    try:
        assert await connect(lab, following) == "in_call"
        assert [len(peer.invites) - count for peer, count in zip(lab.phones, before)] == [1, 1]
        await duplex_pcm(lab, following, following_media, sequence=1)
    finally:
        await terminate_call(lab, following)
        following_media.close()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert lab.phones[1].bye.is_set()
    assert_call_resources_released(lab)


async def test_foreign_native_changes_do_not_publish_state_for_the_live_second_contact(native_shared_account, tmp_path):
    lab = native_shared_account
    lab.phones[0].final_status = 486
    client, media = await caller(lab)
    try:
        assert await connect(lab, client) == "in_call"
        assert all(peer.invites for peer in lab.phones)
        call_id = client.dialog_ids.call_id
        await duplex_pcm(lab, client, media, sequence=1)
        start = len(lab.native_events)
        for value in ("calling", "in_call", "idle"):
            await native_state(lab, value)
        extra = [event for event in lab.native_events[start:] if event.get("scope") == "esp"]
        evidence = {"live_call_id": call_id, "active_call_id": lab.runtime.endpoints.require("bedroom0").active_call_id,
            "session_state": lab.calls.get_session(call_id).state,
            "native_pbx_events": [{key: event.get(key) for key in ("scope", "call_id", "state", "reported_state")} for event in extra]}
        (tmp_path / "already-connected-second-contact.json").write_text(json.dumps(evidence, indent=2))
        assert lab.runtime.endpoints.require("bedroom0").active_call_id == call_id
        assert lab.calls.get_session(call_id).state == "in_call"
        assert not extra, evidence
        await duplex_pcm(lab, client, media, sequence=2)
    finally:
        await terminate_call(lab, client)
        media.close()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert_call_resources_released(lab)


@pytest.mark.parametrize("expired", [False, True], ids=["registered", "expired"])
async def test_native_sensor_keeps_hardware_identity_without_pbx_authority_after_registration_expiry(native_shared_account, expired):
    lab = native_shared_account
    registrar = lab.calls.component("registrar")
    if expired:
        for contact in registrar.registered_contacts("bedroom0"):
            contact.expires_at = 0
        assert registrar.expire()
        assert not registrar.registered_contacts("bedroom0")
    else:
        assert len(registrar.registered_contacts("bedroom0")) == 2
    hardware_id = lab.hardware.id
    entity = er.async_get(lab.hass).async_get(lab.native_state.entity_id)
    identity = (entity.id, entity.entity_id, entity.unique_id, entity.device_id, entity.config_entry_id)
    configuration = dict(lab.native_account.data)
    start = len(lab.native_events)
    for value in ("calling", "incoming", "in_call", "held", "idle"):
        await native_state(lab, value)
        assert lab.runtime.endpoints.require("bedroom0").active_call_id == ""
        assert not lab.calls.sessions
    assert not [event for event in lab.native_events[start:] if event.get("scope") == "esp"]
    kept = er.async_get(lab.hass).async_get(entity.entity_id)
    assert (kept.id, kept.entity_id, kept.unique_id, kept.device_id, kept.config_entry_id) == identity
    assert dr.async_get(lab.hass).async_get(hardware_id).id == hardware_id
    assert dict(lab.native_account.data) == configuration
    assert lab.runtime.endpoints.require("bedroom0").availability.value == ("offline" if expired else "available")
