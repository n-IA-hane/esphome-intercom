"""Local SIP offers retain legacy and directional media through the real bridge."""

import asyncio
from dataclasses import replace
from pathlib import Path
import re
import socket
import struct
import subprocess

import pytest

from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_client import RtpPayloadEncoder
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("codec", ["PCMA", "PCMU", "G722"])
def test_local_common_offer_accepts_the_ten_ms_answer_it_requests(codec):
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_FORMATS
    formats = list(HA_SIP_PCM_FORMATS)
    offer = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 40000, formats, formats,
        include_common_codecs=True, allow_directional_payloads=True)
    selected = next(item for item in sdp.offered_pcm_formats(offer) if item.encoding == codec)
    answer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Ordinary phone\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        f"m=audio 40002 RTP/AVP {selected.payload_type}\r\na=rtpmap:{selected.payload_type} {codec}/8000/1\r\n"
        "a=ptime:10\r\na=sendrecv\r\n")
    negotiated = sdp.negotiate_answer_directional(answer, formats, formats, local_offer_sdp=offer)
    assert negotiated is not None
    assert negotiated.send.frame_ms == negotiated.recv.frame_ms == 10


def test_receiver_reframes_a_legal_twenty_ms_packet_into_its_ten_ms_pcm_contract():
    receiver = RtpAudioReceiver(sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 10, max_frame_ms=20))
    frames = receiver.decode(0, bytes([128]) * 160)
    assert [len(frame) for frame in frames] == [160, 160]
    assert max(struct.unpack("<80h", frames[0])) > 10000


@pytest.mark.parametrize("fmt,payload", [
    pytest.param(sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 10), bytes(160), id="undeclared-long-packet"),
    pytest.param(sdp.RtpPcmFormat(96, "L16", 48000, 1, 10, max_frame_ms=1000),
        bytes(rtp.MAX_AUDIO_RTP_PAYLOAD_BYTES + 1), id="global-audio-ceiling"),
])
def test_receive_envelope_does_not_expand_without_maxptime_or_past_global_audio_limit(fmt, payload):
    with pytest.raises(rtp.RtpError):
        RtpAudioReceiver(fmt).decode(fmt.payload_type, payload)


