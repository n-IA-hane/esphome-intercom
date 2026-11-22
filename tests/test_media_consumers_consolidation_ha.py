"""Native receive owners preserve source admission and conference call lifetimes."""

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_pcm import PcmFrameConverter
from custom_components.voip_stack.audio_ws_frames import BrowserAudioReceiver
from custom_components.voip_stack.conference import ConferenceRoom, _ConferenceLeg, CONFERENCE_FORMAT, conference_manager
from custom_components.voip_stack.conference_ringing import ConferenceRingRuntime, async_ring_conference_members
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.local_call_media import LocalAudioContract, LocalCallMedia
from custom_components.voip_stack.media_ports import RtpPortReservation
from custom_components.voip_stack.outbound_attempts import OutboundLeg
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_client import SipCallClient
from tests.test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha

FORMAT = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20, max_frame_ms=20)


def packet(sequence, *, ssrc=7, value=100, payload_type=96):
    return struct.pack("!BBHII", 0x80, payload_type, sequence, sequence * 320, ssrc) + struct.pack("!320h", *([value] * 320))


def test_browser_receiver_admits_new_source_after_probation_and_decodes_each_packet_once():
    receiver = RtpAudioReceiver(FORMAT)
    incoming = BrowserAudioReceiver("192.0.2.2", 40000)
    session = SimpleNamespace(call_id="browser", recv_format=FORMAT,
        remote_rtp_host="192.0.2.2", signaling_host="192.0.2.2", local_audio_direction="recvonly")
    counters = dict.fromkeys(("drop_addr", "drop_payload_type", "drop_direction", "drop_payload_size", "rtp_rx", "rtp_rx_bytes"), 0)
    observed = []
    receiver.add_pcm_observer(lambda pcm, audio: observed.append((pcm, audio)))

    def receive(raw, port=40000, host="192.0.2.2"):
        return incoming.decode(raw, (host, port), session, decoder=receiver,
            encoder=SimpleNamespace(follow_received=lambda _fmt: False), dtmf_decoder=None,
            dtmf_send_lock=asyncio.Lock(), counters=counters)

    try:
        assert receive(packet(0), host="192.0.2.99") is None
        assert receiver.source_ssrc is None
        assert receive(packet(0)) is not None
        assert receive(packet(0)) is None
        assert receive(packet(10, ssrc=8), port=41000) is None
        assert incoming.ssrc == 7 and incoming.remote_port == 40000
        assert receive(packet(11, ssrc=8, value=500), port=41000) is not None
        assert incoming.ssrc == 8 and incoming.remote_port == 41000
        assert receive(packet(1)) is None
        assert len(observed) == counters["rtp_rx"] == 2
        assert all(audio.sample_rate == 16000 for _pcm, audio in observed)
    finally:
        receiver.close()


async def test_local_application_receiver_keeps_native_observers_and_new_source_proof():
    contract = LocalAudioContract("application", "192.0.2.2", 40000, FORMAT, FORMAT,
                                  local_audio_direction="recvonly")
    media = LocalCallMedia(None, invite=contract, local_rtp_port=42000,
        reservation=SimpleNamespace(release=lambda: None), on_complete=AsyncMock())
    media._accepting_input = True
    observed = []
    media.decoder.add_pcm_observer(lambda pcm, audio: observed.append((pcm, audio)))
    try:
        media.handle_rtp(packet(0), ("192.0.2.99", 40000))
        assert media.remote_ssrc is None and media.decoder.source_ssrc is None
        media.handle_rtp(packet(0), ("192.0.2.2", 40000))
        media.handle_rtp(packet(0), ("192.0.2.2", 40001))
        assert media.remote_rtp_port == 40000
        media.handle_rtp(packet(10, ssrc=8), ("192.0.2.2", 41000))
        assert media.remote_ssrc == 7
        media.handle_rtp(packet(11, ssrc=8, value=500), ("192.0.2.2", 41000))
        assert media.remote_ssrc == 8 and media.remote_rtp_port == 41000
        media.handle_rtp(packet(1), ("192.0.2.2", 40000))
        assert len(observed) == media.counters["rtp_rx"] == 2
        assert media.counters["drop_sequence"] == 1
    finally:
        await media.stop()


