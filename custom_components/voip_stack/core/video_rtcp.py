"""Small RTCP feedback profile used by SIP video."""

from __future__ import annotations

from dataclasses import dataclass
import struct


class RtcpError(ValueError):
    """Malformed or unsupported RTCP packet."""


@dataclass(frozen=True, slots=True)
class RtcpPacket:
    packet_type: int
    fmt: int
    payload: bytes


def parse_compound(data: bytes) -> list[RtcpPacket]:
    """Parse the common headers of one RTCP compound datagram."""

    if not data or len(data) % 4:
        raise RtcpError("RTCP compound packet must be non-empty and word aligned")
    packets: list[RtcpPacket] = []
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise RtcpError("truncated RTCP header")
        first, packet_type, length = struct.unpack_from("!BBH", data, offset)
        if first >> 6 != 2:
            raise RtcpError("unsupported RTCP version")
        size = (int(length) + 1) * 4
        if size < 4 or offset + size > len(data):
            raise RtcpError("truncated RTCP packet")
        packet_end = offset + size
        payload = data[offset + 4 : packet_end]
        if first & 0x20:
            if packet_end != len(data):
                raise RtcpError("only the final RTCP packet may contain padding")
            if not payload:
                raise RtcpError("RTCP padding bit set without padding")
            padding = int(payload[-1])
            if not padding or padding > len(payload):
                raise RtcpError("invalid RTCP padding length")
            payload = payload[:-padding]
        fmt = first & 0x1F
        _validate_feedback_payload(packet_type, fmt, payload)
        packets.append(RtcpPacket(packet_type, fmt, payload))
        offset = packet_end
    return packets


def _validate_feedback_payload(packet_type: int, fmt: int, payload: bytes) -> None:
    """Validate the PSFB layouts consumed by the video media path."""

    if packet_type != 206:
        return
    if fmt == 1:
        # RFC 4585 section 6.3.1 defines PLI with an empty FCI. The payload is
        # therefore exactly the sender and media-source SSRC pair.
        if len(payload) != 8:
            raise RtcpError("invalid RTCP PLI payload length")
        return
    if fmt != 4:
        return
    # RFC 5104 section 4.3.1: FIR has a zero media-source SSRC followed by one
    # or more eight-octet FCI entries. Reserved FCI octets are deliberately
    # ignored by receivers as required by that section.
    if len(payload) < 16 or (len(payload) - 8) % 8:
        raise RtcpError("invalid RTCP FIR payload length")
    if struct.unpack_from("!I", payload, 4)[0] != 0:
        raise RtcpError("RTCP FIR media-source SSRC must be zero")


