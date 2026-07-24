"""RTP v2 packet helpers for PCM audio."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Protocol


RTP_VERSION = 2
RTP_HEADER_SIZE = 12
MAX_RTP_PAYLOAD_BYTES = 1400
MAX_RTP_RECEIVE_PAYLOAD_BYTES = 65507
# Audio is decoded in-process and has a much smaller useful payload envelope
# than generic RTP (which is also used by video).  This ceiling still covers
# every supported PCM frame and a 20 ms Opus packet containing 2.5 ms frames.
MAX_AUDIO_RTP_PAYLOAD_BYTES = 12 * 1024
_MAX_OPUS_FRAME_BYTES = 1275
_MIN_OPUS_FRAME_US = 2500
_RTP_HEADER = struct.Struct("!BBHII")


class RtpError(ValueError):
    """Malformed RTP packet."""


class _AudioRtpFormat(Protocol):
    encoding: str
    sample_rate: int
    channels: int
    frame_ms: int


@dataclass(frozen=True, slots=True)
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False


def audio_payload_size_limit(fmt: _AudioRtpFormat) -> int:
    """Return the receive limit for one negotiated audio RTP payload."""

    try:
        encoding = str(fmt.encoding).strip().upper()
        sample_rate = int(fmt.sample_rate)
        channels = int(fmt.channels)
        frame_ms = int(fmt.frame_ms)
    except (AttributeError, TypeError, ValueError) as err:
        raise RtpError("invalid RTP audio format") from err
    if sample_rate <= 0 or channels <= 0 or frame_ms <= 0:
        raise RtpError("invalid RTP audio format")

    if encoding == "OPUS":
        # RFC 6716 permits multiple same-duration frames in one Opus packet.
        # Bound the maximum frame count from negotiated ptime and include the
        # worst-case two-byte VBR length field per frame.
        frame_us = frame_ms * 1000
        frame_count = max(1, (frame_us + _MIN_OPUS_FRAME_US - 1) // _MIN_OPUS_FRAME_US)
        codec_limit = frame_count * (_MAX_OPUS_FRAME_BYTES + 2)
    else:
        bytes_per_sample = {
            "PCMA": 1,
            "PCMU": 1,
            "G722": 1,
            "L16": 2,
            "L24": 3,
        }.get(encoding)
        if bytes_per_sample is None:
            raise RtpError(f"unsupported RTP audio encoding {encoding or '<empty>'}")
        sample_numerator = sample_rate * frame_ms
        if sample_numerator % 1000:
            raise RtpError("RTP audio ptime does not produce whole samples")
        codec_limit = (sample_numerator // 1000) * channels * bytes_per_sample
    return min(codec_limit, MAX_AUDIO_RTP_PAYLOAD_BYTES)


def validate_audio_payload_size(payload: bytes, fmt: _AudioRtpFormat) -> None:
    """Reject an audio payload that exceeds its negotiated codec envelope."""

    limit = audio_payload_size_limit(fmt)
    if len(payload) > limit:
        raise RtpError(
            f"RTP audio payload too large for {fmt.encoding}/{fmt.sample_rate}/"
            f"{fmt.channels}/{fmt.frame_ms}ms: {len(payload)} bytes; max is {limit}"
        )


def build_packet(packet: RtpPacket) -> bytes:
    if not 0 <= packet.payload_type <= 127:
        raise RtpError("RTP payload type out of range")
    if not 0 <= packet.sequence <= 0xFFFF:
        raise RtpError("RTP sequence out of range")
    if not 0 <= packet.timestamp <= 0xFFFFFFFF:
        raise RtpError("RTP timestamp out of range")
    if not 0 <= packet.ssrc <= 0xFFFFFFFF:
        raise RtpError("RTP SSRC out of range")
    if len(packet.payload) > MAX_RTP_PAYLOAD_BYTES:
        raise RtpError("RTP payload too large")
    b0 = RTP_VERSION << 6
    b1 = (0x80 if packet.marker else 0) | packet.payload_type
    return _RTP_HEADER.pack(b0, b1, packet.sequence, packet.timestamp, packet.ssrc) + packet.payload


def parse_packet(data: bytes) -> RtpPacket:
    if len(data) < RTP_HEADER_SIZE:
        raise RtpError("RTP packet too short")
    b0, b1, sequence, timestamp, ssrc = _RTP_HEADER.unpack_from(data)
    version = b0 >> 6
    if version != RTP_VERSION:
        raise RtpError(f"unsupported RTP version {version}")
    has_padding = bool(b0 & 0x20)
    has_extension = bool(b0 & 0x10)
    csrc_count = b0 & 0x0F
    off = RTP_HEADER_SIZE + csrc_count * 4
    if has_extension:
        if len(data) < off + 4:
            raise RtpError("RTP extension header truncated")
        ext_words = struct.unpack_from("!H", data, off + 2)[0]
        off += 4 + ext_words * 4
    if len(data) < off:
        raise RtpError("RTP CSRC/extension truncated")
    end = len(data)
    if has_padding:
        pad = data[-1]
        if pad == 0 or pad > len(data) - off:
            raise RtpError("invalid RTP padding")
        end -= pad
    payload = data[off:end]
    if len(payload) > MAX_RTP_RECEIVE_PAYLOAD_BYTES:
        raise RtpError("RTP payload too large")
    return RtpPacket(
        payload_type=b1 & 0x7F,
        marker=bool(b1 & 0x80),
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        payload=payload,
    )


def next_sequence(sequence: int) -> int:
    return (int(sequence) + 1) & 0xFFFF


def next_timestamp(timestamp: int, samples_per_frame: int) -> int:
    return (int(timestamp) + int(samples_per_frame)) & 0xFFFFFFFF
