"""Receive codec alternatives retain offer PTs independently of transmit choice."""

from dataclasses import replace

import pytest

from .voip_phase1_support import audio_format, sdp, sip, sip_client


def _description(formats, *, ptime=20, direction="sendrecv"):
    return "\r\n".join([
        "v=0", "o=- 1 1 IN IP4 192.0.2.10", "s=-", "c=IN IP4 192.0.2.10", "t=0 0",
        "m=audio 40000 RTP/AVP " + " ".join(str(pt) for pt, _codec in formats),
        *(f"a=rtpmap:{pt} {codec}" for pt, codec in formats),
        f"a=ptime:{ptime}", f"a={direction}", "",
    ])


def _select(offer, answer, *, frame_ms=20):
    formats = [audio_format.AudioFormat(rate, "s16le", 1, frame_ms) for rate in (8000, 16000, 48000)]
    return sdp.negotiate_answer_directional(answer, formats, formats, local_offer_sdp=offer)


def test_receive_alternatives_preserve_offer_mapping_not_answer_preference():
    offer = _description([(96, "L16/16000"), (8, "PCMA/8000"), (0, "PCMU/8000")])
    answer = _description([(8, "PCMA/8000"), (0, "PCMU/8000"), (110, "L16/16000")])
    selected = _select(offer, answer)
    assert selected.send.payload_type == 8
    assert selected.recv.payload_type == 8
    assert [(fmt.payload_type, fmt.encoding) for fmt in selected.recv_formats] == [
        (96, "L16"), (8, "PCMA"), (0, "PCMU"),
    ]
    assert 110 not in [fmt.payload_type for fmt in selected.recv_formats]


def test_receive_set_excludes_formats_missing_from_either_description():
    selected = _select(
        _description([(8, "PCMA/8000"), (0, "PCMU/8000")]),
        _description([(8, "PCMA/8000"), (110, "L16/16000")]),
    )
    assert [(fmt.payload_type, fmt.encoding) for fmt in selected.recv_formats] == [(8, "PCMA")]


def test_same_dynamic_payload_number_does_not_make_different_codecs_compatible():
    selected = _select(
        _description([(96, "L16/16000"), (8, "PCMA/8000")]),
        _description([(96, "L24/16000"), (8, "PCMA/8000")]),
    )
    assert [(fmt.payload_type, fmt.encoding) for fmt in selected.recv_formats] == [(8, "PCMA")]


@pytest.mark.parametrize("direction,receives", [("recvonly", False), ("sendonly", True), ("inactive", False)])
def test_answer_direction_bounds_receive_set(direction, receives):
    selected = _select(
        _description([(8, "PCMA/8000"), (0, "PCMU/8000")]),
        _description([(8, "PCMA/8000"), (0, "PCMU/8000")], direction=direction),
    )
    assert bool(selected.recv_formats) is receives


def test_unicast_receive_ptime_comes_from_local_offer():
    formats = [audio_format.AudioFormat(8000, "s16le", 1, ms) for ms in (20, 10)]
    offer = _description([(8, "PCMA/8000"), (0, "PCMU/8000")], ptime=20)
    answer = _description([(8, "PCMA/8000"), (0, "PCMU/8000")], ptime=10)
    selected = sdp.negotiate_answer_directional(answer, formats, formats, local_offer_sdp=offer)
    assert selected.send.frame_ms == 10
    assert selected.recv.frame_ms == 20
    assert {fmt.frame_ms for fmt in selected.recv_formats} == {20}


def _dialog(fmt):
    return sip_client.SipDialog(
        target="Door", remote_host="192.0.2.10", remote_sip_port=5060,
        remote_rtp_host="192.0.2.10", remote_rtp_port=40000, local_rtp_port=40002,
        call_id="call", local_uri="sip:HA@192.0.2.1", remote_uri="sip:Door@192.0.2.10",
        remote_target_uri="sip:Door@192.0.2.10", send_format=fmt, recv_format=fmt,
        recv_formats=(fmt,),
    )


def test_changed_receive_alternatives_trigger_media_generation_update():
    original = _dialog(sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20))
    changed = replace(original, recv_formats=(*original.recv_formats, sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)))
    assert not sip_client.SipCallClient._same_dialog_media(original, changed)
    assert sip_client.SipCallClient._same_dialog_media(original, replace(original))


@pytest.mark.parametrize("dahua", [False, True])
def test_local_reoffer_answer_retains_receive_set_and_dahua_compatibility(dahua):
    codec = "PCM/16000" if dahua else "L16/16000"
    offer = _description([(96, codec), (8, "PCMA/8000")])
    answer = sip.parse_message(sip.build_response(200, "OK", [
        ("Content-Type", "application/sdp"), ("Contact", "<sip:Door@192.0.2.10>"),
    ], _description([(110, codec), (8, "PCMA/8000")]).encode()))
    client = sip_client.SipCallClient(
        local_ip="192.0.2.1", local_name="HA", local_sip_port=5060, local_rtp_port=40002,
        peer_user_agent="Dahua UAC/1.0" if dahua else "",
    )
    original = _dialog(sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20))
    candidate = client._dialog_candidate_from_answer(
        original, offer, answer, local_video_rtp_port=0, offered_video_formats=(),
        video_direction="inactive", session_version=2,
    )
    assert candidate is not None
    assert [(fmt.payload_type, fmt.encoding) for fmt in candidate.recv_formats] == [
        (96, "PCM" if dahua else "L16"), (8, "PCMA"),
    ]
    assert candidate.send_format.payload_type == 110
    assert candidate.recv_format.payload_type == 96
    assert original.recv_formats == (original.recv_format,)


