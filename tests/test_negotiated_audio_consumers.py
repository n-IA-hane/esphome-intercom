"""Alternate negotiated audio payloads through existing relay and local sinks."""

import asyncio
import struct
import sys
from types import SimpleNamespace

import pytest

from .test_conference import _load_module


def _formats():
    sdp = _load_module("sdp")
    return (sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20),
            sdp.RtpPcmFormat(97, "PCM", 16000, 1, 20))


def _packet(pt=97, sequence=1):
    rtp = _load_module("rtp")
    return rtp.build_packet(rtp.RtpPacket(
        payload_type=pt, sequence=sequence, timestamp=0, ssrc=55,
        payload=struct.pack("<320h", *([1000] * 320)),
    ))


def _assert_signal(frame):
    values = struct.unpack(f"<{len(frame)//2}h", frame)
    assert len(values) >= 160
    assert all(950 <= sample <= 1050 for sample in values[64:-32])


def test_relay_normalizes_alternate_codec_and_preserves_single_codec_passthrough():
    relay_module = _load_module("sip_rtp_bridge")
    rtp = _load_module("rtp")
    pcmu, pcm = _formats()
    left = relay_module.RtpPeer("127.0.0.2", 40000, 0, pcmu.audio_format,
                               rtp_format=pcmu, send_rtp_format=pcmu, inbound_rtp_formats=(pcmu, pcm))
    right = relay_module.RtpPeer("127.0.0.3", 41000, 97, pcm.audio_format, rtp_format=pcm, send_rtp_format=pcm)
    relay = relay_module.SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    sent = []
    relay.right_transport = SimpleNamespace(sendto=lambda data, _addr: sent.append(data))
    relay.handle_packet("left", _packet(), (left.host, left.port))
    assert relay.dropped == 0
    assert len(sent) == 1
    _assert_signal(rtp.parse_packet(sent[0]).payload)
    relay.handle_packet("left", _packet(pt=111, sequence=2), (left.host, left.port))
    assert len(sent) == 1
    assert relay.dropped == 1
    direct = relay_module.SipRtpRelay(left=right, right=right, left_port=42000, right_port=42002)
    assert direct.left_to_right_passthrough
    assert direct.left_decoder is None


def test_relay_codec_update_retains_only_bounded_receive_overlap(monkeypatch):
    from dataclasses import replace

    module = _load_module("sip_rtp_bridge")
    receiver_module = sys.modules[module.RtpAudioReceiver.__module__]
    now = [100.0]
    monkeypatch.setattr(receiver_module, "monotonic", lambda: now[0])
    pcmu, pcm = _formats()
    left = module.RtpPeer("127.0.0.2", 40000, 0, pcmu.audio_format,
                          rtp_format=pcmu, send_rtp_format=pcmu, inbound_rtp_formats=(pcmu, pcm))
    right = module.RtpPeer("127.0.0.3", 41000, 0, pcmu.audio_format,
                           rtp_format=pcmu, send_rtp_format=pcmu)
    relay = module.SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    sent = []
    relay.right_transport = SimpleNamespace(sendto=lambda data, _addr: sent.append(data))
    relay.prepare_peer_reconfiguration("left", replace(left, inbound_rtp_formats=(pcmu,)))()
    assert not relay.left_to_right_passthrough
    relay.handle_packet("left", _packet(), (left.host, left.port))
    assert len(sent) == 1
    now[0] += 61
    relay.handle_packet("left", _packet(sequence=2), (left.host, left.port))
    assert len(sent) == 1
    assert relay.dropped == 1


