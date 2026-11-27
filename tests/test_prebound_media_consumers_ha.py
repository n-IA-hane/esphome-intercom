"""First RTP survives existing public automation, conference and pickup owners."""
import asyncio
from dataclasses import asdict
from fractions import Fraction
import json
import re
import socket
import struct

import pytest
from homeassistant.core import Context

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from .test_automation_originate_ha import lab as lab, dial
from .test_trunk_calls_ha import trunks as trunks, sip_test_account
from .support.rtp_samples import receive_l16_samples

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
    lab.runtime.endpoints.update("bedroom0", availability="available")


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
    account = sip_test_account(lab, "bedroom1")
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.2", 0))
    media.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name=name,
        username=account.username, password=account.password,
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
    peer_payload = struct.pack("!320h", *([2345] * 320))
    expected = struct.pack("<320h", *([2345] * 320))
    original_decode_packet = RtpAudioReceiver.decode_packet

    def decode_packet(receiver, payload_type: int, payload: bytes) -> bytes:
        result = original_decode_packet(receiver, payload_type, payload)
        if payload == peer_payload:
            decoded.append(result)
        return result

    monkeypatch.setattr(RtpAudioReceiver, "decode_packet", decode_packet)
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
            offered = sdp.parse_sdp(request.body)
            # CC-2: the requested receive quantum can differ from this
            # independent peer's 20 ms packet, within the hard envelope.
            assert offered["ptime"] > 0 and offered["maxptime"] >= 20
            captured["receive_bytes"] = int(offered["ptime"] * 16 * 2)
            captured.update(port=port, socket=sockets[port], fd=sockets[port].fileno())
            captured["raw"] = rtp.build_packet(rtp.RtpPacket(pt, 4001, 320, 0x314159, peer_payload))
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
        assert consumed == [captured["raw"]]
        assert b"".join(decoded) == expected
        assert decoded == [expected]  # One validated 16 kHz/20 ms native PCM packet.
        # The validated packet precedes duration conversion. Keep the local
        # receive quantum tied to the actual SDP, independently of wire ptime.
        assert media.decoder.selected_format.audio_format.nominal_frame_bytes == captured["receive_bytes"]
        assert media.rx_converter.src.nominal_frame_bytes == captured["receive_bytes"]
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
            offered = sdp.parse_sdp(request.body)
            assert offered["ptime"] > 0 and offered["maxptime"] >= 20
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
async def test_public_pickup_preserves_preanswered_source_and_immediate_picker_packets(trunks, monkeypatch, tmp_path, record_property):
    from custom_components.voip_stack import sip_rtp_bridge
    from custom_components.voip_stack.endpoint_routing import roster_from_peers
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sync_registry_from_entry
    from custom_components.voip_stack.sip_registrar import account_from_mapping
    from custom_components.voip_stack.store import config_entry
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
    entry = config_entry(lab.hass)
    configured = _add_phone_subentry(lab.hass, entry, data={
        "kind": "sip_account", "username": "reservation-picker", "name": "Reservation picker",
        "username_source": "user", "password": "controlled-test-only", "enabled": True,
    }, title="Reservation picker")
    sync_registry_from_entry(lab.hass, entry)
    registrar.update_accounts([*registrar.accounts.values(),
        account_from_mapping(dict(configured.data))])
    registration = SipTrunkClient(config=SipTrunkConfig(options_interval=30, enabled=True, transport="udp", server="127.0.0.1",
        port=lab.runtime.transport_config["sip_port"], domain="127.0.0.1", username="reservation-picker",
        auth_username="reservation-picker", password="controlled-test-only", expires=60, audio_codecs=("L16",)),
        local_ip="127.0.0.1", local_sip_port=lab.runtime.transport_config["sip_port"])
    caller, source_media = await make_caller(lab)
    picker_media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    picker_media.bind(("127.0.0.1", 0))
    picker_media.setblocking(False)
    picker = SipCallClient(local_ip="127.0.0.1", local_name="Reservation picker", local_uri_user="reservation-picker",
        username=registration.config.username, password=registration.config.password,
        local_sip_port=0, local_rtp_port=picker_media.getsockname()[1],
        supported_formats=[AudioFormat(16000, frame_ms=20)], codec_policy=CodecPolicy(audio=("L16",)))
    captured, consumed = {}, []
    wire_evidence = {"source_packets": [], "picker_packets": []}
    evidence_path = tmp_path / "pickup-media-contract.json"
    record_property("pickup_media_contract", str(evidence_path))

    def store_evidence():
        for name, peer in (("source", caller), ("picker", picker)):
            if peer.dialog is not None:
                wire_evidence[name + "_contract"] = {
                    "send": asdict(peer.dialog.send_format),
                    "recv": asdict(peer.dialog.recv_format),
                    "recv_formats": [asdict(fmt) for fmt in peer.dialog.recv_formats],
                }
        evidence_path.write_text(json.dumps(wire_evidence, indent=2) + "\n")

    send_source_dialog = caller._send_dialog_request

    def observe_source_dialog(raw, host, port):
        sent = send_source_dialog(raw, host, port)
        message = sip.parse_message(raw)
        if sent and message.method == "ACK" and message.body:
            wire_evidence["source_ack_sdp"] = message.body.decode()
            store_evidence()
        return sent

    monkeypatch.setattr(caller, "_send_dialog_request", observe_source_dialog)
    source_pcm = struct.pack("!320h", *([2345] * 320))
    picker_pcm = struct.pack("!320h", *([-3456] * 320))
    original_source = caller.protocol.datagram_received

    def source_signal(raw, address):
        response = sip.parse_message(raw)
        if response.status_code == 200 and "INVITE" in response.header("CSeq") and response.body and "source_raw" not in captured:
            wire_evidence["source_offer_sdp"] = response.body.decode()
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
        original_picker = picker._handle_invite_success

        async def picker_signal(exchange, response):
            if response.status_code == 200 and response.header("Call-ID") == picker.dialog_ids.call_id and "INVITE" in response.header("CSeq") and response.body:
                wire_evidence["picker_answer_sdp"] = response.body.decode()
                port = int(re.search(rb"m=audio (\d+)", response.body)[1])
                pt = int(re.search(rb"a=rtpmap:(\d+) L16/16000", response.body)[1])
                captured["picker_raw"] = rtp.build_packet(rtp.RtpPacket(pt, 4004, 320, 0x141421, picker_pcm))
                picker_media.sendto(captured["picker_raw"], ("127.0.0.1", port))
                assert captured["picker_socket"].recv(4096, socket.MSG_PEEK) == captured["picker_raw"]
            return await original_picker(exchange, response)

        # Observe the real response before ACK on either registered UDP or
        # an RFC size-selected TCP flow. The RTP arrival window is unchanged.
        monkeypatch.setattr(picker, "_handle_invite_success", picker_signal)
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], delayed_offer=True, timeout=3)
        assert result == "in_call"
        await asyncio.wait_for(lab.phones[0].invited.wait(), 3)
        assert captured["source_socket"].recv(4096, socket.MSG_PEEK) == captured["source_raw"]
        source_id = caller.dialog_ids.call_id
        assert await picker.invite(target="pickup.bedroom0", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3) == "in_call"
        assert "picker_raw" in captured
        await wait_for(lambda: lab.calls.resource_for(source_id, "relay") is not None)
        adopted_relay = lab.calls.resource_for(source_id, "relay")
        assert adopted_relay.left_transport.get_extra_info("socket").fileno() == captured["source_fd"], "Pickup must keep the originally published source fd"

        async def receive_source():
            raw = await lab.hass.loop.sock_recv(source_media, 4096)
            wire_evidence["source_packets"].append(raw.hex())
            store_evidence()
            return raw

        async def receive_picker():
            raw = await lab.hass.loop.sock_recv(picker_media, 4096)
            wire_evidence["picker_packets"].append(raw.hex())
            store_evidence()
            return raw

        async def receive_source_pcm():
            # The real ACK owns every accepted receive alias, independently
            # of the preferred mapping retained in caller.dialog.recv_format.
            audio = wire_evidence["source_ack_sdp"].split("m=audio ", 1)[1].split("\nm=", 1)[0]
            accepted = {int(value) for value in audio.splitlines()[0].split()[2:]}
            mappings = {int(pt): (int(rate), int(channels or "1"))
                for pt, rate, channels in re.findall(
                    r"^a=rtpmap:(\d+) L16/(\d+)(?:/(\d+))?\r?$", audio, re.MULTILINE)
                if int(pt) in accepted}
            declared = {fmt.payload_type: (fmt.sample_rate, fmt.channels)
                for fmt in caller.dialog.recv_formats if fmt.encoding == "L16"}
            duration_ms = Fraction(len(picker_pcm) * 1000,
                2 * picker.dialog.send_format.sample_rate)
            assert duration_ms == 20
            packets, payloads = [], []
            sample_count = 0
            while True:
                packet = rtp.parse_packet(await receive_source())
                assert packet.payload_type in mappings, str(evidence_path)
                rate, channels = mappings[packet.payload_type]
                assert declared[packet.payload_type] == (rate, channels)
                assert channels == 1 and rate in {16000, 48000}
                assert packet.payload and len(packet.payload) % 2 == 0
                if packets:
                    previous = packets[-1]
                    assert packet.payload_type == previous.payload_type
                    assert packet.ssrc == previous.ssrc
                    assert packet.sequence == (previous.sequence + 1) & 0xffff
                    assert packet.timestamp == (previous.timestamp + len(previous.payload) // 2) & 0xffffffff
                packets.append(packet)
                payloads.append(packet.payload)
                sample_count += len(packet.payload) // 2
                expected_samples = rate * duration_ms / 1000
                assert expected_samples.denominator == 1
                assert sample_count <= expected_samples
                if sample_count == expected_samples:
                    break
            received = b"".join(payloads)
            if rate == 16000:
                assert received == picker_pcm
            else:
                assert len(packets) == 2 and all(len(packet.payload) == 480 * 2 for packet in packets)
                assert sample_count == 960
                samples = struct.unpack("!960h", received)
                # The required 16-to-48 kHz interpolation has 24 source
                # samples of finite support, or 72 output samples (1.5 ms).
                # Count that startup in the full duration before testing DC.
                assert all(abs(value - (-3456)) <= 3 for value in samples[72:])

        async with asyncio.timeout(3):
            await receive_source_pcm()
            to_picker = await receive_l16_samples(receive_picker, len(source_pcm), picker.dialog.recv_format.payload_type)
        assert to_picker == source_pcm
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
