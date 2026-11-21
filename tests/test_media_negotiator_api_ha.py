"""Production leg API: fixed capabilities, directions and exact offer bounds."""

from dataclasses import replace

import pytest

from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import AudioFormat, PcmFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media import leg, negotiator

pytestmark = pytest.mark.ha


def description(lines):
    return "\r\n".join(("v=0", "o=- 1 1 IN IP4 192.0.2.2", "s=Call",
        "c=IN IP4 192.0.2.2", "t=0 0", *lines.splitlines(), ""))


PCM16_20 = AudioFormat(16000, PcmFormat.S16LE, 1, 20)


def test_a_fixed_sender_never_invents_a_requested_packet_time():
    fixed = replace(PCM16_20, frame_ms=32)
    offer = description("m=audio 5000 RTP/AVP 96\na=rtpmap:96 L16/16000/1\na=ptime:20")
    result = negotiator.answer_offer(offer, [fixed], [fixed])
    assert isinstance(result, negotiator.LegContract)
    assert (result.send.frame_ms, result.receive.frame_ms) == (32, 32)


def test_a_send_only_leg_does_not_require_a_local_receiver():
    offer = description("m=audio 5000 RTP/AVP 96\na=rtpmap:96 L16/16000/1\na=ptime:20\na=recvonly")
    result = negotiator.answer_offer(offer, [PCM16_20], [])
    assert isinstance(result, negotiator.LegContract)
    projected = negotiator.as_direction(result, offer)
    assert projected.send_formats and not projected.recv_formats


def test_a_receive_only_leg_uses_its_receive_budget_not_its_transmit_budget():
    offer = description("m=audio 5000 RTP/AVP 10\na=ptime:10\na=sendonly")
    result = negotiator.answer_offer(offer, [], [], capabilities=HA_AUDIO_CAPABILITIES)
    assert isinstance(result, negotiator.LegContract), result
    projected = negotiator.as_direction(result, offer)
    assert projected.recv_formats and not projected.send_formats
    assert (projected.recv.sample_rate, projected.recv.channels) == (44100, 2)


def test_explicit_codec_order_is_applied_before_quality_tie_breaks():
    offer = description("m=audio 5000 RTP/AVP 96 8\na=rtpmap:96 L16/48000/1\na=ptime:10")
    result = negotiator.answer_offer(offer, [PCM16_20], [PCM16_20],
        capabilities=HA_AUDIO_CAPABILITIES, codec_policy=CodecPolicy(audio=("PCMA", "L16")))
    assert result.send.encoding == "PCMA"


def test_legacy_directional_peer_keeps_a_common_packet_time():
    offer = description("m=audio 5000 RTP/AVP 96 97\na=rtpmap:96 L16/48000/1\n"
        "a=rtpmap:97 L16/16000/1\na=x-voip-stack-flow:96 recv\n"
        "a=x-voip-stack-flow:97 send\na=ptime:10")
    result = leg.answer_offer(offer, leg.PHONE_POLICY)
    assert isinstance(result, leg.LegContract)
    assert (result.send.frame_ms, result.receive.frame_ms) == (10, 10)
    upgraded = leg.answer_offer(offer + "a=x-voip-stack-independent-ptime:1\r\n", leg.PHONE_POLICY)
    assert (upgraded.send.frame_ms, upgraded.receive.frame_ms) == (10, 20)


def test_an_answer_cannot_replace_the_bounds_advertised_in_the_offer():
    offer = description("m=audio 4000 RTP/AVP 96\na=rtpmap:96 L16/16000/1\n"
        "a=ptime:20\na=maxptime:20")
    answer = description("m=audio 5000 RTP/AVP 97\na=rtpmap:97 L16/16000/1\n"
        "a=ptime:10\na=maxptime:10")
    result = negotiator.apply_answer(offer, answer, [PCM16_20], [PCM16_20],
        capabilities=HA_AUDIO_CAPABILITIES)
    assert (result.send.payload_type, result.send.frame_ms) == (97, 10)
    assert (result.receive.payload_type, result.receive.frame_ms, result.receive.max_frame_ms) == (96, 20, 20)