def test_conference_receiver_changes_source_without_joining_old_partial_pcm():
    room = ConferenceRoom(SimpleNamespace(), name="Room", local_ip="192.0.2.1")
    receiver = RtpAudioReceiver(FORMAT)
    leg = _ConferenceLeg(call_id="member", caller="Member", role="manual",
        remote_host="192.0.2.2", remote_port=40000, decoder=receiver,
        in_converter=PcmFrameConverter(FORMAT.audio_format, CONFERENCE_FORMAT),
        out_converter=PcmFrameConverter(CONFERENCE_FORMAT, FORMAT.audio_format))
    room.legs["member"] = leg
    try:
        room.handle_rtp("member", packet(0), ("192.0.2.2", 40000))
        room.handle_rtp("member", packet(0), ("192.0.2.2", 40000))
        room.handle_rtp("member", packet(10, ssrc=8), ("192.0.2.2", 41000))
        assert leg.rx_ssrc == 7
        room.handle_rtp("member", packet(11, ssrc=8, value=500), ("192.0.2.2", 41000))
        assert leg.rx_ssrc == 8 and leg.remote_port == 41000
        assert leg.rx_packets == 2 and len(leg.in_fifo) == 1
        assert set(struct.unpack("<320h", leg.in_fifo[0])) == {500}
    finally:
        receiver.close()