@pytest.mark.parametrize("packet_time,expected_bytes", [(10, 80), (20, 160)])
def test_g722_encoder_uses_the_negotiated_rtp_packet_time(packet_time, expected_bytes):
    encoder = RtpPayloadEncoder(sdp.RtpPcmFormat(9, "G722", 8000, 1, packet_time))
    pcm = struct.pack(f"<{16 * packet_time}h", *([2000] * (16 * packet_time)))
    packets = [encoder.encode(pcm) for _ in range(4)]
    assert all(len(packet) == expected_bytes for packet in packets)
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-f", "g722", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
        input=b"".join(packets), capture_output=True, check=True)
    assert len(decoded.stdout) == len(pcm) * 4
    assert max(struct.unpack(f"<{len(decoded.stdout)//2}h", decoded.stdout)) >= 1500


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("profile", ["symmetric-sip", "hint-only", "unspecified", "pcma-10", "pcmu-10", "g722-10", "g722-20"])
@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_bridge_capability_contracts_have_real_answer_duplex_rtp_and_cleanup(trunks, monkeypatch, profile, registered):
    lab = trunks
    metadata = {"endpoint_id": "sip:bedroom0", "endpoint_kind": "sip_account", "registered": registered}
    if profile == "symmetric-sip":
        metadata.update(sip_audio_tx_formats=["L16/16000/1/10"],
            sip_audio_rx_formats=["L16/48000/1/10", "L16/16000/1/10"])
    elif profile == "hint-only":
        metadata.update(sdp.options_capabilities((Path(__file__).parent / "fixtures/sdp/native-options-mixed-quanta.sdp").read_bytes()))
    lab.roster[1] = replace(lab.roster[1], metadata=metadata)
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    port = lab.runtime.transport_config["sip_port"]
    target = lab.phones[0]
    peer_contract = {}

    def answer():
        request = target.last_request
        formats = sdp.offered_pcm_formats(request.body)
        common = profile == "unspecified" or profile.startswith(("pcma-", "pcmu-", "g722-"))
        codec = profile.split("-")[0].upper() if profile != "unspecified" else "PCMU"
        receive = next(item for item in formats if item.encoding == (codec if common else "L16")
            and item.sample_rate == (8000 if common else 48000 if profile == "hint-only" else 16000))
        send = (next(item for item in formats if item.encoding == "L16" and item.sample_rate == 16000)
            if profile == "hint-only" else receive)
        packet_time = 20 if profile in {"unspecified", "g722-20"} else 10
        peer_contract.update(send=send, receive=receive, packet_time=packet_time)
        target.payload_type = send.payload_type
        payloads = f"{receive.payload_type} {send.payload_type}" if profile == "hint-only" else str(receive.payload_type)
        mappings = f"a=rtpmap:{receive.payload_type} {receive.encoding}/{receive.sample_rate}/1\r\n"
        if profile == "hint-only":
            mappings += (f"a=rtpmap:{send.payload_type} L16/16000/1\r\n"
                f"a=x-voip-stack-flow:{receive.payload_type} recv\r\na=x-voip-stack-flow:{send.payload_type} send\r\n")
        body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent ordinary phone\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {target.rtp_socket.getsockname()[1]} RTP/AVP {payloads}\r\n{mappings}"
            f"a=ptime:{packet_time}\r\na=sendrecv\r\n").encode()
        target.respond(request, target.remote, 200, body)
        target.answered.set()

    monkeypatch.setattr(target, "answer", answer)
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Independent caller", local_sip_port=5060,
        local_rtp_port=audio.getsockname()[1], supported_formats=[AudioFormat(16000, frame_ms=10)])
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    try:
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=port, timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call" and caller.dialog is not None
        async with asyncio.timeout(3):
            for index in range(6):
                packet = rtp.RtpPacket(caller.dialog.send_format.payload_type, index, index * 160, 901,
                    struct.pack("!160h", *([2000] * 160)))
                await lab.hass.loop.sock_sendto(audio, rtp.build_packet(packet), (caller.dialog.remote_rtp_host, caller.dialog.remote_rtp_port))
            packet = rtp.parse_packet((await target.audio.get())[0])
            assert packet.payload_type == peer_contract["receive"].payload_type
            common = profile == "unspecified" or profile.startswith(("pcma-", "pcmu-", "g722-"))
            if common:
                assert len(packet.payload) == 8 * peer_contract["packet_time"]
                assert any(value != 255 for value in packet.payload)
            else:
                assert len(packet.payload) == peer_contract["receive"].sample_rate // 100 * 2
                assert max(struct.unpack(f"!{len(packet.payload)//2}h", packet.payload)) >= 1900
            destination = int(re.search(rb"m=audio (\d+)", target.invites[0].body)[1])
            samples = peer_contract["send"].sample_rate * peer_contract["packet_time"] // 1000
            if profile.startswith("g722-"):
                import av
                import numpy as np
                codec = av.CodecContext.create("g722", "w")
                codec.sample_rate = 16000
                codec.layout = "mono"
                codec.format = "s16"
                codec.options = {"frame_size": str(16 * peer_contract["packet_time"])}
                codec.open()
                frame = av.AudioFrame.from_ndarray(np.ones((1, 16 * peer_contract["packet_time"]), dtype=np.int16) * 3000,
                    format="s16", layout="mono")
                frame.sample_rate = 16000
                payload = b"".join(bytes(item) for item in codec.encode(frame))
            elif common:
                payload = bytes([170 if profile.startswith("pcma-") else 128]) * samples
            else:
                payload = struct.pack(f"!{samples}h", *([3000] * samples))
            for index in range(3):
                await lab.hass.loop.sock_sendto(target.rtp_socket, rtp.build_packet(rtp.RtpPacket(
                    target.payload_type, index, index * samples, 902, payload)), ("127.0.0.1", destination))
            returned = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
            assert returned.payload_type == caller.dialog.recv_format.payload_type
            assert max(struct.unpack(f"!{len(returned.payload)//2}h", returned.payload)) >= (2000 if profile.startswith("g722-")
                else 10000 if common else 2900)
        session = lab.calls.get_session(caller.dialog_ids.call_id)
        assert caller.bye()
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        audio.close()
