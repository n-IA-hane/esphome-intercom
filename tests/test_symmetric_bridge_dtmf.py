"""Codec following cannot change an active telephone event's RTP identity."""

import asyncio
import struct

import pytest

from .voip_phase1_support import rtp, sdp, sip_rtp_bridge


class _Transport:
    def __init__(self):
        self.sent = []

    def sendto(self, raw, address):
        self.sent.append(rtp.parse_packet(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["left", "right"])
async def test_symmetric_codec_dtmf_uses_live_clock_and_defers_mid_event_switch(side):
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    pcm = sdp.RtpPcmFormat(97, "PCM", 16000, 1, 20)
    peers = {
        name: sip_rtp_bridge.RtpPeer(
            "192.0.2.10" if name == "left" else "192.0.2.20", 40000, 0,
            pcmu.audio_format, rtp_format=pcmu, send_rtp_format=pcmu,
            inbound_rtp_formats=(pcmu, pcm) if name == side else (pcmu,),
            outbound_rtp_formats=(pcmu, pcm), match_received_codec=name == side,
            dtmf_payload_type=101, dtmf_clock_rate=8000,
            send_dtmf_payload_type=101, send_dtmf_clock_rate=8000,
            ssrc=111, dtmf_ssrc=222, clock_rate=8000,
        ) for name in ("left", "right")
    }
    relay = sip_rtp_bridge.SipRtpRelay(left=peers["left"], right=peers["right"], left_port=42000, right_port=42002)
    relay.left_transport = _Transport()
    relay.right_transport = _Transport()
    peer = peers[side]
    output = relay.left_transport if side == "left" else relay.right_transport
    encoder = relay.left_encoder if side == "left" else relay.right_encoder

    def receive(pt, seq):
        payload = struct.pack("<320h", *([1000] * 320)) if pt == 97 else b"\xce" * 160
        relay.handle_packet(side, rtp.build_packet(rtp.RtpPacket(
            payload_type=pt, sequence=seq, timestamp=seq * 320, ssrc=333, payload=payload,
        )), (peer.host, peer.port))

    receive(97, 1)
    assert encoder.fmt == pcm
    assert peer.clock_rate == 8000  # No audio has been sent back yet.
    event = asyncio.create_task(relay._send_dtmf_event(side, peer, "5", duration_ms=40))
    await asyncio.sleep(0)
    assert relay._dtmf_locks[side].locked()
    assert peer.clock_rate == 16000
    assert peer.ssrc != 111
    receive(0, 2)
    assert encoder.fmt == pcm  # A validated packet cannot split an event.
    await event
    packets = [packet for packet in output.sent if packet.payload_type == 101]
    assert len(packets) >= 4
    assert {packet.ssrc for packet in packets} == {222}
    assert len({packet.timestamp for packet in packets}) == 1
    assert [struct.unpack("!H", packet.payload[2:4])[0] for packet in packets][-3:] == [320] * 3
    assert all((after.sequence-before.sequence) % 65536 == 1 for before, after in zip(packets, packets[1:]))
    receive(0, 3)
    assert encoder.fmt == pcmu
    assert relay.dropped == 0