class IndependentSipPeer(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        self.queue.put_nowait((sip.parse_message(data), address))

    async def receive(self, method):
        async with asyncio.timeout(5):
            while True:
                message, address = await self.queue.get()
                if message.method == method:
                    return message, address

    def answer(self, request, address, status, *, body=b""):
        port = self.transport.get_extra_info("sockname")[1]
        headers = [("Via", value) for value in request.header_values("Via")]
        headers += [("From", request.header("From")),
            ("To", request.header("To") if ";tag=" in request.header("To") else request.header("To") + ";tag=peer"),
            ("Call-ID", request.header("Call-ID")), ("CSeq", request.header("CSeq")),
            ("Contact", f"<sip:member@127.0.0.1:{port}>")]
        if body:
            headers.append(("Content-Type", "application/sdp"))
        self.transport.sendto(sip.build_response(status, "Response", headers, body), address)


async def test_caller_hangup_cancels_pending_conference_invite_and_cleans_late_2xx(lab, monkeypatch):
    manager = conference_manager(lab.hass, local_ip="127.0.0.1")
    room = ConferenceRoom(lab.hass, name="Pending room", local_ip="127.0.0.1")
    manager.rooms[room.name] = room
    for call_id in ("conference-owner", "conference-resident"):
        lab.calls.upsert(call_id, state="in_call", owner="bridge", caller=call_id, callee=room.name)
        room.legs[call_id] = _ConferenceLeg(call_id=call_id, caller=call_id, role="manual",
            remote_host="127.0.0.1", remote_port=0, local_out=asyncio.Queue(),
            in_converter=PcmFrameConverter(CONFERENCE_FORMAT, CONFERENCE_FORMAT),
            out_converter=PcmFrameConverter(CONFERENCE_FORMAT, CONFERENCE_FORMAT))
        assert manager._own_membership(room.name, call_id, ha_softphone=False)
    owner = lab.calls.get_session("conference-owner")
    owner.metadata["source_endpoint_id"] = "sip:bedroom0"
    resident = lab.calls.get_session("conference-resident")
    transport, peer = await asyncio.get_running_loop().create_datagram_endpoint(
        IndependentSipPeer, local_addr=("127.0.0.1", 0))
    fmt = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20, max_frame_ms=20)
    ports = RtpPortReservation.allocate(lab.hass)
    ports.bind_audio_sockets(ports=(ports.ports[0],))
    client = SipCallClient(local_ip="127.0.0.1", local_name="Room", local_sip_port=0,
        local_rtp_port=ports.ports[0], supported_send_formats=[fmt.audio_format],
        supported_recv_formats=[fmt.audio_format], supported_send_rtp_formats=(fmt,),
        supported_recv_rtp_formats=(fmt,))
    uri = sip.parse_sip_uri(f"sip:member@127.0.0.1:{transport.get_extra_info('sockname')[1]}")
    endpoint = lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:conference-member",
        name="Pending member", kind=EndpointKind.SIP_ACCOUNT, username="member"))
    attempt = OutboundLeg("Pending member", uri, client, ports, endpoint_id=endpoint.endpoint_id)
    runtime = ConferenceRingRuntime(lab.hass, {}, "127.0.0.1", lambda *_args: None,
                                    lambda **_kwargs: attempt)
    task = owner.create_task(async_ring_conference_members(runtime, room_name=room.name,
        caller="Owner", source_host="192.0.2.1", entry=RosterEntry("room",
        metadata={"ring_members": ["Pending member"]}), peers=[], roster_entries=[],
        owner_call_id=owner.call_id), name="conference_ringing")
    termination = None
    try:
        invite, address = await peer.receive("INVITE")
        peer.answer(invite, address, 180)
        termination = asyncio.create_task(owner.terminate(TerminationIntent("remote_hangup")))
        cancel, address = await peer.receive("CANCEL")
        peer.answer(cancel, address, 200)
        answer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Member\r\nc=IN IP4 127.0.0.1\r\n"
                  "t=0 0\r\nm=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000/1\r\n"
                  "a=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n").encode()
        peer.answer(invite, address, 200, body=answer)
        ack, _address = await peer.receive("ACK")
        bye, address = await peer.receive("BYE")
        peer.answer(bye, address, 200)
        await asyncio.wait_for(termination, 5)
        assert ack.header("Call-ID") == bye.header("Call-ID") == invite.header("Call-ID")
        assert task.done() and not owner.tasks
        assert resident.live and not room._closed and set(room.legs) == {resident.call_id}
        assert lab.calls.get_session(client.dialog_ids.call_id) is None or not lab.calls.get_session(client.dialog_ids.call_id).live
        assert ports.released
    finally:
        if termination is not None and not termination.done():
            termination.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *(tuple([termination]) if termination is not None else ()), return_exceptions=True)
        await client.close()
        ports.release()
        transport.close()
        await manager.close(reason="test_done")


