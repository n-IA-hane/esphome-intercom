"""Explicit local offer contracts survive planning, wire serialization and RTP."""

import asyncio
import math
import re
import socket
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media.payload_map import PayloadMap
from custom_components.voip_stack.core.rtp_payload import RtpPayloadDecoder, RtpPayloadEncoder
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

pytestmark = pytest.mark.ha




def test_explicit_payload_precedes_a_conventional_number_but_never_history_or_static():
    opus, wide, pcma = ("OPUS",48000,2), ("L16",48000,1), ("PCMA",8000,1)
    numbered=PayloadMap.for_offer([opus,wide,pcma],preferences={opus:110,wide:98,pcma:111})
    assert dict(numbered.receive)=={110:opus,98:wide,8:pcma}
    previous=PayloadMap(receive={110:wide})
    resumed=PayloadMap.for_offer([opus,wide],previous=previous,preferences={opus:110,wide:98})
    assert resumed.receive[110]==wide
    assert resumed.receive[98]==opus
    assert resumed.receive_history[110]==wide


def test_explicit_opus_fmtp_is_local_receiver_data_not_a_generated_default():
    fmt=sdp.RtpPcmFormat(110,"OPUS",48000,2,20,fmtp="stereo=0;maxaveragebitrate=24000;useinbandfec=1")
    body=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,[fmt.audio_format],[fmt.audio_format],
        send_rtp_formats=(fmt,),recv_rtp_formats=(fmt,),include_common_codecs=True,
        codec_policy=CodecPolicy(audio=("OPUS",)))
    assert "m=audio 40000 RTP/AVP 110 " in body
    assert f"a=fmtp:110 {fmt.fmtp}\r\n" in body
    assert "m=video" not in body


def test_sendonly_offer_projects_its_real_sender_quantum_without_any_receiver():
    send=AudioFormat(48000,"s16le",1,10)
    receive=AudioFormat(16000,"s16le",1,20)
    body=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,[send],[receive],audio_direction="sendonly")
    assert sdp.offered_pcm_formats(body)[0].audio_format==send
    assert "a=ptime:10\r\n" in body


class IndependentPeer(asyncio.DatagramProtocol):
    def __init__(self,port,*,wide_pcm=False):
        self.port=port
        self.wide_pcm=wide_pcm
        self.invites=[]
        self.transport=None
    def connection_made(self,transport): self.transport=transport
    def datagram_received(self,raw,address):
        request=sip.parse_message(raw)
        if request.method == "ACK":
            return
        if request.method not in {"INVITE", "BYE"}:
            return
        headers = [
            (name, request.header(name)) for name in ["Via", "From", "Call-ID", "CSeq"]
        ]
        to=request.header("To")
        headers.append(("To",to if ";tag=" in to else to+";tag=independent-offer-peer"))
        if request.method=="INVITE":
            self.invites.append(request.body)
            # The independent peer requires the explicit PT or its actual fixed
            # legacy packet time, rather than echoing the production planner.
            mapping=re.search(rb"^a=rtpmap:(\d+) L16/48000/1\r?$",request.body,re.MULTILINE) if self.wide_pcm else None
            accepted=(mapping is not None and b"a=ptime:10\r\n" in request.body) if self.wide_pcm else b"a=rtpmap:110 OPUS/48000/2\r\n" in request.body
            if not accepted:
                self.transport.sendto(sip.build_response(488,"Not Acceptable Here",headers),address)
                return
            payload=int(mapping[1]) if self.wide_pcm else 110
            encoding="L16/48000/1" if self.wide_pcm else "OPUS/48000/2"
            parameters="" if self.wide_pcm else f"a=fmtp:{payload} stereo=0;maxaveragebitrate=24000\r\n"
            packet_ms=10 if self.wide_pcm else 20
            body=("v=0\r\no=- 71 1 IN IP4 127.0.0.1\r\ns=Independent peer\r\n"
                  "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                  f"m=audio {self.port} RTP/AVP {payload}\r\na=rtpmap:{payload} {encoding}\r\n"
                  f"{parameters}a=ptime:{packet_ms}\r\na=sendrecv\r\n").encode()
            headers.extend([("Content-Type","application/sdp"),("Contact",f"<sip:peer@127.0.0.1:{self.transport.get_extra_info('sockname')[1]}>")])
            self.transport.sendto(sip.build_response(200,"OK",headers,body),address)
        else:
            self.transport.sendto(sip.build_response(200,"OK",headers),address)


