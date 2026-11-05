"""Bridge packet-time alternatives come from the actual listener TX policy."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_bridge import bridge_source_audio
from custom_components.voip_stack.sip_listener import SipUdpEndpoint


pytestmark = pytest.mark.ha


OFFER = (
    "v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Independent peer\r\n"
    "c=IN IP4 127.0.0.2\r\nt=0 0\r\nm=audio 35000 RTP/AVP 96 97\r\n"
    "a=rtpmap:96 L16/16000/1\r\na=rtpmap:97 L16/48000/1\r\n"
    "a=x-voip-stack-flow:96 sendrecv\r\na=x-voip-stack-flow:97 recv\r\n"
    "a=x-voip-stack-independent-ptime:1\r\na=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\n"
)


def parse_with_send_formats(formats):
    endpoint = SipUdpEndpoint(
        local_ip="127.0.0.1", local_sip_port=5060, local_rtp_port=40000,
        supported_formats=formats, supported_send_formats=formats,
        supported_recv_formats=[AudioFormat(16000, frame_ms=20)], on_invite=None,
    )
    request = sip.parse_message(sip.build_request("INVITE", "sip:phone@127.0.0.1", [
        ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bK-local-tx"),
        ("From", "<sip:peer@127.0.0.2>;tag=source"), ("To", "<sip:phone@127.0.0.1>"),
        ("Call-ID", "local-transmit-policy"), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:peer@127.0.0.2:5060>"), ("Content-Type", "application/sdp"),
    ], OFFER.encode()))
    return endpoint._parse_invite(request, ("127.0.0.2", 5060))


@pytest.mark.parametrize("other", [None, AudioFormat(16000, "s24le", 1, 10),
                                  AudioFormat(16000, "s16le", 2, 10)])
def test_bridge_never_invents_16k_mono_s16_10ms_for_restricted_listener(other):
    formats = [AudioFormat(16000, frame_ms=20), AudioFormat(48000, frame_ms=10)]
    if other is not None:
        formats.append(other)
    invite = parse_with_send_formats(formats)
    assert invite is not None
    assert invite.local_send_capabilities == tuple(formats)
    destination = SimpleNamespace(
        recv_format=sdp.RtpPcmFormat(100, "L16", 16000, 1, 20),
        send_format=sdp.RtpPcmFormat(101, "L16", 48000, 1, 10),
    )
    selected = bridge_source_audio(invite, destination)
    assert (selected.send_format.sample_rate, selected.send_format.frame_ms) == (48000, 10)
    assert selected.local_send_capabilities is invite.local_send_capabilities
    assert selected.local_recv_capabilities is invite.local_recv_capabilities


def test_20ms_only_listener_rejects_receiver_maxptime10():
    assert parse_with_send_formats([AudioFormat(16000, frame_ms=20)]) is None


def test_answered_source_retains_the_existing_media_contract():
    invite = parse_with_send_formats([AudioFormat(16000, frame_ms=20), AudioFormat(48000, frame_ms=10)])
    invite = replace(invite, initial_response_sent=True)
    destination = SimpleNamespace(
        recv_format=sdp.RtpPcmFormat(100, "L16", 16000, 1, 20),
        send_format=sdp.RtpPcmFormat(101, "L16", 48000, 1, 10),
    )
    assert bridge_source_audio(invite, destination) is invite
