"""Independent explicit RTP offer alternatives, payload maps and receiver ptime."""

from dataclasses import replace
import re

import pytest

from .voip_phase1_support import audio_format, sdp


WIDE = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10)
NARROW = sdp.RtpPcmFormat(97, "L16", 16000, 1, 10)
LOCAL = list(audio_format.HA_SIP_PCM_TX_FORMATS)


def offer(*, send=(WIDE,), recv=(NARROW,), common=True, direction="sendrecv",
          local_send=None, local_recv=None, all_formats=None, independent=True):
    explicit = ({"audio_rtp_formats": all_formats} if all_formats is not None else
                {"send_rtp_formats": send, "recv_rtp_formats": recv})
    return sdp.build_offer_directional(
        "192.0.2.1", "192.0.2.1", 40000,
        list(local_send if local_send is not None else (WIDE.audio_format, *LOCAL)),
        list(local_recv if local_recv is not None else (NARROW.audio_format, *LOCAL)),
        include_common_codecs=common, allow_directional_payloads=True,
        independent_audio_ptime=independent,
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


def test_explicit_offer_uses_receiver_ptime_without_downgrading_send_preference():
    narrow20 = replace(NARROW, frame_ms=20)
    body = offer(send=(WIDE, narrow20, NARROW), recv=(narrow20, NARROW), common=False,
                 local_send=[WIDE.audio_format, narrow20.audio_format, NARROW.audio_format],
                 local_recv=[narrow20.audio_format, NARROW.audio_format])
    assert list(wire_maps(body).values())[:2] == ["L16/48000/1", "L16/16000/1"]
    assert "a=ptime:20\r\n" in body
    assert "a=x-voip-stack-flow:96 send\r\n" in body
    assert "a=x-voip-stack-flow:97 sendrecv\r\n" in body


def test_directional_answer_keeps_each_receivers_packet_time():
    narrow20 = replace(NARROW, frame_ms=20)
    # Independent wire offer: HA receives16k/20 and sends48k/10. Only the
    # receiver's quantum is serialized in the single media-level ptime.
    local_offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=HA\r\n"
        "c=IN IP4 192.0.2.1\r\nt=0 0\r\nm=audio 40000 RTP/AVP 96 97\r\n"
        "a=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\n"
        "a=x-voip-stack-flow:96 send\r\na=x-voip-stack-flow:97 recv\r\n"
        "a=x-voip-stack-independent-ptime:1\r\n"
        "a=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n")
    peer_answer = (local_offer.replace("192.0.2.1", "192.0.2.2")
        .replace("m=audio 40000", "m=audio 41000")
        .replace("flow:96 send", "flow:96 recv")
        .replace("flow:97 recv", "flow:97 send")
        .replace("ptime:20", "ptime:10"))
    selected = sdp.negotiate_answer_directional(peer_answer,
        [WIDE.audio_format, narrow20.audio_format, NARROW.audio_format],
        [narrow20.audio_format, NARROW.audio_format], local_offer_sdp=local_offer)
    assert selected is not None
    assert (selected.send.sample_rate, selected.send.frame_ms) == (48000, 10)
    assert (selected.recv.sample_rate, selected.recv.frame_ms) == (16000, 20)
    assert selected.send.max_frame_ms == 10
    assert selected.recv.max_frame_ms == 20


def test_remote_directional_offer_does_not_retime_our_receiver():
    peer_offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Independent phone\r\n"
        "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 41000 RTP/AVP 96 97\r\n"
        "a=rtpmap:96 L16/16000/1\r\na=rtpmap:97 L16/48000/1\r\n"
        "a=x-voip-stack-flow:96 send\r\na=x-voip-stack-flow:97 recv\r\n"
        "a=x-voip-stack-independent-ptime:1\r\n"
        "a=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\n"
        "m=video 42000 RTP/AVP 102\r\na=rtpmap:102 H264/90000\r\n")
    selected = sdp.negotiate_directional(peer_offer,
        [WIDE.audio_format], [replace(NARROW, frame_ms=20).audio_format, NARROW.audio_format])
    assert selected is not None
    assert (selected.send.sample_rate, selected.send.frame_ms) == (48000, 10)
    assert (selected.recv.sample_rate, selected.recv.frame_ms) == (16000, 20)
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 40000,
        selected.send, selected.recv, remote_sdp=peer_offer)
    assert "a=ptime:20\r\n" in answer
    assert "m=video 0 RTP/AVP 102\r\n" in answer


