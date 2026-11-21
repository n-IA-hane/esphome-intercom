"""One-way answers require only the local direction that will carry RTP."""

import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media import shadow
from custom_components.voip_stack.core.rtp_payload import RtpPayloadEncoder
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver

pytestmark = pytest.mark.ha


def description(rate, packet_ms, direction):
    return ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=One way peer\r\n"
            "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 40000 RTP/AVP 110\r\n"
            f"a=rtpmap:110 L16/{rate}/1\r\na=ptime:{packet_ms}\r\na={direction}\r\n")


@pytest.fixture(autouse=True)
def active_engine():
    previous = shadow.engine_mode()
    shadow.set_engine_mode("active")
    try:
        yield
    finally:
        shadow.set_engine_mode(previous)


@pytest.mark.parametrize("local_direction,remote_direction,rate,packet_ms", [
    ("sendonly", "recvonly", 16000, 20),
    ("recvonly", "sendonly", 48000, 10),
])
def test_public_answer_api_and_rtp_use_only_the_negotiated_direction(
        monkeypatch, local_direction, remote_direction, rate, packet_ms):
    send = [AudioFormat(16000, "s16le", 1, 20)]
    receive = [AudioFormat(48000, "s16le", 1, 10)]
    # The unused opposite direction deliberately has no matching geometry/time.
    local_offer = description(rate, packet_ms, local_direction)
    remote_answer = description(rate, packet_ms, remote_direction)
    allocated = []
    original = RtpPayloadEncoder.__init__
    def record_encoder(self, *args, **kwargs):
        allocated.append(args[0])
        original(self, *args, **kwargs)
    monkeypatch.setattr(RtpPayloadEncoder, "__init__", record_encoder)
    selected = sdp.negotiate_answer_directional(remote_answer, send, receive,
        local_offer_sdp=local_offer, codec_policy=CodecPolicy(audio=("L16",)))
    assert selected is not None
    assert bool(selected.send_formats) is (local_direction == "sendonly")
    assert bool(selected.recv_formats) is (local_direction == "recvonly")
    assert allocated == []
    count = rate * packet_ms // 1000
    expected = struct.pack(f"<{count}h", *([1234] * count))
    if local_direction == "sendonly":
        assert selected.send.audio_format == send[0]
        payload = RtpPayloadEncoder(selected.send).encode(expected)
        assert len(allocated) == 1
    else:
        assert selected.recv.audio_format == receive[0]
        # Independent network-order PCM, no local encoder or encoder factory.
        payload = struct.pack(f"!{count}h", *([1234] * count))
    packet = rtp.parse_packet(rtp.build_packet(rtp.RtpPacket(110, 1, count, 77, payload)))
    assert len(packet.payload) == count * 2
    receiver = RtpAudioReceiver(selected.send if local_direction == "sendonly" else selected.recv,
                                selected.recv_formats)
    assert b"".join(receiver.decode(packet.payload_type, packet.payload)) == expected
    assert len(allocated) == (1 if local_direction == "sendonly" else 0)
