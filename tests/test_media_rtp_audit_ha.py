"""Actual UDP witnesses for shared sequences and retained media continuity."""

import asyncio
from contextlib import asynccontextmanager
import socket
import struct
from unittest.mock import patch

import pytest

from custom_components.voip_stack.core import rtp, sdp, video_rtcp
from custom_components.voip_stack.core.audio_pcm import PcmFrameConverter
from custom_components.voip_stack.core.rtp_payload import RtpPayloadEncoder
from custom_components.voip_stack.core.sip_stun import is_stun_message
from custom_components.voip_stack.rtp_audio_handoff import RtpAudioHandoff
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay, audio_reconfiguration_guard
from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer

pytestmark = [pytest.mark.ha, pytest.mark.usefixtures("socket_enabled")]


def udp_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    return sock


@asynccontextmanager
async def audio_path(fmt, *, events=False):
    left, right, left_bound, right_bound = (udp_socket() for _ in range(4))
    peers = [RtpPeer("127.0.0.1", sock.getsockname()[1], fmt.payload_type,
        fmt.audio_format, rtp_format=fmt, inbound_rtp_formats=(fmt,), outbound_rtp_formats=(fmt,),
        dtmf_payload_type=101 if events else None, dtmf_clock_rate=8000,
        send_dtmf_payload_type=110 if events else None) for sock in (left, right)]
    observed = []
    relay = SipRtpRelay(left=peers[0], right=peers[1],
        left_port=left_bound.getsockname()[1], right_port=right_bound.getsockname()[1],
        on_dtmf=lambda side, digit, transport: observed.append((side, digit, transport)))
    relay.adopt_audio_sockets((left_bound, right_bound))
    try:
        await relay.start()
        yield relay, left, right, observed
    finally:
        await relay.stop()
        left.close()
        right.close()


async def exchange(source, packet, port, receiver):
    loop = asyncio.get_running_loop()
    await loop.sock_sendto(source, packet, ("127.0.0.1", port))
    return rtp.parse_packet(await receive_datagram(receiver))


async def receive_datagram(sock):
    async with asyncio.timeout(2):
        while True:
            data = await asyncio.get_running_loop().sock_recv(sock, 4096)
            if not is_stun_message(data):
                return data


