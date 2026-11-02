"""Runtime sample duration decides stateless PCM passthrough, not RX capacity."""
import asyncio
from contextlib import asynccontextmanager
import socket
import struct
from unittest.mock import Mock

import pytest
from .voip_phase1_support import _load_intercom_module, _reserved_udp_ports, audio_format, rtp, sdp

relay_module = _load_intercom_module('sip_rtp_bridge')
PcmFrameConverter = _load_intercom_module('core.audio_pcm').PcmFrameConverter


@asynccontextmanager
async def wire_relay(incoming_ms=10):
    with _reserved_udp_ports(4) as ports:
        left_port, right_port, source_port, sink_port = ports
    source, sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(('127.0.0.2', source_port)); sink.bind(('127.0.0.3', sink_port))
    source.setblocking(False); sink.setblocking(False)
    incoming = sdp.RtpPcmFormat(96, 'L16', 16000, 1, incoming_ms, max_frame_ms=20)
    outgoing = sdp.RtpPcmFormat(97, 'L16', 16000, 1, 10, max_frame_ms=10)
    left = relay_module.RtpPeer('127.0.0.2', source_port, 96, incoming.audio_format, rtp_format=incoming)
    right = relay_module.RtpPeer('127.0.0.3', sink_port, 97, outgoing.audio_format, rtp_format=outgoing)
    relay = relay_module.SipRtpRelay(left=left, right=right, left_port=left_port, right_port=right_port)
    try:
        await relay.start()
        yield source, sink, relay
    finally:
        await relay.stop(); source.close(); sink.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('incoming_ms',[10,20])
async def test_real_short_packet_never_depends_on_decode_or_conversion(monkeypatch,incoming_ms):
    async with wire_relay(incoming_ms) as (source, sink, relay):
        assert not relay.left_to_right_passthrough
        assert relay.left_decoder is not None and relay.left_to_right is not None
        def forbidden(*args):
            raise AssertionError('Compatible10ms payload must cross without PCM processing')
        monkeypatch.setattr(relay.left_decoder, 'decode', forbidden)
        monkeypatch.setattr(relay.left_to_right, 'convert', forbidden)
        payload = struct.pack('!160h', *range(160))
        await asyncio.get_running_loop().sock_sendto(source, rtp.build_packet(rtp.RtpPacket(96, 1, 0, 55, payload)), ('127.0.0.1', relay.left_port))
        packet = rtp.parse_packet(await asyncio.wait_for(asyncio.get_running_loop().sock_recv(sink, 4096), 1))
        assert packet.payload == payload and packet.payload_type == 97
        assert relay.left_to_right_passthrough and relay.dropped == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('durations,decode_calls', [([10, 20, 10], 1), ([16, 10, 4, 10], 3), ([5, 10, 5, 10], 3)])
async def test_real_packet_mix_preserves_pcm_order_clock_and_pending(durations, decode_calls, monkeypatch):
    async with wire_relay() as (source, sink, relay):
        decode = Mock(wraps=relay.left_decoder.decode)
        monkeypatch.setattr(relay.left_decoder, 'decode', decode)
        original = bytearray(); timestamp = 0
        for index, duration in enumerate(durations):
            samples = 16 * duration
            payload = struct.pack(f'!{samples}h', *range(timestamp, timestamp + samples))
            original.extend(payload)
            await asyncio.get_running_loop().sock_sendto(source, rtp.build_packet(rtp.RtpPacket(96, index, timestamp, 55, payload)), ('127.0.0.1', relay.left_port))
            timestamp += samples
        packets = [rtp.parse_packet(await asyncio.wait_for(asyncio.get_running_loop().sock_recv(sink, 4096), 1)) for _ in range(sum(durations) // 10)]
        assert all(len(packet.payload) == 320 and packet.payload_type == 97 for packet in packets)
        assert b''.join(packet.payload for packet in packets) == original
        assert all((b.sequence-a.sequence)&65535 == 1 and (b.timestamp-a.timestamp)&0xffffffff == 160 for a,b in zip(packets,packets[1:]))
        assert decode.call_count == decode_calls and relay.dropped == 0


def test_pcm_converter_pending_short_nominal_short_keeps_sample_order():
    fmt = audio_format.AudioFormat(16000, 's16le', 1, 10)
    converter = PcmFrameConverter(fmt, fmt)
    expected = struct.pack('<320h', *range(320))
    frames = []
    for data in (expected[:160], expected[160:480], expected[480:]):
        frames.extend(converter.convert(data))
    assert b''.join(frames) == expected


@pytest.mark.parametrize('encoding,rate,channels,size', [('G722',8000,1,80),('OPUS',48000,2,100)])
def test_new_runtime_shortcut_does_not_bypass_stateful_or_vbr_codecs(encoding,rate,channels,size):
    incoming=sdp.RtpPcmFormat(96,encoding,rate,channels,10,max_frame_ms=20)
    outgoing=sdp.RtpPcmFormat(97,encoding,rate,channels,10,max_frame_ms=10)
    assert not relay_module._audio_payload_relay_compatible(incoming,outgoing,payload_size=size)


@pytest.mark.parametrize('size,minptime', [(319,0),(640,0),(672,0),(320,16)])
def test_runtime_shortcut_keeps_sample_alignment_duration_and_bounds(size,minptime):
    incoming=sdp.RtpPcmFormat(96,'L16',16000,1,10,min_frame_ms=minptime,max_frame_ms=20)
    outgoing=sdp.RtpPcmFormat(97,'L16',16000,1,10,max_frame_ms=10)
    assert not relay_module._audio_payload_relay_compatible(incoming,outgoing,payload_size=size)


@pytest.mark.asyncio
async def test_runtime_passthrough_preserves_existing_pcm_observer():
    async with wire_relay() as (source,sink,relay):
        observed=[]
        relay.left_decoder.add_pcm_observer(lambda pcm,fmt:observed.append(pcm))
        payload=struct.pack('!160h',*range(160))
        await asyncio.get_running_loop().sock_sendto(source,rtp.build_packet(rtp.RtpPacket(96,1,0,55,payload)),('127.0.0.1',relay.left_port))
        packet=rtp.parse_packet(await asyncio.wait_for(asyncio.get_running_loop().sock_recv(sink,4096),1))
        assert packet.payload==payload and b''.join(observed)==struct.pack('<160h',*range(160))
        assert relay.left_to_right_passthrough and relay.dropped==0
