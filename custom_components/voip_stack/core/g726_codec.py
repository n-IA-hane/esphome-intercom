"""Stateful RFC 3551 G.726: optional SpanDSP TX and PyAV g726le RX."""

from __future__ import annotations

import ctypes
from ctypes.util import find_library
from functools import lru_cache
import weakref

import numpy as np

from .audio_format import AudioFormat, PcmFormat
from .media.codecs import CODECS


@lru_cache(maxsize=1)
def _spandsp():
    """Use SpanDSP's public opaque-state API, with no structure/ABI offsets."""
    library = find_library("spandsp")
    if not library:
        raise RuntimeError("SpanDSP is required for real-time G.726 RTP encoding")
    lib = ctypes.CDLL(library)
    lib.g726_init.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    lib.g726_init.restype = ctypes.c_void_p
    lib.g726_encode.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
    lib.g726_encode.restype = ctypes.c_int
    lib.g726_decode.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
    lib.g726_decode.restype = ctypes.c_int
    lib.g726_free.argtypes = (ctypes.c_void_p,)
    lib.g726_free.restype = ctypes.c_int
    return lib


class G726Encoder:
    """Encode one negotiated packet without PyAV's 205 to 512 ms batching."""

    def __init__(self, encoding: str, frame_ms: float = 20):
        self.spec = CODECS[encoding]
        if self.spec.pyav_decoder != "g726le":
            raise ValueError("Select a standard G726 RTP encoding")
        self.audio_format = AudioFormat(self.spec.decoded_rate, PcmFormat.S16LE, self.spec.decoded_channels, frame_ms)
        samples = self.audio_format.sample_rate * frame_ms / 1000
        if not samples.is_integer() or samples < 1 or int(samples) % self.spec.sample_quantum:
            raise ValueError("G.726 packet must end at a complete codeword/octet quantum")
        self._samples = int(samples)
        self._lib = _spandsp()
        # g726.h: linear signed PCM=0; RIGHT=2 is RFC 3551 LSB-first packing.
        context = self._lib.g726_init(None, self.spec.bit_rate, 0, 2)
        if not context:
            raise RuntimeError("SpanDSP could not initialize the G.726 encoder")
        self._context = context
        self._finalizer = weakref.finalize(self, self._lib.g726_free, context)
        self._output = ctypes.create_string_buffer(self._samples * self.spec.bits_per_sample // 8)

    def close(self):
        """Release this call's native predictor state exactly once."""
        self._finalizer()
        self._context = None

    def encode(self, pcm: bytes) -> bytes:
        if self._context is None:
            raise RuntimeError("G.726 encoder is closed")
        if len(pcm) != self._samples * 2:
            raise ValueError("G.726 PCM packet does not match the negotiated duration")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.int16, copy=False)
        count = self._lib.g726_encode(self._context, self._output, samples.ctypes.data, self._samples)
        if count != len(self._output):
            raise RuntimeError("SpanDSP did not produce one complete negotiated G.726 packet")
        return self._output.raw


class G726Decoder:
    """Use qualified PyAV decoders, or SpanDSP for the 40 kbit/s mode.

    FFmpeg 8/9's five-bit decoder diverges from the ITU Appendix II output.
    The 40 kbit/s mode therefore requires the optional native backend in both
    directions, rather than advertising an implementation that merely opens.
    """

    def __init__(self, encoding: str, frame_ms: float = 20):
        self.spec = CODECS[encoding]
        if self.spec.pyav_decoder != "g726le":
            raise ValueError("Select a standard G726 RTP encoding")
        self.audio_format = AudioFormat(self.spec.decoded_rate, PcmFormat.S16LE, self.spec.decoded_channels, frame_ms)
        self._native = encoding == "G726-40"
        if self._native:
            self._lib = _spandsp()
            self._context = self._lib.g726_init(None, self.spec.bit_rate, 0, 2)
            if not self._context:
                raise RuntimeError("SpanDSP could not initialize the G.726 decoder")
            self._finalizer = weakref.finalize(self, self._lib.g726_free, self._context)
            return
        import av

        self._av = av
        self._ctx = av.CodecContext.create(self.spec.pyav_decoder, "r")
        self._ctx.sample_rate = self.spec.decoded_rate
        self._ctx.layout = "mono"
        self._ctx.options = {"bits_per_coded_sample": str(self.spec.bits_per_sample)}
        self._ctx.open()

    def decode(self, payload: bytes) -> bytes:
        quantum_bytes = self.spec.sample_quantum * self.spec.bits_per_sample // 8
        if not payload or len(payload) % quantum_bytes:
            raise ValueError("G.726 RTP payload ends inside a codeword/octet quantum")
        if self._native:
            if self._context is None:
                raise RuntimeError("G.726 decoder is closed")
            samples = len(payload) * 8 // self.spec.bits_per_sample
            output = np.empty(samples, dtype=np.int16)
            count = self._lib.g726_decode(self._context, output.ctypes.data, ctypes.c_char_p(payload), len(payload))
            if count != samples:
                raise ValueError("G.726 decoder returned an incomplete PCM packet")
            return output.astype("<i2", copy=False).tobytes()
        frames = self._ctx.decode(self._av.Packet(payload))
        decoded = b"".join(frame.to_ndarray().reshape(-1).astype("<i2").tobytes() for frame in frames)
        if len(decoded) != len(payload) * 8 // self.spec.bits_per_sample * 2:
            raise ValueError("G.726 decoder returned an incomplete PCM packet")
        return decoded

    def close(self):
        if self._native:
            self._finalizer()
            self._context = None
