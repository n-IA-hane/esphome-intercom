"""The pure bridge planner preserves leg contracts and explains adaptation."""

import math
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import AudioFormat, HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media.bridge import AudioOperation, plan_audio_flow, plan_caller_audio
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver

pytestmark = pytest.mark.ha


def fmt(rate, *, pt=96, codec="L16", ms=20):
    return sdp.RtpPcmFormat(pt, codec, rate, 2 if codec == "OPUS" else 1, ms)


@pytest.mark.parametrize("source,target,operations", [
    (fmt(16000), fmt(16000, pt=110), (AudioOperation.PASSTHROUGH,)),
    (fmt(16000), fmt(16000, ms=10), (AudioOperation.REPACKETIZE,)),
    (fmt(16000, ms=10), fmt(48000, ms=10), (AudioOperation.RESAMPLE,)),
    (fmt(8000, codec="PCMA"), fmt(8000, codec="PCMU"), (AudioOperation.TRANSCODE,)),
    (fmt(8000, codec="PCMA"), fmt(48000, codec="OPUS"), (AudioOperation.RESAMPLE, AudioOperation.TRANSCODE)),
    (fmt(8000, codec="G722"), fmt(16000), (AudioOperation.TRANSCODE,)),
])
def test_each_direction_distinguishes_packetization_pcm_and_codec_work(source, target, operations):
    plan = plan_audio_flow(source, target)
    assert plan.source == source and plan.target == target
    assert plan.operations == operations and plan.reason


def test_transmitted_caller_is_frozen_before_reading_any_new_offer_or_policy():
    caller = sdp.RtpPcmDirection(fmt(48000, ms=10), fmt(16000))
    callee = sdp.RtpPcmDirection(fmt(8000, codec="PCMA"), fmt(8000, codec="PCMA"))
    plan = plan_caller_audio(b"not an SDP offer", caller, callee, frozen=True)
    assert plan.caller is caller
    assert plan.trace == ("B3 caller description was transmitted; preserve its contract",)


def test_free_pcm_caller_keeps_48k_receive_contract_and_resamples_both_directions():
    body = ("v=0\r\nc=IN IP4 192.0.2.1\r\nm=audio 40000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\n"
            "a=ptime:10\r\n").encode()
    caller = sdp.negotiate_directional(body, list(HA_SIP_PCM_TX_FORMATS), list(HA_SIP_PCM_RX_FORMATS))
    assert caller is not None
    callee = sdp.RtpPcmDirection(fmt(48000, ms=10), fmt(16000, pt=97))
    plan = plan_caller_audio(body, caller, callee, send_capabilities=HA_SIP_PCM_TX_FORMATS,
                             receive_capabilities=HA_SIP_PCM_RX_FORMATS)
    assert (plan.caller.send.sample_rate, plan.caller.send.frame_ms) == (48000, 10)
    assert (plan.caller.recv.sample_rate, plan.caller.recv.frame_ms) == (16000, 20)
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 42000,
        plan.caller.send, plan.caller.recv, recv_formats=plan.caller.recv_formats,
        remote_sdp=body)
    assert "a=ptime:20\r\n" in answer and "x-voip-stack" not in answer
    assert "L16/16000/1" in answer and "L16/48000/1" in answer
    samples = tuple(round(10000 * math.sin(2 * math.pi * 3000 * index / 16000)) for index in range(320))
    native = struct.pack("<320h", *samples)
    receiver = RtpAudioReceiver(plan.caller.recv, plan.caller.recv_formats)
    try:
        decoded = receiver.decode_rtp(rtp.RtpPacket(plan.caller.recv.payload_type,
            1, 0, 99, struct.pack("!320h", *samples)))
        assert decoded == [native]
    finally:
        receiver.close()
    assert plan.caller_to_callee.source.frame_ms == plan.callee_to_caller.source.frame_ms == 20
    assert plan.caller_to_callee.target.frame_ms == plan.callee_to_caller.target.frame_ms == 10
    assert plan.caller_to_callee.operations == plan.callee_to_caller.operations == (
        AudioOperation.RESAMPLE, AudioOperation.REPACKETIZE)


def test_callee_codec_never_widens_the_callers_policy():
    body = ("v=0\r\nc=IN IP4 192.0.2.1\r\nm=audio 40000 RTP/AVP 96 8\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\n").encode()
    caller = sdp.RtpPcmDirection(fmt(16000), fmt(16000))
    callee = sdp.RtpPcmDirection(fmt(8000, codec="PCMA"), fmt(8000, codec="PCMA"))
    plan = plan_caller_audio(body, caller, callee, codec_policy=CodecPolicy(audio=("L16",)))
    assert plan.caller.send.encoding == plan.caller.recv.encoding == "L16"


def test_admitted_generic_caller_survives_no_feasible_bridge_optimization():
    body = ("v=0\r\nc=IN IP4 192.0.2.1\r\nm=audio 40000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/32000/1\r\na=ptime:10\r\na=sendrecv\r\n").encode()
    send_preferences = (AudioFormat(48000, frame_ms=10),)
    receive_preferences = (AudioFormat(16000, frame_ms=20),)
    caller = sdp.negotiate_directional(body, list(send_preferences), list(receive_preferences),
                                     local_capabilities=HA_AUDIO_CAPABILITIES)
    assert caller is not None and caller.send.sample_rate == 32000
    assert caller.send.audio_format not in send_preferences
    pcma = fmt(8000, pt=8, codec="PCMA")
    plan = plan_caller_audio(body, caller, sdp.RtpPcmDirection(pcma, pcma),
                             send_capabilities=send_preferences,
                             receive_capabilities=receive_preferences)
    assert plan.caller is caller


