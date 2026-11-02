"""Public audio frame contracts retain every sample across legal RTP quanta."""

from dataclasses import replace
import struct

import pytest

from .voip_phase1_support import _load_intercom_module, g722_codec, rtp, sdp, sip_client

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
