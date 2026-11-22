"""RTP v2 packet helpers for PCM audio."""

from __future__ import annotations

import math

from dataclasses import dataclass
import secrets
import struct
from typing import Protocol

from .media.codecs import CODECS, OCTETS_PER_SAMPLE


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


@dataclass(slots=True)
class AudioRtpSenderState:
    """RTP audio source identity owned by one SIP call, not one WebSocket."""

    sequence: int
    timestamp: int
    ssrc: int
    clock_rate: int = 0

    def use_clock_rate(self, clock_rate: int) -> bool:
        return use_audio_clock(self, clock_rate)

    @classmethod
    def create(cls, *, ssrc: int = 0) -> "AudioRtpSenderState":
        return cls(
            sequence=secrets.randbelow(0x10000),
            timestamp=secrets.randbelow(0x100000000),
            ssrc=int(ssrc) & 0xFFFFFFFF or secrets.randbelow(0xFFFFFFFF) + 1,
        )


def use_audio_clock(source, clock_rate: int) -> bool:
    """Use a new SSRC when switching RTP clocks, as recommended by RFC 7160.

    Audio sources here do not emit RTCP. Each call owns its sequence/timestamp
    state; neither another leg's PT nor its RTP source identity is copied.
    """
    changed = bool(source.clock_rate and source.clock_rate != clock_rate)
    if changed:
        fresh = AudioRtpSenderState.create()
        if fresh.ssrc == source.ssrc:
            fresh.ssrc = (source.ssrc + 1) & 0xFFFFFFFF or 1
        source.sequence, source.timestamp, source.ssrc = fresh.sequence, fresh.timestamp, fresh.ssrc
    source.clock_rate = clock_rate
    return changed


