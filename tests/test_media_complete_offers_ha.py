"""Complete initial offers traverse the same planner as reduced Contact offers."""

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_format import HA_TRUNK_AUDIO_FORMATS
from custom_components.voip_stack.core.codec_policy import CodecPolicy, DEFAULT_AUDIO_CODECS
from custom_components.voip_stack.sip_client import SipCallClient

pytestmark = pytest.mark.ha




def actual_offer(codecs):
    client = SipCallClient(local_ip="192.0.2.1", local_name="Trunk", local_sip_port=5060, local_rtp_port=4000,
        supported_send_formats=list(HA_TRUNK_AUDIO_FORMATS), supported_recv_formats=list(HA_TRUNK_AUDIO_FORMATS),
        include_common_codecs=True, codec_policy=CodecPolicy(audio=codecs) if codecs is not None else None)
    client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
    return client._local_sdp_body


def test_a_complete_g723_offer_uses_real_frame_duration_and_static_payload():
    body = actual_offer(("G723",))
    formats = sdp.offered_pcm_formats(body)
    assert [(fmt.encoding, fmt.payload_type, fmt.frame_ms) for fmt in formats] == [("G723", 4, 30)]


def test_an_opus_offer_asks_for_20_ms_without_copying_another_payloads_quantum():
    body = actual_offer(("OPUS",))
    assert sdp.parse_sdp(body)["ptime"] == 20
    assert {fmt.encoding for fmt in sdp.offered_pcm_formats(body)} == {"OPUS"}


def test_wideband_pcm_is_not_removed_because_its_old_preferred_duration_is_too_large():
    body = actual_offer(("L16",))
    assert any(fmt.sample_rate == 48000 for fmt in sdp.offered_pcm_formats(body))
    assert "m=video" not in body


def test_new_optional_codecs_are_not_enabled_by_an_unconfigured_caller():
    body = actual_offer(None)
    assert {fmt.encoding for fmt in sdp.offered_pcm_formats(body)} <= set(DEFAULT_AUDIO_CODECS)
