"""Immediate peer RTP is retained by one existing bridge socket owner."""
import asyncio
from dataclasses import replace
import re
import socket
import struct

import pytest
from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = pytest.mark.ha

@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_bridge_retains_first_rtp_after_ack_before_media_commit(trunks, monkeypatch):
    """The current offer must own real sockets while its relay commit yields."""
    from custom_components.voip_stack.inbound_routing import bridge as route

    lab = trunks
    target = lab.phones[0]
    lab.roster[1] = replace(lab.roster[1], metadata={
        "endpoint_id": "sip:bedroom0", "endpoint_kind": "sip_account", "registered": True,
        "sip_audio_tx_formats": ["L16/16000/1/10"],
        "sip_audio_rx_formats": ["L16/16000/1/10"],
    })
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    commit_entered, release_commit, first_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = {}
    from custom_components.voip_stack import sip_rtp_bridge
    from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
    decoded, received = [], []
    original_decoder = RtpAudioReceiver.__init__
    original_packet = sip_rtp_bridge._RelayProtocol.datagram_received

    def initialize(decoder, *args, **kwargs):
        decoded.append(decoder)
        original_decoder(decoder, *args, **kwargs)

    def packet_received(protocol, raw, address):
        if raw == observed.get("first_packet"):
            received.append(raw)
        original_packet(protocol, raw, address)

    monkeypatch.setattr(RtpAudioReceiver, "__init__", initialize)
    monkeypatch.setattr(sip_rtp_bridge._RelayProtocol, "datagram_received", packet_received)
    original_commit = route.async_commit_outbound_bridge

    async def delayed_commit(*args, **kwargs):
        observed["reservation"] = args[2].winner.ports
        commit_entered.set()
        await release_commit.wait()
        return await original_commit(*args, **kwargs)

    monkeypatch.setattr(route, "async_commit_outbound_bridge", delayed_commit)
    source_payload = struct.pack("!160h", *([2345] * 160))
    original_receive = target.datagram_received

    def receive(raw, address):
        request = sip.parse_message(raw)
        if request.method == "INVITE":
            port = int(re.search(rb"m=audio (\d+)", request.body)[1])
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                try:
                    probe.bind(("0.0.0.0", port))
                except OSError:
                    observed["port_reserved_on_offer"] = True
                else:
                    observed["port_reserved_on_offer"] = False
        original_receive(raw, address)
        if request.method == "ACK" and not first_sent.is_set():
            offer = target.last_request
            observed["offered_port"] = int(re.search(rb"m=audio (\d+)", offer.body)[1])
            observed["target_call_id"] = offer.header("Call-ID")
            packet = rtp.build_packet(rtp.RtpPacket(target.payload_type, 46567, 1600, 902, source_payload))
            observed["first_packet"] = packet
            target.rtp_socket.sendto(packet, ("127.0.0.1", observed["offered_port"]))
            first_sent.set()

    monkeypatch.setattr(target, "datagram_received", receive)
    # Linux reports the real ICMP port-unreachable on the sender's error queue.
    # No synthetic ICMP or production admission/forwarding result is injected.
    ip_recverr = getattr(socket, "IP_RECVERR", 11)
    original_recverr = target.rtp_socket.getsockopt(socket.IPPROTO_IP, ip_recverr)
    target.rtp_socket.setsockopt(socket.IPPROTO_IP, ip_recverr, 1)
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Immediate RTP caller",
        local_sip_port=0, local_rtp_port=audio.getsockname()[1],
        supported_send_formats=[AudioFormat(16000, frame_ms=10)],
        supported_recv_formats=[AudioFormat(16000, frame_ms=10)])
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]

    async def dial():
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        return await caller.wait_for_final(timeout=3) if result == "ringing" else result

    task = asyncio.create_task(dial())
    try:
        await asyncio.wait_for(first_sent.wait(), 3)
        await asyncio.wait_for(commit_entered.wait(), 3)
        await asyncio.sleep(0)
        try:
            error = target.rtp_socket.recvmsg(4096, 4096, socket.MSG_ERRQUEUE | socket.MSG_DONTWAIT)
        except BlockingIOError:
            error = None
        assert error is None, "Current offered RTP port generated ICMP before bridge media commit"
        assert not decoded, "No relay decoder may start before the final media answer is committed"
        assert lab.calls.resource_for(caller.dialog_ids.call_id, "relay") is None
        assert observed["port_reserved_on_offer"], "Offered RTP port was not bound when INVITE arrived"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            with pytest.raises(OSError, match="Address already in use"):
                probe.bind(("0.0.0.0", observed["offered_port"]))
        reservation = observed["reservation"]
        reserved_fds = tuple(sock.fileno() for sock in reservation._audio_sockets)
        release_commit.set()
        assert await asyncio.wait_for(task, 3) == "in_call"
        session = lab.calls.get_session(caller.dialog_ids.call_id)
        relay = lab.calls.resource_for(session.call_id, "relay")
        assert tuple(transport.get_extra_info("socket").fileno() for transport in
                     (relay.left_transport, relay.right_transport)) == reserved_fds
        assert reservation._audio_sockets is None
        async with asyncio.timeout(3):
            packet = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
        assert packet.payload == source_payload
        assert received == [observed["first_packet"]], "The exact first packet must be read once after fd adoption"
        with pytest.raises(BlockingIOError):
            audio.recv(4096, socket.MSG_DONTWAIT)
        assert packet.payload_type == caller.dialog.recv_format.payload_type
        assert observed["target_call_id"] == target.last_request.header("Call-ID")
        assert caller.bye()
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("0.0.0.0", observed["offered_port"]))
    finally:
        release_commit.set()
        await caller.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        target.rtp_socket.setsockopt(socket.IPPROTO_IP, ip_recverr, original_recverr)
        audio.close()

