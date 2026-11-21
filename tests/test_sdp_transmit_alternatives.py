"""Accepted peer receive mappings remain usable by symmetric RTP policies."""

from importlib import import_module

import pytest

from .voip_phase1_support import audio_format, optional_codec_backends

negotiator = import_module("custom_components.voip_stack.core.media.negotiator")


def _description(lines):
    return "\r\n".join(("v=0", "o=- 1 1 IN IP4 192.0.2.2", "s=Call",
        "c=IN IP4 192.0.2.2", "t=0 0", *lines.splitlines(), ""))


def test_answer_keeps_only_common_producible_transmit_alternatives():
    offer = _description("m=audio 40000 RTP/AVP 0 8 96 97 98 110\n"
        "a=rtpmap:96 L16/16000/1\na=rtpmap:97 L16/48000/1\n"
        "a=rtpmap:98 L24/32000/1\na=rtpmap:110 OPUS/48000/2\na=ptime:20")
    answer = _description("m=audio 41000 RTP/AVP 0 120 121 122 124\n"
        "a=rtpmap:120 L16/16000/1\na=rtpmap:121 L16/48000/1\n"
        "a=rtpmap:122 OPUS/48000/2\na=rtpmap:124 L24/32000/1\na=ptime:20\na=maxptime:20")
    local = [audio_format.AudioFormat(rate, frame_ms=20) for rate in (8000, 16000, 32000, 48000)]
    with optional_codec_backends(()):
        result = negotiator.apply_answer(offer, answer, local, local)
    assert isinstance(result, negotiator.LegContract)
    selected = negotiator.as_direction(result, answer)
    # PCMA was only offered; Opus has no backend; L24 has no local sample
    # width; the fixed 48 kHz/20 ms sender exceeds the 1200-octet budget.
    assert [(fmt.payload_type, fmt.encoding, fmt.frame_ms) for fmt in selected.send_formats] == [
        (0, "PCMU", 20), (120, "L16", 20)]
    assert selected.send.payload_type == 0
    assert [(fmt.payload_type, fmt.encoding) for fmt in selected.recv_formats] == [(0, "PCMU"), (96, "L16")]


def test_answer_hard_packet_ceiling_excludes_a_fixed_longer_alternative():
    offer = _description("m=audio 40000 RTP/AVP 0 96\na=rtpmap:96 L16/16000/1\na=ptime:20")
    answer = _description("m=audio 41000 RTP/AVP 0 120\na=rtpmap:120 L16/16000/1\n"
        "a=ptime:20\na=maxptime:10")
    local = [audio_format.AudioFormat(8000, frame_ms=10),
             audio_format.AudioFormat(8000, frame_ms=20),
             audio_format.AudioFormat(16000, frame_ms=20)]
    result = negotiator.apply_answer(offer, answer, local, local)
    assert isinstance(result, negotiator.LegContract)
    assert [(fmt.payload_type, fmt.frame_ms) for fmt in result.send_accept] == [(0, 10)]
    assert result.send.payload_type == 0 and result.receive.frame_ms == 20


def test_answer_flow_attributes_only_expose_the_peers_receive_mapping_for_transmit():
    offer = _description("m=audio 40000 RTP/AVP 0 96\na=rtpmap:96 L16/16000/1\na=ptime:20")
    answer = _description("m=audio 41000 RTP/AVP 0 120\na=rtpmap:120 L16/16000/1\n"
        "a=x-voip-stack-flow:0 recv\na=x-voip-stack-flow:120 send\n"
        "a=x-voip-stack-independent-ptime:1\na=ptime:20")
    local = [audio_format.AudioFormat(rate, frame_ms=20) for rate in (8000, 16000)]
    result = negotiator.apply_answer(offer, answer, local, local)
    assert isinstance(result, negotiator.LegContract)
    assert [(fmt.payload_type, fmt.encoding) for fmt in result.send_accept] == [(0, "PCMU")]
    selected = negotiator.as_direction(result, answer)
    assert selected.send.payload_type == 0 and selected.recv.payload_type == 96


def test_receive_only_answer_exposes_no_transmit_mapping():
    offer = _description("m=audio 40000 RTP/AVP 0\na=ptime:20\na=recvonly")
    answer = _description("m=audio 41000 RTP/AVP 0\na=ptime:20\na=sendonly")
    local = [audio_format.AudioFormat(8000, frame_ms=20)]
    result = negotiator.apply_answer(offer, answer, [], local)
    assert isinstance(result, negotiator.LegContract)
    selected = negotiator.as_direction(result, answer)
    assert not result.send_accept and not selected.send_formats
    assert selected.recv_formats


@pytest.mark.parametrize("flow", ["send", "recv"])
def test_bidirectional_answer_without_one_required_flow_is_refused(flow):
    offer = _description("m=audio 40000 RTP/AVP 0\na=ptime:20")
    answer = _description(f"m=audio 41000 RTP/AVP 0\na=x-voip-stack-flow:0 {flow}\na=ptime:20")
    local = [audio_format.AudioFormat(8000, frame_ms=20)]
    result = negotiator.apply_answer(offer, answer, local, local)
    assert isinstance(result, negotiator.Refusal)


@pytest.mark.parametrize(("direction", "flow", "can_send"), [
    ("recvonly", "recv", True), ("sendonly", "send", False),
])
def test_one_way_answer_with_its_required_flow_remains_usable(direction, flow, can_send):
    offer = _description("m=audio 40000 RTP/AVP 0\na=ptime:20")
    answer = _description(f"m=audio 41000 RTP/AVP 0\na=x-voip-stack-flow:0 {flow}\n"
        f"a=ptime:20\na={direction}")
    local = [audio_format.AudioFormat(8000, frame_ms=20)]
    result = negotiator.apply_answer(offer, answer, local if can_send else [], [] if can_send else local)
    assert isinstance(result, negotiator.LegContract)
    selected = negotiator.as_direction(result, answer)
    assert bool(selected.send_formats) is can_send
    assert bool(selected.recv_formats) is not can_send
