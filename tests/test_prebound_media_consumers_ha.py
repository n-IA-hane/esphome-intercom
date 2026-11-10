"""First RTP survives existing public automation, conference and pickup owners."""
import asyncio
from dataclasses import replace
import re
import socket
import struct

import pytest
from homeassistant.core import Context

from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from .test_automation_originate_ha import lab as lab, dial
from .test_trunk_calls_ha import trunks as trunks

pytestmark = pytest.mark.ha


def observe_reserved_sockets(monkeypatch):
    from custom_components.voip_stack.media_ports import RtpPortReservation
    sockets = {}
    original = RtpPortReservation.bind_audio_sockets

    def bind(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        for sock in owner._audio_sockets or ():
            sockets[sock.getsockname()[1]] = sock
        return result

    monkeypatch.setattr(RtpPortReservation, "bind_audio_sockets", bind)
    return sockets


def ordinary_phone(lab):
    lab.roster[1] = replace(lab.roster[1], metadata={
        "endpoint_id": "sip:bedroom0", "endpoint_kind": "sip_account", "registered": True,
        "sip_audio_tx_formats": ["L16/16000/1/20"], "sip_audio_rx_formats": ["L16/16000/1/20"],
    })
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0)


async def terminate_calls(lab):
    for session in tuple(lab.calls.sessions.values()):
        barrier = lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier


