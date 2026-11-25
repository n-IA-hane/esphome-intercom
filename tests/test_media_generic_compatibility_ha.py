"""Neutral media policy reproduces compatibility without a device identity."""

import pytest

from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import AudioFormat, HA_TRUNK_AUDIO_FORMATS
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media import negotiator
from custom_components.voip_stack.core.media.capability import AnswerOrder, MediaCompatibility
from custom_components.voip_stack.peer_media_profile import resolve_peer_media_profile

pytestmark = pytest.mark.ha


def test_generic_mapping_preferences_reproduce_the_contact_adapter_wire_contract():
    generic = MediaCompatibility(
        optional_mappings=(("PCM", 16000, 1),),
        payload_preferences=((("PCM", 16000, 1), 97),),
        answer_order=AnswerOrder.PEER_IF_UNCONFIGURED,
    )
    contact = resolve_peer_media_profile({"user_agent": "Dahua UAC/1.0"})
    plans = [negotiator.create_offer(HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS,
        common=True, capabilities=HA_AUDIO_CAPABILITIES, compatibility=value)
        for value in (generic, contact.audio_compatibility)]
    assert plans[0] == plans[1]
    assert (97, ("PCM", 16000, 1)) in plans[0].payloads
    assert next(fmt for fmt in plans[0].formats if fmt.encoding == "PCM").audio_format == AudioFormat(16000, frame_ms=20)


@pytest.mark.parametrize("order,configured,encoding", [
    (AnswerOrder.LOCAL, False, "L16"),
    (AnswerOrder.PEER, False, "PCMA"),
    (AnswerOrder.PEER_IF_UNCONFIGURED, False, "PCMA"),
    (AnswerOrder.PEER_IF_UNCONFIGURED, True, "L16"),
])
def test_answer_precedence_is_explicit_and_preserves_configured_preference(order, configured, encoding):
    answer = ("v=0\r\no=peer 1 1 IN IP4 192.0.2.2\r\ns=call\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 8 96\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:96 L16/16000\r\n"
        "a=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n")
    offer = answer.replace("RTP/AVP 8 96", "RTP/AVP 96 8")
    preferred = (AudioFormat(16000), AudioFormat(8000))
    result = negotiator.apply_answer(offer, answer, preferred, preferred,
        capabilities=HA_AUDIO_CAPABILITIES,
        codec_policy=CodecPolicy(audio=("L16", "PCMA")) if configured else None,
        compatibility=MediaCompatibility(answer_order=order))
    assert isinstance(result, negotiator.LegContract)
    assert result.send.encoding == encoding
    assert result.receive.sample_rate == 16000


def test_optional_mapping_does_not_grant_an_unimplemented_geometry():
    body = ("v=0\r\no=peer 1 1 IN IP4 192.0.2.2\r\ns=call\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 97\r\na=rtpmap:97 PCM/48000\r\na=ptime:20\r\n")
    result = negotiator.answer_offer(body, HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS,
        capabilities=HA_AUDIO_CAPABILITIES,
        compatibility=MediaCompatibility(optional_mappings=(("PCM", 48000, 1),)))
    assert isinstance(result, negotiator.Refusal)
