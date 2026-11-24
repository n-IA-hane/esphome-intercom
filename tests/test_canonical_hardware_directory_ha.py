"""Hardware descriptors cannot override canonical SIP account and Contact data."""

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from homeassistant.core import ServiceCall
from homeassistant.helpers import entity_registry as er

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    managed as managed,
    phone_lab as phone_lab,
    bind_account,
    phone_record,
)

pytestmark = pytest.mark.ha

DESCRIPTORS = json.loads((Path(__file__).parent / "fixtures/retired_endpoint_descriptors.json").read_text())
OPTIONS = (
    "v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Standard capabilities\r\n"
    "c=IN IP4 127.0.0.2\r\nt=0 0\r\nm=audio 0 RTP/AVP 96 97\r\n"
    "a=rtpmap:96 L16/16000/1\r\na=rtpmap:97 L16/48000/1\r\n"
    "a=ptime:20\r\na=maxptime:40\r\na=sendrecv\r\n"
)


async def registered_account(lab):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.core import sdp

    await lab.controller.async_sync()
    phone = phone_record(lab)
    await bind_account(lab, phone)
    contact, = lab.registrar.registered_contacts(phone.username)
    contact.capabilities = sdp.options_capabilities(OPTIONS)
    recompute_account_availability(lab.hass)
    return phone, contact


def wire_offer(row):
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
    from custom_components.voip_stack.endpoint_routing import sip_target_rtp_audio_profile

    profile = sip_target_rtp_audio_profile(row)
    return sdp.build_offer_directional("127.0.0.1", "127.0.0.1", 40000,
        list(profile.send_formats), list(profile.recv_formats),
        send_rtp_formats=profile.send_rtp_formats, recv_rtp_formats=profile.recv_rtp_formats,
        local_capabilities=HA_AUDIO_CAPABILITIES)


@pytest.mark.parametrize("descriptor", DESCRIPTORS, ids=lambda value: "descriptor-" + str(DESCRIPTORS.index(value)))
async def test_private_sensor_changes_neither_route_offer_directory_nor_account(managed, descriptor):
    from custom_components.voip_stack.device_resolver import entity_role
    from custom_components.voip_stack.directory_runtime import routing_snapshot
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook
    from custom_components.voip_stack.router import resolve_ha_router

    lab = managed
    phone, contact = await registered_account(lab)
    sensor = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "84c7bb000001-voip_endpoint",
        config_entry=lab.native, device_id=lab.device.id)
    coordinator = entry_phonebook(lab.hass, lab.runtime)
    await coordinator.async_start()
    try:
        before = routing_snapshot(lab.runtime)
        row = next(item for item in before if item.id == phone.username)
        route = resolve_ha_router(phone.username, before)
        offer = wire_offer(row)
        generation = lab.runtime.directory.generation
        accounts = {key: deepcopy(item.as_dict()) for key, item in lab.entry.subentries.items()}
        endpoint = lab.runtime.endpoints.require(phone.username)
        capabilities = deepcopy(contact.capabilities)
        lab.hass.states.async_set(sensor.entity_id, descriptor,
            {"endpoint": descriptor, "sip_port": 5999, "extension": "999", "video": "h264"})
        await lab.hass.async_block_till_done()
        assert entity_role(sensor) == ""
        assert lab.runtime.directory.generation == generation
        after = routing_snapshot(lab.runtime)
        current = next(item for item in after if item.id == phone.username)
        assert after == before
        assert resolve_ha_router(phone.username, after) == route
        assert wire_offer(current) == offer
        assert lab.runtime.endpoints.require(phone.username) == endpoint
        assert contact.capabilities == capabilities
        assert {key: item.as_dict() for key, item in lab.entry.subentries.items()} == accounts
    finally:
        await coordinator.async_close()


