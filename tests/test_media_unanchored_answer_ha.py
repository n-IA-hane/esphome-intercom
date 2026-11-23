"""The public answer API without an offer uses the same leg owner and local RX facts."""

from unittest.mock import Mock

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.media import leg, negotiator

pytestmark = pytest.mark.ha


def description(lines):
    return "\r\n".join(("v=0", "o=- 1 1 IN IP4 192.0.2.2", "s=Answer",
        "c=IN IP4 192.0.2.2", "t=0 0", *lines.splitlines(), ""))


def test_unanchored_opus_answer_keeps_remote_tx_limits_out_of_the_local_receiver():
    remote_parameters = "stereo=0;maxplaybackrate=16000;maxaveragebitrate=12000"
    answer = description("m=audio 5000 RTP/AVP 110\na=rtpmap:110 OPUS/48000/2\n"
        f"a=fmtp:110 {remote_parameters}\na=ptime:10\na=maxptime:10\na=sendrecv")
    selected = sdp.negotiate_answer_directional(answer,
        [AudioFormat(48000, frame_ms=10)], [AudioFormat(48000, frame_ms=20)],
        local_capabilities=HA_AUDIO_CAPABILITIES)
    assert selected is not None
    assert (selected.send.payload_type, selected.send.frame_ms,
            selected.send.max_frame_ms, selected.send.fmtp) == (110, 10, 10, remote_parameters)
    assert (selected.recv.payload_type, selected.recv.frame_ms, selected.recv.min_frame_ms,
            selected.recv.max_frame_ms, selected.recv.fmtp) == (
                110, 20, 0, 120, "stereo=0;sprop-stereo=0;maxaveragebitrate=28000")
    assert selected.recv_formats == (selected.recv,)
    assert selected.send_formats == ()


def test_unanchored_answer_preserves_answer_order_and_one_common_mapping():
    answer = description("m=audio 5000 RTP/AVP 0 96\na=rtpmap:96 L16/16000/1\n"
        "a=ptime:10\na=sendrecv")
    preferred = [AudioFormat(16000, frame_ms=20), AudioFormat(8000, frame_ms=20)]
    selected = sdp.negotiate_answer_directional(answer, preferred, preferred,
        local_offer_sdp=None, local_capabilities=HA_AUDIO_CAPABILITIES)
    assert selected is not None
    # The same public engine honors the supplied local preference, while all
    # valid mappings in the peer description remain receivable.
    assert (selected.send.payload_type, selected.send.encoding, selected.send.frame_ms) == (96, "L16", 10)
    assert (selected.recv.payload_type, selected.recv.encoding, selected.recv.frame_ms) == (96, "L16", 20)
    assert {(fmt.payload_type, fmt.encoding) for fmt in selected.recv_formats} == {(0, "PCMU"), (96, "L16")}
    assert selected.send_formats == ()


@pytest.mark.parametrize("dynamic", [False, True])
def test_unanchored_receiver_keeps_the_callers_first_producible_local_quantum(dynamic):
    answer = description("m=audio 5000 RTP/AVP 0\na=ptime:10\na=sendrecv")
    selected = sdp.negotiate_answer_directional(answer, [AudioFormat(8000, frame_ms=10)],
        [AudioFormat(8000, frame_ms=32), AudioFormat(8000, frame_ms=20)],
        local_capabilities=HA_AUDIO_CAPABILITIES if dynamic else None)
    assert selected is not None
    assert (selected.send.frame_ms, selected.recv.frame_ms) == (10, 32)


def test_unanchored_receive_only_answer_keeps_local_receive_preference_order():
    answer = description("m=audio 5000 RTP/AVP 96 97\na=rtpmap:96 L16/8000/1\n"
        "a=rtpmap:97 L16/16000/1\na=ptime:10\na=sendonly")
    preferred = [AudioFormat(16000, frame_ms=20), AudioFormat(8000, frame_ms=20)]
    selected = sdp.negotiate_answer_directional(answer, [], preferred)
    assert selected is not None
    assert (selected.recv.payload_type, selected.recv.sample_rate, selected.recv.frame_ms) == (97, 16000, 20)
    assert selected.send == selected.recv
    assert selected.recv_formats == (selected.recv,)
    assert selected.send_formats == ()


