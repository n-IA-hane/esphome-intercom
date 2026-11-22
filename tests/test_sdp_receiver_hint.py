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


@pytest.mark.parametrize("ptime,minimum,maximum,local_ms", [
    (20, 0, 32, 10), (10, 10, 0, 20), (20, 0, 0, 10), (10, 0, 0, 20),
    (20, 20, 32, 10), (10, 0, 10, 20), (20, 10, 20, 10), (20, 10, 20, 20),
    (20, 16, 20, 10), (20, 10, 20, 32),
])

def test_only_explicit_receiver_bounds_constrain_declared_packet_time(ptime, minimum, maximum, local_ms):
    bounds = "".join(f"a={name}:{value}\r\n" for name, value in
        (("minptime", minimum), ("maxptime", maximum)) if value)
    offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Bounds\r\n"
             "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 40000 RTP/AVP 96\r\n"
             f"a=rtpmap:96 L16/16000/1\r\na=ptime:{ptime}\r\n"
             f"{bounds}a=recvonly\r\n")
    selected = sdp.negotiate_directional(offer,
        [audio_format.AudioFormat(16000, frame_ms=local_ms)], [])
    # minptime is a preference when no producible duration reaches it. A
    # receiver's explicit maximum remains a hard limit.
    feasible = not maximum or local_ms <= maximum
    assert (selected is not None) is feasible
    if selected is not None:
        assert selected.send.frame_ms == local_ms


def test_requested_packet_time_is_preferred_when_declared_in_same_mapping():
    offer = (FIXTURES / "tx10_receiver20_offer.sdp").read_text()
    answer = (FIXTURES / "tx10_receiver20_answer.sdp").read_text()
    selected = sdp.negotiate_answer_directional(answer,
        [audio_format.AudioFormat(16000, frame_ms=10), audio_format.AudioFormat(16000, frame_ms=20)],
        [audio_format.AudioFormat(16000, frame_ms=10)], local_offer_sdp=offer)
    assert selected is not None
    assert selected.send.frame_ms == 20 and selected.recv.frame_ms == 10


@pytest.mark.parametrize("rate,channels,local_rate,local_ms", [(32000, 1, 16000, 10),
    (16000, 2, 16000, 10), (48000, 1, 48000, 20)])
def test_hint_does_not_override_rtp_mapping_or_udp_payload_budget(rate, channels, local_rate, local_ms):
    offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Mapping\r\n"
             "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 40000 RTP/AVP 96\r\n"
             f"a=rtpmap:96 L16/{rate}/{channels}\r\na=ptime:20\r\na=recvonly\r\n")
    assert sdp.negotiate_directional(offer,
        [audio_format.AudioFormat(local_rate, frame_ms=local_ms)], []) is None
