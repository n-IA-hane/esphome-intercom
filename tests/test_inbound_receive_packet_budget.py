"""A standard peer's20ms L16 packet is reframed into HA's preferred10ms."""
import struct

import pytest

from .voip_phase1_support import _load_intercom_module, audio_format, rtp, sdp

Receiver = _load_intercom_module('rtp_audio_receiver').RtpAudioReceiver

# Original Asterisk231 offer, with only its media address replaced by TEST-NET.
AST_OFFER = ('v=0\r\no=- 1725022972 1725022972 IN IP4 192.0.2.231\r\ns=Asterisk\r\n'
    'c=IN IP4 192.0.2.231\r\nt=0 0\r\nm=audio 18030 RTP/AVP 118 126 0 101 102 103\r\n'
    'a=rtpmap:118 L16/16000\r\na=rtpmap:126 L16/48000\r\na=rtpmap:0 PCMU/8000\r\n'
    'a=rtpmap:101 telephone-event/16000\r\na=fmtp:101 0-16\r\n'
    'a=rtpmap:102 telephone-event/48000\r\na=fmtp:102 0-16\r\n'
    'a=rtpmap:103 telephone-event/8000\r\na=fmtp:103 0-16\r\n'
    'a=ptime:10\r\na=maxptime:60\r\na=sendrecv\r\n')


def test_standard48k20_packet_uses_local_rx_budget_and_preserves_every_sample():
    selected = sdp.negotiate_directional(AST_OFFER, list(audio_format.HA_SIP_PCM_TX_FORMATS),
                                         list(audio_format.HA_SIP_PCM_RX_FORMATS))
    assert selected.recv.sample_rate == 48000 and selected.recv.frame_ms == 10
    samples = tuple(range(-480, 480))
    frames = Receiver(selected.recv).decode(126, struct.pack('!960h', *samples))
    assert len(frames) == 2 and all(len(frame) == 960 for frame in frames)
    assert b''.join(frames) == struct.pack('<960h', *samples)
    answer = sdp.build_answer_directional('192.0.2.230', '192.0.2.230', 44010,
        selected.send, selected.recv, remote_sdp=AST_OFFER)
    assert sdp.parse_sdp(answer)['ptime'] == 10
    assert sdp.parse_sdp(answer)['maxptime'] == 20
    assert sdp.parse_sdp(answer)['payload_order'].count(126) == 1
    with pytest.raises(rtp.RtpError):
        Receiver(selected.recv).decode(126, bytes(21 * 48 * 2))


def test_local10_only_receiver_does_not_inherit_remote60_capacity():
    local = [audio_format.AudioFormat(48000, 's16le', 1, 10)]
    selected = sdp.negotiate_directional(AST_OFFER, local, local)
    with pytest.raises(rtp.RtpError):
        Receiver(selected.recv).decode(126, bytes(20 * 48 * 2))
    answer = sdp.build_answer_directional('192.0.2.230', '192.0.2.230', 44010,
        selected.send, selected.recv, remote_sdp=AST_OFFER)
    assert sdp.parse_sdp(answer)['maxptime'] == 10


def test_other_rates_or_channels_do_not_inflate_selected_receive_budget():
    local = [audio_format.AudioFormat(48000, 's16le', 1, 10),
             audio_format.AudioFormat(16000, 's16le', 1, 32),
             audio_format.AudioFormat(48000, 's16le', 2, 20)]
    selected = sdp.negotiate_directional(AST_OFFER, local[:1], local)
    assert rtp.audio_payload_size_limit(selected.recv) == 960
    with pytest.raises(rtp.RtpError):
        Receiver(selected.recv).decode(126, bytes(20 * 48 * 2))
    with pytest.raises(ValueError):
        Receiver(selected.recv).decode(118, bytes(10 * 16 * 2))


@pytest.mark.asyncio
async def test_real_udp48k20_becomes_two48k10_with_exact_pcm_and_clock():
    import asyncio
    import socket
    from .voip_phase1_support import _reserved_udp_ports

    relay_module = _load_intercom_module('sip_rtp_bridge')
    selected = sdp.negotiate_directional(AST_OFFER, list(audio_format.HA_SIP_PCM_TX_FORMATS),
                                         list(audio_format.HA_SIP_PCM_RX_FORMATS))
    out_format = sdp.RtpPcmFormat(96, 'L16', 48000, 1, 10, max_frame_ms=10)
    s3_send = sdp.RtpPcmFormat(100, 'L16', 16000, 1, 10)
    with _reserved_udp_ports(4) as ports:
        left_port, right_port, source_port, destination_port = ports
    source, sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(('127.0.0.2', source_port));sink.bind(('127.0.0.3', destination_port))
    source.setblocking(False);sink.setblocking(False)
    left = relay_module.RtpPeer('127.0.0.2', source_port, selected.recv.payload_type,
        selected.recv.audio_format, rtp_format=selected.recv, send_rtp_format=selected.send)
    right = relay_module.RtpPeer('127.0.0.3', destination_port, s3_send.payload_type,
        s3_send.audio_format, rtp_format=s3_send, send_rtp_format=out_format,
        send_payload_type=out_format.payload_type, send_audio_format=out_format.audio_format)
    relay = relay_module.SipRtpRelay(left=left, right=right, left_port=left_port, right_port=right_port)
    try:
        await relay.start()
        values = tuple(range(-480, 480))
        payload = struct.pack('!960h', *values)
        packet = rtp.build_packet(rtp.RtpPacket(126, 123, 24000, 9876, payload[:960]))
        # The public builder deliberately limits outbound packets to1400B.
        # This independent standard peer sends the real1920B sample shape.
        packet = packet[:12] + payload
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(source, packet, ('127.0.0.1', left_port))
        first = rtp.parse_packet(await asyncio.wait_for(loop.sock_recv(sink, 4096), 1))
        second = rtp.parse_packet(await asyncio.wait_for(loop.sock_recv(sink, 4096), 1))
        assert len(first.payload) == len(second.payload) == 960
        assert first.payload_type == second.payload_type == 96
        assert first.ssrc == second.ssrc
        assert (second.sequence - first.sequence) & 65535 == 1
        assert (second.timestamp - first.timestamp) & 0xffffffff == 480
        assert first.payload + second.payload == payload
        assert relay.dropped == 0
    finally:
        await relay.stop();source.close();sink.close()
