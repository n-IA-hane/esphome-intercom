"""G.723.1 RTP frames, identified as G723 by RFC 3551."""

from __future__ import annotations

import numpy as np

from .audio_format import AudioFormat, PcmFormat
from .media.codecs import CODECS


def g723_encoder_supports_fmtp(fmtp: str) -> bool:
    """Honor RFC 4856 rates in kb/s; CC7 ignores malformed or unknown hints."""
    for parameter in fmtp.split(";"):
        name, _, value = parameter.strip().partition("=")
        if name.strip().casefold() == "bitrate":
            bit_rate = {"5.3": 5300, "6.3": 6300}.get(value.strip())
            if bit_rate is not None and bit_rate != CODECS["G723"].bit_rate:
                return False
    return True


def g723_frames(payload: bytes):
    """Validate and split 6.3/5.3 kbit/s speech and SID frames before decoding."""
    offset = 0
    frames = []
    while offset < len(payload):
        tag = payload[offset] & 3
        lengths = CODECS["G723"].frame_octets_by_tag
        if tag >= len(lengths):
            raise ValueError("Reserved G.723.1 frame tag")
        size = lengths[tag]
        if offset + size > len(payload):
            raise ValueError("Truncated G.723.1 frame")
        frames.append(payload[offset:offset + size])
        offset += size
    if not frames:
        raise ValueError("Empty G.723.1 payload")
    return tuple(frames)


class G723Encoder:
    """Encode supported 6.3 kbit/s speech, in complete 30 ms codec frames."""

    def __init__(self, frame_ms: float = 30, *, fmtp: str = ""):
        import av

        spec = CODECS["G723"]
        if frame_ms <= 0 or frame_ms % spec.frame_duration_ms:
            raise ValueError("G.723.1 packet duration must be a multiple of 30 ms")
        if not g723_encoder_supports_fmtp(fmtp):
            raise ValueError("The installed G.723.1 encoder supports bitrate=6.3")
        self.audio_format = AudioFormat(spec.decoded_rate, PcmFormat.S16LE, spec.decoded_channels, frame_ms)
        self._av = av
        self._ctx = av.CodecContext.create(spec.pyav_encoder, "w")
        self._ctx.sample_rate = spec.decoded_rate
        self._ctx.layout = "mono"
        self._ctx.format = "s16"
        self._ctx.bit_rate = spec.bit_rate
        self._ctx.open()

    def encode(self, pcm: bytes) -> bytes:
        if len(pcm) != self.audio_format.nominal_frame_bytes:
            raise ValueError("G.723.1 PCM packet does not match the negotiated duration")
        frame_samples = CODECS["G723"].frame_duration_ms * self.audio_format.sample_rate // 1000
        samples = np.frombuffer(pcm, dtype="<i2")
        encoded = []
        for offset in range(0, len(samples), frame_samples):
            frame = self._av.AudioFrame.from_ndarray(samples[offset:offset + frame_samples].reshape(1, -1),
                                                    format="s16", layout="mono")
            frame.sample_rate = self.audio_format.sample_rate
            packets = self._ctx.encode(frame)
            data = b"".join(bytes(packet) for packet in packets)
            if len(g723_frames(data)) != 1:
                raise RuntimeError("G.723.1 encoder did not produce one codec frame")
            encoded.append(data)
        return b"".join(encoded)


class G723Decoder:
    """Decode every RFC-tagged rate and SID frame using the call's state."""

    def __init__(self, frame_ms: float = 30):
        import av

        spec = CODECS["G723"]
        self.audio_format = AudioFormat(spec.decoded_rate, PcmFormat.S16LE, spec.decoded_channels, frame_ms)
        self._av = av
        self._ctx = av.CodecContext.create(CODECS["G723"].pyav_decoder, "r")
        self._ctx.sample_rate = spec.decoded_rate
        self._ctx.layout = "mono"
        self._ctx.open()

    def decode(self, payload: bytes) -> bytes:
        chunks = []
        for packet in g723_frames(payload):
            frames = self._ctx.decode(self._av.Packet(packet))
            pcm = b"".join(frame.to_ndarray().reshape(-1).astype("<i2").tobytes() for frame in frames)
            if len(pcm) != CODECS["G723"].frame_duration_ms * self.audio_format.sample_rate // 1000 * 2:
                raise ValueError("G.723.1 decoder returned an incomplete codec frame")
            chunks.append(pcm)
        return b"".join(chunks)