def test_directional_offer_allows_disjoint_packet_times():
    body = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 40000,
        [WIDE.audio_format], [replace(NARROW, frame_ms=20).audio_format],
        allow_directional_payloads=True, independent_audio_ptime=True)
    assert "a=ptime:20\r\n" in body
    assert list(wire_maps(body).values())[:2] == ["L16/48000/1", "L16/16000/1"]


@pytest.mark.parametrize("larger_direction", ["send", "recv"])
def test_directional_offer_maxptime_cannot_inherit_transmit_capacity(larger_direction):
    narrow20 = replace(NARROW, frame_ms=20)
    narrow32 = replace(NARROW, frame_ms=32)
    send = (WIDE, narrow32) if larger_direction == "send" else (WIDE,)
    recv = (narrow20,) if larger_direction == "send" else (narrow20, narrow32)
    body = offer(send=send, recv=recv, common=False,
                 local_send=[fmt.audio_format for fmt in send],
                 local_recv=[fmt.audio_format for fmt in recv])
    assert "a=ptime:20\r\n" in body
    expected = 20 if larger_direction == "send" else 32
    assert f"a=maxptime:{expected}\r\n" in body


@pytest.mark.parametrize("peer_ptime,receive_ptime", [(20, 10), (10, 20)])
def test_ordinary_g722_has_one_codec_and_independent_receiver_packet_times(peer_ptime, receive_ptime):
    peer_offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Ordinary G722\r\n"
        "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 41000 RTP/AVP 9\r\n"
        f"a=rtpmap:9 G722/8000/1\r\na=ptime:{peer_ptime}\r\na=sendrecv\r\n")
    selected = sdp.negotiate_directional(peer_offer,
        [audio_format.AudioFormat(16000, frame_ms=ms) for ms in (20, 10)],
        [audio_format.AudioFormat(16000, frame_ms=ms) for ms in (receive_ptime, peer_ptime)])
    assert selected is not None
    assert (selected.send.encoding, selected.send.sample_rate, selected.send.frame_ms) == ("G722", 8000, peer_ptime)
    assert (selected.recv.encoding, selected.recv.sample_rate, selected.recv.frame_ms) == ("G722", 8000, receive_ptime)
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 40000,
        selected.send, selected.recv, remote_sdp=peer_offer)
    assert answer.count("a=rtpmap:9 G722/8000/1") == 1
    assert f"a=ptime:{receive_ptime}\r\n" in answer


def test_ordinary_g722_answer_keeps_explicit_receive_preference(monkeypatch):
    monkeypatch.setattr(sdp, "common_sip_codecs", lambda: frozenset({"G722"}))
    send = [audio_format.AudioFormat(16000, frame_ms=10)]
    receive = [audio_format.AudioFormat(16000, frame_ms=ms) for ms in (20, 10)]
    local_offer = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 40000,
        send, receive, include_common_codecs=True)
    assert "a=ptime:20\r\n" in local_offer
    answer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Ordinary G722\r\n"
        "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 41000 RTP/AVP 9\r\n"
        "a=rtpmap:9 G722/8000/1\r\na=ptime:10\r\na=sendrecv\r\n")
    selected = sdp.negotiate_answer_directional(answer, send, receive, local_offer_sdp=local_offer)
    assert selected is not None
    assert (selected.send.frame_ms, selected.recv.frame_ms) == (10, 20)
    assert selected.send.sample_rate == selected.recv.sample_rate == 8000


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


# Captured FULL13b offer, with only its private address anonymized. Codec/PT,
# flow, timing, media order and video constraints remain the observed values.
LEGACY_13B_OFFER = 'v=0\r\no=- 2362955984 0 IN IP4 192.0.2.2\r\ns=VoIP Stack\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 40000 RTP/AVP 96 97 120 122 124 126\r\na=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\na=rtpmap:120 telephone-event/8000\r\na=fmtp:120 0-15\r\na=rtpmap:122 telephone-event/16000\r\na=fmtp:122 0-15\r\na=rtpmap:124 telephone-event/32000\r\na=fmtp:124 0-15\r\na=rtpmap:126 telephone-event/48000\r\na=fmtp:126 0-15\r\na=x-voip-stack-flow:96 recv\r\na=x-voip-stack-flow:97 sendrecv\r\na=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\nm=video 40002 RTP/AVP 26\r\nc=IN IP4 192.0.2.2\r\na=rtcp:40003 IN IP4 192.0.2.2\r\na=rtpmap:26 JPEG/90000\r\na=imageattr:26 recv [x=[8:8:320],y=[8:8:240],sar=1.0,q=1.0]\r\na=framerate:10\r\na=recvonly\r\n'


