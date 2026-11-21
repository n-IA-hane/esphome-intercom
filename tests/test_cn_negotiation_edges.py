"""Retained RTP mappings keep auxiliary receive clocks consistent on reoffers."""

from dataclasses import replace

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_FORMATS
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media import shadow
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_bridge import invite_rtp_peer
from custom_components.voip_stack.sip_listener import SipInvite


pytestmark = pytest.mark.ha


def test_retained_opus_receive_mapping_rejects_preliminary_pcmu_cn_clock():
    previous_mode = shadow.engine_mode()
    shadow.set_engine_mode("active")
    try:
        policy = CodecPolicy(audio=("PCMU", "OPUS", "CN"))
        original_offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=-\r\n"
            "c=IN IP4 192.0.2.1\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 98\r\na=rtpmap:98 OPUS/48000/2\r\n"
            "a=ptime:20\r\na=sendrecv\r\n"
        )
        reoffer = original_offer.replace("RTP/AVP 98", "RTP/AVP 0 98 13").replace(
            "a=rtpmap:98 OPUS/48000/2",
            "a=rtpmap:0 PCMU/8000\r\na=rtpmap:98 OPUS/48000/2\r\na=rtpmap:13 CN/8000",
        )

        def negotiate(description):
            return sdp.negotiate_directional(
                description, HA_SIP_PCM_FORMATS, HA_SIP_PCM_FORMATS,
                codec_policy=policy, local_capabilities=HA_AUDIO_CAPABILITIES,
            )

        original = negotiate(original_offer)
        assert original is not None
        invite = SipInvite(
            source_host="192.0.2.1", source_port=5060,
            request_uri=sip.parse_sip_uri("sip:target@192.0.2.2"), caller_uri=None,
            target="target", caller="source", call_id="retained-cn-clock", cseq="1 INVITE",
            remote_sdp=original_offer.encode(), send_format=original.send,
            recv_format=original.recv, recv_formats=original.recv_formats,
            remote_rtp_host="192.0.2.1", remote_rtp_port=40000, codec_policy=policy,
        )
        established = invite_rtp_peer(invite)
        preliminary = negotiate(reoffer)
        assert preliminary is not None
        assert [fmt.encoding for fmt in preliminary.recv_formats] == ["PCMU", "CN"]
        updated = replace(
            invite, remote_sdp=reoffer.encode(), send_format=preliminary.send,
            recv_format=preliminary.recv, recv_formats=preliminary.recv_formats,
        )
        retained = invite_rtp_peer(updated, established=established)

        assert retained.inbound_rtp_format == established.inbound_rtp_format
        assert retained.outbound_rtp_format == established.outbound_rtp_format
        answer = sdp.build_answer_directional(
            "192.0.2.2", "192.0.2.2", 40002,
            retained.outbound_rtp_format, retained.inbound_rtp_format,
            recv_formats=retained.inbound_rtp_formats, remote_sdp=reoffer,
        )
        assert sdp.parse_sdp(answer)["payload_order"] == [98]
        assert all(fmt.encoding != "CN" for fmt in retained.inbound_rtp_formats)
        receiver = RtpAudioReceiver(retained.inbound_rtp_format, retained.inbound_rtp_formats)
        receiver.close()
    finally:
        shadow.set_engine_mode(previous_mode)


def test_retained_pcmu_packet_time_owns_the_existing_cn_receive_quantum():
    primary = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    cn = sdp.RtpPcmFormat(13, "CN", 8000, 1, 20)
    offer = (
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=-\r\n"
        "c=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 0 13\r\na=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:13 CN/8000\r\na=ptime:20\r\na=sendrecv\r\n"
    )
    invite = SipInvite(
        source_host="192.0.2.1", source_port=5060,
        request_uri=sip.parse_sip_uri("sip:target@192.0.2.2"), caller_uri=None,
        target="target", caller="source", call_id="retained-cn-time", cseq="1 INVITE",
        remote_sdp=offer.encode(), send_format=primary, recv_format=primary,
        recv_formats=(primary, cn), remote_rtp_host="192.0.2.1", remote_rtp_port=40000,
        codec_policy=CodecPolicy(audio=("PCMU", "CN")),
    )
    established = invite_rtp_peer(invite)
    preliminary_primary = replace(primary, frame_ms=10)
    preliminary_cn = replace(cn, frame_ms=10)
    updated = replace(
        invite, remote_sdp=offer.replace("ptime:20", "ptime:10").encode(),
        send_format=preliminary_primary, recv_format=preliminary_primary,
        recv_formats=(preliminary_primary, preliminary_cn),
    )

    retained = invite_rtp_peer(updated, established=established)

    assert retained.inbound_rtp_format == primary
    assert retained.inbound_rtp_formats == (primary, cn)
    receiver = RtpAudioReceiver(retained.inbound_rtp_format, retained.inbound_rtp_formats)
    try:
        pcm = receiver.decode(13, bytes([40]))
        assert [len(frame) for frame in pcm] == [320]
        assert receiver.comfort_noise_packet_ms == 20
    finally:
        receiver.close()
