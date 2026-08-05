"""Optional RTP Opus codec adapter for HA-side SIP legs."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

import numpy as np

from .audio_format import AudioFormat, PcmFormat

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class OpusDecoder:
    sample_rate: int
    channels: int
    _av: object = field(init=False, repr=False)
    _ctx: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            import av
        except Exception as err:  # pragma: no cover - depends on HA runtime deps.
            raise RuntimeError("PyAV is required for Opus RTP decoding") from err
        self._av = av
        self._ctx = av.CodecContext.create("opus", "r")

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(self.sample_rate, PcmFormat.S16LE, self.channels, 20)

    def decode(self, payload: bytes) -> bytes:
        frames = self._ctx.decode(self._av.Packet(payload))
        if not frames:
            return b""
        chunks: list[bytes] = []
        for frame in frames:
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
    bit_rate: int = 64000
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
        self._ctx.bit_rate = int(self.bit_rate)
        self._ctx.open()

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(self.sample_rate, PcmFormat.S16LE, self.channels, 20)

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
