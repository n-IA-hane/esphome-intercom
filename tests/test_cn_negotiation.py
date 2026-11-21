"""RFC 3389 receive mappings survive offer/answer and explicit serialization."""

from dataclasses import replace
import importlib

import pytest

from .voip_phase1_support import _load_intercom_module, audio_format, sdp, sip_client

negotiator = importlib.import_module("custom_components.voip_stack.core.media.negotiator")
policy = importlib.import_module("custom_components.voip_stack.core.codec_policy")
capabilities = importlib.import_module("custom_components.voip_stack.core.audio_capabilities").HA_AUDIO_CAPABILITIES


@pytest.fixture(autouse=True)
def active_engine():
    shadow = importlib.import_module("custom_components.voip_stack.core.media.shadow")
    previous = shadow.engine_mode()
    shadow.set_engine_mode("active")
    try:
        yield
    finally:
        shadow.set_engine_mode(previous)


def description(*, cn_pt=13, primary="PCMU/8000", direction="sendrecv", cn=True):
    rows = ["v=0", "o=- 1 1 IN IP4 127.0.0.1", "s=-", "c=IN IP4 127.0.0.1", "t=0 0",
            "m=audio 40000 RTP/AVP 0" + (f" {cn_pt}" if cn else ""), f"a=rtpmap:0 {primary}"]
    if cn:
        rows.append(f"a=rtpmap:{cn_pt} CN/8000")
    text = "\r\n".join([*rows, "a=ptime:20", f"a={direction}", ""])
    return text if primary == "PCMU/8000" else text.replace("RTP/AVP 0", "RTP/AVP 96").replace("a=rtpmap:0 ", "a=rtpmap:96 ")


def enabled(*names):
    return policy.CodecPolicy(audio=names)


def answer(offer, selected_policy):
    return negotiator.answer_offer(offer, audio_format.HA_SIP_PCM_FORMATS, audio_format.HA_SIP_PCM_FORMATS,
                                  codec_policy=selected_policy, capabilities=capabilities)


def apply(offer, remote, selected_policy):
    return negotiator.apply_answer(offer, remote, audio_format.HA_SIP_PCM_FORMATS, audio_format.HA_SIP_PCM_FORMATS,
                                  codec_policy=selected_policy, capabilities=capabilities)


def test_cn_answer_retains_explicit_negotiated_receive_mapping():
    selected = answer(description(), enabled("PCMU", "CN"))
    assert [(fmt.payload_type, fmt.encoding) for fmt in selected.receive_accept] == [(0, "PCMU"), (13, "CN")]
    assert selected.send.encoding == selected.receive.encoding == "PCMU"
    assert selected.receive_accept[-1].frame_ms == selected.receive.frame_ms


@pytest.mark.parametrize("selected_policy", [enabled("PCMU"), policy.CodecPolicy()])
def test_cn_is_not_enabled_by_remote_offer_or_inherited_defaults(selected_policy):
    selected = answer(description(), selected_policy)
    assert all(fmt.encoding != "CN" for fmt in selected.receive_accept)


@pytest.mark.parametrize("primary", ["L16/16000", "L16/8000/2"])
def test_cn_does_not_attach_to_incompatible_primary_geometry(primary):
    selected = answer(description(primary=primary), enabled("L16", "CN"))
    assert all(fmt.encoding != "CN" for fmt in selected.receive_accept)


def test_cn_alone_never_becomes_primary():
    offer = description().replace(" 0 13", " 13").replace("a=rtpmap:0 PCMU/8000\r\n", "")
    assert isinstance(answer(offer, enabled("PCMU", "CN")), negotiator.Refusal)


def test_answer_cn_intersection_uses_actual_local_offer_payload_number():
    result = apply(description(cn_pt=109), description(cn_pt=110), enabled("PCMU", "CN"))
    assert [(fmt.payload_type, fmt.encoding) for fmt in result.receive_accept] == [(0, "PCMU"), (109, "CN")]
    assert all(fmt.payload_type != 110 for fmt in result.receive_accept)
    result = apply(description(cn=False), description(), enabled("PCMU", "CN"))
    assert all(fmt.encoding != "CN" for fmt in result.receive_accept)


