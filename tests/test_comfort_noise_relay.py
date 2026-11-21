"""Negotiated CN packets use the existing receive, relay and cleanup owners."""

import asyncio
import socket

import pytest

from .voip_phase1_support import _load_intercom_module, rtp, sdp, sip_rtp_bridge

Receiver = _load_intercom_module("rtp_audio_receiver").RtpAudioReceiver


def formats():
    return sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20), sdp.RtpPcmFormat(13, "CN", 8000, 1, 0)


def packet(pt, payload, *, sequence=1, ssrc=55):
    return rtp.build_packet(rtp.RtpPacket(pt, sequence, 1000 + sequence * 160, ssrc, payload))


def test_receiver_retains_cn_state_without_changing_its_primary_pcm_contract():
    primary, cn = formats()
    receiver = Receiver(primary, (primary, cn))
    assert not receiver.comfort_noise_active
    assert receiver.decode(13, bytes([40]))
    assert receiver.selected_format == primary
    generated = [receiver.take_comfort_noise()[0] for _ in range(3)]
    assert all(len(frame) == 320 and any(frame) for frame in generated)
    assert len(set(generated)) == 3
    assert receiver.decode(0, b"\xff" * 160) == [bytes(320)]
    assert not receiver.comfort_noise_active and receiver.take_comfort_noise() == []
    with pytest.raises(ValueError, match="primary"):
        Receiver(cn)
    with pytest.raises(ValueError, match="unnegotiated"):
        Receiver(primary).decode(13, bytes([40]))


async def test_real_udp_cn_period_stops_on_primary_and_teardown_then_second_call_works():
    primary, cn = formats()
    loop = asyncio.get_running_loop()
    source, destination = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for sock in (source, destination):
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
    released = []
    relay_ports = (0, 0)
    try:
        for call in range(2):
            left = sip_rtp_bridge.RtpPeer("127.0.0.1", source.getsockname()[1], 0, primary.audio_format,
                rtp_format=primary, send_rtp_format=primary, inbound_rtp_formats=(primary, cn))
            right = sip_rtp_bridge.RtpPeer("127.0.0.1", destination.getsockname()[1], 0, primary.audio_format,
                rtp_format=primary, send_rtp_format=primary)
            relay = sip_rtp_bridge.SipRtpRelay(left=left, right=right, left_port=relay_ports[0],
                right_port=relay_ports[1], on_release=released.append)
            await relay.start()
            ingress = relay.left_transport.get_extra_info("sockname")
            relay_ports = (ingress[1], relay.right_transport.get_extra_info("sockname")[1])
            try:
                assert not relay._cn_timers
                await loop.sock_sendto(source, packet(13, bytes([40])), ("127.0.0.1", ingress[1]))
                received = [rtp.parse_packet((await asyncio.wait_for(loop.sock_recvfrom(destination, 2048), .5))[0])
                            for _ in range(4)]
                assert all(p.payload_type == 0 and len(p.payload) == 160 for p in received)
                assert len({p.ssrc for p in received}) == 1
                assert [(received[i + 1].sequence - received[i].sequence) & 65535 for i in range(3)] == [1] * 3
                assert [(received[i + 1].timestamp - received[i].timestamp) & 0xffffffff for i in range(3)] == [160] * 3
                decoder = _load_intercom_module("core.rtp_payload").RtpPayloadDecoder(primary)
                assert all(any(decoder.decode(p.payload)) for p in received)
                owner, generation = relay.left_decoder, relay._cn_generations["left"]
                await loop.sock_sendto(source, packet(0, b"\xff" * 160, sequence=2), ("127.0.0.1", ingress[1]))
                while True:
                    p = rtp.parse_packet((await asyncio.wait_for(loop.sock_recvfrom(destination, 2048), .5))[0])
                    if p.payload == b"\xff" * 160:
                        break
                assert not relay._cn_timers and not owner.comfort_noise_active
                relay._comfort_noise_tick("left", owner, generation)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(loop.sock_recvfrom(destination, 2048), .07)
                await loop.sock_sendto(source, packet(13, bytes([45]), sequence=3), ("127.0.0.1", ingress[1]))
                await asyncio.wait_for(loop.sock_recvfrom(destination, 2048), .5)
                current_handle = relay._cn_timers["left"]
                relay._comfort_noise_tick("left", owner, generation)
                assert relay._cn_timers["left"] is current_handle
                current_generation = relay._cn_generations["left"]
            finally:
                await relay.stop()
            assert not relay._cn_timers and current_handle.cancelled()
            relay._comfort_noise_tick("left", owner, current_generation)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(loop.sock_recvfrom(destination, 2048), .07)
            await relay.stop()
            await asyncio.sleep(0)
        assert len(released) == 2
    finally:
        source.close()
        destination.close()