async def test_actual_client_preserves_explicit_opus_on_sip_and_two_real_rtp_calls(hass,socket_enabled):
    loop=asyncio.get_running_loop()
    source=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    destination=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    for sock in [source, destination]:
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
    transport, peer = await loop.create_datagram_endpoint(
        lambda: IndependentPeer(destination.getsockname()[1]),
        local_addr=("127.0.0.1", 0),
    )
    fmt=sdp.RtpPcmFormat(110,"OPUS",48000,2,20,fmtp="stereo=0;maxaveragebitrate=24000;useinbandfec=1")
    try:
        for call in range(2):
            client=SipCallClient(local_ip="127.0.0.1",local_name="Explicit offer",local_sip_port=0,
                local_rtp_port=source.getsockname()[1],supported_formats=[fmt.audio_format],
                supported_send_rtp_formats=(fmt,),supported_recv_rtp_formats=(fmt,),
                include_common_codecs=True,codec_policy=CodecPolicy(audio=("OPUS",)))
            client.transport,client.protocol=await loop.create_datagram_endpoint(lambda:SipDatagramQueueProtocol(client.queue),local_addr=("127.0.0.1",0))
            client.local_sip_port=client.transport.get_extra_info("sockname")[1]
            encoder=decoder=None
            try:
                result=await client.invite(target="peer",remote_host="127.0.0.1",remote_sip_port=transport.get_extra_info("sockname")[1],timeout=2)
                if result == "ringing":
                    result = await client.wait_for_final(timeout=2)
                assert result == "in_call", peer.invites[-1]
                assert re.search(rb"^a=fmtp:110 stereo=0;maxaveragebitrate=24000;useinbandfec=1\r?$",peer.invites[-1],re.MULTILINE)
                assert b"m=video" not in peer.invites[-1]
                dialog=client.dialog
                assert dialog.send_format.payload_type==dialog.recv_format.payload_type==110
                encoder,decoder=RtpPayloadEncoder(dialog.send_format),RtpPayloadDecoder(fmt)
                decoded=[]
                for sequence in range(3):
                    pcm=struct.pack("<960h",*(round(3000*math.sin(2*math.pi*440*(i+sequence*960)/48000)) for i in range(960)))
                    wire=rtp.build_packet(rtp.RtpPacket(110,sequence,sequence*960,100+call,encoder.encode(pcm)))
                    await loop.sock_sendto(source,wire,(dialog.remote_rtp_host,dialog.remote_rtp_port))
                    packet=rtp.parse_packet((await asyncio.wait_for(loop.sock_recvfrom(destination,2048),.5))[0])
                    assert (packet.payload_type,packet.timestamp)==(110,sequence*960)
                    decoded.append(decoder.decode(packet.payload))
                assert all(len(pcm)==1920 and any(pcm) for pcm in decoded)
                assert client.bye()
            finally:
                if encoder:
                    encoder.close()
                if decoder:
                    decoder.close()
                await client.close()
        assert len(peer.invites)==2
    finally:
        transport.close()
        source.close()
        destination.close()


def test_common_offer_uses_actual_directional_backend_probes(monkeypatch):
    from custom_components.voip_stack.core import codec_capabilities, rtp_payload
    from custom_components.voip_stack.core.audio_format import HA_TRUNK_AUDIO_FORMATS
    def unavailable(*args,**kwargs):
        raise RuntimeError("Independent backend-unavailable witness")
    monkeypatch.setattr(rtp_payload,"G722Encoder",unavailable)
    monkeypatch.setattr(rtp_payload,"OpusEncoder",unavailable)
    codec_capabilities.codec_available.cache_clear()
    try:
        body=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,
            list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),include_common_codecs=True)
        assert " OPUS/" not in body and " G722/" not in body
        assert "a=rtpmap:0 PCMU/8000/1\r\n" in body
        assert "a=rtpmap:8 PCMA/8000/1\r\n" in body
        receive=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,
            list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),include_common_codecs=True,
            audio_direction="recvonly")
        assert " OPUS/" in receive and " G722/" in receive
    finally:
        codec_capabilities.codec_available.cache_clear()


