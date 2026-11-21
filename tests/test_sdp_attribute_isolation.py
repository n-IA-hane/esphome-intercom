"""CC-7 isolates malformed SDP attributes from independent usable codecs."""

import pytest

from .voip_phase1_support import audio_format, sdp


def description(attributes, payloads="8 96"):
    return "\r\n".join([
        "v=0", "o=- 1 1 IN IP4 192.0.2.10", "s=CC7",
        "c=IN IP4 192.0.2.10", "t=0 0",
        f"m=audio 40000 RTP/AVP {payloads}", *attributes, "a=sendrecv", "",
    ])


@pytest.mark.parametrize("name", ["ptime", "minptime", "maxptime"])
@pytest.mark.parametrize("invalid", ["bad", "nan", "inf", "0", "-1", "1001"])
@pytest.mark.parametrize("valid_first", [False, True])
def test_malformed_packet_limit_preserves_other_fields_and_valid_same_field(name, invalid, valid_first):
    hints = {"ptime": 20, "minptime": 10, "maxptime": 30}
    valid = [f"a={key}:{value}" for key, value in hints.items()]
    bad = [f"a={name}:{invalid}"]
    parsed = sdp.parse_sdp(description(
        ["a=rtpmap:8 PCMA/8000", *(valid + bad if valid_first else bad + valid)]))
    assert {key: parsed[key] for key in hints} == hints
    assert [fmt.payload_type for fmt in sdp.offered_pcm_formats(description(
        ["a=rtpmap:8 PCMA/8000", *(valid + bad if valid_first else bad + valid)]))] == [8]


def test_invalid_hint_alone_and_unknown_hint_do_not_remove_usable_audio():
    parsed = sdp.parse_sdp(description([
        "a=rtpmap:8 PCMA/8000", "a=ptime:nan", "a=minptime:bad",
        "a=maxptime:inf", "a=x-unknown-receiver-hint:bad",
    ]))
    assert (parsed["ptime"], parsed["minptime"], parsed["maxptime"]) == (0, 0, 0)
    assert parsed["rtpmap"][8] == ("PCMA", 8000, 1)


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_receiver_bounds_keep_the_strictest_independent_of_order(reverse):
    limits = ["a=maxptime:10", "a=maxptime:40", "a=minptime:20", "a=minptime:5"]
    if reverse:
        limits.reverse()
    offer = description(["a=rtpmap:8 PCMA/8000", *limits], payloads="8")
    parsed = sdp.parse_sdp(offer)
    assert (parsed["minptime"], parsed["maxptime"]) == (20, 10)
    # The unachievable hard maximum cannot be relaxed by the later duplicate.
    local = [audio_format.AudioFormat(8000, frame_ms=20)]
    assert sdp.negotiate_directional(offer, local, local) is None


@pytest.mark.parametrize("payload", [8, 96])
@pytest.mark.parametrize("mapping", ["", "PCMA", "PCMA/bad", "PCMA/0", "PCMA/8000/0", "PCMA/8000/1/2", "/8000"])
def test_invalid_rtpmap_removes_only_its_payload_in_offer_and_answer_validation(payload, mapping):
    survivor = 0 if payload == 8 else 8
    encoding = "PCMU" if survivor == 0 else "PCMA"
    offer = description([
        f"a=rtpmap:{payload} {mapping}", f"a=rtpmap:{survivor} {encoding}/8000",
    ], payloads=f"{payload} {survivor}")
    formats = sdp.offered_pcm_formats(offer)
    assert [fmt.payload_type for fmt in formats] == [survivor]
    answer = description([f"a=rtpmap:{survivor} {encoding}/8000"], payloads=str(survivor))
    sdp.validate_sdp_answer(offer, answer)
    sdp.validate_sdp_answer(answer, offer)


def test_invalid_only_mapping_cannot_resurrect_static_payload_defaults():
    offer = description(["a=rtpmap:8 PCMA/bad"], payloads="8")
    assert sdp.offered_pcm_formats(offer) == []
    local = [audio_format.AudioFormat(8000, frame_ms=20)]
    assert sdp.negotiate_directional(offer, local, local) is None


def test_rtpmap_without_a_payload_identity_cannot_invalidate_other_payloads():
    offer = description(["a=rtpmap:unknown L16/bad", "a=rtpmap:8 PCMA/8000"], payloads="8")
    assert [fmt.payload_type for fmt in sdp.offered_pcm_formats(offer)] == [8]


def test_capability_zero_packet_time_and_structural_media_errors_keep_their_contract():
    capability = description(["a=rtpmap:8 PCMA/8000", "a=ptime:0"], payloads="8")
    assert sdp.parse_sdp(capability, capabilities=True)["ptime"] == 0
    with pytest.raises(sdp.SdpError):
        sdp.parse_sdp(capability.replace("m=audio 40000", "m=audio bad"))