async def test_answered_conference_member_moves_to_its_own_session_lifetime(lab):
    manager = conference_manager(lab.hass, local_ip="127.0.0.1")
    room = ConferenceRoom(lab.hass, name="Answered room", local_ip="127.0.0.1")
    manager.rooms[room.name] = room
    owner = lab.calls.upsert("joined-owner", state="in_call", owner="bridge", caller="Owner", callee=room.name)
    owner.metadata["source_endpoint_id"] = "sip:bedroom0"
    room.legs[owner.call_id] = _ConferenceLeg(call_id=owner.call_id, caller="Owner", role="owner",
        remote_host="127.0.0.1", remote_port=0, local_out=asyncio.Queue(),
        in_converter=PcmFrameConverter(CONFERENCE_FORMAT, CONFERENCE_FORMAT),
        out_converter=PcmFrameConverter(CONFERENCE_FORMAT, CONFERENCE_FORMAT))
    assert manager._own_membership(room.name, owner.call_id, ha_softphone=False)
    transport, peer = await asyncio.get_running_loop().create_datagram_endpoint(
        IndependentSipPeer, local_addr=("127.0.0.1", 0))
    ports = RtpPortReservation.allocate(lab.hass)
    ports.bind_audio_sockets(ports=(ports.ports[0],))
    fmt = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20, max_frame_ms=20)
    client = SipCallClient(local_ip="127.0.0.1", local_name="Room", local_sip_port=0,
        local_rtp_port=ports.ports[0], supported_send_formats=[fmt.audio_format],
        supported_recv_formats=[fmt.audio_format], supported_send_rtp_formats=(fmt,),
        supported_recv_rtp_formats=(fmt,))
    uri = sip.parse_sip_uri(f"sip:member@127.0.0.1:{transport.get_extra_info('sockname')[1]}")
    endpoint = lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:conference-member",
        name="Answered member", kind=EndpointKind.SIP_ACCOUNT, username="member"))
    attempt = OutboundLeg("Answered member", uri, client, ports, endpoint_id=endpoint.endpoint_id)
    runtime = ConferenceRingRuntime(lab.hass, {}, "127.0.0.1", lambda *_args: None,
                                    lambda **_kwargs: attempt)
    ringing = owner.create_task(async_ring_conference_members(runtime, room_name=room.name,
        caller="Owner", source_host="192.0.2.1", entry=RosterEntry("room",
        metadata={"ring_members": ["Answered member"]}), peers=[], roster_entries=[],
        owner_call_id=owner.call_id), name="conference_ringing")
    cleanup = None
    try:
        invite, address = await peer.receive("INVITE")
        answer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Member\r\nc=IN IP4 127.0.0.1\r\n"
                  "t=0 0\r\nm=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000/1\r\n"
                  "a=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n").encode()
        peer.answer(invite, address, 200, body=answer)
        await peer.receive("ACK")
        await asyncio.wait_for(ringing, 2)
        member = lab.calls.get_session(client.dialog_ids.call_id)
        assert member is not None and member.live and "conference_dialog" in member.named_tasks
        await owner.terminate(TerminationIntent("remote_hangup"))
        assert member.live and client.dialog is not None
        assert not room._closed and set(room.legs) == {member.call_id}
        assert not ports.released
        cleanup = asyncio.create_task(manager.close(reason="local_hangup"))
        bye, address = await peer.receive("BYE")
        peer.answer(bye, address, 200)
        await asyncio.wait_for(cleanup, 5)
        assert ports.released
    finally:
        if not ringing.done():
            ringing.cancel()
        if cleanup is None:
            cleanup = asyncio.create_task(manager.close(reason="test_done"))
            try:
                bye, address = await peer.receive("BYE")
                peer.answer(bye, address, 200)
            except TimeoutError:
                pass
        await asyncio.gather(ringing, cleanup, return_exceptions=True)
        await client.close()
        ports.release()
        transport.close()


async def test_local_receiver_one_shot_delivers_the_last_packet_without_future_rtp():
    contract = LocalAudioContract("tail", "192.0.2.2", 40000, FORMAT, FORMAT,
                                  local_audio_direction="recvonly")
    media = LocalCallMedia(None, invite=contract, local_rtp_port=42000,
        reservation=SimpleNamespace(release=lambda: None), on_complete=AsyncMock())
    media._accepting_input = True
    native = []
    media.decoder.add_pcm_observer(lambda pcm, _format: native.append(pcm))
    try:
        media.handle_rtp(packet(0), ("192.0.2.2", 40000))
        assert await asyncio.wait_for(media.rx_queue.get(), .5) == struct.pack("<320h", *([100] * 320))
        media.handle_rtp(packet(2, value=500), ("192.0.2.2", 40000))
        assert media.decoder.pending_reorder_deadline is not None
        tail = await asyncio.wait_for(media.rx_queue.get(), .5)
        assert set(struct.unpack("<320h", tail)) == {500}
        assert media.decoder.pending_reorder_deadline is None
        assert len(native) == media.counters["rtp_rx"] == 2
    finally:
        await media.stop()