def test_receive_only_offer_never_allocates_a_transmit_encoder(monkeypatch):
    from custom_components.voip_stack.core import codec_capabilities
    from custom_components.voip_stack.core.audio_format import HA_TRUNK_AUDIO_FORMATS
    allocated=[]
    def forbidden(*args,**kwargs):
        allocated.append(True)
        raise AssertionError("A receive-only offer has no encoder owner")
    monkeypatch.setattr(RtpPayloadEncoder,"__init__",forbidden)
    codec_capabilities.codec_available.cache_clear()
    try:
        body=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,
            list(HA_TRUNK_AUDIO_FORMATS),list(HA_TRUNK_AUDIO_FORMATS),include_common_codecs=True,
            audio_direction="recvonly",codec_policy=CodecPolicy(audio=("OPUS","G722","PCMU")))
        assert allocated==[]
        assert " OPUS/" in body and " G722/" in body and " PCMU/" in body
    finally:
        codec_capabilities.codec_available.cache_clear()


def test_standard_receiver_ptime_keeps_every_advertised_sender_bounded():
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_FORMATS, UDP_SAFE_PAYLOAD_BYTES
    formats=list(HA_SIP_PCM_FORMATS)
    body=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,formats,formats,
        include_common_codecs=True,)
    assert "a=ptime:10\r\n" in body
    offered=sdp.offered_pcm_formats(body)
    wide=next(fmt for fmt in offered if (fmt.encoding,fmt.sample_rate,fmt.channels)==("L16",48000,1))
    encoder=RtpPayloadEncoder(wide)
    try:
        payload=encoder.encode(bytes(wide.audio_format.nominal_frame_bytes))
        assert len(payload)==960 and len(payload)<=UDP_SAFE_PAYLOAD_BYTES
    finally:
        encoder.close()


async def test_actual_standard_offer_has_10_ms_wideband_sip_and_rtp(hass,socket_enabled):
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS
    loop=asyncio.get_running_loop()
    source=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    destination=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    for sock in [source, destination]:
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
    transport, peer = await loop.create_datagram_endpoint(
        lambda: IndependentPeer(destination.getsockname()[1], wide_pcm=True),
        local_addr=("127.0.0.1", 0),
    )
    client=SipCallClient(local_ip="127.0.0.1",local_name="Fixed10 receiver",local_sip_port=0,
        local_rtp_port=source.getsockname()[1],supported_send_formats=list(HA_SIP_PCM_TX_FORMATS),
        supported_recv_formats=[AudioFormat(48000,frame_ms=10),*HA_SIP_PCM_RX_FORMATS],
        include_common_codecs=True)
    client.transport,client.protocol=await loop.create_datagram_endpoint(lambda:SipDatagramQueueProtocol(client.queue),local_addr=("127.0.0.1",0))
    client.local_sip_port=client.transport.get_extra_info("sockname")[1]
    encoder=None
    try:
        result=await client.invite(target="legacy",remote_host="127.0.0.1",remote_sip_port=transport.get_extra_info("sockname")[1],timeout=2)
        if result == "ringing":
            result = await client.wait_for_final(timeout=2)
        assert result == "in_call", peer.invites[-1]
        assert b"a=ptime:10\r\n" in peer.invites[-1]
        dialog=client.dialog
        assert (dialog.send_format.frame_ms,dialog.recv_format.frame_ms)==(10,10)
        encoder=RtpPayloadEncoder(dialog.send_format)
        pcm=struct.pack("<480h",*([2345]*480))
        payload=encoder.encode(pcm)
        assert len(payload)==960
        wire=rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type,1,480,72,payload))
        await loop.sock_sendto(source,wire,(dialog.remote_rtp_host,dialog.remote_rtp_port))
        packet=rtp.parse_packet((await asyncio.wait_for(loop.sock_recvfrom(destination,2048),.5))[0])
        assert struct.unpack("!480h",packet.payload)==(2345,)*480
        assert client.bye()
    finally:
        if encoder:
            encoder.close()
        await client.close()
        transport.close()
        source.close()
        destination.close()