def test_multiple_allowed_codecs_preserve_opus_bytes_and_clock_after_codec_switch():
    from unittest.mock import Mock

    module = _load_module("sip_rtp_bridge")
    rtp = _load_module("rtp")
    sdp = _load_module("sdp")
    opus = sdp.RtpPcmFormat(106, "OPUS", 48000, 2, 20)
    pcmu, _pcm = _formats()
    left = module.RtpPeer("127.0.0.2", 40000, 106, opus.audio_format,
                         rtp_format=opus, send_rtp_format=opus, inbound_rtp_formats=(opus, pcmu))
    right = module.RtpPeer("127.0.0.3", 41000, 106, opus.audio_format,
                          rtp_format=opus, send_rtp_format=opus, timestamp=10000)
    relay = module.SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    relay.left_decoder.decode = Mock(wraps=relay.left_decoder.decode)
    relay.right_encoder.encode = Mock(wraps=relay.right_encoder.encode)
    sent = []
    relay.right_transport = SimpleNamespace(sendto=lambda data, _addr: sent.append(rtp.parse_packet(data)))
    opus_silence = b"\xf8\xff\xfe"
    for sequence, (pt, timestamp, payload) in enumerate([
        (106, 1000, opus_silence), (106, 1960, opus_silence),
        (0, 320, b"\xff" * 160),
        (106, 100000, opus_silence), (106, 100960, opus_silence),
    ]):
        packet = rtp.build_packet(rtp.RtpPacket(
            payload_type=pt, sequence=sequence, timestamp=timestamp, ssrc=55, payload=payload,
        ))
        relay.handle_packet("left", packet, (left.host, left.port))
    assert relay.dropped == 0
    assert len(sent) == 5
    assert [packet.timestamp for packet in sent] == [10000 + 960 * i for i in range(5)]
    assert all(sent[index].payload == opus_silence for index in (0, 1, 3, 4))
    assert all(packet.payload_type == 106 for packet in sent)
    assert [call.args[0] for call in relay.left_decoder.decode.call_args_list] == [0]
    assert relay.right_encoder.encode.call_count == 1
    assert relay.left_to_right_passthrough


@pytest.mark.asyncio
async def test_conference_accepts_alternate_codec_into_existing_mixer_fifo():
    conference = _load_module("conference")
    receiver = _load_module("rtp_audio_receiver")
    pcmu, pcm = _formats()
    room = conference.ConferenceRoom(SimpleNamespace(), name="Test", local_ip="127.0.0.1")
    leg = conference._ConferenceLeg(
        "call", "Door", "sip", "127.0.0.2", 40000,
        conference.PcmFrameConverter(pcmu.audio_format, conference.CONFERENCE_FORMAT),
        conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, pcmu.audio_format),
        decoder=receiver.RtpAudioReceiver(pcmu, (pcmu, pcm)),
    )
    room.legs["call"] = leg
    room.handle_rtp("call", _packet(), (leg.remote_host, leg.remote_port))
    assert leg.rx_packets == 1
    assert len(leg.in_fifo) == 1
    _assert_signal(leg.in_fifo[0])
    room.handle_rtp("call", _packet(pt=111), (leg.remote_host, leg.remote_port))
    assert leg.rx_packets == 1


@pytest.mark.asyncio
async def test_local_assist_media_accepts_alternate_codec_without_new_transport():
    module = _load_module("local_call_media")
    pcmu, pcm = _formats()
    invite = SimpleNamespace(
        call_id="call", recv_format=pcmu, send_format=pcmu, recv_formats=(pcmu, pcm),
        remote_rtp_host="127.0.0.2", remote_rtp_port=40000,
        local_audio_direction="sendrecv", remote_audio_connection_held=False,
        remote_sdp=None,
    )
    session = module.LocalCallMedia(SimpleNamespace(), invite=invite, local_rtp_port=42000,
                                   reservation=None, on_complete=None)
    session._accepting_input = True
    session.handle_rtp(_packet(), (invite.remote_rtp_host, invite.remote_rtp_port))
    assert session.counters["rtp_rx"] == 1
    _assert_signal(session.rx_queue.get_nowait())
    session.handle_rtp(_packet(pt=111), (invite.remote_rtp_host, invite.remote_rtp_port))
    assert session.counters["drop_payload_type"] == 1
    assert session.rx_queue.empty()
