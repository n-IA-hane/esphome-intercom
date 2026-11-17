"""RFC 7587 receive limits reach the actual FFmpeg/libopus encoder."""

from __future__ import annotations

import numpy as np
import pytest

from .voip_phase1_support import _load_intercom_module

opus_codec = _load_intercom_module("opus_codec")
pytest.importorskip("av")


def _packet_bandwidth_hz(payload: bytes) -> int:
    """Read the independently specified RFC 6716 section 3.1 TOC config."""
    config = payload[0] >> 3
    if config < 12:
        return (4000, 6000, 8000)[config // 4]
    if config < 16:
        return (12000, 20000)[(config - 12) // 2]
    return (4000, 8000, 12000, 20000)[(config - 16) // 4]


def _pcm(duration_ms: int, index: int = 0) -> bytes:
    positions = np.arange(48 * duration_ms) + index * 48 * duration_ms
    return (
        (
            10000
            * (
                np.sin(2 * np.pi * 997 * positions / 48000)
                + np.sin(2 * np.pi * 12000 * positions / 48000)
            )
        )
        .astype("<i2")
        .tobytes()
    )


def _flush_decoded_samples(decoder) -> int:
    # FFmpeg's Opus decoder may retain the initial rate-conversion delay for
    # narrowband packets. Its documented end-of-stream flush returns it.
    output_samples = 0
    for decoded in decoder._ctx.decode(None):
        output_samples += sum(
            frame.samples for frame in decoder._resampler.resample(decoded)
        )
    return output_samples + sum(
        frame.samples for frame in decoder._resampler.resample(None)
    )


@pytest.mark.parametrize("duration_ms", [20, 40])
@pytest.mark.parametrize(
    "max_rate", [8000, 11025, 12000, 16000, 22050, 24000, 40000, 48000]
)
@pytest.mark.parametrize("local_bitrate", [20000, 64000])
def test_opus_receive_playback_limit_bounds_actual_encoded_bandwidth(
    duration_ms, max_rate, local_bitrate
):
    encoder = opus_codec.OpusEncoder(
        48000, 1, duration_ms, local_bitrate, fmtp=f"maxplaybackrate={max_rate}"
    )
    decoder = opus_codec.OpusDecoder(48000, 1, duration_ms)
    assert encoder._ctx.is_open
    assert encoder._ctx.options == {}
    assert encoder._ctx.frame_size == duration_ms * 48
    decoded_samples = 0
    for index in range(3):
        pcm = _pcm(duration_ms, index)
        payload = encoder.encode(pcm)
        assert payload
        assert _packet_bandwidth_hz(payload) * 2 <= max_rate
        decoded = decoder.decode(payload)
        assert decoded and len(decoded) % 2 == 0
        decoded_samples += len(decoded) // 2
    assert decoded_samples + _flush_decoded_samples(decoder) == 3 * duration_ms * 48


@pytest.mark.parametrize("duration_ms", [20, 40])
@pytest.mark.parametrize(
    "maximum,local,expected",
    [
        (6000, 64000, 6000),
        (20000, 64000, 20000),
        (510000, 64000, 64000),
        (64000, 32000, 32000),
    ],
)
def test_opus_maxaveragebitrate_caps_the_real_encoder(
    duration_ms, maximum, local, expected
):
    encoder = opus_codec.OpusEncoder(
        48000,
        1,
        duration_ms,
        local,
        fmtp=f"maxaveragebitrate={maximum}; maxplaybackrate=16000",
    )
    decoder = opus_codec.OpusDecoder(48000, 1, duration_ms)
    assert encoder._ctx.bit_rate == expected
    assert encoder.bit_rate == expected
    decoded_samples = 0
    for index in range(3):
        pcm = _pcm(duration_ms, index)
        payload = encoder.encode(pcm)
        assert _packet_bandwidth_hz(payload) <= 8000
        decoded = decoder.decode(payload)
        assert decoded and len(decoded) % 2 == 0
        decoded_samples += len(decoded) // 2
    assert decoded_samples + _flush_decoded_samples(decoder) == 3 * duration_ms * 48


@pytest.mark.parametrize(
    "fmtp",
    [
        "maxaveragebitrate=5999;maxplaybackrate=7999",
        "maxaveragebitrate=510001;maxplaybackrate=48001",
        "maxaveragebitrate=NaN;maxplaybackrate=inf",
        "maxaveragebitrate=-20000;maxplaybackrate=16000.0",
        "maxaveragebitrate=２０000;maxplaybackrate=０8000",
        "x=" + "1" * 2048 + ";maxaveragebitrate=6000;maxplaybackrate=8000",
        "unused=0;" * 32 + "maxaveragebitrate=6000;maxplaybackrate=8000",
    ],
)
def test_invalid_or_over_budget_fmtp_preserves_legacy_encoder_output(fmtp):
    default = opus_codec.OpusEncoder(48000, 1, 20)
    received = opus_codec.OpusEncoder(48000, 1, 20, fmtp=fmtp)
    assert received._ctx.bit_rate == default._ctx.bit_rate == 64000
    for index in range(3):
        pcm = _pcm(20, index)
        assert received.encode(pcm) == default.encode(pcm)