def _header(fmt: int, packet_type: int, payload: bytes) -> bytes:
    if len(payload) % 4:
        raise RtcpError("RTCP payload must be word aligned")
    return struct.pack("!BBH", 0x80 | (fmt & 0x1F), packet_type, len(payload) // 4) + payload


def build_pli(sender_ssrc: int, media_ssrc: int) -> bytes:
    """Build RFC 4585 Picture Loss Indication feedback."""

    return _header(1, 206, struct.pack("!II", sender_ssrc & 0xFFFFFFFF, media_ssrc & 0xFFFFFFFF))


def build_fir(sender_ssrc: int, media_ssrc: int, sequence: int) -> bytes:
    """Build RFC 5104 Full Intra Request feedback."""

    payload = struct.pack(
        "!III4B",
        sender_ssrc & 0xFFFFFFFF,
        0,
        media_ssrc & 0xFFFFFFFF,
        sequence & 0xFF,
        0,
        0,
        0,
    )
    return _header(4, 206, payload)


def build_receiver_report(
    sender_ssrc: int,
    media_ssrc: int,
    *,
    fraction_lost: int = 0,
    cumulative_lost: int = 0,
    highest_sequence: int = 0,
    jitter: int = 0,
) -> bytes:
    """Build one RFC 3550 receiver report block without sender timing data."""

    block = _receiver_report_block(
        media_ssrc,
        fraction_lost=fraction_lost,
        cumulative_lost=cumulative_lost,
        highest_sequence=highest_sequence,
        jitter=jitter,
    )
    return _header(1, 201, struct.pack("!I", sender_ssrc & 0xFFFFFFFF) + block)


def _receiver_report_block(
    media_ssrc: int,
    *,
    fraction_lost: int = 0,
    cumulative_lost: int = 0,
    highest_sequence: int = 0,
    jitter: int = 0,
) -> bytes:
    """Build the report block shared by receiver and sender reports."""

    cumulative = max(-0x800000, min(0x7FFFFF, int(cumulative_lost))) & 0xFFFFFF
    lost_word = ((int(fraction_lost) & 0xFF) << 24) | cumulative
    return struct.pack(
        "!IIIIII",
        media_ssrc & 0xFFFFFFFF,
        lost_word,
        highest_sequence & 0xFFFFFFFF,
        jitter & 0xFFFFFFFF,
        0,
        0,
    )


def build_sender_report(
    sender_ssrc: int,
    media_ssrc: int | None,
    *,
    ntp_seconds: int,
    ntp_fraction: int,
    rtp_timestamp: int,
    packet_count: int,
    octet_count: int,
    fraction_lost: int = 0,
    cumulative_lost: int = 0,
    highest_sequence: int = 0,
    jitter: int = 0,
) -> bytes:
    """Build an RFC 3550 sender report with zero or one reception block."""

    sender_info = struct.pack(
        "!IIIIII",
        sender_ssrc & 0xFFFFFFFF,
        ntp_seconds & 0xFFFFFFFF,
        ntp_fraction & 0xFFFFFFFF,
        rtp_timestamp & 0xFFFFFFFF,
        packet_count & 0xFFFFFFFF,
        octet_count & 0xFFFFFFFF,
    )
    if media_ssrc is None:
        return _header(0, 200, sender_info)
    block = _receiver_report_block(
        media_ssrc,
        fraction_lost=fraction_lost,
        cumulative_lost=cumulative_lost,
        highest_sequence=highest_sequence,
        jitter=jitter,
    )
    return _header(1, 200, sender_info + block)


def build_sdes_cname(sender_ssrc: int, cname: str) -> bytes:
    """Build the mandatory SDES CNAME chunk for a compound RTCP packet."""

    encoded = str(cname).encode("utf-8")
    if not encoded or len(encoded) > 255:
        raise RtcpError("RTCP CNAME must contain between 1 and 255 bytes")
    payload = bytearray(struct.pack("!I", sender_ssrc & 0xFFFFFFFF))
    payload.extend((1, len(encoded)))
    payload.extend(encoded)
    payload.append(0)
    payload.extend(b"\x00" * (-len(payload) % 4))
    return _header(1, 202, bytes(payload))


def build_receiver_compound(
    sender_ssrc: int,
    media_ssrc: int,
    *,
    fraction_lost: int = 0,
    cumulative_lost: int = 0,
    highest_sequence: int = 0,
    jitter: int = 0,
    feedback: bytes = b"",
) -> bytes:
    """Build an RFC 3550 compound RR/SDES packet with optional AVPF feedback."""

    report = build_receiver_report(
        sender_ssrc,
        media_ssrc,
        fraction_lost=fraction_lost,
        cumulative_lost=cumulative_lost,
        highest_sequence=highest_sequence,
        jitter=jitter,
    )
    cname = build_sdes_cname(sender_ssrc, f"voip-stack-{sender_ssrc & 0xFFFFFFFF:08x}")
    if feedback:
        parse_compound(feedback)
    return report + cname + feedback


def build_sender_compound(
    sender_ssrc: int,
    media_ssrc: int | None,
    *,
    ntp_seconds: int,
    ntp_fraction: int,
    rtp_timestamp: int,
    packet_count: int,
    octet_count: int,
    fraction_lost: int = 0,
    cumulative_lost: int = 0,
    highest_sequence: int = 0,
    jitter: int = 0,
) -> bytes:
    """Build an RFC 3550 compound SR/SDES packet for an active sender."""

    report = build_sender_report(
        sender_ssrc,
        media_ssrc,
        ntp_seconds=ntp_seconds,
        ntp_fraction=ntp_fraction,
        rtp_timestamp=rtp_timestamp,
        packet_count=packet_count,
        octet_count=octet_count,
        fraction_lost=fraction_lost,
        cumulative_lost=cumulative_lost,
        highest_sequence=highest_sequence,
        jitter=jitter,
    )
    return report + build_sdes_cname(
        sender_ssrc, f"voip-stack-{sender_ssrc & 0xFFFFFFFF:08x}"
    )
