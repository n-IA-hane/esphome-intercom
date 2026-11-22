"""Adapter profiles reach the actual SDP writer without independent codec choices."""

from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media.payload_map import PayloadMap
from custom_components.voip_stack.endpoint_routing import sip_target_rtp_audio_profile
from custom_components.voip_stack.sip_client import SipCallClient, SipDialog

pytestmark = pytest.mark.ha


def writer(profile):
    client = SipCallClient(local_ip="192.0.2.1", local_name="HA", local_sip_port=5060,
        local_rtp_port=40000, supported_send_formats=list(profile.send_formats),
        supported_recv_formats=list(profile.recv_formats), supported_send_rtp_formats=profile.send_rtp_formats,
        supported_recv_rtp_formats=profile.recv_rtp_formats, codec_policy=profile.codec_policy,
        include_common_codecs=False, include_dahua_pcm=profile.include_dahua_pcm,
        match_received_codec=profile.match_received_codec)
    return client


def test_documented_dahua_pcm_quirk_keeps_pt97_and_real_receive_capacity():
    entry = SimpleNamespace(metadata={"sip_profile": "dahua", "dahua_audio": "pcm"})
    profile = sip_target_rtp_audio_profile(entry, codec_policy=CodecPolicy(audio=("PCM", "PCMU")))
    assert profile.include_dahua_pcm and not profile.match_received_codec
    client = writer(profile)
    client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
    offered = sdp.offered_pcm_formats(client._local_sdp_body, allow_dahua_pcm=True)
    assert [(fmt.payload_type, fmt.encoding) for fmt in offered] == [(97, "PCM"), (0, "PCMU")]
    assert sdp.parse_sdp(client._local_sdp_body)["ptime"] == 20
    assert sdp.parse_sdp(client._local_sdp_body)["maxptime"] >= 40
    assert "x-" not in client._local_sdp_body


def test_hardware_kind_cannot_replace_standard_sip_media_policy():
    plain = sip_target_rtp_audio_profile(SimpleNamespace(metadata={"endpoint_kind": "sip_account"}))
    hardware = sip_target_rtp_audio_profile(SimpleNamespace(metadata={"endpoint_kind": "esphome"}))
    assert hardware == plain
    assert hardware.send_formats[0].sample_rate == 48000


def test_contact_options_geometry_remains_constrained_in_the_real_wire_offer():
    body = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Capabilities\r\nc=IN IP4 192.0.2.2\r\n"
            "t=0 0\r\nm=audio 0 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000/1\r\na=ptime:20\r\na=sendrecv\r\n")
    metadata = {"sip_capabilities_known": True, **sdp.options_capabilities(body)}
    profile = sip_target_rtp_audio_profile(SimpleNamespace(metadata=metadata))
    client = writer(profile)
    client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
    assert {fmt.encoding for fmt in sdp.offered_pcm_formats(client._local_sdp_body)} == {"PCMA"}
    assert all(not capability.times.fixed for capability in profile.evidence.sends)


def test_actual_offer_writer_respects_payload_meaning_history():
    profile = sip_target_rtp_audio_profile(SimpleNamespace(metadata={}), codec_policy=CodecPolicy(audio=("L16",)))
    client = writer(profile)
    history = PayloadMap(receive={96: ("L16", 16000, 1)})
    active = sdp.RtpPcmFormat(97, "L16", 48000, 1, 10)
    client.dialog = SipDialog(target="peer", remote_host="192.0.2.2", remote_sip_port=5060,
        remote_rtp_host="192.0.2.2", remote_rtp_port=41000, local_rtp_port=40000,
        call_id=client.dialog_ids.call_id, local_uri="sip:ha@192.0.2.1", remote_uri="sip:peer@192.0.2.2",
        send_format=active, recv_format=active, audio_payloads=history)
    client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
    rows = {fmt.payload_type: (fmt.encoding, fmt.sample_rate, fmt.channels)
            for fmt in sdp.offered_pcm_formats(client._local_sdp_body)}
    assert rows.get(96) in {None, ("L16", 16000, 1)}
    assert rows[97] == ("L16", 48000, 1)