async def make_caller(lab, name="Independent caller"):
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.2", 0))
    media.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name=name,
        local_sip_port=0, local_rtp_port=media.getsockname()[1],
        supported_formats=[AudioFormat(16000, frame_ms=20)], codec_policy=CodecPolicy(audio=("L16",)))
    protocol = SipDatagramQueueProtocol(caller.queue)
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(lambda: protocol, local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    return caller, media


@pytest.mark.parametrize("arrival", ["invite", "ack"])
async def test_public_automation_reads_first_peer_packet_once_on_reserved_fd(lab, monkeypatch, arrival):
    from custom_components.voip_stack import local_call_media
    from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver

    ordinary_phone(lab)
    sockets = observe_reserved_sockets(monkeypatch)
    phone = lab.phones[0]
    captured, decoded, consumed = {}, [], []
    payload = struct.pack("!320h", *([2345] * 320))
    expected = struct.pack("<320h", *([2345] * 320))
    original_decode = RtpAudioReceiver.decode

    def decode(receiver, pt, value):
        result = original_decode(receiver, pt, value)
        if value == payload:
            decoded.extend(result)
        return result

    monkeypatch.setattr(RtpAudioReceiver, "decode", decode)
    original_packet = local_call_media._LocalRtpProtocol.datagram_received

    def packet(protocol, raw, address):
        if raw == captured.get("raw"):
            consumed.append(raw)
        original_packet(protocol, raw, address)

    monkeypatch.setattr(local_call_media._LocalRtpProtocol, "datagram_received", packet)
    original = phone.datagram_received

    def receive(raw, address):
        request = sip.parse_message(raw)
        if request.method == "INVITE":
            port = int(re.search(rb"m=audio (\d+)", request.body)[1])
            pt = int(re.search(rb"a=rtpmap:(\d+) L16/16000", request.body)[1])
            assert b"a=ptime:20" in request.body
            captured.update(port=port, socket=sockets[port], fd=sockets[port].fileno())
            captured["raw"] = rtp.build_packet(rtp.RtpPacket(pt, 4001, 320, 0x314159, payload))
            if arrival == "invite":
                phone.rtp_socket.sendto(captured["raw"], ("127.0.0.1", port))
                assert captured["socket"].recv(4096, socket.MSG_PEEK) == captured["raw"]
        original(raw, address)
        if request.method == "ACK" and arrival == "ack":
            phone.rtp_socket.sendto(captured["raw"], ("127.0.0.1", captured["port"]))

    monkeypatch.setattr(phone, "datagram_received", receive)
    try:
        result = await dial(lab, Context(), answer_timeout=3)
        media = lab.calls.resource_for(result["call_id"], "relay")
        await wait_for(lambda: media.counters["rtp_rx"] == 1)
        assert media.transport.get_extra_info("socket").fileno() == captured["fd"]
        assert consumed == [captured["raw"]] and decoded == [expected]
        await lab.calls.terminate_call_wait(result["call_id"], reason="test_done")
        assert captured["socket"].fileno() < 0
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        assert not lab.calls.endpoint_claims_snapshot()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("0.0.0.0", captured["port"]))
    finally:
        await terminate_calls(lab)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_public_conference_auto_invite_reads_queued_first_rtp_after_leg_exists(trunks, monkeypatch):
    from custom_components.voip_stack import conference
    from custom_components.voip_stack.roster import RosterEntry

    lab = trunks
    ordinary_phone(lab)
    lab.roster.append(RosterEntry("ReservedConference", metadata={"group_type": "conference", "ring_members": ["bedroom0"]}))
    sockets = observe_reserved_sockets(monkeypatch)
    phone = lab.phones[0]
    captured, consumed = {}, []
    payload = struct.pack("!320h", *([3456] * 320))
    original_signal = phone.datagram_received

    def receive(raw, address):
        request = sip.parse_message(raw)
        if request.method == "INVITE":
            port = int(re.search(rb"m=audio (\d+)", request.body)[1])
            pt = int(re.search(rb"a=rtpmap:(\d+) L16/16000", request.body)[1])
            assert b"a=ptime:20" in request.body
            captured.update(port=port, socket=sockets[port], fd=sockets[port].fileno(), call_id=request.header("Call-ID"))
            captured["raw"] = rtp.build_packet(rtp.RtpPacket(pt, 4002, 320, 0x271828, payload))
            phone.rtp_socket.sendto(captured["raw"], ("127.0.0.1", port))
            assert captured["socket"].recv(4096, socket.MSG_PEEK) == captured["raw"]
        original_signal(raw, address)

    monkeypatch.setattr(phone, "datagram_received", receive)
    original_packet = conference.ConferenceRoom.handle_rtp

    def handle(room, call_id, raw, address):
        if raw == captured.get("raw"):
            assert call_id in room.legs and room.legs[call_id].role == "auto_invited"
            consumed.append(raw)
        original_packet(room, call_id, raw, address)

    monkeypatch.setattr(conference.ConferenceRoom, "handle_rtp", handle)
    caller, media = await make_caller(lab)
    manager = conference.conference_manager(lab.hass, local_ip="127.0.0.1")
    try:
        result = await caller.invite(target="ReservedConference", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert result == "in_call"
        await wait_for(lambda: bool(consumed))
        room = manager.rooms["ReservedConference"]
        leg = room.legs[captured["call_id"]]
        assert leg.transport.get_extra_info("socket").fileno() == captured["fd"]
        assert leg.rx_packets == 1 and consumed == [captured["raw"]]
        async with asyncio.timeout(3):
            while rtp.parse_packet(await lab.hass.loop.sock_recv(media, 4096)).payload != payload:
                pass
    finally:
        await caller.close()
        await manager.close()
        await terminate_calls(lab)
        media.close()
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        assert not lab.calls.endpoint_claims_snapshot()
        if "socket" in captured:
            assert captured["socket"].fileno() < 0


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_public_pickup_preserves_preanswered_source_and_immediate_picker_packets(trunks, monkeypatch):
    from custom_components.voip_stack import sip_rtp_bridge
    from custom_components.voip_stack.endpoint_routing import roster_from_peers
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.sip_registrar import SipAccount
    from custom_components.voip_stack.sip_trunk import SipTrunkClient, SipTrunkConfig
    from custom_components.voip_stack.trunk_signaling import reuse_registered_trunk_flow

    lab = trunks
    from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {}
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    ordinary_phone(lab)
    lab.phones[0].auto_answer = False
    sockets = observe_reserved_sockets(monkeypatch)
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount("reservation-picker", "Reservation picker", "controlled-test-only")])
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="reservation-picker", name="Reservation picker",
        username="reservation-picker", kind=EndpointKind.SIP_ACCOUNT, availability="available"))
    registration = SipTrunkClient(config=SipTrunkConfig(enabled=True, transport="udp", server="127.0.0.1",
        port=lab.runtime.transport_config["sip_port"], domain="127.0.0.1", username="reservation-picker",
        auth_username="reservation-picker", password="controlled-test-only", expires=60, audio_codecs=("L16",)),
        local_ip="127.0.0.1", local_sip_port=lab.runtime.transport_config["sip_port"])
    caller, source_media = await make_caller(lab)
    picker_media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    picker_media.bind(("127.0.0.1", 0))
    picker_media.setblocking(False)
    picker = SipCallClient(local_ip="127.0.0.1", local_name="Reservation picker", local_uri_user="reservation-picker",
        username="reservation-picker", local_sip_port=0, local_rtp_port=picker_media.getsockname()[1],
        supported_formats=[AudioFormat(16000, frame_ms=20)], codec_policy=CodecPolicy(audio=("L16",)))
    captured, consumed = {}, []
    source_pcm = struct.pack("!320h", *([2345] * 320))
    picker_pcm = struct.pack("!320h", *([-3456] * 320))
    original_source = caller.protocol.datagram_received

    def source_signal(raw, address):
        response = sip.parse_message(raw)
        if response.status_code == 200 and "INVITE" in response.header("CSeq") and response.body and "source_raw" not in captured:
            port = int(re.search(rb"m=audio (\d+)", response.body)[1])
            pt = int(re.search(rb"a=rtpmap:(\d+) L16/16000", response.body)[1])
            captured.update(source_port=port, source_socket=sockets[port], source_fd=sockets[port].fileno())
            captured["source_raw"] = rtp.build_packet(rtp.RtpPacket(pt, 4003, 320, 0x161803, source_pcm))
            source_media.sendto(captured["source_raw"], ("127.0.0.1", port))
            assert sockets[port].recv(4096, socket.MSG_PEEK) == captured["source_raw"]
        original_source(raw, address)

    monkeypatch.setattr(caller.protocol, "datagram_received", source_signal)
    original_create = lab.hass.loop.create_datagram_endpoint

    async def create(*args, **kwargs):
        result = await original_create(*args, **kwargs)
        transport, protocol = result
        if isinstance(protocol, sip_rtp_bridge._RelayProtocol) and protocol.side == "right":
            captured.update(picker_socket=kwargs["sock"], picker_fd=transport.get_extra_info("socket").fileno())
        return result

    monkeypatch.setattr(lab.hass.loop, "create_datagram_endpoint", create)
    original_packet = sip_rtp_bridge._RelayProtocol.datagram_received

    def packet(protocol, raw, address):
        if raw in (captured.get("source_raw"), captured.get("picker_raw")):
            assert protocol.relay.left.can_receive and protocol.relay.right.can_receive
            consumed.append(raw)
        original_packet(protocol, raw, address)

    monkeypatch.setattr(sip_rtp_bridge._RelayProtocol, "datagram_received", packet)
    try:
        await registration.start()
        assert await registration.wait_for_registration(timeout=3)
        assert registrar.registration_matches_source("reservation-picker", "127.0.0.1", registration._udp_local_port, "UDP")
        entries = roster_from_peers(lab.hass, [], registrar.registered_roster_entries())
        lab.roster.extend(entry for entry in entries if entry.id == "reservation-picker")
        assert reuse_registered_trunk_flow(registration, picker)
        original_picker = registration.protocol.datagram_received

        def picker_signal(raw, address):
            response = sip.parse_message(raw)
            if response.status_code == 200 and response.header("Call-ID") == picker.dialog_ids.call_id and "INVITE" in response.header("CSeq") and response.body:
                port = int(re.search(rb"m=audio (\d+)", response.body)[1])
                pt = int(re.search(rb"a=rtpmap:(\d+) L16/16000", response.body)[1])
                captured["picker_raw"] = rtp.build_packet(rtp.RtpPacket(pt, 4004, 320, 0x141421, picker_pcm))
                picker_media.sendto(captured["picker_raw"], ("127.0.0.1", port))
                assert captured["picker_socket"].recv(4096, socket.MSG_PEEK) == captured["picker_raw"]
            original_picker(raw, address)

        monkeypatch.setattr(registration.protocol, "datagram_received", picker_signal)
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], delayed_offer=True, timeout=3)
        assert result == "in_call"
        await asyncio.wait_for(lab.phones[0].invited.wait(), 3)
        assert captured["source_socket"].recv(4096, socket.MSG_PEEK) == captured["source_raw"]
        source_id = caller.dialog_ids.call_id
        assert await picker.invite(target="pickup.bedroom0", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3) == "in_call"
        await wait_for(lambda: lab.calls.resource_for(source_id, "relay") is not None)
        adopted_relay = lab.calls.resource_for(source_id, "relay")
        assert adopted_relay.left_transport.get_extra_info("socket").fileno() == captured["source_fd"], "Pickup must keep the originally published source fd"
        async with asyncio.timeout(3):
            to_source = rtp.parse_packet(await lab.hass.loop.sock_recv(source_media, 4096))
            to_picker = rtp.parse_packet(await lab.hass.loop.sock_recv(picker_media, 4096))
        assert to_source.payload == picker_pcm and to_picker.payload == source_pcm
        relay = lab.calls.resource_for(source_id, "relay")
        assert relay.left_port == captured["source_port"]
        assert relay.left_transport.get_extra_info("socket").fileno() == captured["source_fd"]
        assert relay.right_transport.get_extra_info("socket").fileno() == captured["picker_fd"]
        assert consumed.count(captured["source_raw"]) == consumed.count(captured["picker_raw"]) == 1
        assert relay.dropped == 0
        assert len(lab.calls.sessions) == 1
    finally:
        await picker.close()
        await caller.close()
        await terminate_calls(lab)
        await registration.stop()
        source_media.close()
        picker_media.close()
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        assert not lab.calls.endpoint_claims_snapshot()
        for name in ("source_socket", "picker_socket"):
            if name in captured:
                assert captured[name].fileno() < 0
