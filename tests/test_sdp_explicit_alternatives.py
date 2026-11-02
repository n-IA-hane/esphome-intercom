"""Independent explicit RTP offer alternatives, payload maps and receiver ptime."""

from dataclasses import replace
import re

import pytest

from .voip_phase1_support import audio_format, sdp


WIDE = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10)
NARROW = sdp.RtpPcmFormat(97, "L16", 16000, 1, 10)
LOCAL = list(audio_format.HA_SIP_PCM_TX_FORMATS)


def offer(*, send=(WIDE,), recv=(NARROW,), common=True, direction="sendrecv",
          local_send=None, local_recv=None, all_formats=None):
    explicit = ({"audio_rtp_formats": all_formats} if all_formats is not None else
                {"send_rtp_formats": send, "recv_rtp_formats": recv})
    return sdp.build_offer_directional(
        "192.0.2.1", "192.0.2.1", 40000,
        list(local_send if local_send is not None else (WIDE.audio_format, *LOCAL)),
        list(local_recv if local_recv is not None else (NARROW.audio_format, *LOCAL)),
        include_common_codecs=common, allow_directional_payloads=True,
        audio_direction=direction, **explicit,
    )


def wire_maps(body):
    """Parse the emitted wire independently of the production SDP parser."""
    rows = re.findall(r"^a=rtpmap:(\d+) ([^\r\n]+)", body, re.MULTILINE)
    assert len({int(pt) for pt, _ in rows}) == len(rows)
    payloads = [int(value) for value in re.search(
        r"^m=audio \d+ RTP/AVP ([^\r\n]+)", body, re.MULTILINE
    )[1].split()]
    assert len(set(payloads)) == len(payloads)
    assert set(payloads) == {int(pt) for pt, _ in rows}
    return {int(pt): encoding for pt, encoding in rows}


def answer_pcmu():
    return ("v=0\r\no=- 2 1 IN IP4 192.0.2.2\r\ns=ordinary phone\r\n"
            "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 41000 RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000/1\r\na=ptime:20\r\na=sendrecv\r\n")


@pytest.mark.parametrize("shared_explicit", [False, True])
def test_common_alternatives_preserve_d1_and_authoritative_receiver_ptime(monkeypatch, shared_explicit):
    monkeypatch.setattr(sdp, "common_sip_codecs", lambda: frozenset())
    body = offer(all_formats=(WIDE, NARROW) if shared_explicit else None)
    maps = wire_maps(body)
    assert list(maps)[:2] == [96, 97]
    assert maps[96] == "L16/48000/1" and maps[97] == "L16/16000/1"
    assert maps[0] == "PCMU/8000/1" and maps[8] == "PCMA/8000/1"
    if not shared_explicit:
        assert "a=x-voip-stack-flow:96 send\r\n" in body
        assert "a=x-voip-stack-flow:97 recv\r\n" in body
    assert "a=x-voip-stack-flow:0 sendrecv\r\n" in body
    assert "a=ptime:10\r\n" in body and "a=maxptime:20\r\n" in body
    selected = sdp.negotiate_answer_directional(
        answer_pcmu(), [WIDE.audio_format, *LOCAL], [NARROW.audio_format, *LOCAL],
        local_offer_sdp=body,
    )
    assert selected is not None
    assert (selected.send.encoding, selected.send.payload_type, selected.send.frame_ms) == ("PCMU", 0, 20)
    assert (selected.recv.encoding, selected.recv.payload_type, selected.recv.frame_ms) == ("PCMU", 0, 10)


def test_d1_answer_keeps_ha_send48_receive16_without_endpoint_identity(monkeypatch):
    monkeypatch.setattr(sdp, "common_sip_codecs", lambda: frozenset())
    body = offer()
    peer = ("v=0\r\no=- 2 1 IN IP4 192.0.2.2\r\ns=ordinary D1 phone\r\n"
            "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 41000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\n"
            "a=x-voip-stack-flow:96 recv\r\na=x-voip-stack-flow:97 send\r\n"
            "a=ptime:10\r\na=sendrecv\r\n")
    selected = sdp.negotiate_answer_directional(
        peer, [WIDE.audio_format, *LOCAL], [NARROW.audio_format, *LOCAL], local_offer_sdp=body,
    )
    assert selected is not None
    assert (selected.send.encoding, selected.send.sample_rate, selected.send.channels, selected.send.frame_ms) == ("L16", 48000, 1, 10)
    assert (selected.recv.encoding, selected.recv.sample_rate, selected.recv.channels, selected.recv.frame_ms) == ("L16", 16000, 1, 10)


