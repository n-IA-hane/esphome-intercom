"""Optional HA-side G.722 RTP codec backed by PyAV/FFmpeg."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

import numpy as np

from .audio_format import AudioFormat, PcmFormat

_LOGGER = logging.getLogger(__name__)
G722_PCM_SAMPLE_RATE = 16000
G722_CHANNELS = 1
G722_FRAME_MS = 20


def _frame_to_s16le(frame) -> bytes:
    samples = frame.to_ndarray()
    if samples.dtype.kind == "f":
        interleaved = np.clip(samples.T.reshape(-1), -1.0, 1.0)
        return np.minimum(interleaved * 32768.0, 32767.0).astype("<i2").tobytes()
    return samples.T.reshape(-1).astype("<i2", copy=False).tobytes()


@dataclass(slots=True)
class G722Decoder:
    """Decode one negotiated G.722 RTP stream to 16 kHz mono PCM."""

    _av: object = field(init=False, repr=False)
    _ctx: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            import av
        except Exception as err:  # pragma: no cover - runtime dependency.
            raise RuntimeError("PyAV is required for G.722 RTP decoding") from err
        self._av = av
        self._ctx = av.CodecContext.create("g722", "r")

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(
            G722_PCM_SAMPLE_RATE,
            PcmFormat.S16LE,
            G722_CHANNELS,
            G722_FRAME_MS,
        )

    def decode(self, payload: bytes) -> bytes:
        frames = self._ctx.decode(self._av.Packet(payload))
        return b"".join(_frame_to_s16le(frame) for frame in frames)


@dataclass(slots=True)
class G722Encoder:
    """Encode 16 kHz mono PCM as one stateful G.722 RTP payload."""

    bit_rate: int = 64000
    _av: object = field(init=False, repr=False)
    _ctx: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            import av
        except Exception as err:  # pragma: no cover - runtime dependency.
            raise RuntimeError("PyAV is required for G.722 RTP encoding") from err
        self._av = av
        self._ctx = av.CodecContext.create("g722", "w")
        self._ctx.sample_rate = G722_PCM_SAMPLE_RATE
        self._ctx.layout = "mono"
        self._ctx.format = "s16"
        self._ctx.bit_rate = int(self.bit_rate)
        self._ctx.open()

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat(
            G722_PCM_SAMPLE_RATE,
            PcmFormat.S16LE,
            G722_CHANNELS,
            G722_FRAME_MS,
        )

    def encode(self, pcm: bytes) -> bytes:
        expected = self.audio_format.nominal_frame_bytes
        if len(pcm) != expected:
            raise ValueError(
                f"G.722 encoder expected {expected} bytes, got {len(pcm)}"
            )
        samples = np.frombuffer(pcm, dtype="<i2").reshape(1, -1)
        frame = self._av.AudioFrame.from_ndarray(
            samples,
            format="s16",
            layout="mono",
        )
        frame.sample_rate = G722_PCM_SAMPLE_RATE
        packets = self._ctx.encode(frame)
        if len(packets) > 1:
            _LOGGER.debug(
                "G.722 encoder produced %d packets for one RTP frame",
                len(packets),
            )
        return b"".join(bytes(packet) for packet in packets)
