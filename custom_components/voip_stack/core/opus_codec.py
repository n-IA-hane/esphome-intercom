"""Optional RTP Opus codec adapter for HA-side SIP legs."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

import numpy as np

from .audio_format import AudioFormat, PcmFormat

_LOGGER = logging.getLogger(__name__)
_MAX_FMTP_CHARS = 2048
_MAX_FMTP_PARAMETERS = 32
_RECEIVE_LIMITS = {
    "maxaveragebitrate": (6000, 510000),
    "maxplaybackrate": (8000, 48000),
}


def _receive_limits(fmtp: str) -> dict[str, int]:
    """Read bounded RFC 7587 receiver limits, ignoring invalid parameters."""
    value = str(fmtp or "")
    if len(value) > _MAX_FMTP_CHARS:
        return {}
    limits: dict[str, int] = {}
    for parameter in value.split(";", _MAX_FMTP_PARAMETERS)[:_MAX_FMTP_PARAMETERS]:
        name, separator, raw_value = parameter.partition("=")
        name = name.strip().lower()
        if not separator or name not in _RECEIVE_LIMITS:
            continue
        raw_value = raw_value.strip()
        if not raw_value.isascii() or not raw_value.isdecimal():
            continue
        number = int(raw_value)
        minimum, maximum = _RECEIVE_LIMITS[name]
        if minimum <= number <= maximum:
            limits[name] = number
    return limits


def _playback_cutoff(max_playback_rate: int) -> int:
    """Choose an Opus bandpass that cannot exceed the receiver's Nyquist."""
    # FFmpeg exposes 4/6/8/12/20 kHz cutoff values. CELT has no 6 kHz
    # mediumband mode and can round that request up to 8 kHz; use bands that
    # remain bounded across the encoder's automatic SILK/CELT mode changes.
    return max(cutoff for cutoff in (4000, 8000, 12000, 20000)
               if cutoff * 2 <= max_playback_rate)


@dataclass(slots=True)
class OpusDecoder:
    sample_rate: int
    channels: int
    frame_ms: int = 20
    _av: object = field(init=False, repr=False)
    _ctx: object = field(init=False, repr=False)
    _resampler: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            import av
        except Exception as err:  # pragma: no cover - depends on HA runtime deps.
            raise RuntimeError("PyAV is required for Opus RTP decoding") from err
        self._av = av
        self._ctx = av.CodecContext.create("opus", "r")
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=self.sample_rate
        )

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(self.sample_rate, PcmFormat.S16LE, self.channels, self.frame_ms)

    def decode(self, payload: bytes) -> bytes:
        frames = self._ctx.decode(self._av.Packet(payload))
        if not frames:
            return b""
        chunks: list[bytes] = []
        for decoded in frames:
            for frame in self._resampler.resample(decoded):
                channels = frame.to_ndarray()
                if channels.dtype.kind == "f":
                    interleaved = np.clip(channels.T.reshape(-1), -1.0, 1.0)
                    scaled = np.minimum(interleaved * 32768.0, 32767.0).astype("<i2")
                    chunks.append(scaled.tobytes())
                else:
                    interleaved = channels.T.reshape(-1).astype("<i2", copy=False)
                    chunks.append(interleaved.tobytes())
        return b"".join(chunks)


@dataclass(slots=True)
class OpusEncoder:
    sample_rate: int
    channels: int
    frame_ms: int = 20
    bit_rate: int = 64000
    fmtp: str = ""
    _av: object = field(init=False, repr=False)
    _ctx: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            import av
        except Exception as err:  # pragma: no cover - depends on HA runtime deps.
            raise RuntimeError("PyAV is required for Opus RTP encoding") from err
        self._av = av
        self._ctx = av.CodecContext.create("libopus", "w")
        self._ctx.sample_rate = self.sample_rate
        self._ctx.layout = "stereo" if self.channels == 2 else "mono"
        self._ctx.format = "s16"
        receive_limits = _receive_limits(self.fmtp)
        if "maxaveragebitrate" in receive_limits:
            self.bit_rate = min(int(self.bit_rate), receive_limits["maxaveragebitrate"])
        self._ctx.bit_rate = int(self.bit_rate)
        options = {"frame_duration": str(self.frame_ms)}
        if "maxplaybackrate" in receive_limits:
            options["cutoff"] = str(_playback_cutoff(receive_limits["maxplaybackrate"]))
        self._ctx.options = options
        self._ctx.open()

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(self.sample_rate, PcmFormat.S16LE, self.channels, self.frame_ms)

    def encode(self, pcm: bytes) -> bytes:
        expected = self.audio_format.nominal_frame_bytes
        if len(pcm) != expected:
            raise ValueError(f"Opus encoder expected {expected} bytes, got {len(pcm)}")
        samples = np.frombuffer(pcm, dtype="<i2").reshape(1, -1)
        frame = self._av.AudioFrame.from_ndarray(
            samples,
            format="s16",
            layout="stereo" if self.channels == 2 else "mono",
        )
        frame.sample_rate = self.sample_rate
        packets = self._ctx.encode(frame)
        if not packets:
            return b""
        if len(packets) > 1:
            _LOGGER.debug("Opus encoder produced %d packets for one RTP frame", len(packets))
        return b"".join(bytes(packet) for packet in packets)
