"""CC-12: RFC 3551 static L16 mappings use the production parser and engine."""

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.media import leg
from custom_components.voip_stack.core.media.payload_map import PayloadMap

pytestmark = pytest.mark.ha


def offer(payload, mapping=""):
    return "\r\n".join((
        "v=0", "o=- 1 1 IN IP4 192.0.2.2", "s=Static audio",
        "c=IN IP4 192.0.2.2", "t=0 0", f"m=audio 5000 RTP/AVP {payload}",
        *(mapping.splitlines() if mapping else ()), "a=ptime:10", "a=sendrecv", "",
    ))


@pytest.mark.parametrize("payload,channels,packet_ms", [(11, 1, 10)])
def test_static_l16_offer_is_negotiated_without_rtpmap(payload, channels, packet_ms):
    result = leg.answer_offer(offer(payload), leg.PHONE_POLICY)
    assert isinstance(result, leg.LegContract), result
    assert (result.send.payload_type, result.send.encoding, result.send.sample_rate,
            result.send.channels, result.send.frame_ms) == (payload, "L16", 44100, channels, packet_ms)
    answer = sdp.build_answer_directional(
        "192.0.2.1", "192.0.2.1", 4000, result.send, result.receive,
        remote_sdp=offer(payload),
    )
    sdp.validate_sdp_answer(offer(payload), answer)


def test_static_stereo_is_recognized_but_the_transmit_budget_is_not_widened():
    """At 44.1 kHz, 5 ms is 220.5 samples; the first exact 10 ms needs 1764 bytes."""
    parsed = sdp.offered_pcm_formats(offer(10))
    assert [(item.encoding, item.sample_rate, item.channels) for item in parsed] == [("L16", 44100, 2)]
    refused = leg.answer_offer(offer(10), leg.PHONE_POLICY)
    assert isinstance(refused, leg.Refusal)
    assert "packet time" in refused.reason


@pytest.mark.parametrize("payload,channels", [(10, 2), (11, 1)])
def test_static_l16_numbers_are_used_in_new_local_offers(payload, channels):
    mapping = ("L16", 44100, channels)
    numbered = PayloadMap.for_offer([mapping])
    assert numbered.receive == {payload: mapping}


@pytest.mark.parametrize("payload,channels", [(10, 2), (11, 1)])
def test_static_l16_redefinition_is_refused(payload, channels):
    body = offer(payload, f"a=rtpmap:{payload} L16/16000/{channels}")
    assert not sdp.offered_pcm_formats(body)
    assert isinstance(leg.answer_offer(body, leg.PHONE_POLICY), leg.Refusal)
