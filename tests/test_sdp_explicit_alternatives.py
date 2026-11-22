"""Standard sendrecv alternatives support independent transmit and receive choices."""

from dataclasses import replace
import re

import pytest

from .voip_phase1_support import audio_format, sdp
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.codec_policy import CodecPolicy

WIDE = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10, max_frame_ms=20)
NARROW = sdp.RtpPcmFormat(97, "L16", 16000, 1, 20, max_frame_ms=20)
LOCAL = [WIDE.audio_format, replace(WIDE, frame_ms=20).audio_format,
         NARROW.audio_format, replace(NARROW, frame_ms=10).audio_format]


def wire_maps(body):
    rows = re.findall(r"^a=rtpmap:(\d+) ([^\r\n]+)", body, re.MULTILINE)
    payloads = [int(value) for value in re.search(r"^m=audio \d+ RTP/AVP ([^\r\n]+)", body, re.MULTILINE)[1].split()]
    assert len({int(pt) for pt, _ in rows}) == len(rows)
    assert len(set(payloads)) == len(payloads)
    assert set(payloads) == {int(pt) for pt, _ in rows}
    return {int(pt): encoding for pt, encoding in rows}


def offer(*, send=(WIDE, NARROW), receive=(WIDE, NARROW), direction="sendrecv", common=False):
    return sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 40000, LOCAL, LOCAL,
        send_rtp_formats=send, recv_rtp_formats=receive, include_common_codecs=common,
        audio_direction=direction, local_capabilities=HA_AUDIO_CAPABILITIES,
        codec_policy=CodecPolicy(audio=("L16", "PCMA", "PCMU")))


def test_explicit_numbers_and_standard_receiver_bounds_are_preserved():
    body = offer()
    maps = wire_maps(body)
    assert list(maps.items())[:2] == [(96, "L16/48000/1"), (97, "L16/16000/1")]
    assert "a=ptime:10\r\na=maxptime:20\r\n" in body
    assert "a=x-" not in body
    assert "b=RS:0\r\nb=RR:0\r\n" in body


def test_sendrecv_never_advertises_a_mapping_missing_from_either_direction():
    body = offer(receive=(NARROW,))
    assert "L16/48000/1" not in wire_maps(body).values()
    assert "L16/16000/1" in wire_maps(body).values()


@pytest.mark.parametrize("direction,primary", [("sendonly", WIDE), ("recvonly", NARROW)])
def test_one_way_stream_uses_only_its_active_capability(direction, primary):
    body = offer(send=(WIDE,), receive=(NARROW,), direction=direction)
    primary_maps = {pt: value for pt, value in wire_maps(body).items() if not value.startswith("telephone-event")}
    assert primary_maps == {primary.payload_type: f"L16/{primary.sample_rate}/1"}
    assert f"a={direction}\r\n" in body


def test_multiformat_answer_keeps_native_sender_and_independent_receiver_preferences():
    peer = offer()
    selected = sdp.negotiate_directional(peer,
        [NARROW.audio_format, WIDE.audio_format], [WIDE.audio_format, NARROW.audio_format],
        local_capabilities=HA_AUDIO_CAPABILITIES)
    assert selected is not None
    assert selected.send.sample_rate == 16000
    assert selected.recv.sample_rate == 48000
    assert {fmt.sample_rate for fmt in selected.recv_formats} == {16000, 48000}
    answer = sdp.build_answer_directional("192.0.2.2", "192.0.2.2", 41000,
        selected.send, selected.recv, recv_formats=selected.recv_formats, remote_sdp=peer)
    assert list(wire_maps(answer).values())[:2] == ["L16/48000/1", "L16/16000/1"]
    assert "a=x-" not in answer


def test_each_receive_preference_survives_answer_application():
    local_offer = offer(receive=(NARROW, WIDE))
    peer_answer = offer().replace("40000", "41000")
    selected = sdp.negotiate_answer_directional(peer_answer,
        [WIDE.audio_format, NARROW.audio_format], [NARROW.audio_format, WIDE.audio_format],
        local_offer_sdp=local_offer, local_capabilities=HA_AUDIO_CAPABILITIES)
    assert selected is not None
    assert (selected.send.sample_rate, selected.send.frame_ms) == (48000, 10)
    assert (selected.recv.sample_rate, selected.recv.frame_ms) == (16000, 20)
    assert {fmt.sample_rate for fmt in selected.recv_formats} == {16000, 48000}


def test_common_codecs_add_alternatives_without_reusing_explicit_numbers():
    body = offer(send=(replace(WIDE, payload_type=98), NARROW), common=True)
    maps = wire_maps(body)
    assert maps[98] == "L16/48000/1" and maps[97] == "L16/16000/1"
    assert len(maps) == len(set(maps))
    assert "a=x-" not in body


def test_g722_packet_times_are_independent_without_an_extension():
    peer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=G722\r\nc=IN IP4 192.0.2.2\r\n"
            "t=0 0\r\nm=audio 41000 RTP/AVP 9\r\na=rtpmap:9 G722/8000/1\r\n"
            "a=ptime:10\r\na=maxptime:20\r\na=sendrecv\r\n")
    selected = sdp.negotiate_directional(peer,
        [audio_format.AudioFormat(16000, frame_ms=ms) for ms in (20, 10)],
        [audio_format.AudioFormat(16000, frame_ms=ms) for ms in (20, 10)])
    assert selected is not None
    assert (selected.send.frame_ms, selected.recv.frame_ms) == (10, 20)
    body = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 40000,
        selected.send, selected.recv, remote_sdp=peer)
    assert "a=ptime:20\r\n" in body and "a=x-" not in body