def test_unconfigured_codec_policy_preserves_input_order_and_explicit_order_is_honored():
    local=[AudioFormat(rate,"s16le",1,10) for rate in [48000,16000,8000]]
    options=dict(include_common_codecs=True)
    default=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,local,local,**options)
    ordered=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,local,local,
        codec_policy=CodecPolicy(audio=("OPUS","L16","PCMU")),**options)
    assert sdp.offered_pcm_formats(default)[0].encoding=="L16"
    assert sdp.offered_pcm_formats(ordered)[0].encoding=="OPUS"
    wide=sdp.RtpPcmFormat(96,"L16",48000,1,10)
    opus=sdp.RtpPcmFormat(110,"OPUS",48000,2,10,fmtp="stereo=0;maxaveragebitrate=24000")
    explicit=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,local,local,
        audio_rtp_formats=(wide,opus),codec_policy=CodecPolicy(audio=("OPUS","L16")))
    assert sdp.offered_pcm_formats(explicit)[0].payload_type==110
    assert "a=fmtp:110 stereo=0;maxaveragebitrate=24000\r\n" in explicit


def test_standard_alternatives_keep_actual_receiver_capacity_and_bounded_transmit():
    from custom_components.voip_stack.core.media import negotiator
    from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES

    wide=AudioFormat(48000,"s16le",1,10)
    stereo20=AudioFormat(48000,"s16le",2,20)
    narrow=AudioFormat(16000,"s16le",1,10)
    plan=negotiator.create_offer([stereo20,wide,narrow],[stereo20,wide,narrow],
        capabilities=HA_AUDIO_CAPABILITIES)
    assert plan.packet_ms==20
    assert {(fmt.sample_rate,fmt.channels) for fmt in plan.formats}=={(48000,2),(48000,1),(16000,1)}
    assert all(fmt.audio_format.nominal_frame_bytes<=1200 for fmt in plan.send_formats)
    assert any(fmt.audio_format.nominal_frame_bytes>1200 for fmt in plan.receive_formats)
    # Strict explicit catalogs with no shared mapping cannot form sendrecv SDP.
    with pytest.raises(ValueError,match="required local directions"):
        negotiator.create_offer([wide],[stereo20,narrow],capabilities=HA_AUDIO_CAPABILITIES,
            send_rtp_formats=(sdp.RtpPcmFormat(96,"L16",48000,1,10),),
            recv_rtp_formats=(sdp.RtpPcmFormat(97,"L16",48000,2,20),sdp.RtpPcmFormat(99,"L16",16000,1,10)))


@pytest.mark.parametrize("quantum",[10,16])
def test_explicit_independent_receiver_maxptime_is_visible_and_enforced(quantum):
    from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
    from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
    send=sdp.RtpPcmFormat(96,"L16",48000,1,10)
    receive=sdp.RtpPcmFormat(97,"L16",16000,1,quantum,max_frame_ms=quantum)
    body=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,[send.audio_format],[receive.audio_format],
        send_rtp_formats=(send,receive),recv_rtp_formats=(receive,send),local_capabilities=HA_AUDIO_CAPABILITIES)
    assert f"a=maxptime:{quantum}\r\n" in body
    rx=next(fmt for fmt in sdp.offered_pcm_formats(body) if fmt.payload_type==97)
    receiver=RtpAudioReceiver(rx)
    try:
        assert receiver.decode(97,bytes(16000*quantum*2//1000))
        with pytest.raises(rtp.RtpError,match="payload too large"):
            receiver.decode(97,bytes(16000*(quantum+1)*2//1000))
    finally:
        receiver.close()