@pytest.mark.parametrize("direction", ["recvonly", "inactive"])
def test_peer_not_sending_does_not_enable_cn_receive(direction):
    result = apply(description(), description(direction=direction), enabled("PCMU", "CN"))
    assert all(fmt.encoding != "CN" for fmt in result.receive_accept)


def test_answer_serializer_requires_negotiated_receive_tuple():
    selected = answer(description(), enabled("PCMU", "CN"))
    options = dict(remote_sdp=description(), audio_rtcp=False)
    plain = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 40002, selected.send, selected.receive, **options)
    assert 13 not in sdp.parse_sdp(plain)["payload_order"]
    explicit = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 40002, selected.send, selected.receive,
                                          recv_formats=selected.receive_accept, **options)
    assert sdp.parse_sdp(explicit)["payload_order"] == [0, 13]
    assert "a=rtpmap:13 CN/8000/1" in explicit
    assert "b=RS:0" in explicit and "b=RR:0" in explicit
    forged = replace(selected.receive, payload_type=110, encoding="CN")
    rejected = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 40002, selected.send, selected.receive,
                                          recv_formats=(selected.receive, forged), **options)
    assert 110 not in sdp.parse_sdp(rejected)["payload_order"]


def test_actual_client_offer_cn_is_the_answer_intersection_authority():
    client = sip_client.SipCallClient(local_ip="127.0.0.1", local_rtp_port=40004, local_sip_port=0, local_name="CN test",
                                    codec_policy=enabled("PCMU", "CN"), include_common_codecs=True)
    client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
    offer = client._local_sdp_body
    local_cn = [fmt for fmt in sdp.offered_pcm_formats(offer, include_auxiliary=True) if fmt.encoding == "CN"]
    assert len(local_cn) == 1
    result = apply(offer, description(), client.codec_policy)
    assert [fmt.encoding for fmt in result.receive_accept] == ["PCMU", "CN"]
    assert result.receive_accept[-1].payload_type == local_cn[0].payload_type


def test_decoder_only_g726_is_offered_only_for_receive_and_applies_one_way_answer(monkeypatch):
    g726 = importlib.import_module("custom_components.voip_stack.core.g726_codec")
    codec_capabilities = importlib.import_module("custom_components.voip_stack.core.codec_capabilities")
    def unavailable():
        raise RuntimeError("optional SpanDSP is absent")
    monkeypatch.setattr(g726, "_spandsp", unavailable)
    codec_capabilities.codec_available.cache_clear()
    try:
        assert codec_capabilities.codec_directions("G726-32") == {"send": False, "receive": True}
        client = sip_client.SipCallClient(local_ip="127.0.0.1", local_rtp_port=40004, local_sip_port=0,
            local_name="Receive only", codec_policy=enabled("G726-32"), include_common_codecs=True)
        with pytest.raises(ValueError, match="required local directions"):
            client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
        client._prepare_initial_offer(delayed_offer=False, audio_direction="recvonly")
        offer = client._local_sdp_body
        offered = sdp.offered_pcm_formats(offer)
        assert len(offered) == 1 and offered[0].encoding == "G726-32"
        remote = offer.replace("a=recvonly", "a=sendonly")
        result = apply(offer, remote, client.codec_policy)
        projected = negotiator.as_direction(result, remote)
        assert projected is not None
        assert projected.send_formats == ()
        assert [fmt.encoding for fmt in projected.recv_formats] == ["G726-32"]
    finally:
        codec_capabilities.codec_available.cache_clear()


@pytest.mark.parametrize("direction,allowed", [("sendrecv", False), ("recvonly", False), ("sendonly", True)])
def test_g723_valid_bitrate_restriction_bounds_only_local_transmit(direction, allowed):
    offer = description(primary="G723/8000", direction=direction, cn=False)
    offer = offer.replace("RTP/AVP 96", "RTP/AVP 4").replace("a=rtpmap:96", "a=rtpmap:4")
    offer = offer.replace("a=ptime:20", "a=ptime:30") + "a=fmtp:4 bitrate=5.3\r\n"
    formats = [audio_format.AudioFormat(8000, "s16le", 1, 30)]
    result = negotiator.answer_offer(offer, formats, formats, codec_policy=enabled("G723"))
    assert (not isinstance(result, negotiator.Refusal)) is allowed