def audio_payload_size_limit(fmt: _AudioRtpFormat) -> int:
    """Return the receive limit for one negotiated audio RTP payload."""

    try:
        encoding = str(fmt.encoding).strip().upper()
        sample_rate = int(fmt.sample_rate)
        channels = int(fmt.channels)
        frame_ms = float(fmt.frame_ms)
        max_frame_ms = float(getattr(fmt, "max_frame_ms", 0) or 0)
    except (AttributeError, TypeError, ValueError) as err:
        raise RtpError("invalid RTP audio format") from err
    if sample_rate <= 0 or channels <= 0 or (frame_ms <= 0 and encoding != "CN"):
        raise RtpError("invalid RTP audio format")
    # ptime is a preference; an explicit maxptime bounds accepted packet size.
    # Retain the global RTP payload limit and the strict default without it.
    frame_ms = max(frame_ms, max_frame_ms)

    spec = CODECS.get(encoding)
    if encoding == "CN":
        from .comfort_noise import MAX_CN_PAYLOAD_BYTES

        codec_limit = MAX_CN_PAYLOAD_BYTES
    elif spec is not None and spec.frame_duration_ms and spec.max_frame_octets:
        frames = frame_ms / spec.frame_duration_ms
        if not math.isclose(frames, round(frames), abs_tol=1e-8):
            raise RtpError("RTP audio ptime does not contain whole codec frames")
        codec_limit = round(frames) * spec.max_frame_octets * channels
    elif encoding == "OPUS":
        # RFC 6716 permits multiple same-duration frames in one Opus packet.
        # Bound the maximum frame count from negotiated ptime and include the
        # worst-case two-byte VBR length field per frame.
        frame_us = frame_ms * 1000
        frame_count = max(1, (frame_us + _MIN_OPUS_FRAME_US - 1) // _MIN_OPUS_FRAME_US)
        codec_limit = frame_count * (_MAX_OPUS_FRAME_BYTES + 2)
    else:
        bytes_per_sample = OCTETS_PER_SAMPLE.get(encoding)
        if bytes_per_sample is None:
            raise RtpError(f"unsupported RTP audio encoding {encoding or '<empty>'}")
        sample_numerator = sample_rate * frame_ms
        if not math.isclose(sample_numerator / 1000, round(sample_numerator / 1000), abs_tol=1e-8):
            raise RtpError("RTP audio ptime does not produce whole samples")
        if spec is not None and round(sample_numerator / 1000) % spec.sample_quantum:
            raise RtpError("RTP audio ptime ends inside a codeword/octet quantum")
        codec_limit = round(sample_numerator / 1000) * channels * bytes_per_sample
    return int(min(codec_limit, MAX_AUDIO_RTP_PAYLOAD_BYTES))


def validate_audio_payload_size(payload: bytes, fmt: _AudioRtpFormat) -> None:
    """Reject an audio payload that exceeds its negotiated codec envelope."""

    limit = audio_payload_size_limit(fmt)
    if len(payload) > limit:
        raise RtpError(
            f"RTP audio payload too large for {fmt.encoding}/{fmt.sample_rate}/"
            f"{fmt.channels}/{fmt.frame_ms}ms: {len(payload)} bytes; max is {limit}"
        )
    if fmt.encoding == "OPUS":
        duration = opus_packet_duration_ms(payload)
        maximum = float(getattr(fmt, "max_frame_ms", 0) or 120)
        if duration > maximum:
            raise RtpError(f"Opus packet duration {duration:g} ms exceeds maxptime {maximum:g} ms")
    spec = CODECS.get(fmt.encoding)
    if spec is not None and spec.pyav_decoder == "g726le":
        quantum_bytes = spec.sample_quantum * spec.bits_per_sample // 8
        if not payload or len(payload) % quantum_bytes:
            raise RtpError("RTP audio payload ends inside a codeword/octet quantum")
    elif fmt.encoding == "G723":
        from .g723_codec import g723_frames

        try:
            g723_frames(payload)
        except ValueError as error:
            raise RtpError(str(error)) from error
    elif fmt.encoding == "CN":
        if not payload or payload[0] & 0x80 or 255 in payload[1:]:
            raise RtpError("Invalid RFC 3389 comfort-noise payload")
    elif fmt.encoding in {"L16", "L24", "PCM", "PCMA", "PCMU", "G722"}:
        stride = (2 if fmt.encoding in {"L16", "PCM"} else 3 if fmt.encoding == "L24" else 1) * fmt.channels
        if not payload or len(payload) % stride:
            raise RtpError("RTP audio payload is empty or not sample-aligned")


def opus_packet_duration_ms(payload: bytes) -> float:
    """Validate RFC 6716 section 3 framing and return actual media duration."""
    if not payload:
        raise RtpError("empty Opus packet")
    config, code = payload[0] >> 3, payload[0] & 3
    frame_ms = ((10, 20, 40, 60)[config & 3] if config < 12 else
                (10, 20)[config & 1] if config < 16 else
                (2.5, 5, 10, 20)[config & 3])
    offset, end = 1, len(payload)

    def length():
        nonlocal offset
        if offset >= end:
            raise RtpError("truncated Opus frame length")
        first = payload[offset]
        offset += 1
        if first < 252:
            return first
        if offset >= end:
            raise RtpError("truncated Opus two-byte frame length")
        second = payload[offset]
        offset += 1
        return first + 4 * second

    if code == 0:
        sizes = [end - offset]
    elif code == 1:
        if (end - offset) % 2:
            raise RtpError("unequal Opus CBR frame lengths")
        sizes = [(end - offset) // 2] * 2
    elif code == 2:
        first = length()
        sizes = [first, end - offset - first]
    else:
        if offset >= end:
            raise RtpError("missing Opus frame count")
        control = payload[offset]
        offset += 1
        count = control & 63
        if not 1 <= count <= 48 or count * frame_ms > 120:
            raise RtpError("invalid Opus frame count or duration")
        padding = 0
        if control & 64:
            while True:
                if offset >= end:
                    raise RtpError("truncated Opus padding length")
                value = payload[offset]
                offset += 1
                padding += 254 if value == 255 else value
                if value != 255:
                    break
            end -= padding
            if end < offset:
                raise RtpError("Opus padding exceeds packet length")
        if control & 128:
            sizes = [length() for _ in range(count - 1)]
            sizes.append(end - offset - sum(sizes))
        else:
            if (end - offset) % count:
                raise RtpError("unequal Opus CBR frame lengths")
            sizes = [(end - offset) // count] * count
    if any(size < 0 or size > _MAX_OPUS_FRAME_BYTES for size in sizes):
        raise RtpError("invalid Opus frame length")
    duration = len(sizes) * frame_ms
    if duration > 120:
        raise RtpError("Opus packet duration exceeds 120 ms")
    return duration


def audio_payload_duration_ms(payload, fmt) -> float:
    """Media duration from validated encoded data, independent of preferred ptime."""
    if fmt.encoding == "OPUS":
        return opus_packet_duration_ms(payload)
    spec = CODECS.get(fmt.encoding)
    if spec is not None and spec.octets_per_sample:
        return len(payload) * 1000 / (spec.octets_per_sample * fmt.channels * fmt.sample_rate)
    return fmt.frame_ms


class RtpReceiveState:
    """Bound duplicate history and validate a source change with two packets.

    Callers validate the negotiated origin, payload mapping and media envelope
    before admission. A source change needs consecutive sequence numbers;
    retired sources cannot displace the current source with delayed packets.
    """

    def __init__(self, *, window: int = 128) -> None:
        self.ssrc: int | None = None
        self.highest: int | None = None
        self._seen: set[int] = set()
        self._retired: set[int] = set()
        self._candidate: tuple[int, int] | None = None
        self._restart: int | None = None
        self.window = window
        self.changed = False

    def admit(self, packet: RtpPacket) -> bool:
        self.changed = False
        if self.ssrc is None:
            self.ssrc = packet.ssrc
        elif packet.ssrc != self.ssrc:
            if packet.ssrc in self._retired:
                return False
            if self._candidate != (packet.ssrc, (packet.sequence - 1) & 65535):
                self._candidate = (packet.ssrc, packet.sequence)
                return False
            self._retired.add(self.ssrc)
            if len(self._retired) > 8:
                self._retired.pop()
            self.ssrc = packet.ssrc
            self.highest = None
            self._seen.clear()
            self._candidate = None
            self.changed = True
        sequence = packet.sequence
        extended = sequence if self.highest is None else self.highest + (
            (sequence - self.highest + 32768) % 65536 - 32768)
        if self.highest is not None and abs(extended - self.highest) > 3000:
            if self._restart != sequence:
                self._restart = (sequence + 1) & 65535
                return False
            self.highest = extended
            self._seen.clear()
            self._restart = None
            self.changed = True
        if extended in self._seen or (self.highest is not None and extended <= self.highest - self.window):
            return False
        self.highest = max(extended, self.highest if self.highest is not None else extended)
        self._seen.add(extended)
        self._seen = {value for value in self._seen if value > self.highest - self.window}
        return True

    @property
    def probation_ssrc(self):
        return self._candidate[0] if self._candidate is not None else None


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
