"""Captured offers and answers traverse the production SDP entry points."""

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS
from custom_components.voip_stack.local_call_media import source_dtmf_formats
from custom_components.voip_stack.sip_bridge import invite_rtp_peer
from custom_components.voip_stack.sip_listener import SipUdpEndpoint
from tests.media_corpus_support import load_steps, mismatches, run_production

pytestmark = pytest.mark.ha




@pytest.mark.parametrize("step", load_steps(), ids=lambda step: step.name)
def test_production_entry_points_apply_the_captured_audio_contract(step):
    actual = run_production(step)
    expected = {key: value for key, value in step.expect.items()
                if key in {"outcome", "send", "recv", "recv_accept", "dtmf", "answer_contains", "answer_excludes"}}
    assert not mismatches(expected, actual), mismatches(expected, actual)


def test_directional_audio_selects_and_advertises_each_event_clock():
    body = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=ESP\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 5000 RTP/AVP 97 96 101 102 103\r\n"
        "a=rtpmap:96 L16/16000/1\r\n"
        "a=rtpmap:97 L16/48000/1\r\n"
        "a=ptime:10\r\n"
        "a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-15\r\n"
        "a=rtpmap:102 telephone-event/16000\r\na=fmtp:102 0-15\r\n"
        "a=rtpmap:103 telephone-event/48000\r\na=fmtp:103 0-15\r\n").encode()
    endpoint = SipUdpEndpoint(local_ip="192.0.2.1", local_sip_port=5060, local_rtp_port=4000,
        supported_formats=list(HA_SIP_PCM_TX_FORMATS), supported_send_formats=list(HA_SIP_PCM_TX_FORMATS),
        supported_recv_formats=list(HA_SIP_PCM_RX_FORMATS), on_invite=None)
    request = sip.parse_message(sip.build_request("INVITE", "sip:Casa@192.0.2.1", [
        ("Via", "SIP/2.0/UDP 192.0.2.2:5060;branch=z9hG4bK-clocks"),
        ("From", "<sip:ESP@192.0.2.2>;tag=esp"), ("To", "<sip:Casa@192.0.2.1>"),
        ("Call-ID", "clocks@esp"), ("CSeq", "1 INVITE"), ("Content-Type", "application/sdp"),
    ], body))
    invite = endpoint._parse_invite(request, ("192.0.2.2", 5060))
    assert invite is not None
    peer = invite_rtp_peer(invite)
    assert (peer.dtmf_payload_type, peer.dtmf_clock_rate) == (102, 16000)
    assert (peer.outbound_dtmf_payload_type, peer.outbound_dtmf_clock_rate) == (103, 48000)
    assert source_dtmf_formats(invite)[0] == invite.recv_dtmf_format
    assert {fmt.sample_rate for fmt in source_dtmf_formats(invite)} == {8000, 16000, 48000}
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 4000,
        invite.send_format, invite.recv_format, dtmf_direction=invite.dtmf_direction, remote_sdp=body)
    assert {item.sample_rate for item in sdp.offered_dtmf_formats(answer)} == {8000, 16000, 48000}
    assert (invite.send_format.frame_ms, invite.recv_format.frame_ms) == (10, 20)


def test_answer_serializer_preserves_exact_chosen_event_without_selecting_again():
    remote = ("v=0\r\no=peer 1 1 IN IP4 192.0.2.2\r\ns=call\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 8 101 102\r\na=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-15\r\n"
        "a=rtpmap:102 telephone-event/16000\r\na=fmtp:102 0-15\r\n")
    audio = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
    chosen = sdp.RtpDtmfFormat(101, 8000, frozenset({1, 2, 3}))
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 40002,
        audio, audio, remote_sdp=remote, dtmf=chosen)
    assert sdp.offered_dtmf_formats(answer) == [chosen]
