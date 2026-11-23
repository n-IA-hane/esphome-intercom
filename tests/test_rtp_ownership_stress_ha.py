"""Repeated real media-owner handoffs preserve RTP leases and bound sockets.

This is an owner and UDP stress test. Existing SIP transfer and audiovisual
parking tests separately exercise negotiation, slot policy and remote peers.
"""

import asyncio
from dataclasses import replace
import secrets
import socket
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_pcm import PcmFrameConverter
from custom_components.voip_stack.core.rtp_payload import RtpPayloadEncoder
from custom_components.voip_stack.core.sip_stun import is_stun_message
from custom_components.voip_stack.local_media_bridge import LocalMediaBridge, adopt_relay_for_application
from custom_components.voip_stack.local_call_media import LocalCallMedia
from custom_components.voip_stack.media_ports import RtpPortReservation, reserve_sip_video_relay_media
from custom_components.voip_stack.sip_listener import SipInvite
from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay
from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer
from custom_components.voip_stack.rtp_audio_handoff import RtpAudioHandoff
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


def _peer_socket():
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    return peer


async def _new_audio_relay(hass, peers, reservations, owners):
    reservation = RtpPortReservation.allocate(hass)
    reservations.append(reservation)
    reservation.bind_audio_sockets()
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    legs = [RtpPeer("127.0.0.1", peer.getsockname()[1], fmt.payload_type,
        fmt.audio_format, rtp_format=fmt, send_rtp_format=fmt,
        inbound_rtp_formats=(fmt,), outbound_rtp_formats=(fmt,)) for peer in peers]
    relay = SipRtpRelay(left=legs[0], right=legs[1], left_port=reservation.ports[0],
        right_port=reservation.ports[1], on_release=lambda _ports: reservation.release())
    owners.append(relay)
    reservation.transfer_audio_sockets(relay)
    await relay.start()
    return relay, reservation


async def _exchange(relay, sender, receiver, side, sequence, sample, source_ssrc):
    payload = struct.pack("!320h", *([sample] * 320))
    packet = rtp.build_packet(rtp.RtpPacket(96, sequence, sequence * 320, source_ssrc, payload))
    loop = asyncio.get_running_loop()
    await loop.sock_sendto(sender, packet, ("127.0.0.1", getattr(relay, f"{side}_port")))
    opposite = "right" if side == "left" else "left"
    async with asyncio.timeout(2):
        while True:
            raw, address = await loop.sock_recvfrom(receiver, 4096)
            if is_stun_message(raw):
                continue
            received = rtp.parse_packet(raw)
            if received.payload == payload:
                assert address[1] == getattr(relay, f"{opposite}_port")
                return


def _assert_bound(ports):
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            with pytest.raises(OSError):
                probe.bind(("0.0.0.0", port))


async def test_application_adoption_first_rtp_uses_retained_clock_with_native_eager_tasks(lab):
    """HA can run a newly created sender immediately, before the caller returns."""
    peer = _peer_socket()
    reservation = RtpPortReservation.allocate(lab.hass)
    reservation.bind_audio_sockets(ports=(reservation.ports[0],))
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    contract = SipInvite(source_host="127.0.0.1", source_port=5060,
        request_uri=sip.SipUri("701", "127.0.0.1", 5060),
        caller_uri=sip.SipUri("source", "127.0.0.1", 5060), target="701", caller="Source",
        call_id="eager-adoption-clock", cseq="1 INVITE", remote_sdp=b"",
        send_format=fmt, recv_format=fmt, recv_formats=(fmt,), send_formats=(fmt,),
        remote_rtp_host="127.0.0.1", remote_rtp_port=peer.getsockname()[1])
    receiver, encoder = RtpAudioReceiver(fmt), RtpPayloadEncoder(fmt)
    sender = rtp.AudioRtpSenderState(501, 123456, 0x12345678, 16000)
    state = RtpAudioHandoff(receiver, encoder, None, sender,
        PcmFrameConverter(fmt.audio_format, fmt.audio_format), "127.0.0.1", peer.getsockname()[1])
    media = LocalCallMedia(lab.hass, invite=contract, local_rtp_port=reservation.ports[0],
        reservation=reservation, on_complete=lambda _reason: None)
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        asyncio.DatagramProtocol, sock=reservation.take_audio_socket(reservation.ports[0]))
    try:
        commit_audio = media.prepare_audio_state(state)
        media.adopt_transport(transport)
        commit_audio()
        async with asyncio.timeout(2):
            first = rtp.parse_packet(await asyncio.get_running_loop().sock_recv(peer, 4096))
        assert (first.sequence, first.timestamp, first.ssrc) == (501, 123456, 0x12345678)
    finally:
        await media.stop()
        peer.close()


