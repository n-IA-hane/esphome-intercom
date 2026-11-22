"""Captured Native audio shapes keep destination packet-time preferences."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as E

import pytest

from custom_components.voip_stack import endpoint_routing
from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS
from custom_components.voip_stack.dialog_info import DialogState, document

pytestmark = pytest.mark.ha
FIXTURES = Path(__file__).parent / "fixtures/sdp"


@pytest.mark.parametrize("maximum,expected_send_ms", [(20, 20), (10, 10)])
def test_standard_contact_receiver_envelope_retains_bridge_preferences(maximum, expected_send_ms):
    caller = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Standard peer\r\n"
        "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 40000 RTP/AVP 96 97\r\n"
        "a=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\n"
        f"a=ptime:10\r\na=maxptime:{maximum}\r\na=sendrecv\r\n")
    selected = sdp.negotiate_directional(caller, list(HA_SIP_PCM_TX_FORMATS), list(HA_SIP_PCM_RX_FORMATS))
    assert selected is not None
    assert (selected.send.sample_rate, selected.send.frame_ms) == (48000, 10)
    assert (selected.recv.sample_rate, selected.recv.frame_ms) == (16000, 20)
    metadata = {"sip_capabilities_known": True,
        **sdp.options_capabilities(caller.replace("m=audio 40000", "m=audio 0"))}
    profile = endpoint_routing.sip_target_rtp_audio_profile(SimpleNamespace(metadata=metadata))
    assert all(not capability.times.fixed for capability in profile.evidence.sends)
    invite = SimpleNamespace(send_format=selected.send, recv_format=selected.recv)
    bridge = endpoint_routing.bridge_audio_profile(invite, profile)
    assert set(bridge.send_rtp_formats) == set(profile.send_rtp_formats)
    assert set(bridge.recv_rtp_formats) == set(profile.recv_rtp_formats)
    # Retain the receiver's preferred geometry even when16k could bypass SRC.
    assert (bridge.send_rtp_formats[0].sample_rate, bridge.send_rtp_formats[0].frame_ms) == (48000, 10)
    assert any((fmt.sample_rate, fmt.frame_ms) == (16000, expected_send_ms)
               for fmt in bridge.send_rtp_formats)
    body = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 40020,
        list(bridge.send_formats), list(bridge.recv_formats), send_rtp_formats=bridge.send_rtp_formats,
        recv_rtp_formats=bridge.recv_rtp_formats)
    assert "a=ptime:20\r\n" in body
    assert "L16/16000/1" in body and "L16/48000/1" in body
    assert "x-voip-stack" not in body


@pytest.mark.parametrize("fixture", ["native_independent_offer.sdp", "native_13b_offer.sdp"])
def test_historical_unknown_attributes_cannot_change_standard_media(fixture):
    captured = (FIXTURES / fixture).read_text()
    standard = "\r\n".join(line for line in captured.splitlines()
        if not line.startswith("a=x-")) + "\r\n"
    args = (list(HA_SIP_PCM_TX_FORMATS), list(HA_SIP_PCM_RX_FORMATS))
    assert sdp.negotiate_directional(captured, *args) == sdp.negotiate_directional(standard, *args)


def _document_dialog(item, version=0):
    return E.fromstring(document("sip:demo@example.test", version, [item])).find("{*}dialog")


def test_long_dialog_identifier_is_bounded_stable_and_keeps_sip_identity():
    long_identity = "source-" * 24 + ":2:wire"
    item = DialogState(long_identity, "proceeding", "initiator", call_id="wire-call", local_tag="local", remote_tag="remote")
    before = _document_dialog(item)
    after = _document_dialog(replace(item, state="confirmed"), version=1)
    assert before.get("id") == after.get("id")
    assert len(before.get("id")) == 64
    for key, value in {"call-id": "wire-call", "local-tag": "local", "remote-tag": "remote"}.items():
        assert before.get(key) == after.get(key) == value


def test_dialog_identifier_separates_long_shared_prefix_generation_and_wire():
    session = "same-prefix-" * 20
    ids = {_document_dialog(DialogState(f"{session}:{generation}:{wire}", "confirmed", "initiator",
            session_id=session, generation=generation)).get("id")
           for generation in (1, 2) for wire in ("wire-a", "wire-b")}
    assert len(ids) == 4
    assert all(len(value) == 64 for value in ids)


def test_bridge_preserves_destination_geometry_without_copying_source_packet_time():
    entry = SimpleNamespace(metadata={
        "sip_capabilities_known": True,
        "sip_audio_receiver_maxptime": 10,
        "sip_audio_tx_formats": ["L16/16000/1/10"],
        "sip_audio_rx_formats": ["L16/48000/1/10", "L16/16000/1/10"],
    })
    profile = endpoint_routing.sip_target_rtp_audio_profile(entry)
    invite = SimpleNamespace(send_format=sdp.RtpPcmFormat(96, "L16", 48000, 1, 10),
                             recv_format=sdp.RtpPcmFormat(97, "L16", 16000, 1, 20))
    bridge = endpoint_routing.bridge_audio_profile(invite, profile)
    assert (bridge.send_rtp_formats[0].sample_rate, bridge.send_rtp_formats[0].frame_ms) == (48000, 10)
    assert set(bridge.send_rtp_formats) == set(profile.send_rtp_formats)
    assert bridge.recv_rtp_formats == profile.recv_rtp_formats