async def test_account_hardware_getter_is_read_only_and_preserves_native_camera_control_and_ids(managed, monkeypatch):
    from custom_components.voip_stack.esphome_phone_inventory import _get_voip_devices
    from custom_components.voip_stack.phone_control import PhoneOperation
    from homeassistant.components.esphome.manager import _async_register_service
    from custom_components.voip_stack.phone_config import sync_registry_from_entry

    lab = managed
    phone, contact = await registered_account(lab)
    registry = er.async_get(lab.hass)
    camera = registry.async_get_or_create("camera", "esphome", "84c7bb000001-camera",
        config_entry=lab.native, device_id=lab.device.id)
    camera = registry.async_update_entity(camera.entity_id, new_entity_id="camera.kept_preview")
    native = lab.native.runtime_data
    action = next(row for row in native.services.values() if row.name == "start_call_on_line")
    _async_register_service(lab.hass, native, native.device_info, action)
    await lab.hass.async_block_till_done()
    sync_registry_from_entry(lab.hass, lab.entry)
    endpoint = lab.runtime.endpoints.require(phone.username)
    native_id = endpoint.account_device_id, lab.device.id, camera.id, camera.entity_id
    before = lab.runtime.endpoints.endpoints
    with monkeypatch.context() as getter_only:
        getter_only.setattr(lab.runtime.endpoints, "upsert", Mock(side_effect=AssertionError("getter cannot upsert")))
        getter_only.setattr(lab.runtime.endpoints, "update", Mock(side_effect=AssertionError("getter cannot update")))
        device, = await _get_voip_devices(lab.hass)
    handle = await lab.runtime.phones.resolve_source(ServiceCall(lab.hass, "voip_stack", "call",
        {"device_id": lab.device.id}), PhoneOperation.ORIGINATE)
    assert lab.runtime.endpoints.endpoints == before
    assert device["name"] == endpoint.name
    assert device["capabilities"] == sorted(endpoint.capabilities)
    assert device["camera_entity_id"] == "camera.kept_preview"
    assert device["entities"]["camera"] == "camera.kept_preview"
    assert device["sip_audio_tx_formats"] == contact.capabilities["sip_audio_tx_formats"]
    assert device["sip_audio_rx_formats"] == contact.capabilities["sip_audio_rx_formats"]
    assert handle.supports(PhoneOperation.ORIGINATE) and handle.device_id == lab.device.id
    assert native_id == (endpoint.account_device_id, lab.device.id, camera.id, camera.entity_id)
    assert device["endpoint_type"] == "sip_account"


async def test_native_api_online_without_contact_keeps_account_offline_and_sends_no_route(managed):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.esphome_phone_inventory import _get_voip_devices
    from custom_components.voip_stack.peer_snapshot import async_build_peer_snapshot

    lab = managed
    phone, _contact = await registered_account(lab)
    native_id = lab.runtime.endpoints.require(phone.username).account_device_id
    lab.registrar.remove_registration(phone.username)
    recompute_account_availability(lab.hass)
    device, = await _get_voip_devices(lab.hass)
    assert lab.native.runtime_data.available and lab.native.runtime_data.client.is_connected
    assert device["api_available"] and not device["registered"]
    assert device["availability"] == "offline"
    assert device["host"] == "" and device["sip_port"] is None
    assert await async_build_peer_snapshot(lab.hass) == []
    endpoint = lab.runtime.endpoints.require(phone.username)
    assert endpoint.availability.value == "offline" and endpoint.account_device_id == native_id


async def test_phonebook_sensor_keeps_its_native_identity_with_six_field_account_projection(managed):
    from custom_components.voip_stack.phonebook_coordinator import entry_phonebook
    from custom_components.voip_stack.sensor import VoipPhonebookSensor

    lab = managed
    phone, _contact = await registered_account(lab)
    coordinator = entry_phonebook(lab.hass, lab.runtime)
    await coordinator.async_start()
    try:
        sensor = VoipPhonebookSensor(coordinator)
        assert sensor.unique_id == "voip_stack_phonebook"
        assert sensor.entity_id == "sensor.voip_phonebook"
        assert sensor.native_value == "1 entry"
        assert sensor.extra_state_attributes["phonebook"] == coordinator.phonebook
        assert sensor.extra_state_attributes["roster_json"] == coordinator.roster_json
        assert json.loads(coordinator.roster_json)
        fields = coordinator.phonebook.split("|")
        assert fields == [phone.display_name, phone.username, "127.0.0.2", "41000", "0", "udp"]
        assert len(fields) == 6
    finally:
        await coordinator.async_close()


