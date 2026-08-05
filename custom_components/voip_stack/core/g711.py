"""G.711 PCMA/PCMU conversion for SIP trunk interop."""

from __future__ import annotations

import numpy as np

_BIAS = 0x84
_CLIP = 32635
_ULAW_SEG_END = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)
_ALAW_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _search(value: int, table: tuple[int, ...]) -> int:
    for index, end in enumerate(table):
        if value <= end:
            return index
    return len(table)


def _clip_s16(value: int) -> int:
    return max(-32768, min(32767, value))


def _decode_alaw_byte(value: int) -> int:
    value ^= 0x55
    sample = (value & 0x0F) << 4
    segment = (value & 0x70) >> 4
    if segment == 0:
        sample += 8
    elif segment == 1:
        sample += 0x108
    else:
        sample += 0x108
        sample <<= segment - 1
    return _clip_s16(sample if value & 0x80 else -sample)


def _decode_ulaw_byte(value: int) -> int:
    value = (~value) & 0xFF
    sample = ((value & 0x0F) << 3) + _BIAS
    sample <<= (value & 0x70) >> 4
    return _clip_s16(_BIAS - sample if value & 0x80 else sample - _BIAS)


def _encode_alaw_sample(sample: int) -> int:
    # ITU-T G.711 linear PCM input is reduced to 13 significant bits before
    # segment selection.  Searching ``sample >> 4`` while quantizing the
    # original 16-bit value shifts every segment up by one and costs roughly
    # 6 dB on decoded telephone audio.
    sample >>= 3
    if sample >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        sample = -sample - 1
    segment = _search(sample, _ALAW_SEG_END)
    if segment >= 8:
        return 0x7F ^ mask
    encoded = segment << 4
    if segment < 2:
        encoded |= (sample >> 1) & 0x0F
    else:
        encoded |= (sample >> segment) & 0x0F
    return encoded ^ mask


def _encode_ulaw_sample(sample: int) -> int:
    if sample < 0:
        sample = _BIAS - sample
        mask = 0x7F
    else:
        sample = _BIAS + sample
        mask = 0xFF
    if sample > _CLIP:
        sample = _CLIP
    segment = _search(sample, _ULAW_SEG_END)
    if segment >= 8:
        return 0x7F ^ mask
    encoded = (segment << 4) | ((sample >> (segment + 3)) & 0x0F)
    return encoded ^ mask


def _build_alaw_encode_lut() -> np.ndarray:
    """Build the exhaustive signed-16 to A-law map without Python hot loops."""

    samples = np.arange(-32768, 32768, dtype=np.int32) >> 3
    positive = samples >= 0
    mask = np.where(positive, 0xD5, 0x55).astype(np.uint8)
    magnitude = np.where(positive, samples, -samples - 1)
    segment = np.searchsorted(
        np.asarray(_ALAW_SEG_END, dtype=np.int32),
        magnitude,
        side="left",
    ).astype(np.int32)
    quantized = np.where(
        segment < 2,
        magnitude >> 1,
        magnitude >> np.minimum(segment, 7),
    )
    encoded = ((segment << 4) | (quantized & 0x0F)) ^ mask
    encoded = np.where(segment >= 8, 0x7F ^ mask, encoded)
    return encoded.astype(np.uint8)


def _build_ulaw_encode_lut() -> np.ndarray:
    """Build the exhaustive signed-16 to mu-law map without Python hot loops."""

    samples = np.arange(-32768, 32768, dtype=np.int32)
    negative = samples < 0
    mask = np.where(negative, 0x7F, 0xFF).astype(np.uint8)
    magnitude = np.where(negative, _BIAS - samples, _BIAS + samples)
    magnitude = np.minimum(magnitude, _CLIP)
    segment = np.searchsorted(
        np.asarray(_ULAW_SEG_END, dtype=np.int32),
        magnitude,
        side="left",
    ).astype(np.int32)
    encoded = (segment << 4) | (
        np.right_shift(magnitude, np.minimum(segment + 3, 10)) & 0x0F
    )
    encoded = np.where(segment >= 8, 0x7F ^ mask, encoded ^ mask)
    return encoded.astype(np.uint8)


_ALAW_DECODE_LUT = np.fromiter(
    (_decode_alaw_byte(value) for value in range(256)),
    dtype="<i2",
    count=256,
)
_ULAW_DECODE_LUT = np.fromiter(
    (_decode_ulaw_byte(value) for value in range(256)),
    dtype="<i2",
    count=256,
)
_ALAW_ENCODE_LUT = _build_alaw_encode_lut()
_ULAW_ENCODE_LUT = _build_ulaw_encode_lut()


def _s16le_lut_indices(data: bytes) -> np.ndarray:
    if len(data) % 2:
        raise ValueError("s16le frame length is not sample-aligned")
    # The encode LUT is ordered from -32768 through +32767. Flipping the sign
    # bit maps the native two's-complement representation to that order.
    return np.frombuffer(data, dtype="<u2") ^ np.uint16(0x8000)


def alaw_to_s16le(payload: bytes) -> bytes:
    return _ALAW_DECODE_LUT[np.frombuffer(payload, dtype=np.uint8)].tobytes()


def ulaw_to_s16le(payload: bytes) -> bytes:
    return _ULAW_DECODE_LUT[np.frombuffer(payload, dtype=np.uint8)].tobytes()


def s16le_to_alaw(pcm: bytes) -> bytes:
    return _ALAW_ENCODE_LUT[_s16le_lut_indices(pcm)].tobytes()


def s16le_to_ulaw(pcm: bytes) -> bytes:
    return _ULAW_ENCODE_LUT[_s16le_lut_indices(pcm)].tobytes()