@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("failure", ["validation", "start-left", "start-right", "stop-before-start"])
async def test_prebound_relay_keeps_one_owner_on_adoption_and_start_failure(trunks, monkeypatch, failure):
    from custom_components.voip_stack.media_ports import RtpPortReservation, release_sip_rtp_port_pair
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

    lab = trunks
    reservation = RtpPortReservation.allocate(lab.hass)
    reservation.bind_audio_sockets()
    sockets = reservation._audio_sockets
    transferred = False
    released = []

    def release(ports):
        if transferred:
            released.append(ports)
            release_sip_rtp_port_pair(lab.hass, ports)

    fmt = AudioFormat(16000, frame_ms=10)
    relay = SipRtpRelay(left=RtpPeer("127.0.0.2", 9, 96, fmt),
        right=RtpPeer("127.0.0.1", 9, 96, fmt),
        left_port=reservation.ports[0], right_port=reservation.ports[1], on_release=release)
    try:
        if failure == "validation":
            relay.right_port += 2
            with pytest.raises(ValueError, match="do not match"):
                reservation.transfer_audio_sockets(relay)
            assert reservation._audio_sockets is sockets
            assert all(sock.fileno() >= 0 for sock in sockets)
            assert relay._audio_sockets is None
            reservation.release()
        else:
            reservation.transfer_audio_sockets(relay)
            assert relay._audio_sockets is sockets and reservation._audio_sockets is None
            reservation.detach()
            transferred = True
            if failure != "stop-before-start":
                original_create = lab.hass.loop.create_datagram_endpoint
                calls = 0

                async def create(*args, **kwargs):
                    nonlocal calls
                    if kwargs.get("sock") in sockets:
                        calls += 1
                        if calls == (1 if failure == "start-left" else 2):
                            raise OSError("controlled endpoint start failure")
                    return await original_create(*args, **kwargs)

                with monkeypatch.context() as patch:
                    patch.setattr(lab.hass.loop, "create_datagram_endpoint", create)
                    with pytest.raises(OSError, match="controlled endpoint"):
                        await relay.start()
        await relay.stop()
        async with asyncio.timeout(2):
            while any(sock.fileno() >= 0 for sock in sockets):
                await asyncio.sleep(0)
        assert released == ([reservation.ports] if transferred else [])
        for port in reservation.ports:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(("0.0.0.0", port))
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        await relay.stop()
        reservation.release()

@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("failure", ["decline", "cancel", "timeout", "session-adoption"])
async def test_inbound_bridge_failure_releases_prebound_audio(trunks, monkeypatch, failure):
    from custom_components.voip_stack.media_ports import RtpPortReservation

    lab = trunks
    target = lab.phones[0]
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    target.final_status = 486 if failure == "decline" else 200
    target.auto_answer = failure not in {"cancel", "timeout"}
    if failure == "timeout":
        original_receive = target.datagram_received

        def silent(raw, address):
            if sip.parse_message(raw).method == "INVITE":
                target.invited.set()  # An independently received request, no response.
                return
            original_receive(raw, address)

        monkeypatch.setattr(target, "datagram_received", silent)
    owners = []
    bound = RtpPortReservation.bind_audio_sockets

    def observe(owner):
        bound(owner)
        owners.append((owner, owner._audio_sockets))

    monkeypatch.setattr(RtpPortReservation, "bind_audio_sockets", observe)
    if failure == "session-adoption":
        def reject(_registry, _call_id, _relay):
            raise RuntimeError("controlled session adoption failure")

        monkeypatch.setattr(type(lab.calls), "attach_relay", reject)
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Aborted bridge caller",
        local_sip_port=0, local_rtp_port=audio.getsockname()[1],
        supported_send_formats=[AudioFormat(16000, frame_ms=10)],
        supported_recv_formats=[AudioFormat(16000, frame_ms=10)])
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]

    async def dial():
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=12 if failure == "timeout" else 3)
        return await caller.wait_for_final(timeout=12 if failure == "timeout" else 3) if result == "ringing" else result

    task = asyncio.create_task(dial())
    try:
        await asyncio.wait_for(target.invited.wait(), 3)
        assert len(owners) == 1
        if failure == "cancel":
            assert caller.cancel()
        assert await asyncio.wait_for(task, 12 if failure == "timeout" else 3) != "in_call"
        async with asyncio.timeout(3):
            while lab.calls.sessions or lab.runtime.rtp_port_pool.get("used"):
                await asyncio.sleep(0)
        for owner, sockets in owners:
            assert owner._audio_sockets is None
            assert all(sock.fileno() < 0 for sock in sockets)
            for port in owner.ports:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.bind(("0.0.0.0", port))
    finally:
        await caller.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        audio.close()