@pytest.mark.parametrize("new_pt,accepted", [(96, False), (97, True)])
def test_remote_reoffer_cannot_reassign_receive_payload_before_commit(new_pt, accepted):
    client = sip_client.SipCallClient(
        local_ip="192.0.2.1", local_name="HA", local_sip_port=5060, local_rtp_port=40002,
        supported_formats=[audio_format.AudioFormat(rate, "s16le", 1, 20) for rate in (16000, 8000)],
    )
    original = _dialog(sdp.RtpPcmFormat(96, "L16", 16000, 1, 20))
    client.dialog = original
    request = sip.parse_message(sip.build_request("INVITE", "sip:HA@192.0.2.1", [
        ("Content-Type", "application/sdp"), ("Contact", "<sip:Door@192.0.2.10>"),
    ], _description([(new_pt, "L16/8000")]).encode()))
    prepared = client._answer_remote_offer(request)
    assert (prepared is not None) is accepted
    assert client.dialog is original
    assert original.recv_format.sample_rate == 16000
    if prepared is not None:
        candidate, _answer = prepared
        assert candidate.recv_formats == (candidate.recv_format,)
        assert candidate.recv_format.payload_type == 97


def test_local_reoffer_rejects_conflicting_receive_map_before_commit():
    client = sip_client.SipCallClient(
        local_ip="192.0.2.1", local_name="HA", local_sip_port=5060, local_rtp_port=40002,
    )
    original = _dialog(sdp.RtpPcmFormat(96, "L16", 16000, 1, 20))
    client.dialog = original
    offer = _description([(96, "L16/8000")])
    answer = sip.parse_message(sip.build_response(200, "OK", [
        ("Content-Type", "application/sdp"), ("Contact", "<sip:Door@192.0.2.10>"),
    ], offer.encode()))
    assert client._dialog_candidate_from_answer(
        original, offer, answer, local_video_rtp_port=0, offered_video_formats=(),
        video_direction="inactive", session_version=2,
    ) is None
    assert client.dialog is original


@pytest.mark.parametrize("local_offer", [False, True])
def test_retired_payload_identity_survives_intermediate_media_generation(local_offer):
    client = sip_client.SipCallClient(
        local_ip="192.0.2.1", local_name="HA", local_sip_port=5060, local_rtp_port=40002,
        supported_formats=[audio_format.AudioFormat(rate, "s16le", 1, 20) for rate in (8000, 16000)],
        peer_user_agent="Dahua UAC/1.0",
    )
    first = _dialog(sdp.RtpPcmFormat(97, "PCM", 16000, 1, 20))
    client.dialog = first

    def prepare(current, formats, version):
        body = _description(formats)
        headers = [("Content-Type", "application/sdp"), ("Contact", "<sip:Door@192.0.2.10>")]
        if local_offer:
            answer = sip.parse_message(sip.build_response(200, "OK", headers, body.encode()))
            return client._dialog_candidate_from_answer(
                current, body, answer, local_video_rtp_port=0, offered_video_formats=(),
                video_direction="inactive", session_version=version,
            )
        request = sip.parse_message(sip.build_request("INVITE", "sip:HA@192.0.2.1", headers, body.encode()))
        prepared = client._answer_remote_offer(request)
        return prepared[0] if prepared is not None else None

    second = prepare(first, [(0, "PCMU/8000")], 2)
    assert second is not None
    assert [(fmt.payload_type, fmt.encoding) for fmt in second.recv_formats] == [(0, "PCMU")]
    assert [(fmt.payload_type, fmt.encoding) for fmt in second.recv_payload_history] == [(97, "PCM"), (0, "PCMU")]
    client.dialog = second  # Commit generation B before preparing C.
    assert prepare(second, [(97, "L16/16000")], 3) is None
    assert client.dialog is second
    assert second.recv_format.encoding == "PCMU"
    # Reusing the retired PT for its original codec is still legal.
    restored = prepare(second, [(97, "PCM/16000")], 3)
    assert restored is not None
    assert len(restored.recv_payload_history) == 2
    assert client.dialog is second  # Preparation never publishes a candidate.


def test_payload_history_is_bounded_and_independent_of_active_receive_set():
    formats = tuple(sdp.RtpPcmFormat(pt, "L16", 16000, 1, 20) for pt in range(128))
    history = sip_client._validate_receive_payload_update(None, formats)
    assert len(history) == 128
    current = replace(_dialog(formats[0]), recv_payload_history=history)
    assert sip_client._validate_receive_payload_update(current, (formats[127],)) == history
    assert current.recv_formats == (formats[0],)
    with pytest.raises(sdp.SdpError, match="outside the RTP range"):
        sip_client._validate_receive_payload_update(current, (replace(formats[0], payload_type=128),))
