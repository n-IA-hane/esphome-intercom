"""Independent SDP and UDP oracles for the declared CC-2 and CC-4 contracts."""

import asyncio
import re
import socket
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.rtp_payload import RtpPayloadDecoder, RtpPayloadEncoder
from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

pytestmark = pytest.mark.ha




def udp_pair():
    result=[]
    for _ in range(2):
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1",0));sock.setblocking(False);result.append(sock)
    return result


@pytest.mark.parametrize("remote_ptime",[10,20])
async def test_cc2_own_receiver_policy_and_g722_wire_clocks_are_independent(socket_enabled,remote_ptime):
    loop=asyncio.get_running_loop()
    pcm_socket,phone_socket=udp_pair()
    remote=("v=0\r\no=- 81 1 IN IP4 127.0.0.1\r\ns=Independent G722 phone\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {phone_socket.getsockname()[1]} RTP/AVP 9\r\na=rtpmap:9 G722/8000/1\r\n"
            f"a=ptime:{remote_ptime}\r\na=sendrecv\r\n")
    selected=sdp.negotiate_directional(remote,[AudioFormat(16000,frame_ms=t) for t in [20,10]],
        [AudioFormat(16000,frame_ms=t) for t in [10,20]])
    assert selected is not None
    answer=sdp.build_answer_directional("127.0.0.1","127.0.0.1",40000,
        selected.send,selected.recv,remote_sdp=remote)
    # CC-2/R1: the actual answer advertises the local policy, not list order.
    assert re.search(r"^a=ptime:20\r?$",answer,re.MULTILINE)
    assert selected.recv.frame_ms==20 and selected.send.frame_ms==remote_ptime
    pcm=sdp.RtpPcmFormat(96,"L16",16000,1,20)
    left=RtpPeer("127.0.0.1",pcm_socket.getsockname()[1],96,pcm.audio_format,
                 rtp_format=pcm,send_rtp_format=pcm)
    right=RtpPeer("127.0.0.1",phone_socket.getsockname()[1],9,selected.recv.audio_format,
                  rtp_format=selected.recv,send_rtp_format=selected.send,
                  send_audio_format=selected.send.audio_format)
    relay=SipRtpRelay(left=left,right=right,left_port=0,right_port=0)
    encoder=decoder=None
    try:
        await relay.start()
        payload=struct.pack("!320h",*([2000]*320))
        await loop.sock_sendto(pcm_socket,rtp.build_packet(rtp.RtpPacket(96,1,320,81,payload)),
                              relay.left_transport.get_extra_info("sockname"))
        outgoing=[rtp.parse_packet((await asyncio.wait_for(loop.sock_recvfrom(phone_socket,2048),.5))[0])
                  for _ in range(20//remote_ptime)]
        assert all(packet.payload_type==9 and len(packet.payload)==8*remote_ptime for packet in outgoing)
        if len(outgoing)>1:
            assert (outgoing[1].timestamp-outgoing[0].timestamp)&0xffffffff==80
            assert (outgoing[1].sequence-outgoing[0].sequence)&65535==1
        decoder=RtpPayloadDecoder(selected.send)
        assert all(any(decoder.decode(packet.payload)) for packet in outgoing)
        # The opposite direction really sends 20ms, on G722's 8kHz RTP clock.
        encoder=RtpPayloadEncoder(sdp.RtpPcmFormat(9,"G722",8000,1,20))
        for sequence in [1,2]:
            payload=encoder.encode(struct.pack("<320h",*([3000]*320)))
            assert len(payload)==160
            await loop.sock_sendto(phone_socket,rtp.build_packet(rtp.RtpPacket(9,sequence,sequence*160,82,payload)),
                                  relay.right_transport.get_extra_info("sockname"))
        received=[rtp.parse_packet((await asyncio.wait_for(loop.sock_recvfrom(pcm_socket,2048),.5))[0]) for _ in range(2)]
        assert all(packet.payload_type==96 and len(packet.payload)==640 and any(packet.payload) for packet in received)
        assert (received[1].timestamp-received[0].timestamp)&0xffffffff==320
    finally:
        if encoder: encoder.close()
        if decoder: decoder.close()
        await relay.stop()
        pcm_socket.close();phone_socket.close()


async def test_cc4_negotiated_event_mapping_clock_and_identity_reach_real_udp(socket_enabled):
    loop=asyncio.get_running_loop()
    source,destination=udp_pair()
    wide=sdp.RtpPcmFormat(96,"L16",48000,1,10)
    narrow=sdp.RtpPcmFormat(97,"L16",16000,1,10)
    offer=sdp.build_offer_directional("127.0.0.1","127.0.0.1",40000,[wide.audio_format],[narrow.audio_format],
        send_rtp_formats=(wide,),recv_rtp_formats=(narrow,),allow_directional_payloads=True,
        independent_audio_ptime=True)
    # Independent text parsing also protects the original false-flags invariant.
    maps={int(pt):token for pt,token in re.findall(r"^a=rtpmap:(\d+) ([^\r\n]+)",offer,re.MULTILINE)}
    assert maps=={96:"L16/48000/1",97:"L16/16000/1",101:"telephone-event/8000",
                  98:"telephone-event/48000",99:"telephone-event/16000"}
    answer=("v=0\r\no=- 91 1 IN IP4 127.0.0.1\r\ns=Independent directional peer\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 41000 RTP/AVP 96 97 114 115 116\r\n"
            "a=rtpmap:96 L16/48000/1\r\na=x-voip-stack-flow:96 recv\r\n"
            "a=rtpmap:97 L16/16000/1\r\na=x-voip-stack-flow:97 send\r\n"
            "a=rtpmap:114 telephone-event/48000\r\na=fmtp:114 0-15\r\n"
            "a=rtpmap:115 telephone-event/16000\r\na=fmtp:115 0-15\r\n"
            "a=rtpmap:116 telephone-event/8000\r\na=fmtp:116 0-15\r\n"
            "a=x-voip-stack-independent-ptime:1\r\na=ptime:10\r\na=sendrecv\r\n")
    selected=sdp.negotiate_answer_directional(answer,[wide.audio_format],[narrow.audio_format],local_offer_sdp=offer)
    assert selected is not None
    events=sdp.negotiate_dtmf_answer_directional(answer,offer,audio=selected)
    assert (events.send.payload_type,events.send.sample_rate)==(114,48000)
    assert (events.recv.payload_type,events.recv.sample_rate)==(99,16000)
    left=RtpPeer("127.0.0.1",source.getsockname()[1],97,narrow.audio_format,
                 rtp_format=narrow,send_rtp_format=narrow)
    right=RtpPeer("127.0.0.1",destination.getsockname()[1],97,selected.recv.audio_format,
        rtp_format=selected.recv,send_rtp_format=selected.send,send_audio_format=selected.send.audio_format,
        dtmf_payload_type=events.recv.payload_type,dtmf_clock_rate=events.recv.sample_rate,
        send_dtmf_payload_type=events.send.payload_type,send_dtmf_clock_rate=events.send.sample_rate,
        send_dtmf_events=events.send.events,ssrc=91,timestamp=2400,sequence=10,clock_rate=48000)
    relay=SipRtpRelay(left=left,right=right,left_port=0,right_port=0)
    try:
        await relay.start()
        assert relay.relay_dtmf("left","5",duration_ms=40)
        packets=[rtp.parse_packet((await asyncio.wait_for(loop.sock_recvfrom(destination,2048),.5))[0]) for _ in range(4)]
        events_wire=[struct.unpack("!BBH",packet.payload) for packet in packets]
        assert {packet.payload_type for packet in packets}=={114}
        assert {packet.timestamp for packet in packets}=={2400}
        assert {packet.ssrc for packet in packets}=={91}
        assert [packet.sequence for packet in packets]==[10,11,12,13]
        assert packets[0].marker and not any(packet.marker for packet in packets[1:])
        assert all(event==5 for event,_flags,_duration in events_wire)
        assert [bool(flags&128) for _event,flags,_duration in events_wire]==[False,True,True,True]
        assert [duration for _event,_flags,duration in events_wire][-3:]==[1920]*3
        await asyncio.gather(*tuple(relay._dtmf_tasks))
        assert not relay._dtmf_tasks
    finally:
        await relay.stop()
        source.close();destination.close()
