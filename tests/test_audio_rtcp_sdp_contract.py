"""The common answer serializer respects the peer's explicit RTCP disable."""

import pytest

from .voip_phase1_support import sdp


@pytest.mark.parametrize("bandwidth,enabled", [
    ("", True),
    ("b=RS:0\r\nb=RR:0\r\n", False),
    ("b=RS:100\r\nb=RR:300\r\n", True),
])
def test_answer_rtcp_capability_honors_selected_remote_audio_bandwidth(bandwidth, enabled):
    offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=Audio\r\n"
        "c=IN IP4 192.0.2.1\r\nt=0 0\r\nm=audio 40000 RTP/AVP 8\r\n"
        + bandwidth + "a=rtpmap:8 PCMA/8000\r\na=sendrecv\r\n")
    pcm = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
    answer = sdp.build_answer_directional("192.0.2.2", "192.0.2.2", 42000,
        pcm, pcm, remote_sdp=offer, audio_rtcp=True)
    assert ("b=RS:0\r\nb=RR:0" not in answer) is enabled
    assert sdp.parse_sdp(answer)["media_port"] == 42000
