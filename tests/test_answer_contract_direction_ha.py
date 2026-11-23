"""The real answer writer and contract gate agree for one-way and held audio."""

from dataclasses import replace

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_format import AudioFormat, PcmFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media import negotiator

pytestmark = pytest.mark.ha

SEND = tuple(AudioFormat(8000, PcmFormat.S16LE, 1, ms) for ms in (10, 20))
RECEIVE = tuple(AudioFormat(8000, PcmFormat.S16LE, 1, ms) for ms in (10, 20, 40))
POLICY = CodecPolicy(audio=("PCMU", "PCMA"))


def remote_offer(direction, *, maximum=10):
    """Independent peer bytes retain two ordinary G.711 mappings."""
    return (
        "v=0\r\no=- 9 2 IN IP4 192.0.2.2\r\ns=Direction contract witness\r\n"
        "c=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 32000 RTP/AVP 0 8\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n"
        f"a=ptime:10\r\na=maxptime:{maximum}\r\na={direction}\r\n"
    )


def contract(offer, *, established=None):
    selected = negotiator.answer_offer(offer, SEND, RECEIVE,
        codec_policy=POLICY, established=established)
    assert not isinstance(selected, negotiator.Refusal), selected
    # This is the production engine's public projection, including its inactive
    # empty directional lists. It is not a reconstruction of its selection.
    audio = negotiator.as_direction(selected, offer)
    assert audio is not None
    return selected, audio


def answer(offer, audio, *, direction=None):
    return sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 40000,
        audio.send, audio.recv, send_formats=audio.send_formats,
        recv_formats=audio.recv_formats, dtmf_direction=audio.dtmf_direction,
        remote_sdp=offer, audio_direction=direction)


@pytest.mark.parametrize("established", [False, True], ids=["initial", "reinvite"])
def test_recvonly_multicodec_answer_serializes_every_committed_send_mapping(established):
    previous = contract(remote_offer("sendrecv", maximum=40))[0] if established else None
    offer = remote_offer("recvonly")
    _selected, audio = contract(offer, established=previous)
    assert {fmt.payload_type for fmt in audio.send_formats} == {0, 8}
    written = answer(offer, audio)
    assert sdp.parse_sdp(written)["direction"] == "sendonly"
    assert set(sdp.parse_sdp(written)["payload_order"]) == {0, 8}
    sdp.validate_sdp_answer(offer, written)
    negotiator.validate_answer_contract(audio, written, remote_sdp=offer)
    # A serializer still omitting the newly supplied send catalog remains a
    # hard contract failure, even though the reduced answer is valid SIP SDP.
    reduced = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 40000,
        audio.send, audio.recv, recv_formats=audio.recv_formats, remote_sdp=offer)
    with pytest.raises(sdp.SdpError):
        negotiator.validate_answer_contract(audio, reduced, remote_sdp=offer)


@pytest.mark.parametrize("remote_direction", ["sendrecv", "sendonly", "recvonly", "inactive"])
def test_inactive_answer_uses_offer_direction_and_only_the_local_receive_envelope(remote_direction):
    established = contract(remote_offer("sendrecv", maximum=40))[0]
    offer = remote_offer(remote_direction)
    _selected, audio = contract(offer, established=established)
    assert audio.send.max_frame_ms == 10
    assert audio.recv.max_frame_ms == 40
    written = answer(offer, audio, direction="inactive")
    parsed = sdp.parse_sdp(written)
    assert parsed["direction"] == "inactive"
    sdp.validate_sdp_answer(offer, written)
    negotiator.validate_answer_contract(audio, written, remote_sdp=offer)
    if remote_direction != "recvonly":
        assert parsed["ptime"] == audio.recv.frame_ms
        assert parsed["maxptime"] == 40
        with pytest.raises(sdp.SdpError, match="receiver envelope"):
            negotiator.validate_answer_contract(audio,
                written.replace("a=maxptime:40", "a=maxptime:10"), remote_sdp=offer)


def test_answer_contract_gate_still_rejects_payload_and_local_receiver_parameter_drift():
    offer = remote_offer("sendrecv")
    _selected, audio = contract(offer)
    written = answer(offer, audio)
    negotiator.validate_answer_contract(audio, written, remote_sdp=offer)
    bad_mapping = written.replace("PCMA/8000", "PCMA/16000")
    with pytest.raises(sdp.SdpError):
        negotiator.validate_answer_contract(audio, bad_mapping, remote_sdp=offer)
    with pytest.raises(sdp.SdpError, match="packet time"):
        negotiator.validate_answer_contract(audio,
            written.replace(f"a=ptime:{audio.recv.frame_ms}", "a=ptime:10"), remote_sdp=offer)
    with pytest.raises(sdp.SdpError):
        negotiator.validate_answer_contract(replace(audio,
            recv_formats=tuple(fmt for fmt in audio.recv_formats if fmt.payload_type != 8)),
            written, remote_sdp=offer)
