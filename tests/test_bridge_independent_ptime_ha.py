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


@pytest.mark.parametrize("independent,expected_ms", [(True, 20), (False, 10)])
def test_actual_options_profile_retains_receiver_preference_across_bridge(independent, expected_ms):
    # Captured offers have only addresses and SDP origin identifiers anonymized.
    caller = (FIXTURES / ("native_independent_offer.sdp" if independent else "native_13b_offer.sdp")).read_text()
    selected = sdp.negotiate_directional(caller, list(HA_SIP_PCM_TX_FORMATS), list(HA_SIP_PCM_RX_FORMATS))
    assert selected is not None
    assert (selected.send.sample_rate, selected.send.frame_ms) == (48000, 10)
    # HA's receive default is independent of the target's preference below.
    assert (selected.recv.sample_rate, selected.recv.frame_ms) == (16000, 10)
    metadata = {
        "sip_capabilities_known": True,
        "sdp_features": ["directional_audio_v1"] + (["independent_audio_ptime_v1"] if independent else []),
        "sip_audio_tx_formats": (["L16/16000/1/20", "L16/16000/1/10", "G722/8000/1/20", "G722/8000/1/10"]
                                 if independent else ["L16/16000/1/10"]),
        "sip_audio_rx_formats": ["L16/48000/1/10", "L16/16000/1/20", "L16/16000/1/10"],
    }
    profile = endpoint_routing.sip_target_rtp_audio_profile(None, SimpleNamespace(metadata=metadata))
    assert profile is not None
    assert (profile.recv_rtp_formats[0].sample_rate, profile.recv_rtp_formats[0].frame_ms) == (16000, expected_ms)
    assert any(f.sample_rate == 48000 for f in profile.recv_rtp_formats), "Exercise the local advisory fallback"
    invite = SimpleNamespace(send_format=selected.send, recv_format=selected.recv)
    bridge = endpoint_routing.bridge_audio_profile(invite, list(profile.send_formats), list(profile.recv_formats), profile,
                                                    include_common_codecs=False)
    assert set(bridge.send_rtp_formats) == set(profile.send_rtp_formats)
    assert set(bridge.recv_rtp_formats) == set(profile.recv_rtp_formats)
    # Passthrough still prefers the caller's incoming wire encoding/rate/time.
    assert (bridge.send_rtp_formats[0].encoding, bridge.send_rtp_formats[0].sample_rate,
            bridge.send_rtp_formats[0].channels) == (selected.recv.encoding, selected.recv.sample_rate,
                                                   selected.recv.channels)
    if independent:
        assert bridge.recv_rtp_formats == profile.recv_rtp_formats
    body = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 40020,
        list(bridge.send_formats), list(bridge.recv_formats), send_rtp_formats=bridge.send_rtp_formats,
        recv_rtp_formats=bridge.recv_rtp_formats, include_common_codecs=False,
        allow_directional_payloads=True, independent_audio_ptime=independent)
    assert f"a=ptime:{expected_ms}\r\n" in body
    assert "L16/16000/1" in body and "L16/48000/1" in body


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


def test_bridge_prefers_common_mapping_without_copying_source_packet_time():
    entry = SimpleNamespace(metadata={
        "sdp_features": ["directional_audio_v1"],
        "sip_audio_tx_formats": ["L16/16000/1/10"],
        "sip_audio_rx_formats": ["L16/48000/1/10", "L16/16000/1/10"],
    })
    profile = endpoint_routing.sip_target_rtp_audio_profile(None, entry)
    invite = SimpleNamespace(send_format=sdp.RtpPcmFormat(96, "L16", 48000, 1, 10),
                             recv_format=sdp.RtpPcmFormat(97, "L16", 16000, 1, 20))
    bridge = endpoint_routing.bridge_audio_profile(invite, list(profile.send_formats), list(profile.recv_formats), profile,
                                                    include_common_codecs=False)
    assert (bridge.send_rtp_formats[0].sample_rate, bridge.send_rtp_formats[0].frame_ms) == (16000, 10)
    assert set(bridge.send_rtp_formats) == set(profile.send_rtp_formats)
    assert bridge.recv_rtp_formats == profile.recv_rtp_formats