async def test_registered_opaque_contact_is_preserved_on_wire_without_becoming_the_account_identity(managed, socket_enabled):
    import asyncio
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.dial_plan import RingPolicy, build_sip_contact_targets
    from custom_components.voip_stack.directory_runtime import compose_directory_entries
    from custom_components.voip_stack.peer import sip_uri_for_peer
    from custom_components.voip_stack.peer_snapshot import async_build_peer_snapshot
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sync_registry_from_entry
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.store import sip_accounts

    lab = managed
    _add_phone_subentry(lab.hass, lab.entry, title="Reception", data={"kind": "sip_account",
        "name": "Reception", "username": "Reception", "password": "fixture-reception-password"})
    sync_registry_from_entry(lab.hass, lab.entry)
    lab.registrar.update_accounts(sip_accounts(lab.hass))
    received = asyncio.Queue()
    connections = set()
    async def peer(reader, writer):
        connections.add(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next((int(line.partition(b":")[2]) for line in head.split(b"\r\n")
                    if line.partition(b":")[0].lower() in {b"content-length", b"l"}), 0)
                body = await reader.readexactly(length)
                request = sip.parse_message(head + body)
                if request.method == "INVITE":
                    received.put_nowait(request)
                    headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
                    headers.append(("To", request.header("To") + ";tag=ordinary-phone"))
                    writer.write(sip.build_response(486, "Busy Here", headers))
                    await writer.drain()
                elif request.method == "ACK":
                    break
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(writer)
    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    first = f"sip:opaque-device@127.0.0.1:{port};transport=tcp;ob"
    second = "sip:other-instance@127.0.0.3:42002;transport=tcp;ob"
    registrar_uri = "sip:127.0.0.1:5060"
    authorization = build_digest_authorization(challenge_header=lab.registrar._challenge()[1],
        username="Reception", password="fixture-reception-password", method="REGISTER", uri=registrar_uri)
    register = sip.parse_message(sip.build_request("REGISTER", registrar_uri, [
        ("Via", f"SIP/2.0/TCP 127.0.0.1:{port};branch=z9hG4bK-opaque;rport"),
        ("From", "<sip:Reception@127.0.0.1>;tag=reception"), ("To", "<sip:Reception@127.0.0.1>"),
        ("Call-ID", "opaque-contact-registration"), ("CSeq", "1 REGISTER"),
        ("Contact", f"<{first}>;q=1"), ("Contact", f"<{second}>;q=0.5"),
        ("Expires", "120"), ("User-Agent", "Ordinary SIP Phone"), ("Authorization", authorization)]))
    client = SipCallClient(local_ip="127.0.0.1", local_name="Caller", local_uri_user="Caller",
        local_sip_port=5060, local_rtp_port=40000, signaling_transport="TCP")
    try:
        assert (await lab.registrar.handle_register(register, ("127.0.0.1", port), "TCP")).status == 200
        recompute_account_availability(lab.hass)
        projected, = await async_build_peer_snapshot(lab.hass)
        assert projected.request_uri_user == projected.sip_uri_user == projected.name == "Reception"
        assert projected.endpoint_kind == "sip_account"
        bindings = lab.registrar.registered_contacts("Reception")
        effective = bindings[0].contact_uri
        advertised = sip.parse_sip_uri(first)
        actual = sip.parse_sip_uri(effective)
        assert actual.user == advertised.user == "opaque-device"
        assert dict(actual.params) == dict(advertised.params) == {"transport": "tcp", "ob": None}
        assert str(sip_uri_for_peer(projected, default_port=5060)) == effective
        rows = compose_directory_entries(lab.hass, [projected], lab.registrar.registered_roster_entries())
        account = next(row for row in rows if row.id == "Reception")
        assert account.extension == "" and account.name == "Reception"
        branches = build_sip_contact_targets(["Reception"], rows, policy=RingPolicy())
        assert {branch.uri for branch in branches} == {binding.contact_uri for binding in bindings}
        assert {sip.parse_sip_uri(branch.uri).user for branch in branches} == {"opaque-device", "other-instance"}
        assert all(branch.endpoint_id == "Reception" and branch.roster_entry.name == "Reception" for branch in branches)
        target = sip_uri_for_peer(projected, default_port=5060)
        result = await client.invite(target=projected.request_uri_user, remote_host=target.host,
            remote_sip_port=target.port, request_uri=str(target), target_display_name=projected.name, timeout=2)
        request = await asyncio.wait_for(received.get(), 2)
        assert request.uri == effective
        assert sip.name_addr_display_name(request.header("To")) == "Reception"
        assert result == "busy" and client.last_sip_status_code == 486
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
        for writer in tuple(connections):
            writer.close()
            await writer.wait_closed()


async def test_upgrade_records_cannot_authenticate_until_native_account_migration_and_keep_memberships(managed):
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.group_config import group_records, reconcile_group_records
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    from custom_components.voip_stack.store import sip_accounts

    lab = managed
    old = "esphome:" + lab.device.id
    raw = {"device_id": lab.device.id, "origin_entry_id": lab.native.entry_id,
        "mac": "84c7bb000001", "username": "Reception", "username_source": "user",
        "password": "fixture-migration-password", "endpoint_id": old,
        "display_name": "Reception", "queues": ["Support"], "ring_group": "Front Ring"}
    lab.hass.config_entries.async_update_entry(lab.entry, version=7, minor_version=3,
        data={**lab.entry.data, "sip_registrar_enabled": True,
              "managed_esphome_phones": {lab.device.id: raw}})
    lab.runtime.transport_config["sip_registrar_enabled"] = True
    lab.controller._refresh_accounts()
    assert sip_accounts(lab.hass) == [] and not lab.registrar.accounts
    uri = "sip:127.0.0.1:5060"
    auth = build_digest_authorization(challenge_header=lab.registrar._challenge()[1],
        username="Reception", password=raw["password"], method="REGISTER", uri=uri)
    request = sip.parse_message(sip.build_request("REGISTER", uri, [
        ("Via", "SIP/2.0/UDP 127.0.0.2:41000;branch=z9hG4bK-no-raw-auth;rport"),
        ("From", "<sip:Reception@127.0.0.1>;tag=migration"), ("To", "<sip:Reception@127.0.0.1>"),
        ("Call-ID", "raw-is-not-account"), ("CSeq", "1 REGISTER"),
        ("Contact", "<sip:Reception@127.0.0.2:41000>"), ("Expires", "120"), ("Authorization", auth)]))
    assert (await lab.registrar.handle_register(request, ("127.0.0.2", 41000), "UDP")).status == 401
    assert not lab.registrar.registrations
    reconcile_group_records(lab.hass, lab.entry)
    await async_migrate_account_identities(lab.hass, lab.entry)
    lab.controller._refresh_accounts()
    assert [account.username for account in sip_accounts(lab.hass)] == ["Reception"]
    assert "managed_esphome_phones" not in lab.entry.data
    assert all(row["members"] == ["Reception"] for row in group_records(lab.entry).values())
    native = next(row for row in lab.entry.subentries.values() if row.subentry_type == "phone")
    assert "queues" not in native.data and "ring_group" not in native.data
    await bind_account(lab, phone_record(lab))
    assert lab.registrar.registered_contacts("Reception")


async def test_common_endpoint_dialer_keeps_registered_contact_profile_and_real_offer(managed, socket_enabled, unused_udp_port_factory):
    import asyncio
    import socket
    from urllib.parse import quote
    from custom_components.voip_stack.core import sip, sdp
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.directory_runtime import compose_directory_entries
    from custom_components.voip_stack.endpoint_dialing import EndpointDialer
    from custom_components.voip_stack.endpoint_routing import EndpointRouteResolver, peer_video_codec
    from custom_components.voip_stack.peer_snapshot import async_build_peer_snapshot
    from custom_components.voip_stack.sip_runtime import uri_transport, enable_reused_signaling_flow
    from custom_components.voip_stack.media_ports import RtpPortReservation
    from custom_components.voip_stack.sip_endpoint import SipEndpointManager
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_FORMATS

    lab = managed
    phone, _previous = await registered_account(lab)
    sip_port = unused_udp_port_factory()
    lab.runtime.transport_config["sip_port"] = sip_port
    lab.registrar.local_sip_port = sip_port
    uri = f"sip:127.0.0.1:{sip_port}"
    user = quote(phone.username, safe="-._~")
    auth = build_digest_authorization(challenge_header=lab.registrar._challenge()[1],
        username=phone.username, password=phone.password, method="REGISTER", uri=uri)
    request = sip.parse_message(sip.build_request("REGISTER", uri, [
        ("Via", "SIP/2.0/UDP 127.0.0.2:41000;branch=z9hG4bK-contact-profile;rport"),
        ("From", f"<sip:{user}@127.0.0.1>;tag=profile"), ("To", f"<sip:{user}@127.0.0.1>"),
        ("Call-ID", "contact-profile"), ("CSeq", "1 REGISTER"),
        ("Contact", f"<sip:{user}@127.0.0.2:41000>"), ("Expires", "120"),
        ("User-Agent", "Dahua UAC/V4.511.0.0"), ("Authorization", auth)]))
    manager = SipEndpointManager(host="127.0.0.1", port=sip_port, local_ip="127.0.0.1",
        local_rtp_port=40000, supported_formats=list(HA_SIP_PCM_FORMATS), on_invite=None,
        on_register=lab.registrar.handle_register, udp_enabled=True, tcp_enabled=False)
    lab.runtime.sip.adopt_component("udp_listener", manager, closer=manager.stop)
    assert await manager.start()
    # Register over the same real listener that the selected outbound-flow
    # adapter will borrow. No signaling-flow or admission guard is replaced.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wire:
        wire.bind(("127.0.0.2", 41000))
        wire.setblocking(False)
        loop = asyncio.get_running_loop()
        async def send_register(cseq, authorization=""):
            headers = [(name, value) for name, value in request.headers
                if name.casefold() not in {"via", "cseq", "authorization", "content-length"}]
            headers.extend([("Via", f"SIP/2.0/UDP 127.0.0.2:41000;branch=z9hG4bK-real-profile-{cseq};rport"),
                            ("CSeq", f"{cseq} REGISTER")])
            if authorization:
                headers.append(("Authorization", authorization))
            await loop.sock_sendto(wire, sip.build_request("REGISTER", uri, headers), ("127.0.0.1", sip_port))
            raw, _address = await asyncio.wait_for(loop.sock_recvfrom(wire, 65535), 2)
            return sip.parse_message(raw)
        challenge = await send_register(1)
        assert challenge.status_code == 401
        authenticated = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username=phone.username, password=phone.password, method="REGISTER", uri=uri)
        assert (await send_register(2, authenticated)).status_code == 200
    contact, = lab.registrar.registered_contacts(phone.username)
    contact.capabilities = sdp.options_capabilities(OPTIONS + "m=video 0 RTP/AVP 26\r\na=sendrecv\r\n")
    peers = await async_build_peer_snapshot(lab.hass)
    rows = compose_directory_entries(lab.hass, peers, lab.registrar.registered_roster_entries())
    entry = next(row for row in rows if row.id == phone.username)
    dialer = EndpointDialer(hass=lab.hass, local_ip="127.0.0.1", config=lab.runtime.transport_config,
        route_resolver=EndpointRouteResolver(lab.hass, "127.0.0.1", sip_port),
        sip_uri_transport=uri_transport, enable_reused_tcp_connection=enable_reused_signaling_flow)
    _uri, peer, selected = dialer.sip_uri_for_member(phone.username, peers, rows)
    assert peer is not None and selected is entry
    assert selected.metadata["user_agent"] == "Dahua UAC/V4.511.0.0"
    assert selected.metadata["sip_audio_rx_formats"] == contact.capabilities["sip_audio_rx_formats"]
    assert peer_video_codec(peer, selected) == "jpeg"
    reservation = RtpPortReservation.allocate(lab.hass)
    leg = None
    try:
        # This is the exact shared builder called by conference ringing, pickup
        # and automation. The SIP client and media reservation are production objects.
        leg = dialer.prepare_outbound_leg(member=phone.username, peers=peers, roster_entries=rows,
            local_name="Caller", local_rtp_port_index=0, port_reservation=reservation)
        assert leg is not None and leg.bridge_to_softphone
        leg.client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
        mappings = sdp.offered_pcm_formats(leg.client._local_sdp_body, allow_dahua_pcm=True)
        assert any(fmt.encoding == "PCM" for fmt in mappings)
        assert {fmt.encoding for fmt in mappings} >= {"PCMA", "PCMU"}
    finally:
        if leg is not None:
            await leg.client.close()
        reservation.release()