@pytest.mark.parametrize("independent", [False, True])
def test_real13b_directional_offer_gates_independent_packet_time(independent):
    peer = LEGACY_13B_OFFER
    if independent:
        peer = peer.replace("a=ptime:10", "a=x-voip-stack-independent-ptime:1\r\na=ptime:10", 1)
    selected = sdp.negotiate_directional(peer,
        list(audio_format.HA_SIP_PCM_TX_FORMATS), list(audio_format.HA_SIP_PCM_RX_FORMATS))
    assert selected is not None
    assert (selected.send.sample_rate, selected.send.frame_ms) == (48000, 10)
    # With independent timing HA now prefers RX16/20. The separate legacy
    # branch still negotiates the captured peer's ordinary 10 ms packet time.
    assert (selected.recv.sample_rate, selected.recv.frame_ms) == (16000, 20 if independent else 10)
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 40000,
        selected.send, selected.recv, remote_sdp=peer)
    assert "a=x-voip-stack-flow:96 send\r\n" in answer
    assert "a=x-voip-stack-flow:97 recv\r\n" in answer
    assert f"a=ptime:{20 if independent else 10}\r\n" in answer


@pytest.mark.parametrize("independent", [False, True])
def test_directional_outbound_capability_selects_legacy_or_independent_quantum(independent):
    narrow20 = replace(NARROW, frame_ms=20)
    body = offer(send=(WIDE, narrow20, NARROW), recv=(narrow20, NARROW), common=False,
        local_send=[WIDE.audio_format, narrow20.audio_format, NARROW.audio_format],
        local_recv=[narrow20.audio_format, NARROW.audio_format], independent=independent)
    assert list(wire_maps(body).values())[:2] == ["L16/48000/1", "L16/16000/1"]
    assert f"a=ptime:{20 if independent else 10}\r\n" in body
    assert "a=x-voip-stack-independent-ptime:1\r\n" in body


@pytest.mark.parametrize("placement", ["audio", "session", "video", "invalid-value"])
def test_independent_packet_time_capability_is_scoped_to_audio(placement):
    marker = "a=x-voip-stack-independent-ptime:1\r\n"
    body = LEGACY_13B_OFFER
    if placement == "audio":
        body = body.replace("a=ptime:10", marker + "a=ptime:10", 1)
    elif placement == "session":
        body = body.replace("m=audio", marker + "m=audio", 1)
    elif placement == "video":
        body += marker
    else:
        body = body.replace("a=ptime:10", "a=x-voip-stack-independent-ptime:2\r\na=ptime:10", 1)
    assert sdp.supports_independent_audio_ptime(body) == (placement == "audio")
    assert ("independent_audio_ptime_v1" in sdp.options_capabilities(body)["sdp_features"]) == (placement == "audio")


def test_new_options_without_directional_lists_never_invents_transmit_quantum():
    body = LEGACY_13B_OFFER.replace("a=ptime:10",
        "a=x-voip-stack-independent-ptime:1\r\na=ptime:10", 1)
    capabilities = sdp.options_capabilities(body)
    assert capabilities["sdp_features"] == ["directional_audio_v1", "independent_audio_ptime_v1"]
    assert capabilities["sip_audio_tx_formats"] == []
    assert capabilities["sip_audio_rx_formats"] == ["L16/48000/1/10", "L16/16000/1/10"]


@pytest.mark.parametrize("invalid", ["duplicate", "too-many", "wrong-encoding", "wrong-direction"])
def test_new_options_rejects_invalid_directional_packet_lists(invalid):
    body = LEGACY_13B_OFFER.replace("a=ptime:10",
        "a=x-voip-stack-independent-ptime:1\r\na=ptime:10", 1)
    attribute = "a=x-voip-stack-audio-tx:"
    if invalid == "duplicate":
        values = attribute + "L16/16000/1/20\r\n" + attribute + "L16/16000/1/10\r\n"
    elif invalid == "too-many":
        values = attribute + ",".join(["L16/16000/1/10"] * 9) + "\r\n"
    elif invalid == "wrong-encoding":
        values = attribute + "G722/8000/1/20\r\n"
    else:
        values = attribute + "L16/48000/1/10\r\n"
    body = body.replace("a=ptime:10", values + "a=ptime:10", 1)
    with pytest.raises(sdp.SdpError):
        sdp.options_capabilities(body)