@pytest.mark.parametrize("rate", [16000, 48000])
async def test_first_different_clock_dtmf_after_four_thousand_audio_packets(rate):
    fmt = sdp.RtpPcmFormat(96, "L16", rate, 1, 10, max_frame_ms=20)
    payload = struct.pack(f"!{rate // 100}h", *([1250] * (rate // 100)))
    ssrc = 0x12345678
    async with audio_path(fmt, events=True) as (relay, left, right, observed):
        def event(seq, ts, digit, flags=10, duration=80):
            return rtp.build_packet(rtp.RtpPacket(
                101, seq, ts, ssrc, bytes((digit, flags)) + duration.to_bytes(2, "big"), True))
        await exchange(left, event(1, 8000, 1, flags=138), relay.left_port, right)
        for sequence in range(2, 4002):
            packet = rtp.build_packet(rtp.RtpPacket(96, sequence,
                (sequence - 2) * (rate // 100), ssrc, payload))
            forwarded = await exchange(left, packet, relay.left_port, right)
            assert forwarded.payload == payload
        await exchange(left, event(4002, 400000, 2), relay.left_port, right)
        assert [row[1] for row in observed] == ["1", "2"]
        assert relay._receive_states["left"].highest == 4002
        decoder = relay._dtmf_decoders["left"]
        assert decoder.decode_report(event(4002, 400000, 2)) is None
        ended = decoder.decode_report(event(4003, 400000, 2, flags=138, duration=160))
        assert ended is not None and ended.end and not ended.first
        assert decoder.decode_report(event(4004, 400000, 2, flags=10, duration=80)) is None


async def test_retired_pcm_debt_drains_then_udp_payload_relay_is_bit_exact():
    fmt = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10, max_frame_ms=20)
    old = list(range(240))
    fresh = list(range(240, 480))
    async with audio_path(fmt) as (relay, left, right, _observed):
        converter = PcmFrameConverter(fmt.audio_format, fmt.audio_format)
        assert converter.convert(struct.pack("<240h", *old)) == []
        state = RtpAudioHandoff(RtpAudioReceiver(fmt), RtpPayloadEncoder(fmt), None,
            rtp.AudioRtpSenderState(501, 123456, 0xBEEF, 48000), converter,
            "127.0.0.1", left.getsockname()[1])
        async with audio_reconfiguration_guard(relay):
            relay.adopt_audio_state("left", state)
        first = rtp.build_packet(rtp.RtpPacket(96, 10, 10000, 0xAABB,
            struct.pack("!240h", *fresh)))
        forwarded = await exchange(right, first, relay.right_port, left)
        assert forwarded.payload == struct.pack("!480h", *(old + fresh))
        assert (forwarded.sequence, forwarded.timestamp, forwarded.ssrc) == (501, 123456, 0xBEEF)
        assert not relay.right_to_left.has_pending_pcm
        raw = struct.pack("!480h", *([4321] * 480))
        packet = rtp.build_packet(rtp.RtpPacket(96, 11, 10240, 0xAABB, raw))
        with patch.object(relay.right_decoder, "decode_rtp", wraps=relay.right_decoder.decode_rtp) as decode:
            second = await exchange(right, packet, relay.right_port, left)
            assert decode.call_count == 0
        assert second.payload == raw and relay.right_to_left_passthrough
        assert (second.sequence, second.timestamp, second.ssrc) == (502, 123936, 0xBEEF)


@pytest.mark.parametrize("encoding,pt,rate,channels", [("L16", 96, 16000, 1), ("OPUS", 111, 48000, 2)])
async def test_adopted_compatible_encoded_payload_is_not_decoded_again(encoding, pt, rate, channels):
    fmt = sdp.RtpPcmFormat(pt, encoding, rate, channels, 20, max_frame_ms=20)
    async with audio_path(fmt) as (relay, left, right, _observed):
        sender = rtp.AudioRtpSenderState(31, 48000, 0xABCD, fmt.rtp_clock_rate)
        retained_encoder = RtpPayloadEncoder(fmt)
        state = RtpAudioHandoff(RtpAudioReceiver(fmt), retained_encoder, None, sender,
            PcmFrameConverter(fmt.audio_format, fmt.audio_format), "127.0.0.1", left.getsockname()[1])
        remote = RtpPayloadEncoder(fmt)
        try:
            async with audio_reconfiguration_guard(relay):
                relay.adopt_audio_state("left", state)
            pcm = struct.pack(f"<{fmt.audio_format.nominal_frame_bytes // 2}h",
                              *([2400] * (fmt.audio_format.nominal_frame_bytes // 2)))
            payload = remote.encode(pcm)
            packet = rtp.build_packet(rtp.RtpPacket(pt, 5, 80000, 0x7788, payload))
            with patch.object(relay.right_decoder, "decode_rtp", wraps=relay.right_decoder.decode_rtp) as decode:
                received = await exchange(right, packet, relay.right_port, left)
                assert decode.call_count == 0
            assert received.payload == payload and relay.right_to_left_passthrough
            assert received.ssrc == 0xABCD and received.sequence == 31
        finally:
            remote.close()


async def test_pure_video_udp_has_no_startup_feedback_and_forwards_receiver_pli():
    peers = [udp_socket() for _ in range(4)]
    bound = [udp_socket() for _ in range(4)]
    formats = [sdp.RtpVideoFormat(pt, "H264", packetization_mode=1,
        rtcp_feedback=("nack pli", "ccm fir")) for pt in (96, 97)]
    relay = SipVideoRtpRelay(left=VideoRtpPeer("127.0.0.1", peers[0].getsockname()[1],
        peers[1].getsockname()[1], formats[0]), right=VideoRtpPeer("127.0.0.1",
        peers[2].getsockname()[1], peers[3].getsockname()[1], formats[1]),
        left_port=bound[0].getsockname()[1], right_port=bound[2].getsockname()[1],
        left_socket=bound[0], left_rtcp_socket=bound[1], right_socket=bound[2], right_rtcp_socket=bound[3])
    right_rtcp_port = bound[3].getsockname()[1]
    try:
        await relay.start()
        for sequence in range(1, 4):
            packet = rtp.build_packet(rtp.RtpPacket(96, sequence, sequence * 9000,
                0x102030, b"\x65\x88\x84", True))
            forwarded = await exchange(peers[0], packet, relay.left_port, peers[2])
            assert forwarded.sequence == sequence and forwarded.ssrc == 0x102030
        for sock in (peers[1], peers[3]):
            while True:
                try:
                    data = sock.recv(4096)
                except BlockingIOError:
                    break
                assert is_stun_message(data)
        loop = asyncio.get_running_loop()
        pli = video_rtcp.build_pli(0x9988, 0x102030)
        await loop.sock_sendto(peers[3], pli, ("127.0.0.1", right_rtcp_port))
        assert await receive_datagram(peers[1]) == pli
        assert not relay._keyframe_requests
        assert relay.rtcp_forwarded == 1
    finally:
        await relay.stop()
        for sock in peers:
            sock.close()