async def test_canonical_hardware_entity_index_rename_and_late_idle_keep_new_claim(managed, monkeypatch):
    import asyncio
    from custom_components.voip_stack import esphome_state_bridge

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    entities = er.async_get(lab.hass)
    state = next(row for row in entities.entities.values() if row.platform == "esphome" and "voip_state" in row.unique_id)
    assert lab.runtime.endpoints.by_entity_id(state.entity_id).endpoint_id == phone.username
    esphome_state_bridge.register_state_event_bridge(lab.hass)
    old_id = state.entity_id
    entities.async_update_entity(old_id, new_entity_id="sensor.renamed_native_state")
    await lab.hass.async_block_till_done()
    assert lab.runtime.endpoints.by_entity_id(old_id) is None
    assert lab.runtime.endpoints.by_entity_id("sensor.renamed_native_state").endpoint_id == phone.username
    # Establish the old native state before subscribing anew. No fake endpoint
    # index or alternate state owner is installed for the ABA witness.
    lab.runtime.esp_state_event_bridge_unsub()
    lab.hass.states.async_set("sensor.renamed_native_state", "in_call")
    lab.runtime.endpoints.claim_call(phone.username, "old-native-call")
    esphome_state_bridge.register_state_event_bridge(lab.hass)
    entered, release = asyncio.Event(), asyncio.Event()
    original = esphome_state_bridge.async_emit_state_event
    captured = {}
    async def pause_terminal(*args, **kwargs):
        captured.update(kwargs)
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(esphome_state_bridge, "async_emit_state_event", pause_terminal)
    try:
        lab.hass.states.async_set("sensor.renamed_native_state", "idle")
        await asyncio.wait_for(entered.wait(), 2)
        assert captured["expected_endpoint_id"] == phone.username
        assert captured["expected_call_id"] == "old-native-call"
        assert lab.runtime.endpoints.release_call(phone.username, "old-native-call")
        lab.runtime.endpoints.claim_call(phone.username, "new-native-call")
        release.set()
        await lab.hass.async_block_till_done()
        assert lab.runtime.endpoints.require(phone.username).active_call_id == "new-native-call"
    finally:
        release.set()
        if lab.runtime.esp_state_event_bridge_unsub is not None:
            lab.runtime.esp_state_event_bridge_unsub()
        lab.runtime.endpoints.release_call(phone.username, "new-native-call")


