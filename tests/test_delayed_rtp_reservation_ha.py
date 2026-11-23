"""Immediate peer RTP is retained by one existing bridge socket owner."""
import asyncio
from dataclasses import replace
from fractions import Fraction
import re
import socket
import struct

import pytest
from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from tests.test_trunk_calls_ha import lab as lab, sip_test_account, trunks as trunks
from tests.test_automation_call import application as application
from tests.support.rtp_samples import receive_l16_samples

pytestmark = pytest.mark.ha

@pytest.mark.timeout(45)
@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("finish", ["terminate", "ack-timeout"])
async def test_initial_delayed_offer_is_bound_before_sdp_without_decoders(trunks, monkeypatch, finish):
    """The UAS's first SDP is already public before an ACK chooses the route."""
    from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver

    lab = trunks
    signal = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    signal.bind(("127.0.0.2", 0))
    signal.setblocking(False)
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.2", 0))
    media.setblocking(False)
    media.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_RECVERR", 11), 1)
    call_id = f"reservation-delayed-{signal.getsockname()[1]}"
    decoders = []
    original_init = RtpAudioReceiver.__init__

    def initialize(decoder, *args, **kwargs):
        decoders.append(decoder)
        original_init(decoder, *args, **kwargs)

    monkeypatch.setattr(RtpAudioReceiver, "__init__", initialize)
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization

    account = sip_test_account(lab, "bedroom1")
    target = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    uri = f"sip:bedroom0@127.0.0.1:{target[1]}"

    def request(method, sequence, *, to="", authorization=""):
        headers = [
            ("Via", f"SIP/2.0/UDP 127.0.0.2:{signal.getsockname()[1]};branch=z9hG4bKdelayed{sequence}"),
            ("From", f"<sip:{account.username}@127.0.0.2>;tag=caller"),
            ("To", to or "<sip:bedroom0@127.0.0.1>"), ("Call-ID", call_id),
            ("CSeq", f"{sequence} {method}"), ("Max-Forwards", "70"),
            ("Contact", f"<sip:{account.username}@127.0.0.2:{signal.getsockname()[1]}>"),
        ]
        if authorization:
            headers.append(("Authorization", authorization))
        return sip.build_request(method, uri, headers)

    try:
        await lab.hass.loop.sock_sendto(signal, request("INVITE", 1), target)
        async with asyncio.timeout(3):
            denied = sip.parse_message(await lab.hass.loop.sock_recv(signal, 16384))
        assert denied.status_code == 401 and not decoders and not lab.runtime.rtp_port_pool.get("used")
        await lab.hass.loop.sock_sendto(signal, request("ACK", 1, to=denied.header("To")), target)
        digest = build_digest_authorization(challenge_header=denied.header("WWW-Authenticate"),
            username=account.username, password=account.password, method="INVITE", uri=uri)
        await lab.hass.loop.sock_sendto(signal, request("INVITE", 2, authorization=digest), target)
        async with asyncio.timeout(3):
            while True:
                response = sip.parse_message(await lab.hass.loop.sock_recv(signal, 16384))
                if response.status_code == 200:
                    break
        assert not decoders, "No media decoder may start before the delayed ACK answer"
        assert lab.calls.resource_for(call_id, "relay") is None
        port = int(re.search(rb"m=audio (\d+)", response.body)[1])
        payload_type = int(re.search(rb"a=rtpmap:(\d+) L16/16000", response.body)[1])
        raw = rtp.build_packet(rtp.RtpPacket(payload_type, 617, 1000, 0x10203040, b"\x12\x34" * 160))
        await lab.hass.loop.sock_sendto(media, raw, ("127.0.0.1", port))
        await asyncio.sleep(0)
        try:
            error = media.recvmsg(4096, 4096, socket.MSG_ERRQUEUE | socket.MSG_DONTWAIT)
        except BlockingIOError:
            error = None
        assert error is None, "Delayed SDP port generated ICMP before the ACK answer"
        reservation = lab.calls.artifacts_for(call_id).delayed_offer_ports
        assert reservation._audio_sockets is not None, "Delayed SDP published an unbound numeric reservation"
        first = reservation._audio_sockets[0]
        assert first.getsockname()[1] == port
        assert first.recv(4096, socket.MSG_PEEK) == raw
        if finish == "ack-timeout":
            async with asyncio.timeout(35):
                while lab.calls.get_session(call_id) is not None:
                    await asyncio.sleep(0.02)
            assert first.fileno() < 0 and not decoders
            assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        barrier = lab.calls.request_termination(call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier
        signal.close()
        media.close()
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_both_preanswer_leg_packets_survive_transport_adoption(trunks):
    from custom_components.voip_stack.media_ports import RtpPortReservation, release_sip_rtp_port_pair
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

    lab = trunks
    reservation = RtpPortReservation.allocate(lab.hass)
    reservation.bind_audio_sockets()
    sockets = reservation._audio_sockets
    fds = tuple(sock.fileno() for sock in sockets)
    left, right = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
    for peer, host in [(left, "127.0.0.2"), (right, "127.0.0.3")]:
        peer.bind((host, 0))
        peer.setblocking(False)
    fmt = AudioFormat(16000, frame_ms=20)
    relay = SipRtpRelay(left=RtpPeer("127.0.0.2", left.getsockname()[1], 96, fmt),
        right=RtpPeer("127.0.0.3", right.getsockname()[1], 97, fmt),
        left_port=reservation.ports[0], right_port=reservation.ports[1],
        on_release=lambda ports: release_sip_rtp_port_pair(lab.hass, ports))
    first_left = struct.pack("!320h", *([1234] * 320))
    first_right = struct.pack("!320h", *([-2345] * 320))
    try:
        for peer, port, pt, payload in [(left, relay.left_port, 96, first_left), (right, relay.right_port, 97, first_right)]:
            peer.sendto(rtp.build_packet(rtp.RtpPacket(pt, 617, 1000, pt, payload)), ("127.0.0.1", port))
        reservation.transfer_audio_sockets(relay)
        reservation.detach()
        await relay.start()
        assert tuple(t.get_extra_info("socket").fileno() for t in (relay.left_transport, relay.right_transport)) == fds
        async with asyncio.timeout(1):
            returned_left, returned_right = await asyncio.gather(
                lab.hass.loop.sock_recv(left, 4096), lab.hass.loop.sock_recv(right, 4096))
        assert rtp.parse_packet(returned_left).payload == first_right
        assert rtp.parse_packet(returned_right).payload == first_left
        assert relay.forwarded == 2 and relay.dropped == 0
    finally:
        await relay.stop()
        reservation.release()
        left.close()
        right.close()


async def test_local_receiver_and_spare_keep_queued_packets_and_exact_fds(application, monkeypatch):
    from custom_components.voip_stack import local_application, local_call_media
    from custom_components.voip_stack.local_media_bridge import LocalMediaBridge

    app, registry, caller, released = application
    owner = local_application.RtpPortReservation.allocate(app.hass)
    owner.bind_audio_sockets()
    source, spare = owner._audio_sockets
    source_fd, spare_fd = source.fileno(), spare.fileno()
    first_payload = struct.pack("!320h", *([2345] * 320))
    first = rtp.build_packet(rtp.RtpPacket(96, 511, 640, 123, first_payload))
    seen, arrived = [], asyncio.Event()
    original = local_call_media._LocalRtpProtocol.datagram_received

    def receive(protocol, raw, address):
        original(protocol, raw, address)
        if raw == first:
            seen.append(raw)
            arrived.set()

    monkeypatch.setattr(local_call_media._LocalRtpProtocol, "datagram_received", receive)
    picker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    picker.bind(("127.0.0.1", 0))
    picker.setblocking(False)
    handoff = None
    try:
        caller.sendto(first, ("127.0.0.1", owner.ports[0]))
        await app.answer()
        media = app.media
        await asyncio.wait_for(arrived.wait(), 2)
        assert seen == [first] and media.counters["rtp_rx"] == 1
        assert media.transport.get_extra_info("socket").fileno() == source_fd
        assert owner.audio_socket(owner.ports[0]) is None and owner.audio_socket(owner.ports[1]) is spare
        second_payload = struct.pack("!320h", *([-3456] * 320))
        second = rtp.build_packet(rtp.RtpPacket(96, 512, 960, 456, second_payload))
        picker.sendto(second, ("127.0.0.1", owner.ports[1]))
        handoff = await LocalMediaBridge.prepare(media, replace(app.invite, remote_rtp_port=picker.getsockname()[1]))
        assert handoff.relay.right_transport.get_extra_info("socket").fileno() == spare_fd
        assert spare.recv(4096, socket.MSG_PEEK) == second, "Prepared spare must retain RTP until commit"
        relay = handoff.commit()
        assert relay.left_transport.get_extra_info("socket").fileno() == source_fd
        async with asyncio.timeout(2):
            while True:
                returned = rtp.parse_packet(await asyncio.get_running_loop().sock_recv(caller, 4096))
                if returned.payload == second_payload:
                    break
        assert relay.right_rx_packets == 1
        await relay.stop()
        await media.stop()
        owner.release()
        assert released == [owner.ports]
    finally:
        if handoff is not None:
            await handoff.relay.stop()
        await registry.shutdown()
        owner.release()
        picker.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("finish", ["digit", "cancel"])
async def test_reserved_dtmf_reader_borrows_then_returns_same_fd(trunks, finish):
    from custom_components.voip_stack.dtmf import DtmfCollector
    from custom_components.voip_stack.media_ports import RtpPortReservation

    lab = trunks
    owner = RtpPortReservation.allocate(lab.hass)
    owner.bind_audio_sockets()
    borrowed = owner.audio_socket(owner.ports[0])
    fd = borrowed.fileno()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.2", 0))
    collector = DtmfCollector(host="0.0.0.0", port=owner.ports[0], payload_type=101,
        routes={"5": "Desk"}, timeout=1, remote_host="127.0.0.2", rtp_socket=borrowed)
    task = asyncio.create_task(collector.collect())
    transport = None
    try:
        if finish == "digit":
            raw = rtp.build_packet(rtp.RtpPacket(101, 12, 160, 123, bytes([5, 0x8A, 0, 160])))
            peer.sendto(raw, ("127.0.0.1", owner.ports[0]))
            assert await asyncio.wait_for(task, 2) == ("5", "Desk")
        else:
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert borrowed.fileno() == fd and owner.audio_socket(owner.ports[0]) is borrowed
        assert not lab.hass.loop.remove_reader(fd), "Collector must remove its reader before the next owner"
        queue = asyncio.Queue()

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, raw, _addr):
                queue.put_nowait(raw)

        moved = owner.take_audio_socket(owner.ports[0])
        transport, _ = await lab.hass.loop.create_datagram_endpoint(Receiver, sock=moved)
        assert transport.get_extra_info("socket").fileno() == fd
        peer.sendto(b"next-media-owner", ("127.0.0.1", owner.ports[0]))
        assert await asyncio.wait_for(queue.get(), 1) == b"next-media-owner"
        assert queue.empty()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if transport is not None:
            transport.close()
        owner.release()
        peer.close()
        await asyncio.sleep(0)
        assert not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_browser_receiver_adopts_reserved_fd_and_first_packet(trunks, monkeypatch):
    from types import SimpleNamespace
    from custom_components.voip_stack import audio_ws_view as view, audio_ws
    from custom_components.voip_stack.media_ports import RtpPortReservation
    from custom_components.voip_stack.local_call_media import LocalAudioContract

    lab = trunks
    owner = RtpPortReservation.allocate(lab.hass)
    owner.bind_audio_sockets()
    source = owner.audio_socket(owner.ports[0])
    fd = source.fileno()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.2", 0))
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    invite = LocalAudioContract("browser-reserved", "127.0.0.2", peer.getsockname()[1], fmt, fmt,
        remote_sdp=f"v=0\r\nc=IN IP4 127.0.0.2\r\nm=audio {peer.getsockname()[1]} RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n")
    invite = SimpleNamespace(**vars_for_contract(invite), source_host="127.0.0.2")
    lab.calls.upsert(invite.call_id, state="in_call", owner="ha_softphone")
    lab.calls.attach_media(invite.call_id, {"invite": invite, "local_rtp_port": owner.ports[0], "rtp_reservation": owner})
    store = {"call_id": invite.call_id, "state": "in_call"}
    monkeypatch.setattr(view, "active_media_call", lambda *_: SimpleNamespace(call_id=invite.call_id, registry=lab.calls))
    monkeypatch.setattr(view, "_ha_softphone_store", lambda *_: store)
    monkeypatch.setattr(view, "_listen_for_call_end", lambda *_: (asyncio.Event(), lambda: None))
    session = view._active_softphone_media_session(lab.hass, "reservation-browser")
    assert session.rtp_reservation is owner
    raw = rtp.build_packet(rtp.RtpPacket(96, 718, 640, 99, struct.pack("!320h", *([3456] * 320))))
    peer.sendto(raw, ("127.0.0.1", owner.ports[0]))
    adopted = []
    original_create = lab.hass.loop.create_datagram_endpoint

    async def create(*args, **kwargs):
        if kwargs.get("sock") is source:
            adopted.append(kwargs["sock"].fileno())
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(lab.hass.loop, "create_datagram_endpoint", create)

    class Browser:
        def __init__(self):
            self.audio, self.messages = asyncio.Queue(), asyncio.Queue()
        def force_close(self):
            pass
        async def send_json(self, _value):
            pass
        async def send_bytes(self, value):
            self.audio.put_nowait(value)
        def __aiter__(self):
            return self
        async def __anext__(self):
            return await self.messages.get()

    browser = Browser()
    worker = asyncio.create_task(view._run_audio_session(lab.hass, browser, session, endpoint_id="reservation-browser"))
    try:
        # Playback retains the SIP quantum at the card's native 48 kHz clock.
        frame = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 2))
        assert len(frame) == 960 * 2
        samples = struct.unpack("<960h", frame)
        # Ignore only the resampler's startup response to the initial DC step.
        assert all(abs(value - 3456) <= 3 for value in samples[64:])
        assert adopted == [fd] and owner.audio_socket(owner.ports[0]) is None
        assert browser.audio.empty()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        owner.release()
        peer.close()
        barrier = lab.calls.request_termination(invite.call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier
        await asyncio.sleep(0)
        assert source.fileno() < 0 and not lab.runtime.rtp_port_pool.get("used")


def vars_for_contract(contract):
    return {name: getattr(contract, name) for name in contract.__dataclass_fields__}


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("route_kind", ["direct", "group"])
async def test_delayed_call_keeps_published_source_and_first_rtp_both_directions(trunks, monkeypatch, route_kind):
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack import sip_rtp_bridge
    from custom_components.voip_stack.core.codec_policy import CodecPolicy

    lab = trunks
    phone = lab.phones[0]
    phone_index = next(index for index, row in enumerate(lab.roster) if row.id == "bedroom0")
    lab.roster[phone_index] = replace(lab.roster[phone_index], metadata={
        "endpoint_id": "bedroom0", "endpoint_kind": "sip_account", "registered": True,
    })
    lab.runtime.endpoints.update("bedroom0", availability="available")
    target = "bedroom0"
    if route_kind == "group":
        lab.roster.append(RosterEntry("ReservedGroup", metadata={"group_type": "ring", "members": ["bedroom0"]}))
        target = "ReservedGroup"
    lab.runtime.directory.replace(lab.roster)
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.2", 0))
    media.setblocking(False)
    account = sip_test_account(lab, "bedroom1")
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Delayed reservation caller",
        local_uri_user=account.username, username=account.username, password=account.password,
        local_sip_port=0, local_rtp_port=media.getsockname()[1],
        supported_send_formats=[AudioFormat(16000, frame_ms=20)],
        supported_recv_formats=[AudioFormat(16000, frame_ms=20)],
        codec_policy=CodecPolicy(audio=("L16",)))
    protocol = SipDatagramQueueProtocol(caller.queue)
    source_pcm = struct.pack("!320h", *([2345] * 320))
    destination_pcm = struct.pack("!320h", *([-3456] * 320))
    observed, consumed = {}, []
    original_signal = protocol.datagram_received

    def signal(raw, address):
        message = sip.parse_message(raw)
        if message.status_code == 200 and "INVITE" in message.header("CSeq") and message.body and "source_raw" not in observed:
            port = int(re.search(rb"m=audio (\d+)", message.body)[1])
            pt = int(re.search(rb"a=rtpmap:(\d+) L16/16000", message.body)[1])
            observed["source_offer_ptime"] = float(re.search(rb"a=ptime:([\d.]+)", message.body)[1])
            observed["source_offer_maxptime"] = sdp.parse_sdp(message.body)["maxptime"]
            reservation = lab.calls.artifacts_for(caller.dialog_ids.call_id).delayed_offer_ports
            observed["source_port"], observed["source_fd"] = port, reservation.audio_socket(port).fileno()
            observed["source_raw"] = rtp.build_packet(rtp.RtpPacket(pt, 718, 1600, 901, source_pcm))
            media.sendto(observed["source_raw"], ("127.0.0.1", port))
        original_signal(raw, address)

    protocol.datagram_received = signal
    original_phone = phone.datagram_received

    def receive_phone(raw, address):
        message = sip.parse_message(raw)
        original_phone(raw, address)
        if message.method == "ACK" and "destination_raw" not in observed:
            port = int(re.search(rb"m=audio (\d+)", phone.last_request.body)[1])
            observed["destination_raw"] = rtp.build_packet(rtp.RtpPacket(phone.payload_type, 719, 1600, 902, destination_pcm))
            phone.rtp_socket.sendto(observed["destination_raw"], ("127.0.0.1", port))

    monkeypatch.setattr(phone, "datagram_received", receive_phone)
    original_packet = sip_rtp_bridge._RelayProtocol.datagram_received

    def received_packet(protocol, raw, address):
        if raw in (observed.get("source_raw"), observed.get("destination_raw")):
            consumed.append(raw)
        original_packet(protocol, raw, address)

    monkeypatch.setattr(sip_rtp_bridge._RelayProtocol, "datagram_received", received_packet)
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(lambda: protocol, local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    native_send = caller.transport.sendto

    def capture_answer(raw, address=None):
        native_send(raw, address)
        if raw.startswith(b"ACK "):
            answer = sip.parse_message(raw)
            if answer.body:
                observed["caller_answer"] = answer.body

    monkeypatch.setattr(caller.transport, "sendto", capture_answer)
    try:
        result = await caller.invite(target=target, remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], delayed_offer=True, timeout=3)
        assert result == "in_call"
        assert caller.dialog.send_format.encoding == caller.dialog.recv_format.encoding == "L16"
        # The late offer's receive preference drives our TX. Our receive
        # preference remains 20 ms; offer/answer need not be symmetric.
        assert caller.dialog.send_format.frame_ms == observed["source_offer_ptime"]
        assert caller.dialog.recv_format.frame_ms == 20
        assert observed["source_offer_maxptime"] >= 20
        await asyncio.wait_for(phone.invited.wait(), 3)

        async def receive_destination():
            return (await phone.audio.get())[0]

        # The actual ACK advertises receive alternatives. SIP permits the peer
        # to send any accepted mapping, not only our preferred primary codec.
        section = next(block for block in re.split(rb"(?m)^m=", observed["caller_answer"])
                       if block.startswith(b"audio "))
        accepted_payloads = set(map(int, section.splitlines()[0].split()[3:]))
        receive_mappings = {int(match[1]): (int(match[2]), int(match[3] or 1))
            for match in re.finditer(rb"a=rtpmap:(\d+) L16/(\d+)(?:/(\d+))?\r?\n", section)
            if int(match[1]) in accepted_payloads}

        async def receive_source_frame():
            elapsed, payloads, previous, geometry = Fraction(0), [], None, None
            expected_duration = Fraction(len(destination_pcm) // 2, 16000)
            while elapsed < expected_duration:
                packet = rtp.parse_packet(await lab.hass.loop.sock_recv(media, 4096))
                assert packet.payload_type in receive_mappings
                rate, channels = receive_mappings[packet.payload_type]
                assert packet.payload and len(packet.payload) % (2 * channels) == 0
                if previous is not None:
                    assert geometry == (rate, channels)
                    assert packet.ssrc == previous.ssrc
                    assert packet.sequence == (previous.sequence + 1) & 0xFFFF
                    assert packet.timestamp == (previous.timestamp + len(previous.payload) // (2 * channels)) & 0xFFFFFFFF
                geometry = rate, channels
                elapsed += Fraction(len(packet.payload) // (2 * channels), rate)
                payloads.append(packet.payload)
                previous = packet
            assert elapsed == expected_duration
            data = b"".join(payloads)
            samples = struct.unpack(f"!{len(data) // 2}h", data)
            if geometry == (16000, 1):
                assert data == destination_pcm
            else:
                assert all(abs(value + 3456) <= 3 for value in samples[64 * geometry[1]:])
            return data

        destination_pt = int(re.search(rb"a=rtpmap:(\d+) L16/16000", phone.last_request.body)[1])
        async with asyncio.timeout(3):
            to_destination = await receive_l16_samples(receive_destination, len(source_pcm), destination_pt)
            to_source = await receive_source_frame()
        assert to_destination == source_pcm and to_source
        relay = lab.calls.resource_for(caller.dialog_ids.call_id, "relay")
        assert relay.left_port == observed["source_port"]
        assert relay.left_transport.get_extra_info("socket").fileno() == observed["source_fd"]
        assert consumed.count(observed["source_raw"]) == consumed.count(observed["destination_raw"]) == 1
        assert relay.dropped == 0
    finally:
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            barrier = lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
            if barrier is not None:
                await barrier
        media.close()
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("failed_bind", [1, 2])
async def test_delayed_bind_failure_rejects_before_sdp_and_releases_all(trunks, monkeypatch, failed_bind):
    from custom_components.voip_stack import media_ports

    lab = trunks
    opened, attempts, final_responses = [], [], []
    binder = media_ports.bind_sip_audio_socket

    def bind(port):
        attempts.append(port)
        if len(attempts) == failed_bind:
            raise OSError("controlled delayed reservation bind failure")
        sock = binder(port)
        opened.append(sock)
        return sock

    monkeypatch.setattr(media_ports, "bind_sip_audio_socket", bind)
    account = sip_test_account(lab, "bedroom1")
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Bind failure caller",
        local_uri_user=account.username, username=account.username, password=account.password,
        local_sip_port=0, local_rtp_port=40000)
    protocol = SipDatagramQueueProtocol(caller.queue)
    original = protocol.datagram_received

    def receive(raw, address):
        response = sip.parse_message(raw)
        if response.status_code is not None and response.status_code >= 200 and "INVITE" in response.header("CSeq"):
            final_responses.append(response)
        original(raw, address)

    protocol.datagram_received = receive
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(lambda: protocol, local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    try:
        await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], delayed_offer=True, timeout=3)
        assert len(attempts) == failed_bind
        assert final_responses[0].status_code == 401 and len(final_responses) >= 2
        assert all(response.status_code == 480 and not response.body
            and not response.header("X-Voip-Stack-Decline-Reason")
            for response in final_responses[1:])
        assert all(sock.fileno() < 0 for sock in opened)
        assert not lab.phones[0].invites
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        await caller.close()


@pytest.mark.parametrize("outcome", ["tcp-bind-fails", "cancel-after-udp-bind"])
async def test_endpoint_startup_publishes_one_owner_and_releases_partial_listeners(lab, monkeypatch, outcome):
    from custom_components.voip_stack import endpoint_runtime
    from custom_components.voip_stack.pbx_runtime import RuntimePhase
    from custom_components.voip_stack.runtime_data import sip_endpoint_manager
    from custom_components.voip_stack.sip_tcp_server import SipTcpServer

    # The media lab starts with a synthetic active call registry. This
    # endpoint startup starts with the same unloaded state as config setup.
    lab.runtime.sip = None
    observed = asyncio.Event()
    original = SipTcpServer.start
    owners = []

    async def inspect_tcp_start(server):
        observed.set()
        # UDP has bound. The first inbound record can already see the registry.
        owner = lab.runtime.sip
        manager = sip_endpoint_manager(lab.hass)
        assert owner.phase is RuntimePhase.ACTIVE
        assert owner.component("udp_listener") is owner.component("tcp_listener") is manager
        assert owner.component("registrar") is not None
        assert owner.component("sip_application") is manager.request_router
        assert callable(owner.refer_call) and callable(owner.ring_conference_members_from_ha)
        assert callable(owner.start_ring_group_from_ha)
        owners.append(owner)
        if outcome == "cancel-after-udp-bind":
            await asyncio.Future()
        return await original(server)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        lab.runtime.transport_config["sip_port"] = port
        with monkeypatch.context() as patch:
            patch.setattr(SipTcpServer, "start", inspect_tcp_start)
            startup = asyncio.create_task(endpoint_runtime.async_start_sip_endpoint(lab.hass))
            try:
                async with asyncio.timeout(3):
                    await observed.wait()
                if outcome == "cancel-after-udp-bind":
                    startup.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await startup
                else:
                    assert not await startup
            finally:
                if not startup.done():
                    startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)
    assert lab.runtime.sip is None and owners[0].phase is RuntimePhase.STOPPED
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(("127.0.0.1", port))

    try:
        assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
        replacement = lab.runtime.sip
        manager = sip_endpoint_manager(lab.hass)
        assert replacement is not owners[0] and replacement.phase is RuntimePhase.ACTIVE
        assert manager.udp_server is not None and manager.tcp_server is not None
        assert callable(replacement.forward_call)
    finally:
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
    assert lab.runtime.sip is None