@pytest.mark.timeout(90)
async def test_105_transfer_park_pickup_video_owner_cycles_preserve_live_leases(lab):
    runtime = lab.runtime
    runtime.transport_config["rtp_port"] = 20000 + 2 * secrets.randbelow(2000)
    baseline_owner = RtpPortReservation.allocate(lab.hass)
    baseline_owner.bind_audio_sockets()
    baseline = set(runtime.rtp_port_pool["used"])
    baseline_fds = tuple(sock.fileno() for sock in baseline_owner._audio_sockets)
    peers = [_peer_socket() for _ in range(3)]
    reservations, owners = [], []
    completed = 0
    try:
        for cycle in range(105):
            first, first_ports = await _new_audio_relay(lab.hass, peers[:2], reservations, owners)
            second, second_ports = await _new_audio_relay(lab.hass, peers[1:], reservations, owners)
            assert runtime.rtp_port_pool["used"] == baseline | set(first_ports.ports) | set(second_ports.ports)
            retained = []
            def merge(previous, keep):
                reservation = RtpPortReservation.merge(lab.hass, previous, keep)
                reservations.append(reservation)
                retained.append(reservation)
                return lambda _ports: reservation.release()
            source_transport = first.left_transport
            combined = SipRtpRelay.splice_audio(first, "left", second, "right", merge_reservations=merge)
            owners.append(combined)
            lease = retained[0]
            first_ports.detach()
            second_ports.detach()
            await first.stop()
            await second.stop()
            first_ports.release()
            second_ports.release()
            assert combined.left_transport is source_transport and not source_transport.is_closing()
            assert runtime.rtp_port_pool["used"] == baseline | set(lease.ports)
            _assert_bound(lease.ports)
            await _exchange(combined, peers[0], peers[2], "left", 1, 1500 + cycle, 10000 + cycle)
            await _exchange(combined, peers[2], peers[0], "right", 1, -2000 - cycle, 20000 + cycle)

            fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
            contract = SipInvite(source_host="127.0.0.1", source_port=5060,
                request_uri=sip.SipUri("701", "127.0.0.1", 5060),
                caller_uri=sip.SipUri("source", "127.0.0.1", 5060), target="701", caller="Source",
                call_id=f"ownership-stress-{cycle}", cseq="1 INVITE", remote_sdp=b"",
                send_format=fmt, recv_format=fmt, recv_formats=(fmt,), send_formats=(fmt,),
                remote_rtp_host="127.0.0.1", remote_rtp_port=peers[0].getsockname()[1])
            async def complete(_reason):
                raise AssertionError("Owner handoff must retain the established caller")
            parked = adopt_relay_for_application(lab.hass, combined, "left", contract, lease, complete)
            owners.append(parked)
            await combined.stop()
            assert parked.transport is source_transport and not source_transport.is_closing()
            assert runtime.rtp_port_pool["used"] == baseline | set(lease.ports)

            video_ports, sockets = reserve_sip_video_relay_media(lab.hass)
            reservations.append(video_ports)
            held = sdp.RtpVideoFormat(103, encoding="H264", packetization_mode=1, direction="inactive")
            video_peers = [VideoRtpPeer("127.0.0.1", peer.getsockname()[1],
                peer.getsockname()[1] + 1, held) for peer in (peers[0], peers[2])]
            video = SipVideoRtpRelay(left=video_peers[0], right=video_peers[1],
                left_port=video_ports.ports[0], right_port=video_ports.ports[1],
                left_socket=sockets[0], left_rtcp_socket=sockets[1],
                right_socket=sockets[2], right_rtcp_socket=sockets[3],
                on_release=lambda _ports, reservation=video_ports: reservation.release())
            owners.append(video)
            await video.start()
            parked.video_relay = video
            expected_live = baseline | set(lease.ports) | set(video_ports.ports)
            assert runtime.rtp_port_pool["used"] == expected_live
            pickup = await LocalMediaBridge.prepare(parked, replace(contract,
                call_id=f"pickup-stress-{cycle}", remote_rtp_port=peers[2].getsockname()[1]))
            resumed = pickup.commit()
            owners.append(resumed)
            lease.detach()
            resumed_lease = RtpPortReservation(lab.hass, (resumed.left_port, resumed.right_port))
            reservations.append(resumed_lease)
            resumed._on_release = lambda _ports, reservation=resumed_lease: reservation.release()
            await parked.stop()
            lease.release()
            assert resumed.left_transport is source_transport and not source_transport.is_closing()
            assert resumed.video_relay is video and parked.video_relay is None
            assert video.started and all(not transport.is_closing() for transport in video._transports.values())
            assert runtime.rtp_port_pool["used"] == expected_live
            _assert_bound([*lease.ports, *video_ports.ports, *(port + 1 for port in video_ports.ports)])
            await _exchange(resumed, peers[0], peers[2], "left", 2, 1700 + cycle, 10000 + cycle)
            await _exchange(resumed, peers[2], peers[0], "right", 2, -2200 - cycle, 20000 + cycle)
            await resumed.stop()
            await resumed.stop()
            assert video._released and all(sock.fileno() < 0 for sock in sockets)
            assert runtime.rtp_port_pool["used"] == baseline

            # A complete subsequent media generation must bind and carry RTP
            # while an unrelated live reservation continues to own its pair.
            following, following_lease = await _new_audio_relay(lab.hass, (peers[0], peers[2]), reservations, owners)
            assert runtime.rtp_port_pool["used"] == baseline | set(following_lease.ports)
            await _exchange(following, peers[0], peers[2], "left", 1, 2500 + cycle, 30000 + cycle)
            await _exchange(following, peers[2], peers[0], "right", 1, -2700 - cycle, 40000 + cycle)
            await following.stop()
            assert runtime.rtp_port_pool["used"] == baseline
            assert tuple(sock.fileno() for sock in baseline_owner._audio_sockets) == baseline_fds
            _assert_bound(baseline)
            completed += 1
            owners.clear()
            reservations.clear()
        assert completed == 105
    finally:
        for owner in reversed(owners):
            await owner.stop()
        for reservation in reversed(reservations):
            reservation.release()
        baseline_owner.release()
        for peer in peers:
            peer.close()
    assert not runtime.rtp_port_pool["used"]
