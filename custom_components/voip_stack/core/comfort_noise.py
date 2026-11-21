"""RFC 3389 zeroth-order comfort-noise analysis and per-call synthesis."""

import math

import numpy as np

from .audio_format import AudioFormat, PcmFormat

MAX_CN_PAYLOAD_BYTES = 256


class ComfortNoiseDecoder:
    """Retain one SID level and generator for the lifetime of its RTP owner.

    RFC 3389 permits reducing spectral order. This decoder accepts optional
    reflection coefficients and synthesizes the allowed zeroth-order model.
    """

    def __init__(self, sample_rate=8000, frame_ms=20, *, seed=None):
        self.audio_format = AudioFormat(sample_rate, PcmFormat.S16LE, 1, frame_ms)
        self._random = np.random.default_rng(seed)
        self.level = 127
        self.active = False

    def update(self, payload: bytes):
        if not 1 <= len(payload) <= MAX_CN_PAYLOAD_BYTES or payload[0] & 0x80:
            raise ValueError("Invalid RFC 3389 noise level or payload length")
        if 255 in payload[1:]:
            raise ValueError("Reserved RFC 3389 reflection coefficient")
        self.level = payload[0]
        self.active = True

    def generate(self, samples=None) -> bytes:
        count = self.audio_format.nominal_frame_samples if samples is None else samples
        if not isinstance(count, int) or not 0 <= count <= self.audio_format.sample_rate:
            raise ValueError("Comfort-noise output exceeds one second")
        if not self.active:
            return bytes(count * 2)
        rms = 32768 * 10 ** (-self.level / 20)
        noise = self._random.normal(0, rms, count)
        return np.clip(np.rint(noise), -32768, 32767).astype("<i2").tobytes()

    def decode(self, payload: bytes) -> bytes:
        self.update(payload)
        return self.generate()

    def reset(self):
        """End the silence period when primary audio resumes or the leg ends."""
        self.active = False


class ComfortNoiseEncoder:
    """Analyze one mono PCM packet and emit a valid zeroth-order SID."""

    def __init__(self, sample_rate=8000, frame_ms=20):
        self.audio_format = AudioFormat(sample_rate, PcmFormat.S16LE, 1, frame_ms)

    def encode(self, pcm: bytes) -> bytes:
        if not pcm or len(pcm) % 2:
            raise ValueError("Comfort-noise input must contain whole signed 16-bit samples")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        rms = float(np.sqrt(np.mean(samples * samples)))
        level = 127 if rms == 0 else min(127, max(0, math.ceil(-20 * math.log10(rms / 32768))))
        return bytes([level])