@pytest.mark.parametrize("receive_bytes", [1920, 12288])
def test_bridge_keeps_the_admitting_backend_receive_envelope_instead_of_preset_times(receive_bytes):
    from custom_components.voip_stack.core.audio_capabilities import AudioCapabilities

    body = ("v=0\r\nc=IN IP4 192.0.2.1\r\nm=audio 40000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/48000/1\r\na=ptime:10\r\na=maxptime:60\r\n").encode()
    preferences = (AudioFormat(48000, frame_ms=10),)
    backend = AudioCapabilities(receive_payload_bytes=receive_bytes)
    caller = sdp.negotiate_directional(body, list(preferences), list(preferences), local_capabilities=backend)
    callee = sdp.RtpPcmDirection(fmt(16000, ms=10), fmt(48000, ms=10))
    plan = plan_caller_audio(body, caller, callee, send_capabilities=preferences,
                             receive_capabilities=preferences, capabilities=backend)
    assert plan.caller is caller
    assert plan.caller.recv.max_frame_ms == receive_bytes // 96
    assert plan.caller.recv.frame_ms == 20 and plan.caller.send.frame_ms == 10


@pytest.mark.parametrize("receive_bytes", [1920, 12288])
def test_bridge_tx_candidates_stay_configured_while_backend_rx_remains_independent(receive_bytes):
    from custom_components.voip_stack.core.audio_capabilities import AudioCapabilities

    body = ("v=0\r\nc=IN IP4 192.0.2.1\r\nm=audio 40000 RTP/AVP 97 96\r\n"
        "a=rtpmap:96 L16/48000/1\r\n"
        "a=rtpmap:97 L16/16000/1\r\n"
        "a=ptime:10\r\na=maxptime:10\r\n").encode()
    transmit = (AudioFormat(16000, frame_ms=20), AudioFormat(48000, frame_ms=10))
    receive = (AudioFormat(48000, frame_ms=10),)
    backend = AudioCapabilities(receive_payload_bytes=receive_bytes)
    caller = sdp.negotiate_directional(body, list(transmit), list(receive), local_capabilities=backend)
    assert caller.send.audio_format == AudioFormat(16000, frame_ms=10)  # Dynamic initial admission.
    callee = sdp.RtpPcmDirection(fmt(48000, ms=10), fmt(16000))
    plan = plan_caller_audio(body, caller, callee, send_capabilities=transmit,
                             receive_capabilities=receive, capabilities=backend)
    assert plan.caller.send.audio_format == AudioFormat(48000, frame_ms=10)
    assert plan.caller.send.audio_format in transmit
    assert plan.caller.recv is caller.recv
    assert plan.caller.recv.max_frame_ms == receive_bytes // 96


def test_retained_receive_mapping_cannot_restore_bounds_above_the_actual_backend():
    from custom_components.voip_stack.core.audio_capabilities import AudioCapabilities

    body = ("v=0\r\nc=IN IP4 192.0.2.1\r\nm=audio 40000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/48000/1\r\na=ptime:10\r\na=maxptime:60\r\n").encode()
    preferences = (AudioFormat(48000, frame_ms=10),)
    caller = sdp.negotiate_directional(body, list(preferences), list(preferences),
                                     local_capabilities=HA_AUDIO_CAPABILITIES)
    assert caller.recv.max_frame_ms == 128
    plan = plan_caller_audio(body, caller, caller, send_capabilities=preferences,
        receive_capabilities=preferences, capabilities=AudioCapabilities(receive_payload_bytes=1920))
    assert plan.caller.recv.frame_ms == 20 and plan.caller.recv.max_frame_ms == 20


@pytest.mark.parametrize("already_accepted", [True, False])
def test_primary_no_op_retains_hard_bounds_without_hiding_a_new_cn_mapping(already_accepted):
    from dataclasses import replace

    body = ("v=0\r\nc=IN IP4 192.0.2.1\r\nm=audio 40000 RTP/AVP 96 13\r\n"
            "a=rtpmap:96 L16/8000/1\r\na=ptime:10\r\na=sendrecv\r\n").encode()
    primary = fmt(8000, ms=10)
    received = replace(primary, max_frame_ms=10 if already_accepted else 0)
    cn = sdp.RtpPcmFormat(13, "CN", 8000, 1, 10)
    caller = sdp.RtpPcmDirection(primary, received,
        recv_formats=(received, cn) if already_accepted else ())
    callee = sdp.RtpPcmDirection(primary, primary)
    capabilities = (AudioFormat(8000, frame_ms=10),)

    plan = plan_caller_audio(body, caller, callee, codec_policy=CodecPolicy(audio=("L16", "CN")),
                             send_capabilities=capabilities, receive_capabilities=capabilities)

    assert (plan.caller is caller) is already_accepted
    if already_accepted:
        assert plan.caller.recv.max_frame_ms == 10
        assert plan.caller.recv_formats is caller.recv_formats
    else:
        assert any(item.payload_type == 13 and item.encoding == "CN" for item in plan.caller.recv_formats)
