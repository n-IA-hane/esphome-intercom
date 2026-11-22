"""One-way answers require only the local direction that will carry RTP."""

import asyncio
import json
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import (
    AudioFormat, HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS, HA_TRUNK_AUDIO_FORMATS,
)
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.rtp_payload import RtpPayloadEncoder
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_client import SipCallClient

pytestmark = pytest.mark.ha


def description(rate, packet_ms, direction):
    return ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=One way peer\r\n"
            "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 40000 RTP/AVP 110\r\n"
            f"a=rtpmap:110 L16/{rate}/1\r\na=ptime:{packet_ms}\r\na={direction}\r\n")




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


@pytest.mark.parametrize("trunk", [False, True])
async def test_initial_delayed_offer_answers_real_udp_200_without_an_established_media_contract(
    hass, socket_enabled, tmp_path, trunk,
):
    requests = []
    acknowledged = asyncio.Queue()
    ended = asyncio.Event()
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Delayed peer\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 42000 RTP/AVP 110\r\n"
        "a=rtpmap:110 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n")

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            request = sip.parse_message(raw)
            requests.append(request)
            if request.method == "ACK":
                acknowledged.put_nowait(request)
                return
            if request.method == "INVITE":
                port = self.transport.get_extra_info("sockname")[1]
                response = sip.build_uas_response(request, 200, "OK", to_tag="delayed",
                    contact_uri=f"sip:delayed@127.0.0.1:{port}", body=offer.encode())
            else:
                response = sip.build_uas_response(request, 200, "OK", to_tag="delayed")
                if request.method == "BYE":
                    ended.set()
            self.transport.sendto(response, address)

    server, _ = await hass.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    port = server.get_extra_info("sockname")[1]
    client = SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0,
        local_rtp_port=41000,
        supported_send_formats=list(HA_TRUNK_AUDIO_FORMATS if trunk else HA_SIP_PCM_TX_FORMATS),
        supported_recv_formats=list(HA_TRUNK_AUDIO_FORMATS if trunk else HA_SIP_PCM_RX_FORMATS),
        codec_policy=CodecPolicy(audio=("L16",)))
    try:
        await client.start()
        assert await client.invite(target="Delayed", remote_host="127.0.0.1",
            remote_sip_port=port, delayed_offer=True, timeout=3) == "in_call"
        ack = await asyncio.wait_for(acknowledged.get(), 3)
        invite = next(request for request in requests if request.method == "INVITE")
        assert not invite.body and not invite.header("Content-Type")
        assert ack.header("Content-Type") == "application/sdp"
        assert ack.header("Call-ID") == invite.header("Call-ID")
        sdp.validate_sdp_answer(offer, ack.body)
        assert client.dialog is not None
        assert client.dialog.send_format.payload_type == client.dialog.recv_format.payload_type == 110
        assert client.dialog.remote_rtp_port == 42000
        assert (client.dialog.send_format.sample_rate, client.dialog.send_format.frame_ms) == (16000, 20)
        assert client.bye()
        await asyncio.wait_for(ended.wait(), 3)
        (tmp_path / "delayed-offer-wire.json").write_text(json.dumps({
            "profile": "trunk" if trunk else "phone", "call_id": invite.header("Call-ID"),
            "invite_body_empty": not invite.body, "peer_200_offer": offer,
            "ack_answer": ack.body.decode(), "methods": [request.method for request in requests],
            "send": client.dialog.send_format.wire_token(),
            "receive": client.dialog.recv_format.wire_token(),
        }, indent=2) + "\n")
    finally:
        await client.close()
        server.close()
        assert client.transport is None and client._closed
