"""Shared SDP descriptions reuse interpretation without sharing mutable results."""

import importlib

import pytest

from .voip_phase1_support import sdp


def body(port=40000, clock=8000):
    return ("v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=Facts\r\n"
            "c=IN IP4 192.0.2.1\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP 8 101\r\na=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/{clock}\r\na=fmtp:101 0-15\r\na=ptime:20\r\n")


def test_repeated_consumers_do_not_reinterpret_the_same_audio_description(monkeypatch):
    parser = importlib.import_module(sdp.parse_sdp.__module__)
    original = parser.parse_connection_line
    connections = []

    def observe(line):
        connections.append(line)
        return original(line)

    monkeypatch.setattr(parser, "parse_connection_line", observe)
    document = sdp.SdpDocument.parse(body())
    assert sdp.parse_sdp(document)["media_port"] == 40000
    assert sdp.offered_dtmf_formats(document)[0].sample_rate == 8000
    assert sdp.offered_pcm_formats(document)[0].encoding == "PCMA"
    assert sdp.parse_sdp(document)["direction"] == "sendrecv"
    assert len(connections) == 1
    # A changed description owns new facts instead of reusing session state.
    assert sdp.parse_sdp(sdp.SdpDocument.parse(body(port=41000)))["media_port"] == 41000
    assert len(connections) == 2


def test_mutable_parser_views_do_not_poison_the_shared_document():
    document = sdp.SdpDocument.parse(body())
    expected = sdp.parse_sdp(document)
    changed = sdp.parse_sdp(document)
    changed["media_port"] = 0
    changed["payload_order"].clear()
    changed["rtpmap"][101] = ("TELEPHONE-EVENT", 16000, 1)
    changed["fmtp"][101] = "4-7"
    assert sdp.parse_sdp(document) == expected
    assert sdp.offered_dtmf_formats(document)[0].events == frozenset(range(16))


@pytest.mark.parametrize("capability_first", [False, True])
def test_capability_and_offer_answer_interpretations_remain_distinct(capability_first):
    document = sdp.SdpDocument.parse(body(port=0))
    if capability_first:
        assert sdp.parse_sdp(document, capabilities=True)["media_port"] == 0
    with pytest.raises(sdp.SdpError):
        sdp.parse_sdp(document)
    assert sdp.parse_sdp(document, capabilities=True)["media_port"] == 0
    with pytest.raises(sdp.SdpError):
        sdp.parse_sdp(document)


def test_new_documents_keep_strict_decode_and_do_not_reuse_old_media_facts():
    old = sdp.SdpDocument.parse(body())
    assert sdp.offered_dtmf_formats(old)[0].sample_rate == 8000
    new = sdp.SdpDocument.parse(body(clock=16000).encode())
    assert sdp.offered_dtmf_formats(new)[0].sample_rate == 16000
    assert sdp.offered_dtmf_formats(old)[0].sample_rate == 8000
    with pytest.raises(UnicodeDecodeError):
        sdp.SdpDocument.parse(b"v=0\r\n\xff")
