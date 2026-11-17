"""Receiver packet-time hints never invent lower or upper packet-size bounds."""
from pathlib import Path

import pytest

from .voip_phase1_support import audio_format, sdp

FIXTURES = Path(__file__).parent / "fixtures/sdp"


@pytest.mark.parametrize("bounded", [True, False])
def test_recorded_answer_accepts_declared_tx10_below_receiver_preference20(bounded):
    # Captured from the existing isolated bridge peer, network data anonymized.
    offer = (FIXTURES / "tx10_receiver20_offer.sdp").read_text()
    answer = (FIXTURES / "tx10_receiver20_answer.sdp").read_text()
    if not bounded:
        answer = answer.replace("a=maxptime:32\n", "")
    selected = sdp.negotiate_answer_directional(answer,
        [audio_format.AudioFormat(16000, frame_ms=10)],
        [audio_format.AudioFormat(48000, frame_ms=10), audio_format.AudioFormat(16000, frame_ms=10)],
        local_offer_sdp=offer)
    assert selected is not None
    assert (selected.send.payload_type, selected.send.sample_rate, selected.send.frame_ms) == (96, 16000, 10)
    assert (selected.recv.payload_type, selected.recv.sample_rate, selected.recv.frame_ms) == (96, 16000, 10)


@pytest.mark.parametrize("ptime,minimum,maximum,local_ms,accepted", [
    (20, 0, 32, 10, True), (10, 10, 0, 20, True),
    (20, 0, 0, 10, True), (10, 0, 0, 20, True),
    (20, 20, 32, 10, False), (10, 0, 10, 20, False),
    (20, 10, 20, 10, True), (20, 10, 20, 20, True),
    (20, 16, 20, 10, False), (20, 10, 20, 32, False),
])
def test_only_explicit_receiver_bounds_constrain_declared_packet_time(ptime, minimum, maximum, local_ms, accepted):
    receiver = sdp.RtpPcmFormat(96, "L16", 16000, 1, ptime, min_frame_ms=minimum, max_frame_ms=maximum)
    selected = sdp._rtp_compatible_audio(receiver, audio_format.AudioFormat(16000, frame_ms=local_ms))
    assert (selected is not None) is accepted
    if selected is not None:
        assert selected.frame_ms == local_ms


def test_requested_packet_time_is_preferred_when_declared_in_same_mapping():
    offer = (FIXTURES / "tx10_receiver20_offer.sdp").read_text()
    answer = (FIXTURES / "tx10_receiver20_answer.sdp").read_text()
    selected = sdp.negotiate_answer_directional(answer,
        [audio_format.AudioFormat(16000, frame_ms=10), audio_format.AudioFormat(16000, frame_ms=20)],
        [audio_format.AudioFormat(16000, frame_ms=10)], local_offer_sdp=offer)
    assert selected is not None
    assert selected.send.frame_ms == 20 and selected.recv.frame_ms == 10


def test_hint_does_not_override_rtp_mapping_or_udp_payload_budget():
    local = audio_format.AudioFormat(16000, frame_ms=10)
    assert sdp._rtp_compatible_audio(sdp.RtpPcmFormat(96, "L16", 32000, 1, 20), local) is None
    assert sdp._rtp_compatible_audio(sdp.RtpPcmFormat(96, "L16", 16000, 2, 20), local) is None
    unsafe = audio_format.AudioFormat(48000, frame_ms=20)
    assert sdp._rtp_compatible_audio(sdp.RtpPcmFormat(96, "L16", 48000, 1, 20), unsafe) is None
