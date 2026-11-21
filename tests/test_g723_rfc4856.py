"""Registered G723 bitrate restrictions and the local CC7 fmtp contract."""

import pytest

from .test_optional_audio_codecs import load


g723 = load("core.g723_codec")
payload = load("core.rtp_payload")
capabilities = load("core.audio_capabilities").HA_AUDIO_CAPABILITIES
RtpPcmFormat = load("core.sdp_types").RtpPcmFormat


@pytest.mark.parametrize("fmtp,can_send", [
    ("", True),
    ("bitrate=6.3", True),
    (" BITRATE = 6.3 ; annexa=no ", True),
    ("bitrate=5.3", False),
    (" BITRATE = 5.3 ; x-vendor=hint ", False),
    # CC7 ignores malformed values instead of treating bit/s as kb/s aliases.
    ("bitrate=5300", True),
    ("bitrate=6300", True),
    ("bitrate=5.30", True),
    ("bitrate=NaN", True),
    ("bitrate=", True),
    ("bitrate", True),
    ("x-vendor=5.3", True),
    ("bitrate=5300;bitrate=5.3", False),
    ("bitrate=5.3;bitrate=6300", False),
])
def test_registered_bitrate_restricts_transmit_without_narrowing_receive(fmtp, can_send):
    fmt = RtpPcmFormat(4, "G723", 8000, 1, 60, fmtp=fmtp)

    assert capabilities.supports_rtp(fmt) is can_send
    assert capabilities.supports_rtp(fmt, receive=True)
    decoder = payload.RtpPayloadDecoder(fmt)
    assert len(decoder.decode(bytes([2, 0, 0, 0]))) == 480

    if not can_send:
        with pytest.raises(ValueError, match=r"bitrate=6\.3"):
            payload.RtpPayloadEncoder(fmt)
        return

    encoder = payload.RtpPayloadEncoder(fmt)
    wire = encoder.encode(bytes(fmt.audio_format.nominal_frame_bytes))
    frames = g723.g723_frames(wire)
    # RFC 3551 identifies 6.3 kb/s speech by tag 00 and 24 octets per frame.
    assert [len(frame) for frame in frames] == [24, 24]
    assert all(frame[0] & 3 == 0 for frame in frames)
    assert len(decoder.decode(wire)) == fmt.audio_format.nominal_frame_bytes
