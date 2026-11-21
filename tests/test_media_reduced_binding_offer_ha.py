"""A fresh complete Contact declaration bounds the real SipCallClient offer."""

from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.media import shadow
from custom_components.voip_stack.endpoint_routing import sip_target_offer_common_audio, sip_target_rtp_audio_profile
from custom_components.voip_stack.sip_client import SipCallClient

pytestmark = pytest.mark.ha

CAPABILITIES = {
    "sip_capabilities_known": True, "sip_audio_capabilities_complete": True,
    "sip_audio_tx_formats": ["L16/16000/1/20", "L16/16000/1/10"],
    "sip_audio_rx_formats": ["L16/48000/1/10", "L16/16000/1/20", "L16/16000/1/10"],
    "sdp_features": ["directional_audio_v1", "independent_audio_ptime_v1"],
}


@pytest.fixture(autouse=True)
def active_engine():
    previous = shadow.engine_mode()
    shadow.set_engine_mode("active")
    try:
        yield
    finally:
        shadow.set_engine_mode(previous)


def actual_offer(metadata):
    row = SimpleNamespace(metadata=metadata)
    profile = sip_target_rtp_audio_profile(None, row)
    client = SipCallClient(local_ip="192.0.2.1", local_name="Casa", local_sip_port=5060, local_rtp_port=4000,
        supported_send_formats=list(profile.send_formats), supported_recv_formats=list(profile.recv_formats),
        supported_send_rtp_formats=profile.send_rtp_formats, supported_recv_rtp_formats=profile.recv_rtp_formats,
        include_common_codecs=sip_target_offer_common_audio(None, row),
        allow_directional_audio_payloads=True, independent_audio_ptime=True)
    client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
    return client._local_sdp_body


def test_complete_binding_receives_only_its_declared_audio_mappings():
    body = actual_offer(CAPABILITIES)
    assert {(item.encoding, item.sample_rate, item.channels) for item in sdp.offered_pcm_formats(body)} == {
        ("L16", 48000, 1), ("L16", 16000, 1)}
    assert sdp.parse_sdp(body)["ptime"] == 20
    assert "m=video" not in body


def test_partial_or_unknown_binding_does_not_narrow_the_offer():
    body = actual_offer({**CAPABILITIES, "sip_audio_capabilities_complete": False})
    assert len(sdp.offered_pcm_formats(body)) > 2


def test_another_contacts_declaration_cannot_replace_this_bindings_codecs():
    body = actual_offer({**CAPABILITIES, "sip_audio_tx_formats": ["OPUS/48000/2/20"],
                         "sip_audio_rx_formats": ["OPUS/48000/2/20"]})
    assert {item.encoding for item in sdp.offered_pcm_formats(body)} == {"OPUS"}
