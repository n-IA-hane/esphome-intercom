"""Generic packetization derives representable source-sample quanta."""

import importlib

import pytest

from .voip_phase1_support import audio_format, sdp

HA_AUDIO_CAPABILITIES = importlib.import_module(
    sdp.__package__ + ".audio_capabilities"
).HA_AUDIO_CAPABILITIES


@pytest.mark.parametrize("bounds,expected_ms", [
    ("", 40),
    ("a=minptime:20\r\na=maxptime:30\r\n", None),
    ("a=minptime:41\r\na=maxptime:80\r\n", None),
    ("a=minptime:41\r\na=maxptime:79\r\n", None),
])
def test_unknown_11025hz_offer_without_ptime_derives_finite_packetization(bounds, expected_ms):
    preferred = [
        audio_format.AudioFormat(48000, "s16le", 1, 10),
        audio_format.AudioFormat(16000, "s16le", 1, 10),
    ]
    offer = (
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=Finite quantum\r\n"
        "c=IN IP4 192.0.2.1\r\nt=0 0\r\nm=audio 40000 RTP/AVP 96\r\n"
        "a=rtpmap:96 L16/11025/1\r\n" + bounds
    )

    selected = sdp.negotiate_directional(
        offer, preferred, preferred, local_capabilities=HA_AUDIO_CAPABILITIES
    )

    if expected_ms is None:
        assert selected is None
        return
    assert selected is not None
    assert selected.send.frame_ms == expected_ms
    assert selected.send.audio_format.nominal_frame_samples == 441 * expected_ms // 40
    assert selected.send.audio_format.nominal_frame_bytes <= HA_AUDIO_CAPABILITIES.transmit_payload_bytes
    assert selected.send.rtp_timestamp_step == selected.send.audio_format.nominal_frame_samples
    assert selected.recv.max_frame_ms == 520
    assert HA_AUDIO_CAPABILITIES.supports_rtp(selected.recv, receive=True)
