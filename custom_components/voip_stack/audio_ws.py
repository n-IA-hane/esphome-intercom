"""Versioned binary browser-audio framing for VoIP Stack."""

from __future__ import annotations

from dataclasses import dataclass
import struct

AUDIO_FRAME_TYPE = 1
AUDIO_FRAME_MAGIC = 0x56
AUDIO_FRAME_VERSION = 1
AUDIO_FRAME_HEADER = struct.Struct("!BBBBIHIH")
AUDIO_FRAME_HEADER_BYTES = AUDIO_FRAME_HEADER.size
AUDIO_FRAME_FLAG_DISCONTINUITY = 0x01


@dataclass(frozen=True, slots=True)
class BrowserAudioFrame:
    """One PCM frame placed on an explicit media timeline."""

    payload: bytes
    generation: int
    sequence: int
    timestamp: int
    flags: int = 0


def encode_audio_frame(
    payload: bytes,
    *,
    generation: int,
    sequence: int,
    timestamp: int,
    flags: int = 0,
) -> bytes:
    if not payload:
        raise ValueError("empty audio payload")
    if len(payload) > 0xFFFF:
        raise ValueError("audio payload is too large")
    header = AUDIO_FRAME_HEADER.pack(
        AUDIO_FRAME_MAGIC,
        AUDIO_FRAME_VERSION,
        AUDIO_FRAME_TYPE,
        int(flags) & 0xFF,
        int(generation) & 0xFFFFFFFF,
        int(sequence) & 0xFFFF,
        int(timestamp) & 0xFFFFFFFF,
        len(payload),
    )
    return header + payload


def decode_audio_frame(frame: bytes) -> BrowserAudioFrame:
    if len(frame) < AUDIO_FRAME_HEADER_BYTES:
        raise ValueError("invalid audio frame")
    magic, version, kind, flags, generation, sequence, timestamp, payload_size = (
        AUDIO_FRAME_HEADER.unpack_from(frame)
    )
    if (
        magic != AUDIO_FRAME_MAGIC
        or version != AUDIO_FRAME_VERSION
        or kind != AUDIO_FRAME_TYPE
        or payload_size != len(frame) - AUDIO_FRAME_HEADER_BYTES
        or payload_size == 0
    ):
        raise ValueError("invalid audio frame")
    return BrowserAudioFrame(
        payload=frame[AUDIO_FRAME_HEADER_BYTES:],
        generation=generation,
        sequence=sequence,
        timestamp=timestamp,
        flags=flags,
    )
