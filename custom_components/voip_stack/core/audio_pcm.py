"""PCM frame conversion between negotiated VoIP audio formats.

The HA bridge may need to convert between two ESP/browser legs with different
PCM formats. The implementation keeps decode/encode vectorized and uses a
stateful anti-aliased rational resampler so downsampling does not fold content
above the destination Nyquist frequency back into the audible band.
"""

from __future__ import annotations

from math import gcd

import numpy as np

from .audio_format import AudioFormat, PcmFormat

_RESAMPLER_TAPS_PER_PHASE = 24
_KAISER_BETA = 9.0
_ROLLOFF = 0.945

def _decode_frame(data: bytes, fmt: AudioFormat) -> np.ndarray:
    """Decode PCM bytes to a float64 array shaped (channels, samples)."""
    stride = fmt.container_bytes_per_sample * fmt.channels
    if len(data) % stride:
        raise ValueError(
            f"audio frame length {len(data)} is not aligned to {fmt.wire_token()}"
        )
    if fmt.pcm_format is PcmFormat.S16LE:
        flat = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    elif fmt.pcm_format is PcmFormat.S24LE:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        value = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int8).astype(np.int32) << 16)
        )
        flat = value.astype(np.float64) / 8388608.0
    elif fmt.pcm_format is PcmFormat.S24LE_IN_S32:
        # ESPHome's s24le_in_s32 contract is a sign-extended 24-bit value in
        # the low 24 bits (right-aligned), matching the RTP L24 converter.
        flat = np.frombuffer(data, dtype="<i4").astype(np.float64) / 8388608.0
    else:
        flat = np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0
    return flat.reshape(-1, fmt.channels).T


def _encode_frame(channels: np.ndarray, dst: AudioFormat) -> bytes:
    """Encode a float array shaped (channels, samples) to destination PCM bytes."""
    interleaved = np.clip(channels.T.reshape(-1), -1.0, None)
    if dst.pcm_format is PcmFormat.S16LE:
        scaled = np.minimum(interleaved * 32768.0, 32767.0)
        return scaled.astype("<i2").tobytes()
    if dst.pcm_format is PcmFormat.S24LE:
        scaled = np.minimum(interleaved * 8388608.0, 8388607.0).astype("<i4")
        return scaled.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    if dst.pcm_format is PcmFormat.S24LE_IN_S32:
        scaled = np.minimum(interleaved * 8388608.0, 8388607.0).astype("<i4")
        return scaled.astype("<i4").tobytes()
    scaled = np.minimum(interleaved * 2147483648.0, 2147483647.0)
    return scaled.astype("<i4").tobytes()


def _map_channels(channels: np.ndarray, out_channels: int) -> np.ndarray:
    in_channels = channels.shape[0]
    if out_channels == 1 and in_channels > 1:
        return channels.mean(axis=0, keepdims=True)
    if out_channels <= in_channels:
        return channels[:out_channels]
    pad = np.repeat(channels[-1:], out_channels - in_channels, axis=0)
    return np.concatenate([channels, pad], axis=0)


class _PolyphaseResampler:
    """Stateful rational resampler with a Kaiser-windowed low-pass filter."""

    def __init__(self, src_rate: int, dst_rate: int, in_samples: int, channels: int) -> None:
        g = gcd(src_rate, dst_rate)
        self._up = dst_rate // g
        self._down = src_rate // g
        self._identity = self._up == self._down
        if self._identity:
            return

        out_samples = (in_samples * self._up) // self._down
        taps = _RESAMPLER_TAPS_PER_PHASE
        filter_len = taps * self._up
        n = np.arange(filter_len) - (filter_len - 1) / 2.0
        cutoff = _ROLLOFF * min(1.0 / self._up, 1.0 / self._down)
        kernel = cutoff * np.sinc(cutoff * n) * np.kaiser(filter_len, _KAISER_BETA)
        kernel *= self._up / kernel.reshape(taps, self._up).sum(axis=0).max()

        self._history = taps
        out_index = np.arange(out_samples)
        upsampled = out_index * self._down
        phase = upsampled % self._up
        center = upsampled // self._up
        tap_index = np.arange(taps)
        self._gather = self._history + center[:, None] - tap_index[None, :]
        self._coeffs = kernel[phase[:, None] + tap_index[None, :] * self._up]
        self._tail = np.zeros((channels, self._history), dtype=np.float64)

    def process(self, channels: np.ndarray) -> np.ndarray:
        if self._identity:
            return channels
        x = np.concatenate([self._tail, channels], axis=1)
        self._tail = x[:, -self._history:]
        return np.einsum("ot,cot->co", self._coeffs, x[:, self._gather], optimize=True)


class PcmFrameConverter:
    """Stateful PCM converter that also reframes differing frame durations."""

    def __init__(self, src: AudioFormat, dst: AudioFormat) -> None:
        self.src = src
        self.dst = dst
        if (src.frame_ms * dst.sample_rate) % 1000:
            raise ValueError(
                f"{src.wire_token()} cannot be reframed exactly at {dst.sample_rate} Hz"
            )
        self._resampler = _PolyphaseResampler(
            src.sample_rate, dst.sample_rate, src.nominal_frame_samples, dst.channels
        )
        self._pending = np.empty((dst.channels, 0), dtype=np.float64)

    def convert(self, data: bytes) -> list[bytes]:
        if len(data) != self.src.nominal_frame_bytes:
            raise ValueError(
                f"audio frame length {len(data)} does not match {self.src.wire_token()} "
                f"({self.src.nominal_frame_bytes} bytes)"
            )
        if self.src == self.dst:
            return [data]

        channels = _map_channels(_decode_frame(data, self.src), self.dst.channels)
        converted = self._resampler.process(channels)
        frame_samples = self.dst.nominal_frame_samples
        if self._pending.shape[1] == 0 and converted.shape[1] == frame_samples:
            return [_encode_frame(converted, self.dst)]
        self._pending = np.concatenate([self._pending, converted], axis=1)

        out: list[bytes] = []
        while self._pending.shape[1] >= frame_samples:
            out.append(_encode_frame(self._pending[:, :frame_samples], self.dst))
            self._pending = self._pending[:, frame_samples:]
        return out