@pytest.mark.parametrize("receive_ms,direction", [(80, "sendrecv"), (120, "sendonly")])
def test_unanchored_opus_receiver_keeps_its_dynamic_receive_quantum(receive_ms, direction):
    answer = description("m=audio 5000 RTP/AVP 110\na=rtpmap:110 OPUS/48000/2\n"
        f"a=ptime:10\na={direction}")
    selected = sdp.negotiate_answer_directional(answer, [AudioFormat(48000, frame_ms=10)],
        [AudioFormat(48000, frame_ms=receive_ms)], local_capabilities=HA_AUDIO_CAPABILITIES)
    assert selected is not None
    assert (selected.recv.frame_ms, selected.recv.max_frame_ms) == (receive_ms, 120)


def test_unanchored_receive_preference_distinguishes_pcm_width():
    answer = description("m=audio 5000 RTP/AVP 96 97\na=rtpmap:96 L16/16000/1\n"
        "a=rtpmap:97 L24/16000/1\na=ptime:20\na=sendonly")
    selected = sdp.negotiate_answer_directional(answer, [],
        [AudioFormat(16000, "s24le", 1, 20), AudioFormat(16000, "s16le", 1, 20)])
    assert selected is not None
    assert (selected.recv.payload_type, selected.recv.encoding, selected.recv.frame_ms) == (97, "L24", 20)


def test_unanchored_padded24_pcm_keeps_the_existing_l24_wire_mapping():
    answer = description("m=audio 5000 RTP/AVP 110\na=rtpmap:110 L24/16000/1\na=ptime:20\na=sendrecv")
    preferred = [AudioFormat(16000, "s24le_in_s32", 1, 20)]
    selected = sdp.negotiate_answer_directional(answer, preferred, preferred)
    assert selected is not None
    assert (selected.send.payload_type, selected.send.encoding, selected.send.frame_ms) == (110, "L24", 20)
    assert selected.recv == selected.send


def test_unanchored_receiver_without_capabilities_keeps_its_mappable_linear_presets():
    answer = description("m=audio 5000 RTP/AVP 110\na=rtpmap:110 L16/48000/1\na=ptime:10\na=sendrecv")
    preferred = [AudioFormat(48000, frame_ms=20), AudioFormat(48000, frame_ms=10)]
    selected = sdp.negotiate_answer_directional(answer, preferred, preferred)
    assert selected is not None
    assert (selected.send.frame_ms, selected.recv.frame_ms) == (10, 10)


@pytest.mark.parametrize("direction,expected_rate", [("sendonly", 48000), ("recvonly", 16000)])
def test_unanchored_inactive_answer_preserves_the_callers_capability_direction(direction, expected_rate):
    answer = description("m=audio 5000 RTP/AVP 96 97\na=rtpmap:96 L16/48000/1\n"
        "a=rtpmap:97 L16/16000/1\na=ptime:10\na=inactive")
    selected = sdp.negotiate_answer_directional(answer, [AudioFormat(48000, frame_ms=10)],
        [AudioFormat(16000, frame_ms=10)], local_offer_direction=direction)
    assert selected is not None
    assert selected.send.sample_rate == selected.recv.sample_rate == expected_rate
    assert selected.send_formats == selected.recv_formats == ()


def test_unanchored_answer_passes_missing_offer_to_the_leg_without_serializing_a_replacement(monkeypatch):
    answer = description("m=audio 5000 RTP/AVP 0\na=ptime:10\na=sendrecv")
    engine = Mock(wraps=leg.apply_answer)
    monkeypatch.setattr(leg, "apply_answer", engine)
    preferred = [AudioFormat(8000, frame_ms=20)]
    selected = sdp.negotiate_answer_directional(answer, preferred, preferred,
        local_capabilities=HA_AUDIO_CAPABILITIES)
    assert selected is not None
    engine.assert_called_once()
    assert engine.call_args.args[0] is None
    assert sdp.parse_sdp(engine.call_args.args[1])["payload_order"] == [0]


def test_public_leg_api_accepts_a_missing_offer_without_creating_offer_payloads():
    answer = description("m=audio 5000 RTP/AVP 0\na=ptime:10\na=sendrecv")
    preferred = [AudioFormat(8000, frame_ms=20)]
    result = negotiator.apply_answer(None, answer, preferred, preferred,
        capabilities=HA_AUDIO_CAPABILITIES)
    assert isinstance(result, leg.LegContract)
    assert (result.send.payload_type, result.send.frame_ms, result.receive.payload_type,
            result.receive.frame_ms) == (0, 10, 0, 20)
    assert result.receive_accept == (result.receive,)
    assert result.send_accept == ()
