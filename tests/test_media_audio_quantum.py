"""Public audio frame contracts retain every sample across legal RTP quanta."""

from importlib import import_module
from dataclasses import replace
import json
import math
import struct

import pytest

from .voip_phase1_support import _load_intercom_module, audio_format, rtp, sdp, sip_client

RtpAudioReceiver = _load_intercom_module("rtp_audio_receiver").RtpAudioReceiver


@pytest.mark.parametrize("frame_ms", [10, 16, 20, 32])
def test_g722_encoder_and_decoder_keep_the_negotiated_sample_quantum(frame_ms):
    fmt = sdp.RtpPcmFormat(9, "G722", 8000, 1, frame_ms)
    encoder = sip_client.RtpPayloadEncoder(fmt)
    decoder = sip_client.RtpPayloadDecoder(fmt)
    samples = 16 * frame_ms
    pcm = struct.pack("<h", 12000) * samples
    for _ in range(3):
        payload = encoder.encode(pcm)
        # G.722 64 kbit/s carries one octet per pair of 16 kHz samples.
        assert len(payload) == 8 * frame_ms
        decoded = decoder.decode(payload)
        assert len(decoded) == samples * 2
        assert max(struct.unpack(f"<{samples}h", decoded)) > 10000
    assert fmt.audio_format.frame_ms == frame_ms
    assert fmt.sample_rate == 8000 and fmt.audio_format.sample_rate == 16000


def test_l16_long_packet_is_reframed_without_losing_or_reordering_samples():
    fmt = replace(sdp.RtpPcmFormat(96, "L16", 16000, 1, 10), max_frame_ms=20)
    receiver = RtpAudioReceiver(fmt)
    samples = tuple(range(-160, 160))
    wire = struct.pack("!320h", *samples)
    frames = receiver.decode(96, wire)
    assert [len(frame) for frame in frames] == [320, 320]
    assert b"".join(frames) == struct.pack("<320h", *samples)
    # The next record has its own samples, with no remainder from the first.
    assert receiver.decode(96, struct.pack("!160h", *([1234] * 160))) == [struct.pack("<160h", *([1234] * 160))]


def test_g711_long_packet_retains_all_samples_in_two_selected_quanta():
    fmt = replace(sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 10), max_frame_ms=20)
    receiver = RtpAudioReceiver(fmt)
    frames = receiver.decode(8, bytes([0xd5]) * 160)
    assert frames == [struct.pack("<h", 8) * 80] * 2


def test_explicit_maxptime_still_rejects_an_oversized_l16_packet():
    fmt = replace(sdp.RtpPcmFormat(96, "L16", 16000, 1, 10), max_frame_ms=20)
    receiver = RtpAudioReceiver(fmt)
    with pytest.raises(rtp.RtpError):
        receiver.decode(96, bytes(21 * 16 * 2))


@pytest.mark.parametrize("encoding,rate", [
    ("L16", 48000), ("L16", 32000), ("L16", 16000), ("L16", 8000),
    ("OPUS", 48000), ("G722", 8000), ("PCMA", 8000), ("PCMU", 8000),
])
@pytest.mark.parametrize("receive_preference_ms", [10, 20])
def test_generic_offer_quantum_and_maxptime_are_receivable_for_every_codec(
    monkeypatch, tmp_path, encoding, rate, receive_preference_ms,
):
    monkeypatch.setattr(import_module(sdp.common_sip_codecs.__module__), "common_sip_codecs", lambda: frozenset({"OPUS", "G722"}))
    formats = list(audio_format.HA_SIP_PCM_FORMATS)
    receive_formats = sorted(formats, key=lambda fmt: fmt.frame_ms != receive_preference_ms)
    offer = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 40000,
        formats, receive_formats, include_common_codecs=True,
        local_capabilities=sip_client.HA_AUDIO_CAPABILITIES)
    parsed = sdp.parse_sdp(offer)
    assert parsed["ptime"] == receive_preference_ms
    assert parsed["ptime"] <= parsed["maxptime"]
    assert parsed["maxptime"] == min(sip_client.HA_AUDIO_CAPABILITIES.max_receive_packet_ms(fmt)
        for fmt in sdp.offered_pcm_formats(offer))
    fmt = next(fmt for fmt in sdp.offered_pcm_formats(offer)
        if fmt.encoding == encoding and fmt.sample_rate == rate)
    records = []
    # maxptime is a ceiling, not a promise that every codec has that frame
    # duration. RFC 7587 permits Opus encoder frames up to 60 ms, not 64 ms.
    maximum = (max(ms for ms in (2.5, 5, 10, 20, 40, 60) if ms <= parsed["maxptime"])
               if encoding == "OPUS" else parsed["maxptime"])
    for packet_ms in (parsed["ptime"], maximum):
        wire_format = replace(fmt, frame_ms=packet_ms)
        samples = wire_format.audio_format.nominal_frame_samples * wire_format.audio_format.channels
        packet_count = int(fmt.frame_ms) // math.gcd(int(fmt.frame_ms), int(packet_ms))
        encoder = sip_client.RtpPayloadEncoder(wire_format)
        receiver = RtpAudioReceiver(fmt)
        frames, expected, payload_bytes = [], [], 0
        try:
            for packet in range(packet_count):
                pcm = struct.pack(f"<{samples}h", *(1000 + (packet * samples + index) % 100
                                                   for index in range(samples)))
                expected.append(pcm)
                payload = encoder.encode(pcm)
                payload_bytes += len(payload)
                frames.extend(receiver.decode(fmt.payload_type, payload, normalize_frames=True))
            assert not receiver.has_pending_pcm
        finally:
            encoder.close()
            receiver.close()
        decoded = b"".join(frames)
        assert len(decoded) == sum(map(len, expected))
        assert all(len(frame) == fmt.audio_format.nominal_frame_bytes for frame in frames)
        if encoding == "L16":
            assert decoded == b"".join(expected)
        records.append({"packet_ms": packet_ms, "packets": packet_count, "samples": samples,
            "payload_bytes": payload_bytes, "decoded_bytes": len(decoded), "frames": len(frames)})
    (tmp_path / "codec-quantum.json").write_text(json.dumps({
        "encoding": encoding, "rate": rate, "offer": offer, "records": records,
    }, indent=2))
