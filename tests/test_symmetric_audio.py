"""Negotiated symmetric encoder and RTP source-clock behavior."""

import struct
import asyncio
from types import SimpleNamespace

import pytest

from .voip_phase1_support import rtp, sdp, sip_client


PCMU = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
PCM_RX = sdp.RtpPcmFormat(97, "PCM", 16000, 1, 20)
PCM_TX = sdp.RtpPcmFormat(112, "PCM", 16000, 1, 20)


def test_symmetric_encoder_follows_only_negotiated_codec_and_preserves_input():
    encoder = sip_client.RtpPayloadEncoder(PCMU, send_formats=(PCMU, PCM_TX), match_received_codec=True)
    assert encoder.follow_received(PCM_RX)
    assert encoder.fmt == PCM_TX
    assert encoder.input_format == PCMU.audio_format
    samples = struct.unpack("<320h", encoder.encode(struct.pack("<160h", *([2000] * 160))))
    assert all(abs(value - 2000) <= 3 for value in samples[64:])
    assert not encoder.follow_received(sdp.RtpPcmFormat(112, "L16", 32000, 1, 20))
    assert encoder.fmt == PCM_TX
    assert encoder.follow_received(PCMU)
    assert encoder.encode(bytes(320)) == bytes([255] * 160)


def test_default_encoder_keeps_selected_codec():
    encoder = sip_client.RtpPayloadEncoder(PCMU, send_formats=(PCMU, PCM_TX))
    assert not encoder.follow_received(PCM_RX)
    assert encoder.fmt == PCMU


def test_clock_change_starts_new_source_and_same_clock_keeps_state():
    source = rtp.AudioRtpSenderState(17, 1000, 123)
    assert not source.use_clock_rate(8000)
    assert (source.sequence, source.timestamp, source.ssrc) == (17, 1000, 123)
    assert source.use_clock_rate(16000)
    second = (source.sequence, source.timestamp, source.ssrc)
    assert second[2] != 123
    assert not source.use_clock_rate(16000)
    assert (source.sequence, source.timestamp, source.ssrc) == second
    assert source.use_clock_rate(8000)
    assert source.ssrc != second[2]


def test_failed_encoder_change_leaves_active_contract_intact(monkeypatch):
    opus = sdp.RtpPcmFormat(98, "OPUS", 48000, 2, 20)
    encoder = sip_client.RtpPayloadEncoder(PCMU, send_formats=(PCMU, opus), match_received_codec=True)
    def unavailable(*_args):
        raise RuntimeError("codec allocation failed")
    monkeypatch.setattr(sip_client, "OpusEncoder", unavailable)
    with pytest.raises(RuntimeError):
        encoder.follow_received(opus)
    assert encoder.fmt == PCMU
    assert encoder.encode(bytes(320)) == bytes([255] * 160)


@pytest.mark.asyncio
@pytest.mark.parametrize("symmetric", [False, True])
async def test_local_media_sends_negotiated_tx_payload_after_valid_rx(symmetric):
    from .test_conference import _load_module

    media = _load_module("local_call_media")
    contract = media.LocalAudioContract(
        call_id="local", remote_rtp_host="127.0.0.2", remote_rtp_port=40000,
        send_format=PCMU, recv_format=PCMU, recv_formats=(PCMU, PCM_RX),
        send_formats=(PCMU, PCM_TX), match_received_codec=symmetric,
    )
    session = media.LocalCallMedia(None, invite=contract, local_rtp_port=42000,
                                   reservation=None, on_complete=None)
    received = rtp.build_packet(rtp.RtpPacket(
        payload_type=97, sequence=1, timestamp=0, ssrc=44,
        payload=struct.pack("<320h", *([1000] * 320)),
    ))
    session.handle_rtp(received, (contract.remote_rtp_host, contract.remote_rtp_port))
    assert session.counters["rtp_rx"] == 1
    assert session.encoder.input_format == PCMU.audio_format
    assert session.invite.send_format == PCMU
    sent = []

    def capture(raw, _address):
        sent.append(rtp.parse_packet(raw))
        session.closed.set()

    session.transport = SimpleNamespace(sendto=capture)
    await session.tx_queue.put(struct.pack("<160h", *([2000] * 160)))
    await asyncio.wait_for(session._send_loop(), 1)
    assert len(sent) == 1
    assert sent[0].payload_type == (112 if symmetric else 0)
    assert len(sent[0].payload) == (640 if symmetric else 160)
    if symmetric:
        samples = struct.unpack("<320h", sent[0].payload)
        assert all(abs(value - 2000) <= 3 for value in samples[64:])


@pytest.mark.asyncio
@pytest.mark.parametrize("symmetric", [False, True])
async def test_conference_mixer_uses_answer_tx_mapping_without_changing_pcm_contract(symmetric):
    from .test_conference import _FakeHass, _load_module

    conference = _load_module("conference")
    receiver = _load_module("rtp_audio_receiver")
    room = conference.ConferenceRoom(_FakeHass(), name="Room", local_ip="127.0.0.1")
    leg = conference._ConferenceLeg(
        "door", "Door", "sip", "127.0.0.2", 40000,
        conference.PcmFrameConverter(PCMU.audio_format, conference.CONFERENCE_FORMAT),
        conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, PCMU.audio_format),
        decoder=receiver.RtpAudioReceiver(PCMU, (PCMU, PCM_RX)),
        encoder=sip_client.RtpPayloadEncoder(PCMU, send_formats=(PCMU, PCM_TX), match_received_codec=symmetric),
    )
    other = conference._ConferenceLeg(
        "browser", "Browser", "local", "", 0,
        conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT),
        conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT),
        local_out=asyncio.Queue(), in_fifo=[struct.pack("<320h", *([2000] * 320))],
    )
    room.legs.update(door=leg, browser=other)
    room.handle_rtp("door", rtp.build_packet(rtp.RtpPacket(
        payload_type=97, sequence=1, timestamp=0, ssrc=44, payload=bytes(640),
    )), (leg.remote_host, leg.remote_port))
    assert leg.rx_packets == 1
    sent = []

    def capture(raw, _address):
        sent.append(rtp.parse_packet(raw))
        room.legs.clear()

    leg.transport = SimpleNamespace(sendto=capture)
    await asyncio.wait_for(room._mix_loop(), 1)
    assert leg.encoder.input_format == PCMU.audio_format
    assert leg.out_converter.dst == PCMU.audio_format
    assert len(sent) == 1
    assert sent[0].payload_type == (112 if symmetric else 0)
    assert len(sent[0].payload) == (640 if symmetric else 160)
    if symmetric:
        samples = struct.unpack("<320h", sent[0].payload)
        assert all(abs(value - 2000) <= 5 for value in samples[64:])