async def test_missing_native_actions_repair_is_owned_by_provisioning_and_checks_schema(managed):
    from dataclasses import replace
    from homeassistant.helpers import issue_registry as ir
    from custom_components.voip_stack.esphome_phone_inventory import _get_voip_devices

    lab = managed
    await lab.controller.async_sync()
    original = lab.native.runtime_data.services
    issue_id = "esphome_actions_" + lab.device.id
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is None
    lab.native.runtime_data.services = {key: service for key, service in original.items() if service.name != "answer_call"}
    await lab.controller.async_sync()
    issue = ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id)
    assert issue is not None and "answer_call" in issue.translation_placeholders["actions"]
    lab.native.runtime_data.services = {key: replace(service, args=service.args[:2])
        if service.name == "start_call_on_line" else service for key, service in original.items()}
    await lab.controller.async_sync()
    issue = ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id)
    assert issue is not None and "start_call_on_line" in issue.translation_placeholders["actions"]
    lab.native.runtime_data.services = original
    await _get_voip_devices(lab.hass)
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is not None
    await lab.controller.async_sync()
    assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue_id) is None


async def test_account_enabled_is_independent_of_native_auto_provisioning_and_api_offline(managed):
    from urllib.parse import quote
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.phone_config import update_phone_subentry
    from custom_components.voip_stack.store import sip_accounts
    from .test_sip_account_username_identity_ha import register_client

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    account = next(row for row in lab.entry.subentries.values() if row.data.get("username") == phone.username)
    original = lab.native.runtime_data.services
    lab.native.runtime_data.services = {key: service for key, service in original.items() if service.name != "register_ha_account"}
    await lab.controller.async_sync()
    assert account.data["active"] is False and account.data.get("enabled", True)
    lab.native.runtime_data.available = False
    lab.native.runtime_data.client.is_connected = False
    await lab.controller.async_sync()
    assert sip_accounts(lab.hass)[0].enabled
    await register_client(lab, phone, host="127.0.0.9", port=42000)
    assert lab.registrar.registered_contacts(phone.username)
    update_phone_subentry(lab.hass, lab.entry, phone.username, {"enabled": False})
    lab.native.runtime_data.services = original
    lab.native.runtime_data.available = True
    lab.native.runtime_data.client.is_connected = True
    await lab.controller.async_sync()
    assert account.data["enabled"] is False and not sip_accounts(lab.hass)[0].enabled
    uri = "sip:127.0.0.1:5060"
    auth = build_digest_authorization(challenge_header=lab.registrar._challenge()[1],
        username=phone.username, password=phone.password, method="REGISTER", uri=uri)
    user = quote(phone.username, safe="-._~")
    request = sip.parse_message(sip.build_request("REGISTER", uri, [
        ("Via", "SIP/2.0/UDP 127.0.0.9:42000;branch=z9hG4bK-explicit-disabled;rport"),
        ("From", f"<sip:{user}@127.0.0.1>;tag=disabled"), ("To", f"<sip:{user}@127.0.0.1>"),
        ("Call-ID", "disabled-account"), ("CSeq", "1 REGISTER"),
        ("Contact", f"<sip:{user}@127.0.0.9:42000>"), ("Expires", "120"), ("Authorization", auth)]))
    assert (await lab.registrar.handle_register(request, ("127.0.0.9", 42000), "UDP")).status == 401
