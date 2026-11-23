"""Default 16 kHz packet preference, with independent transmit and receive clocks."""

import struct
from dataclasses import replace

import pytest

from .voip_phase1_support import (
    _load_intercom_module, audio_format, sdp, sip_client,
)
from custom_components.voip_stack.core.media import leg, negotiator
from custom_components.voip_stack.core.codec_policy import CodecPolicy


@pytest.mark.parametrize("initiator", ["esp", "ha"])
@pytest.mark.parametrize("esp_supports_20", [True, False])
def test_default_ha_and_full_esp_negotiate_both_directions(initiator, esp_supports_20):
    """Round-trip actual SDP, then deliver the peer's actual PCM packet quantum."""
    esp_send = [audio_format.AudioFormat(16000, frame_ms=ms)
                for ms in ((20, 10) if esp_supports_20 else (10,))]
    esp_send.append(audio_format.AudioFormat(48000, frame_ms=10))
    esp_recv = [audio_format.AudioFormat(48000, frame_ms=10),
                audio_format.AudioFormat(48000, frame_ms=20),
                audio_format.AudioFormat(16000, frame_ms=20),
                audio_format.AudioFormat(16000, frame_ms=10)]
    local = {
        "ha": (list(audio_format.HA_SIP_PCM_TX_FORMATS), list(audio_format.HA_SIP_PCM_RX_FORMATS)),
        "esp": (esp_send, esp_recv),
    }
    answerer = "ha" if initiator == "esp" else "esp"
    pcm_policy = CodecPolicy(audio=("L16",))
    offer_options = {}
    if initiator == "esp":
        wide = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10, max_frame_ms=20)
        narrow = sdp.RtpPcmFormat(97, "L16", 16000, 1, 20 if esp_supports_20 else 10,
                                 max_frame_ms=20 if esp_supports_20 else 10)
        offer_options = {"send_rtp_formats": (wide, narrow), "recv_rtp_formats": (wide, narrow)}
    offer = sdp.build_offer_directional(
        "192.0.2.1", "192.0.2.1", 40000, *local[initiator],
        local_capabilities=sip_client.HA_AUDIO_CAPABILITIES if initiator == "ha" else None,
        codec_policy=pcm_policy if initiator == "esp" else None,
        **offer_options,
    )
    if answerer == "esp":
        # The fixed ESP receiver asks for 10 ms independently of the native
        # transmitter's 20 ms preference and its declared fallback formats.
        policy = replace(negotiator.policy_for_leg(*local[answerer], codec_policy=pcm_policy), default_packet_ms=10)
        selected = negotiator.as_direction(leg.answer_offer(offer, policy), offer)
    else:
        selected = sdp.negotiate_directional(
            offer, *local[answerer], local_capabilities=sip_client.HA_AUDIO_CAPABILITIES,
        )
    assert selected is not None
    answer = sdp.build_answer_directional(
        "192.0.2.2", "192.0.2.2", 41000,
        selected.send, selected.recv, remote_sdp=offer, recv_formats=selected.recv_formats,
    )
    accepted = sdp.negotiate_answer_directional(
        answer, *local[initiator], local_offer_sdp=offer,
        local_capabilities=sip_client.HA_AUDIO_CAPABILITIES if initiator == "ha" else None,
        codec_policy=pcm_policy if initiator == "esp" else None,
    )
    assert accepted is not None
    ha, esp = (selected, accepted) if answerer == "ha" else (accepted, selected)
    assert (ha.send.sample_rate, ha.send.frame_ms) == (48000, 10)
    assert (esp.recv.sample_rate, esp.recv.frame_ms) == (48000, 10)
    assert (ha.recv.sample_rate, ha.recv.frame_ms) == (16000, 20)
    assert (esp.send.sample_rate, esp.send.frame_ms) == (16000, 20 if esp_supports_20 else 10)
    for description in (offer, answer):
        assert "x-voip-stack" not in description
        assert {fmt.sample_rate for fmt in sdp.offered_pcm_formats(description)} >= {16000, 48000}

    # A 10 ms-only transmitter may ignore the receiver's 20 ms preference.
    # The existing receiver repacks its packets into the chosen local quantum.
    receiver = _load_intercom_module("rtp_audio_receiver").RtpAudioReceiver(ha.recv)
    encoder = sip_client.RtpPayloadEncoder(esp.send)
    expected, frames = [], []
    for index in range(4):
        count = esp.send.audio_format.nominal_frame_samples
        pcm = struct.pack(f"<{count}h", *(300 + index * count + i for i in range(count)))
        expected.append(pcm)
        frames.extend(receiver.decode(ha.recv.payload_type, encoder.encode(pcm), normalize_frames=True))
    assert len(frames) == (4 if esp_supports_20 else 2)
    assert all(len(frame) == 640 for frame in frames)
    assert b"".join(frames) == b"".join(expected)
    assert not receiver.has_pending_pcm


@pytest.mark.parametrize("frame_ms", [30, 32, 40])
def test_receive_preference_does_not_narrow_backend_packet_capability(frame_ms):
    fmt = sdp.RtpPcmFormat(110, "L16", 16000, 1, frame_ms)
    capabilities = sip_client.HA_AUDIO_CAPABILITIES
    assert capabilities.supports_rtp(fmt, receive=True)
    assert capabilities.max_receive_packet_ms(fmt) >= frame_ms