@pytest.mark.parametrize("shared_explicit", [False, True])
def test_false_flags_do_not_expand_or_change_explicit_mapping(monkeypatch, shared_explicit):
    def forbidden(*args, **kwargs):
        raise AssertionError("False flags must not enter codec expansion")
    monkeypatch.setattr(sdp, "_common_codec_offer_formats", forbidden)
    body = offer(common=False, all_formats=(WIDE, NARROW) if shared_explicit else None)
    assert wire_maps(body) == {96: "L16/48000/1", 97: "L16/16000/1", 98: "telephone-event/8000"}
    assert "a=ptime:10\r\n" in body and "PCMU" not in body and "OPUS" not in body
    assert sdp.negotiate_answer_directional(answer_pcmu(), LOCAL, LOCAL, local_offer_sdp=body) is None


def test_opus98_collision_moves_only_the_new_alternative(monkeypatch):
    monkeypatch.setattr(sdp, "common_sip_codecs", lambda: frozenset({"OPUS"}))
    wide = replace(WIDE, payload_type=98)
    body = offer(send=(wide,))
    maps = wire_maps(body)
    assert maps[98] == "L16/48000/1" and maps[97] == "L16/16000/1"
    opus = [pt for pt, encoding in maps.items() if encoding == "OPUS/48000/2"]
    assert opus == [96]
    assert "a=x-voip-stack-flow:98 send\r\n" in body
    assert "a=x-voip-stack-flow:96 sendrecv\r\n" in body
    assert list(maps)[:2] == [98, 97]
    assert "a=ptime:10\r\n" in body


@pytest.mark.parametrize("available", [frozenset(), frozenset({"OPUS", "G722"})])
def test_unavailable_or_non_bidirectional_codecs_are_not_advertised(monkeypatch, available):
    monkeypatch.setattr(sdp, "common_sip_codecs", lambda: available)
    # Only16k/10 is common PCM:48k exists only on TX,8k exists nowhere.
    body = offer(local_send=[WIDE.audio_format, NARROW.audio_format], local_recv=[NARROW.audio_format])
    maps = wire_maps(body)
    assert "OPUS/48000/2" not in maps.values()
    assert "PCMU/8000/1" not in maps.values() and "PCMA/8000/1" not in maps.values()
    assert ("G722/8000/1" in maps.values()) == ("G722" in available)


@pytest.mark.parametrize("direction,has_pcmu,explicit_pt", [("sendonly", True, 96), ("recvonly", False, 97)])
def test_one_way_alternatives_use_only_the_active_local_direction(monkeypatch, direction, has_pcmu, explicit_pt):
    monkeypatch.setattr(sdp, "common_sip_codecs", lambda: frozenset())
    pcm8 = audio_format.AudioFormat(8000, audio_format.PcmFormat.S16LE, 1, 20)
    body = offer(direction=direction, local_send=[WIDE.audio_format, pcm8], local_recv=[NARROW.audio_format])
    maps = wire_maps(body)
    assert list(maps)[0] == explicit_pt
    assert ("PCMU/8000/1" in maps.values()) == has_pcmu
    assert "a=x-voip-stack-flow:" not in body
    assert f"a={direction}\r\n" in body


def test_existing_codec_at_another_ptime_retains_its_pt_and_fmtp(monkeypatch):
    monkeypatch.setattr(sdp, "common_sip_codecs", lambda: frozenset({"OPUS"}))
    opus20 = sdp.RtpPcmFormat(110, "OPUS", 48000, 2, 20, fmtp="stereo=0;maxaveragebitrate=24000")
    body = offer(send=(WIDE, opus20), recv=(NARROW, opus20))
    maps = wire_maps(body)
    assert [pt for pt, encoding in maps.items() if encoding == "OPUS/48000/2"] == [110]
    assert "a=fmtp:110 stereo=0;maxaveragebitrate=24000\r\n" in body
    assert maps[96] == "L16/48000/1" and maps[97] == "L16/16000/1"
    assert "a=ptime:10\r\n" in body and "a=maxptime:20\r\n" in body
