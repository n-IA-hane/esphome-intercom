"""Bounded RTP video packetization for the HA video phone."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import struct
from typing import Generic, TypeVar

from . import rtp


ANNEX_B_START_CODE = b"\x00\x00\x00\x01"
DEFAULT_MAX_RTP_PAYLOAD = 1200
MAX_ACCESS_UNIT_BYTES = 4 * 1024 * 1024
MAX_NAL_UNIT_BYTES = 2 * 1024 * 1024
MAX_ACCESS_UNIT_NALS = 512
MAX_ACCESS_UNIT_FRAGMENTS = 4096
DEFAULT_REORDER_DELAY = 0.020
DEFAULT_REORDER_PACKETS = 128
_DYNAMIC_RTP_PAYLOAD_TYPES = tuple(range(127, 95, -1))
_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)

_T = TypeVar("_T")


class H264RtpError(ValueError):
    """Malformed or unsupported RFC 6184 payload."""


def jpeg_dimensions(access_unit: bytes) -> tuple[int, int]:
    """Return bounded JPEG geometry without asking a decoder to allocate."""

    data = bytes(access_unit)
    if (
        len(data) < 4
        or not data.startswith(b"\xff\xd8")
        or not data.endswith(b"\xff\xd9")
    ):
        raise ValueError("invalid JPEG access unit")
    position = 2
    while position < len(data) - 1:
        if data[position] != 0xFF:
            raise ValueError("invalid JPEG marker alignment")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = int(data[position])
        position += 1
        if marker in {0x00, 0xD9, 0xDA}:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            raise ValueError("truncated JPEG segment")
        segment_length = int.from_bytes(data[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            raise ValueError("invalid JPEG segment length")
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 8:
                raise ValueError("truncated JPEG frame header")
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            if width <= 0 or height <= 0:
                raise ValueError("invalid JPEG dimensions")
            return width, height
        position += segment_length
    raise ValueError("JPEG access unit has no dimensions")


@dataclass(frozen=True, slots=True)
class H264AccessUnit:
    """One Annex B access unit reconstructed from one RTP timestamp."""

    data: bytes
    timestamp: int
    key_frame: bool


@dataclass(frozen=True, slots=True)
class VideoAccessUnit:
    """One codec access unit ready for the browser or a transcoder."""

    data: bytes
    timestamp: int
    key_frame: bool
    encoding: str


@dataclass(slots=True)
class RtpTimestampClock:
    """Map browser timestamps and RTP keepalives onto one continuous clock."""

    clock_rate: int
    origin_timestamp: int
    origin_time: float
    _browser_source_base: int | None = None
    _browser_clock_base: int | None = None

    def current(self, now: float) -> int:
        elapsed = max(0.0, float(now) - float(self.origin_time))
        return (
            int(self.origin_timestamp) + round(elapsed * int(self.clock_rate))
        ) & 0xFFFFFFFF

    def map_browser(self, source_timestamp: int, now: float) -> int:
        source = int(source_timestamp) & 0xFFFFFFFF
        if self._browser_source_base is None or self._browser_clock_base is None:
            self._browser_source_base = source
            self._browser_clock_base = self.current(now)
        delta = (source - self._browser_source_base) & 0xFFFFFFFF
        return (self._browser_clock_base + delta) & 0xFFFFFFFF

    def reset_browser(self) -> None:
        """Start a new browser capture epoch without resetting the RTP clock."""

        self._browser_source_base = None
        self._browser_clock_base = None


@dataclass(slots=True)
class RtpSenderState:
    """Persistent RTP source identity across browser media handoffs."""

    sequence: int
    ssrc: int
    clock: RtpTimestampClock
    keepalives: int = 0

    @classmethod
    def create(cls, *, clock_rate: int, now: float) -> "RtpSenderState":
        return cls(
            sequence=secrets.randbelow(0x10000),
            ssrc=secrets.randbelow(0xFFFFFFFF) + 1,
            clock=RtpTimestampClock(
                clock_rate=int(clock_rate),
                origin_timestamp=secrets.randbelow(0x100000000),
                origin_time=float(now),
            ),
        )

    def build_keepalive(self, payload_type: int, *, now: float) -> bytes:
        """Build one RFC 6263 section 4.6 packet and advance the source."""

        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=int(payload_type),
                sequence=int(self.sequence),
                timestamp=self.clock.current(float(now)),
                ssrc=int(self.ssrc),
                payload=b"",
            )
        )
        self.sequence = rtp.next_sequence(self.sequence)
        self.keepalives += 1
        return packet


def unknown_dynamic_payload_type(
    payload_types: set[int] | tuple[int, ...],
) -> int | None:
    """Return a dynamic RTP payload type absent from the negotiated media."""

    negotiated = {int(payload_type) for payload_type in payload_types}
    return next(
        (
            payload_type
            for payload_type in _DYNAMIC_RTP_PAYLOAD_TYPES
            if payload_type not in negotiated
        ),
        None,
    )


@dataclass(slots=True)
class RtpExtendedSequenceTracker:
    """Track the RFC 3550 extended highest RTP sequence number."""

    _highest: int | None = None

    @property
    def highest(self) -> int:
        """Return the extended maximum suitable for an RTCP report block."""

        return 0 if self._highest is None else self._highest

    def observe(self, sequence: int) -> int:
        """Observe a packet sequence while preserving wrap-cycle history."""

        sequence = int(sequence) & 0xFFFF
        if self._highest is None:
            self._highest = sequence
            return self._highest

        highest_low = self._highest & 0xFFFF
        cycles = self._highest & ~0xFFFF
        if sequence < highest_low and highest_low - sequence > 0x8000:
            candidate = cycles + 0x10000 + sequence
        elif sequence > highest_low and sequence - highest_low > 0x8000:
            # A reordered packet from the preceding cycle must not advance the
            # extended maximum after the current cycle has wrapped.
            candidate = cycles - 0x10000 + sequence
        else:
            candidate = cycles + sequence
        if candidate > self._highest:
            self._highest = candidate
        return self._highest

    def reset(self) -> None:
        """Start a fresh RTP source generation."""

        self._highest = None


class RtpReorderBuffer(Generic[_T]):
    """Small RFC 3550 sequence reorder window with a bounded gap wait.

    The caller supplies monotonic time.  A missing packet delays later packets
    for at most ``max_delay``; after that the gap is counted and media moves on.
    No periodic polling task is required because ``next_deadline`` can be used
    as the timeout of the next queue read.
    """

    def __init__(
        self,
        *,
        max_delay: float = DEFAULT_REORDER_DELAY,
        max_packets: int = DEFAULT_REORDER_PACKETS,
    ) -> None:
        self.max_delay = max(0.0, float(max_delay))
        self.max_packets = max(2, int(max_packets))
        self.expected: int | None = None
        self._pending: dict[int, tuple[_T, float]] = {}
        self.duplicates = 0
        self.late = 0
        self.lost = 0
        self.reordered = 0
        self.max_depth = 0

    @staticmethod
    def _ahead(sequence: int, expected: int) -> int:
        return (int(sequence) - int(expected)) & 0xFFFF

    @property
    def next_deadline(self) -> float | None:
        if self.expected is None or not self._pending or self.expected in self._pending:
            return None
        return min(arrival for _item, arrival in self._pending.values()) + self.max_delay

    def push(self, sequence: int, item: _T, now: float) -> list[_T]:
        sequence &= 0xFFFF
        now = float(now)
        if self.expected is None:
            self.expected = sequence
        delta = self._ahead(sequence, self.expected)
        if delta >= 0x8000:
            self.late += 1
            return []
        if sequence in self._pending:
            self.duplicates += 1
            return []
        if delta:
            self.reordered += 1
        self._pending[sequence] = (item, now)
        self.max_depth = max(self.max_depth, len(self._pending))
        return self._drain(now, force=len(self._pending) > self.max_packets)

    def flush(self, now: float, *, force: bool = False) -> list[_T]:
        return self._drain(float(now), force=force)

    def _drain(self, now: float, *, force: bool) -> list[_T]:
        out: list[_T] = []
        while self.expected is not None and self._pending:
            current = self._pending.pop(self.expected, None)
            if current is not None:
                out.append(current[0])
                self.expected = rtp.next_sequence(self.expected)
                continue
            oldest = min(arrival for _item, arrival in self._pending.values())
            if not force and now - oldest < self.max_delay:
                break
            nearest = min(
                self._pending,
                key=lambda candidate: self._ahead(candidate, self.expected or 0),
            )
            skipped = self._ahead(nearest, self.expected)
            if skipped >= 0x8000:
                break
            self.lost += skipped
            self.expected = nearest
            force = len(self._pending) > self.max_packets
        return out

    def reset(self) -> None:
        self.expected = None
        self._pending.clear()


def split_annex_b(data: bytes) -> list[bytes]:
    """Split an Annex B byte stream into NAL units without start codes."""

    if not data:
        return []
    starts: list[tuple[int, int]] = []
    index = 0
    end = len(data)
    while index + 3 <= end:
        if data[index : index + 4] == ANNEX_B_START_CODE:
            starts.append((index, 4))
            index += 4
            continue
        if data[index : index + 3] == b"\x00\x00\x01":
            starts.append((index, 3))
            index += 3
            continue
        index += 1
    if not starts:
        raise H264RtpError("H.264 access unit is not Annex B")
    out: list[bytes] = []
    for position, (start, prefix_len) in enumerate(starts):
        nal_start = start + prefix_len
        nal_end = starts[position + 1][0] if position + 1 < len(starts) else end
        while nal_end > nal_start and data[nal_end - 1] == 0:
            nal_end -= 1
        nal = data[nal_start:nal_end]
        if nal:
            if len(nal) > MAX_NAL_UNIT_BYTES:
                raise H264RtpError("H.264 NAL unit exceeds safety limit")
            out.append(nal)
    if not out:
        raise H264RtpError("H.264 access unit has no NAL units")
    if len(out) > MAX_ACCESS_UNIT_NALS:
        raise H264RtpError("H.264 access unit has too many NAL units")
    return out


def join_annex_b(nal_units: list[bytes]) -> bytes:
    """Join validated NAL units as an Annex B access unit."""

    if not nal_units:
        raise H264RtpError("cannot build an empty H.264 access unit")
    total = sum(len(nal) + len(ANNEX_B_START_CODE) for nal in nal_units)
    if total > MAX_ACCESS_UNIT_BYTES:
        raise H264RtpError("H.264 access unit exceeds safety limit")
    return b"".join(ANNEX_B_START_CODE + nal for nal in nal_units)


class H264Depacketizer:
    """Reassemble RFC 6184 mode-1 packets into Annex B access units."""

    def __init__(self, parameter_sets: list[bytes] | None = None) -> None:
        self._timestamp: int | None = None
        self._expected_sequence: int | None = None
        self._nals: list[bytes] = []
        self._fu: bytearray | None = None
        self._damaged = False
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self.dropped_access_units = 0
        self.sequence_gaps = 0
        for nal in parameter_sets or []:
            if not nal:
                continue
            nal_type = nal[0] & 0x1F
            if nal_type == 7:
                self._sps = bytes(nal)
            elif nal_type == 8:
                self._pps = bytes(nal)

    def reset(self) -> None:
        self._timestamp = None
        self._expected_sequence = None
        self._nals.clear()
        self._fu = None
        self._damaged = False

    @property
    def parameter_sets(self) -> tuple[bytes, ...]:
        """Return the latest complete decoder bootstrap state.

        A browser may reconnect while the SIP dialog and RTP sender continue.
        Persisting these two small NAL units at call scope lets the replacement
        decoder join the next IDR without waiting for a peer to repeat SPS/PPS.
        """

        return tuple(item for item in (self._sps, self._pps) if item is not None)

    def push(self, packet: rtp.RtpPacket) -> H264AccessUnit | None:
        """Consume one packet and return an access unit on its marker."""

        if not packet.payload:
            self._damage()
            return self._finish(packet.timestamp) if packet.marker else None
        if self._timestamp is None:
            self._timestamp = packet.timestamp
        elif packet.timestamp != self._timestamp:
            if self._nals or self._fu is not None:
                self.dropped_access_units += 1
            self._timestamp = packet.timestamp
            self._expected_sequence = None
            self._nals.clear()
            self._fu = None
            self._damaged = False
        if self._expected_sequence is not None and packet.sequence != self._expected_sequence:
            self.sequence_gaps += 1
            self._damage()
        self._expected_sequence = rtp.next_sequence(packet.sequence)

        try:
            self._consume_payload(packet.payload)
        except H264RtpError:
            self._damage()
        return self._finish(packet.timestamp) if packet.marker else None

    def _damage(self) -> None:
        self._damaged = True
        self._fu = None

    def _append_nal(self, nal: bytes) -> None:
        if not nal or len(nal) > MAX_NAL_UNIT_BYTES:
            raise H264RtpError("invalid H.264 NAL unit size")
        if not 1 <= (nal[0] & 0x1F) <= 23:
            raise H264RtpError("invalid nested H.264 packetization NAL type")
        if len(self._nals) >= MAX_ACCESS_UNIT_NALS:
            raise H264RtpError("too many H.264 NAL units")
        self._nals.append(nal)
        if sum(len(item) + 4 for item in self._nals) > MAX_ACCESS_UNIT_BYTES:
            raise H264RtpError("H.264 access unit exceeds safety limit")

    def _consume_payload(self, payload: bytes) -> None:
        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            if self._fu is not None:
                raise H264RtpError("single NAL interrupted an FU-A")
            self._append_nal(payload)
            return
        if nal_type == 24:  # STAP-A
            if self._fu is not None:
                raise H264RtpError("STAP-A interrupted an FU-A")
            offset = 1
            appended = 0
            while offset < len(payload):
                if offset + 2 > len(payload):
                    raise H264RtpError("truncated STAP-A length")
                size = int.from_bytes(payload[offset : offset + 2], "big")
                offset += 2
                if size == 0 or offset + size > len(payload):
                    raise H264RtpError("invalid STAP-A NAL size")
                self._append_nal(payload[offset : offset + size])
                appended += 1
                offset += size
            if not appended:
                raise H264RtpError("empty STAP-A")
            return
        if nal_type == 28:  # FU-A
            if len(payload) < 3:
                raise H264RtpError("truncated FU-A")
            indicator = payload[0]
            header = payload[1]
            start = bool(header & 0x80)
            end = bool(header & 0x40)
            reconstructed_type = header & 0x1F
            if (
                not 1 <= reconstructed_type <= 23
                or header & 0x20
                or start and end
            ):
                raise H264RtpError("invalid FU-A header")
            fragment = payload[2:]
            if not fragment:
                raise H264RtpError("empty FU-A fragment")
            if start:
                if self._fu is not None:
                    raise H264RtpError("nested FU-A start")
                self._fu = bytearray(((indicator & 0xE0) | reconstructed_type,))
            elif self._fu is None:
                raise H264RtpError("FU-A continuation without start")
            assert self._fu is not None
            if self._fu[0] != ((indicator & 0xE0) | reconstructed_type):
                raise H264RtpError("FU-A NRI/type changed during fragmentation")
            self._fu.extend(fragment)
            if len(self._fu) > MAX_NAL_UNIT_BYTES:
                raise H264RtpError("FU-A NAL exceeds safety limit")
            if end:
                nal = bytes(self._fu)
                self._fu = None
                self._append_nal(nal)
            return
        raise H264RtpError(f"unsupported RFC 6184 NAL packet type {nal_type}")

    def _finish(self, timestamp: int) -> H264AccessUnit | None:
        nals = self._nals
        damaged = self._damaged or self._fu is not None or not nals
        self._timestamp = None
        self._expected_sequence = None
        self._nals = []
        self._fu = None
        self._damaged = False
        if damaged:
            self.dropped_access_units += 1
            return None
        received_nals = nals
        key_frame = any((nal[0] & 0x1F) == 5 for nal in received_nals)
        if key_frame:
            present = {nal[0] & 0x1F for nal in nals}
            prefix: list[bytes] = []
            if 7 not in present and self._sps is not None:
                prefix.append(self._sps)
            if 8 not in present and self._pps is not None:
                prefix.append(self._pps)
            nals = [*prefix, *nals]
        try:
            data = join_annex_b(nals)
        except H264RtpError:
            self.dropped_access_units += 1
            return None
        # Parameter sets belong to the persistent decoder bootstrap cache only
        # after their complete access unit has passed sequence, fragmentation,
        # size and Annex-B validation. A damaged RTP unit must not poison the
        # next IDR (or a replacement browser decoder) with partial SPS/PPS.
        for nal in received_nals:
            nal_type = nal[0] & 0x1F
            if nal_type == 7:
                self._sps = nal
            elif nal_type == 8:
                self._pps = nal
        return H264AccessUnit(data=data, timestamp=timestamp, key_frame=key_frame)


class Vp8Depacketizer:
    """Reassemble the VP8 payload descriptor defined by RFC 7741."""

    def __init__(self) -> None:
        self._timestamp: int | None = None
        self._expected_sequence: int | None = None
        self._parts: list[bytes] = []
        self._parts_bytes = 0
        self._started = False
        self._key_frame = False
        self._damaged = False
        self.dropped_access_units = 0
        self.sequence_gaps = 0

    def _reset_unit(self) -> None:
        self._parts = []
        self._parts_bytes = 0
        self._started = False
        self._damaged = False
        self._expected_sequence = None

    @staticmethod
    def _payload(payload: bytes) -> tuple[bytes, bool, int]:
        if not payload:
            raise ValueError("empty VP8 RTP payload")
        first = payload[0]
        extended = bool(first & 0x80)
        start = bool(first & 0x10)
        partition = first & 0x0F
        offset = 1
        if extended:
            if offset >= len(payload):
                raise ValueError("truncated VP8 extension")
            extension = payload[offset]
            offset += 1
            if extension & 0x80:  # PictureID
                if offset >= len(payload):
                    raise ValueError("truncated VP8 PictureID")
                wide = bool(payload[offset] & 0x80)
                offset += 2 if wide else 1
            if extension & 0x40:  # TL0PICIDX
                offset += 1
            if extension & 0x20 or extension & 0x10:  # TID / KEYIDX share a byte
                offset += 1
        if offset > len(payload):
            raise ValueError("truncated VP8 payload descriptor")
        return payload[offset:], start, partition

    def push(self, packet: rtp.RtpPacket) -> VideoAccessUnit | None:
        if self._timestamp is not None and packet.timestamp != self._timestamp:
            if self._parts:
                self.dropped_access_units += 1
            self._reset_unit()
        self._timestamp = packet.timestamp
        if self._expected_sequence is not None and packet.sequence != self._expected_sequence:
            self.sequence_gaps += 1
            self._damaged = True
        self._expected_sequence = rtp.next_sequence(packet.sequence)
        try:
            payload, start, partition = self._payload(packet.payload)
        except ValueError:
            self._reset_unit()
            self.dropped_access_units += 1
            return None
        if start and partition == 0:
            self._parts = []
            self._parts_bytes = 0
            self._started = True
            self._key_frame = bool(payload) and not bool(payload[0] & 0x01)
        if not self._started or not payload:
            if packet.marker:
                self.dropped_access_units += 1
            return None
        if (
            len(self._parts) >= MAX_ACCESS_UNIT_FRAGMENTS
            or self._parts_bytes + len(payload) > MAX_ACCESS_UNIT_BYTES
        ):
            self._reset_unit()
            self.dropped_access_units += 1
            return None
        self._parts.append(payload)
        self._parts_bytes += len(payload)
        if not packet.marker:
            return None
        damaged = self._damaged
        data = b"".join(self._parts)
        timestamp = int(self._timestamp)
        key_frame = self._key_frame
        self._parts = []
        self._parts_bytes = 0
        self._started = False
        self._damaged = False
        self._timestamp = None
        self._expected_sequence = None
        if damaged:
            self.dropped_access_units += 1
            return None
        return VideoAccessUnit(data, timestamp, key_frame, "VP8")


# Standard 8-bit JPEG tables from RFC 2435 appendices A and B.  Keeping the
# RTP/JPEG reconstruction here avoids a native decoder dependency: the browser
# receives an ordinary JFIF frame and decodes it with its image pipeline.
_JPEG_BASE_QUANTIZERS = bytes.fromhex(
    "100b0c0e0c0a100e0d0e1211101318281a181616183123251d283a333d3c3933"
    "383740485c4e404457453738506d51575f626768673e4d71797064785c656763"
    "1112121815182f1a1a2f63423842636363636363636363636363636363636363"
    "6363636363636363636363636363636363636363636363636363636363636363"
)
_JPEG_STANDARD_DHT = bytes.fromhex(
    "ffc401a20000010501010101010100000000000000000102030405060708090a0b"
    "0100030101010101010101010000000000000102030405060708090a0b10000201"
    "0303020403050504040000017d010203000411051221314106135161072271143281"
    "91a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738"
    "393a434445464748494a535455565758595a636465666768696a737475767778797a"
    "838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8"
    "b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2"
    "f3f4f5f6f7f8f9fa1100020102040403040705040400010277000102031104052131"
    "061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f1"
    "1718191a262728292a35363738393a434445464748494a535455565758595a636465"
    "666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9"
    "dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9fa"
)


@dataclass(frozen=True, slots=True)
class _RtpJpegFrame:
    """Baseline JPEG fields representable by RFC 2435 types 0/1."""

    scan: bytes
    quantizers: bytes
    width: int
    height: int
    jpeg_type: int
    dri: int = 0


def _jpeg_huffman_tables(segment: bytes) -> dict[int, bytes]:
    """Parse one DHT payload into complete table definitions."""

    tables: dict[int, bytes] = {}
    offset = 0
    while offset < len(segment):
        start = offset
        table_id = int(segment[offset])
        offset += 1
        if table_id not in {0x00, 0x01, 0x10, 0x11} or offset + 16 > len(segment):
            raise ValueError("unsupported JPEG Huffman table")
        value_count = sum(segment[offset : offset + 16])
        offset += 16
        if value_count <= 0 or offset + value_count > len(segment):
            raise ValueError("truncated JPEG Huffman table")
        offset += value_count
        if table_id in tables:
            raise ValueError("duplicate JPEG Huffman table")
        tables[table_id] = bytes(segment[start:offset])
    return tables


_JPEG_STANDARD_HUFFMAN_TABLES = _jpeg_huffman_tables(_JPEG_STANDARD_DHT[4:])


def _parse_jpeg_for_rtp(access_unit: bytes) -> _RtpJpegFrame:
    """Parse the allocation-bounded RFC 2435 subset of one complete JPEG."""

    data = bytes(access_unit)
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        raise ValueError("not a JPEG access unit")

    quantizers: dict[int, bytes] = {}
    huffman_tables: dict[int, bytes] = {}
    width = 0
    height = 0
    jpeg_type: int | None = None
    chroma_quantizer = 1
    dri = 0
    position = 2
    while position + 1 < len(data):
        if data[position] != 0xFF:
            raise ValueError("invalid JPEG marker alignment")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = int(data[position])
        position += 1
        if marker == 0xD9:
            break
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            raise ValueError("truncated JPEG segment")
        segment_length = int.from_bytes(data[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            raise ValueError("invalid JPEG segment length")
        segment = data[position + 2 : position + segment_length]

        if marker == 0xDB:
            offset = 0
            while offset < len(segment):
                descriptor = int(segment[offset])
                offset += 1
                precision = descriptor >> 4
                table_id = descriptor & 0x0F
                if precision != 0 or table_id not in {0, 1}:
                    raise ValueError("unsupported JPEG quantization table")
                if offset + 64 > len(segment) or table_id in quantizers:
                    raise ValueError("invalid JPEG quantization table")
                table = bytes(segment[offset : offset + 64])
                if 0 in table:
                    raise ValueError("invalid zero JPEG quantization coefficient")
                quantizers[table_id] = table
                offset += 64
        elif marker == 0xC4:
            for table_id, table in _jpeg_huffman_tables(segment).items():
                if table_id in huffman_tables:
                    raise ValueError("duplicate JPEG Huffman table")
                huffman_tables[table_id] = table
        elif marker == 0xC0:
            if (
                len(segment) != 15
                or segment[0] != 8
                or segment[5] != 3
                or segment[6] != 1
                or segment[8] != 0
                or segment[9] != 2
                or segment[10] != 0x11
                or segment[11] not in {0, 1}
                or segment[12] != 3
                or segment[13] != 0x11
                or segment[14] != segment[11]
            ):
                raise ValueError("unsupported JPEG component layout")
            chroma_quantizer = int(segment[11])
            height = int.from_bytes(segment[1:3], "big")
            width = int.from_bytes(segment[3:5], "big")
            if not 0 < width <= 2040 or not 0 < height <= 2040:
                raise ValueError("unsupported JPEG dimensions")
            if segment[7] == 0x21:
                jpeg_type = 0
            elif segment[7] == 0x22:
                jpeg_type = 1
            else:
                raise ValueError("unsupported JPEG chroma subsampling")
        elif marker == 0xDD:
            if len(segment) != 2:
                raise ValueError("invalid JPEG restart interval")
            dri = int.from_bytes(segment, "big")
            if dri == 0:
                raise ValueError("invalid JPEG restart interval")
        elif marker == 0xDA:
            if (
                jpeg_type is None
                or 0 not in quantizers
                or (chroma_quantizer == 1 and 1 not in quantizers)
                or (
                    chroma_quantizer == 0
                    and 1 in quantizers
                    and quantizers[1] != quantizers[0]
                )
                or len(segment) != 10
                or segment != b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
            ):
                raise ValueError("unsupported JPEG scan layout")
            if huffman_tables and huffman_tables != _JPEG_STANDARD_HUFFMAN_TABLES:
                raise ValueError("RFC 2435 requires standard JPEG Huffman tables")
            scan_start = position + segment_length
            scan_end = data.rfind(b"\xff\xd9", scan_start)
            if scan_end <= scan_start:
                raise ValueError("JPEG scan has no EOI marker")
            return _RtpJpegFrame(
                scan=data[scan_start:scan_end],
                quantizers=quantizers[0]
                + (
                    quantizers[1]
                    if chroma_quantizer == 1
                    else quantizers[0]
                ),
                width=width,
                height=height,
                jpeg_type=jpeg_type,
                dri=dri,
            )
        elif 0xC1 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            raise ValueError("progressive/lossless JPEG is not RFC 2435 type 0/1")

        position += segment_length
    raise ValueError("JPEG access unit has no supported baseline scan")


def _jpeg_quantizers(quality: int) -> bytes:
    if not 1 <= int(quality) <= 99:
        raise ValueError("reserved RFC 2435 JPEG quality")
    scale = 5000 // int(quality) if int(quality) < 50 else 200 - int(quality) * 2
    return bytes(max(1, min(255, (value * scale + 50) // 100)) for value in _JPEG_BASE_QUANTIZERS)


def _jpeg_interchange_header(
    *, jpeg_type: int, width_blocks: int, height_blocks: int, quantizers: bytes, dri: int
) -> bytes:
    if jpeg_type not in {0, 1} or not width_blocks or not height_blocks:
        raise ValueError("unsupported RFC 2435 JPEG geometry/type")
    if len(quantizers) != 128:
        raise ValueError("unsupported RFC 2435 quantization tables")
    width = int(width_blocks) << 3
    height = int(height_blocks) << 3
    out = bytearray(b"\xff\xd8")
    out.extend(b"\xff\xe0\x00\x10JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00")
    out.extend(b"\xff\xdb")
    out.extend(struct.pack("!H", 2 + (len(quantizers) // 64) * 65))
    for table_id, offset in enumerate(range(0, len(quantizers), 64)):
        out.append(table_id)
        out.extend(quantizers[offset : offset + 64])
    if dri:
        out.extend(b"\xff\xdd\x00\x04")
        out.extend(struct.pack("!H", int(dri)))
    out.extend(b"\xff\xc0\x00\x11\x08")
    out.extend(struct.pack("!HH", height, width))
    out.extend(
        bytes(
            (
                3,
                1, 0x22 if jpeg_type else 0x21, 0,
                2, 0x11, 1 if len(quantizers) >= 128 else 0,
                3, 0x11, 1 if len(quantizers) >= 128 else 0,
            )
        )
    )
    out.extend(_JPEG_STANDARD_DHT)
    out.extend(b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00")
    return bytes(out)


class JpegDepacketizer:
    """Reassemble RFC 2435 fragments into complete baseline JFIF frames."""

    def __init__(
        self,
        *,
        max_access_unit_bytes: int = MAX_ACCESS_UNIT_BYTES,
    ) -> None:
        limit = int(max_access_unit_bytes)
        if not 1 <= limit <= MAX_ACCESS_UNIT_BYTES:
            raise ValueError("invalid JPEG access-unit byte limit")
        self._max_access_unit_bytes = limit
        self._timestamp: int | None = None
        self._frame_header: tuple[int, int, int, int, int, int] | None = None
        self._header = b""
        self._scan = bytearray()
        self._dynamic_quantizers: dict[int, bytes] = {}
        self.dropped_access_units = 0

    def _drop(self) -> None:
        self._timestamp = None
        self._frame_header = None
        self._header = b""
        self._scan.clear()
        self.dropped_access_units += 1

    def push(self, packet: rtp.RtpPacket) -> VideoAccessUnit | None:
        payload = packet.payload
        if len(payload) < 8:
            self._drop()
            return None
        offset = int.from_bytes(payload[1:4], "big")
        type_specific = int(payload[0])
        jpeg_type_raw = int(payload[4])
        jpeg_type = jpeg_type_raw
        quality = int(payload[5])
        width = int(payload[6])
        height = int(payload[7])
        cursor = 8
        dri = 0
        if type_specific:
            # RFC 2435 type-specific values 1..3 describe interlaced fields;
            # emitting them as a progressive JFIF would corrupt geometry.
            self._drop()
            return None
        if jpeg_type & 0x40:
            if len(payload) < cursor + 4:
                self._drop()
                return None
            dri = int.from_bytes(payload[cursor : cursor + 2], "big")
            cursor += 4
            if not dri:
                self._drop()
                return None
            jpeg_type &= ~0x40
        if jpeg_type not in {0, 1}:
            self._drop()
            return None
        if offset == 0:
            try:
                table_len = 0
                if quality >= 128:
                    if len(payload) < cursor + 4:
                        raise ValueError("missing RFC 2435 quantization header")
                    if payload[cursor] != 0:
                        raise ValueError("invalid RFC 2435 quantization MBZ field")
                    precision = payload[cursor + 1]
                    table_len = int.from_bytes(payload[cursor + 2 : cursor + 4], "big")
                    cursor += 4
                    if precision or table_len not in {0, 64, 128}:
                        raise ValueError("unsupported RFC 2435 quantization header")
                    if table_len:
                        if len(payload) < cursor + table_len:
                            raise ValueError("truncated RFC 2435 quantization tables")
                        quantizers = bytes(payload[cursor : cursor + table_len])
                        cursor += table_len
                    else:
                        quantizers = self._dynamic_quantizers.get(quality, b"")
                        if not quantizers:
                            raise ValueError("unknown RFC 2435 quantization tables")
                else:
                    quantizers = _jpeg_quantizers(quality)
                # RFC 2435 types 0/1 require distinct luma/chroma matrices.
                # FFmpeg's RTP/JPEG muxer is also commonly deployed without
                # ``force_duplicated_matrix`` and then emits one table while
                # warning about the violation.  Tolerate that receive-side
                # quirk at this boundary, but keep every downstream helper and
                # every locally generated packet on the strict 128-byte form.
                if len(quantizers) == 64:
                    quantizers += quantizers
                if len(quantizers) != 128 or 0 in quantizers:
                    raise ValueError("invalid RFC 2435 quantization tables")
                if 128 <= quality < 255 and table_len:
                    cached_quantizers = self._dynamic_quantizers.get(quality)
                    if (
                        cached_quantizers is not None
                        and cached_quantizers != quantizers
                    ):
                        raise ValueError(
                            "changed static RFC 2435 quantization tables"
                        )
                    self._dynamic_quantizers.setdefault(quality, quantizers)
                self._header = _jpeg_interchange_header(
                    jpeg_type=jpeg_type,
                    width_blocks=width,
                    height_blocks=height,
                    quantizers=quantizers,
                    dri=dri,
                )
            except ValueError:
                self._drop()
                return None
            self._timestamp = packet.timestamp
            self._frame_header = (
                type_specific,
                jpeg_type_raw,
                quality,
                width,
                height,
                dri,
            )
            self._scan.clear()
        elif (
            self._timestamp != packet.timestamp
            or offset != len(self._scan)
            or self._frame_header
            != (type_specific, jpeg_type_raw, quality, width, height, dri)
        ):
            self._drop()
            return None
        frame_data = payload[cursor:]
        if (
            offset != len(self._scan)
            or len(self._header) + len(self._scan) + len(frame_data) + 2
            > self._max_access_unit_bytes
        ):
            self._drop()
            return None
        self._scan.extend(frame_data)
        if not packet.marker:
            return None
        if self._timestamp != packet.timestamp or not self._header or not self._scan:
            self._drop()
            return None
        data = self._header + bytes(self._scan) + b"\xff\xd9"
        timestamp = int(packet.timestamp)
        self._timestamp = None
        self._frame_header = None
        self._header = b""
        self._scan.clear()
        return VideoAccessUnit(data, timestamp, True, "JPEG")


def packetize_jpeg(
    access_unit: bytes,
    *,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
    max_payload: int = DEFAULT_MAX_RTP_PAYLOAD,
) -> list[rtp.RtpPacket]:
    """Packetize one baseline JPEG according to RFC 2435.

    The browser and ESPHome camera both emit complete ordinary JPEG images.
    Only the entropy scan is sent; quantization tables and geometry travel in
    the standard RTP/JPEG header so every SIP peer sees plain PT 26 media.
    """

    if int(payload_type) != 26:
        raise ValueError("RTP/JPEG uses static payload type 26")
    if not 145 <= int(max_payload) <= rtp.MAX_RTP_PAYLOAD_BYTES:
        raise ValueError("invalid RTP/JPEG payload limit")
    frame = _parse_jpeg_for_rtp(access_unit)
    width_blocks = (frame.width + 7) // 8
    height_blocks = (frame.height + 7) // 8
    current = int(sequence) & 0xFFFF
    offset = 0
    packets: list[rtp.RtpPacket] = []
    while offset < len(frame.scan):
        first = offset == 0
        jpeg_type = frame.jpeg_type | (0x40 if frame.dri else 0)
        header = bytearray(
            (
                0,
                (offset >> 16) & 0xFF,
                (offset >> 8) & 0xFF,
                offset & 0xFF,
                jpeg_type,
                255,
                width_blocks,
                height_blocks,
            )
        )
        if frame.dri:
            header.extend(struct.pack("!HH", frame.dri, 0xFFFF))
        if first:
            header.extend(struct.pack("!BBH", 0, 0, len(frame.quantizers)))
            header.extend(frame.quantizers)
        room = int(max_payload) - len(header)
        if room <= 0:
            raise ValueError("RTP/JPEG header exceeds payload limit")
        end = min(len(frame.scan), offset + room)
        header.extend(frame.scan[offset:end])
        packets.append(
            rtp.RtpPacket(
                payload_type=26,
                sequence=current,
                timestamp=int(timestamp) & 0xFFFFFFFF,
                ssrc=int(ssrc) & 0xFFFFFFFF,
                payload=bytes(header),
                marker=end == len(frame.scan),
            )
        )
        current = rtp.next_sequence(current)
        offset = end
    return packets


def packetize_vp8(
    access_unit: bytes,
    *,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
    max_payload: int = DEFAULT_MAX_RTP_PAYLOAD,
) -> list[rtp.RtpPacket]:
    """Packetize a VP8 frame using the minimal one-byte payload descriptor."""

    if not access_unit or max_payload < 2:
        raise ValueError("invalid VP8 access unit or payload limit")
    chunk_size = int(max_payload) - 1
    out: list[rtp.RtpPacket] = []
    current = int(sequence) & 0xFFFF
    for offset in range(0, len(access_unit), chunk_size):
        end = min(len(access_unit), offset + chunk_size)
        descriptor = 0x10 if offset == 0 else 0x00
        out.append(
            rtp.RtpPacket(
                payload_type=int(payload_type),
                sequence=current,
                timestamp=int(timestamp) & 0xFFFFFFFF,
                ssrc=int(ssrc) & 0xFFFFFFFF,
                payload=bytes((descriptor,)) + access_unit[offset:end],
                marker=end == len(access_unit),
            )
        )
        current = rtp.next_sequence(current)
    return out


def packetize_annex_b(
    access_unit: bytes,
    *,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
    max_payload: int = DEFAULT_MAX_RTP_PAYLOAD,
) -> list[rtp.RtpPacket]:
    """Packetize one Annex B access unit using single NAL and FU-A packets."""

    if not 3 <= int(max_payload) <= rtp.MAX_RTP_PAYLOAD_BYTES:
        raise H264RtpError("invalid H.264 RTP payload limit")
    nals = split_annex_b(access_unit)
    packets: list[rtp.RtpPacket] = []
    current_sequence = int(sequence) & 0xFFFF
    for nal in nals:
        nal_type = nal[0] & 0x1F
        if not 1 <= nal_type <= 23:
            raise H264RtpError("cannot packetize reserved or nested H.264 NAL type")
        if len(nal) <= max_payload:
            packets.append(
                rtp.RtpPacket(
                    payload_type=payload_type,
                    sequence=current_sequence,
                    timestamp=timestamp,
                    ssrc=ssrc,
                    payload=nal,
                )
            )
            current_sequence = rtp.next_sequence(current_sequence)
            continue
        nal_header = nal[0]
        fu_indicator = (nal_header & 0xE0) | 28
        fragment_size = max_payload - 2
        body = memoryview(nal)[1:]
        offset = 0
        while offset < len(body):
            end = min(len(body), offset + fragment_size)
            fu_header = nal_type
            if offset == 0:
                fu_header |= 0x80
            if end == len(body):
                fu_header |= 0x40
            packets.append(
                rtp.RtpPacket(
                    payload_type=payload_type,
                    sequence=current_sequence,
                    timestamp=timestamp,
                    ssrc=ssrc,
                    payload=bytes((fu_indicator, fu_header)) + bytes(body[offset:end]),
                )
            )
            current_sequence = rtp.next_sequence(current_sequence)
            offset = end
    if not packets:
        raise H264RtpError("H.264 access unit produced no RTP packets")
    last = packets[-1]
    packets[-1] = rtp.RtpPacket(
        payload_type=last.payload_type,
        sequence=last.sequence,
        timestamp=last.timestamp,
        ssrc=last.ssrc,
        payload=last.payload,
        marker=True,
    )
    return packets
