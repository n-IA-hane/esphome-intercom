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


def test_standard_multiformat_peer_has_independent_receiver_packet_time():
    offer = description("m=audio 5000 RTP/AVP 96 97\na=rtpmap:96 L16/48000/1\n"
        "a=rtpmap:97 L16/16000/1\na=ptime:10\na=maxptime:20")
    result = leg.answer_offer(offer, leg.PHONE_POLICY)
    assert isinstance(result, leg.LegContract)
    assert (result.send.sample_rate, result.receive.sample_rate) == (48000, 16000)
    assert (result.send.frame_ms, result.receive.frame_ms) == (10, 20)
    assert {fmt.sample_rate for fmt in result.receive_accept} == {16000, 48000}


def test_an_answer_cannot_replace_the_bounds_advertised_in_the_offer():
    offer = description("m=audio 4000 RTP/AVP 96\na=rtpmap:96 L16/16000/1\n"
        "a=ptime:20\na=maxptime:20")
    answer = description("m=audio 5000 RTP/AVP 97\na=rtpmap:97 L16/16000/1\n"
        "a=ptime:10\na=maxptime:10")
    result = negotiator.apply_answer(offer, answer, [PCM16_20], [PCM16_20],
        capabilities=HA_AUDIO_CAPABILITIES)
    assert (result.send.payload_type, result.send.frame_ms) == (97, 10)
    assert (result.receive.payload_type, result.receive.frame_ms, result.receive.max_frame_ms) == (96, 20, 20)


def test_declared_peer_plan_is_consumed_by_the_production_offer_writer():
    """Binding evidence must produce the same complete plan as an ordinary call."""
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS
    from custom_components.voip_stack.core.media.capability import PeerEvidence
    from custom_components.voip_stack.core.sdp_composer import _write_offer_plan

    evidence = PeerEvidence.from_tokens(
        ["L16/16000/1/20", "L16/16000/1/10", "L16/48000/1/10"],
        ["L16/48000/1/10", "L16/48000/1/20", "L16/16000/1/20", "L16/16000/1/10"], complete=True)
    plan = negotiator.create_offer(
        HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS, evidence,
        capabilities=HA_AUDIO_CAPABILITIES, common=True)
    body = _write_offer_plan(
        plan, "192.0.2.1", "192.0.2.1", 40000,
        audio_direction="sendrecv", audio_rtcp=False, video_port=0, video_formats=(),
        video_direction="inactive", codec_policy=None)

    assert {(fmt.encoding, fmt.sample_rate, fmt.channels)
            for fmt in sdp.offered_pcm_formats(body)} == {
                ("L16", 48000, 1), ("L16", 16000, 1)}
    assert sdp.offered_dtmf_formats(body)
    assert sdp.parse_sdp(body)["ptime"] == 20


@pytest.mark.parametrize("peer_ptime,send_time,receive_time", [(10, 10, 20), (20, 20, 20), (30, 30, 20)])
def test_public_api_applies_independent_standard_receiver_preferences(peer_ptime, send_time, receive_time):
    peer = description(f"m=audio 5000 RTP/AVP 96\na=rtpmap:96 L16/16000/1\na=ptime:{peer_ptime}")
    result = negotiator.answer_offer(peer, [PCM16_20], [PCM16_20], capabilities=HA_AUDIO_CAPABILITIES)
    assert isinstance(result, negotiator.LegContract)
    assert (result.send.sample_rate, result.send.frame_ms) == (16000, send_time)
    assert (result.receive.sample_rate, result.receive.frame_ms) == (16000, receive_time)
