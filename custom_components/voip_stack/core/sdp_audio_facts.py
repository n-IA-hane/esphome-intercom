"""Neutral SDP audio facts, mapping helpers and telephone-event parsing.

This layer reads advertised wire facts without choosing a media contract. It
must stay independent of the negotiator and the offer/answer adapters.
"""

from __future__ import annotations

from .audio_format import UDP_SAFE_PAYLOAD_BYTES, AudioFormat, PcmFormat
from .media.codecs import CODECS
from .sdp_parser import STATIC_RTPMAP as _STATIC_RTPMAP, parse_sdp
from .sdp_parser import parse_packet_time as _packet_time
from .sdp_types import RtpDtmfFormat, RtpPcmFormat, SdpError


def parse_rtp_audio_capability(token: str, payload_type: int = 96) -> RtpPcmFormat:
    """Parse one codec/rate/channels/ptime SIP capability token."""
    parts = [part.strip() for part in str(token or "").split("/")]
    if len(parts) != 4:
        raise SdpError(f"invalid RTP audio capability {token!r}")
    encoding = parts[0].upper()
    if encoding not in CODECS or CODECS[encoding].auxiliary:
        raise SdpError(f"unsupported RTP audio encoding {encoding!r}")
    try:
        rate, channels = map(int, parts[1:3])
        frame_ms = _packet_time(parts[3])
    except ValueError as err:
        raise SdpError(f"invalid RTP audio capability {token!r}") from err
    fmt = RtpPcmFormat(int(payload_type), encoding, rate, channels, frame_ms)
    if encoding == "OPUS" and (rate != 48000 or channels != 2):
        raise SdpError("Opus SDP capability must use opus/48000/2")
    try:
        if frame_ms <= 0:
            raise ValueError("packet time must be positive")
        fmt.audio_format
    except ValueError as err:
        raise SdpError(f"unrepresentable RTP audio capability {token!r}") from err
    return fmt


def _pcm_wire_bytes(fmt: AudioFormat) -> int:
    width = (
        3
        if fmt.pcm_format in (PcmFormat.S24LE, PcmFormat.S24LE_IN_S32)
        else fmt.container_bytes_per_sample
    )
    return fmt.nominal_frame_samples * fmt.channels * width


def audio_format_to_rtp(fmt: AudioFormat, payload_type: int) -> RtpPcmFormat:
    if not 96 <= int(payload_type) <= 127:
        raise SdpError("PCM format construction requires dynamic RTP payload types 96-127")
    if _pcm_wire_bytes(fmt) > UDP_SAFE_PAYLOAD_BYTES:
        raise SdpError(
            f"RTP PCM transmit frame exceeds payload budget: "
            f"{fmt.wire_token()} is {fmt.nominal_frame_bytes} bytes; "
            f"max is {UDP_SAFE_PAYLOAD_BYTES}"
        )
    if fmt.pcm_format == PcmFormat.S16LE:
        encoding = "L16"
    elif fmt.pcm_format in (PcmFormat.S24LE, PcmFormat.S24LE_IN_S32):
        encoding = "L24"
    else:
        raise SdpError(f"{fmt.pcm_format.value} has no RTP PCM mapping")
    return RtpPcmFormat(
        int(payload_type), encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms
    )


def is_rtp_pcm_mappable(fmt: AudioFormat) -> bool:
    if _pcm_wire_bytes(fmt) > UDP_SAFE_PAYLOAD_BYTES:
        return False
    return fmt.pcm_format in {PcmFormat.S16LE, PcmFormat.S24LE, PcmFormat.S24LE_IN_S32}


def _rtp_contract_key(fmt: RtpPcmFormat) -> tuple[int, str, int, int, int]:
    return (
        fmt.payload_type,
        fmt.encoding,
        fmt.sample_rate,
        fmt.channels,
        fmt.frame_ms,
    )


def _rtp_encoding_key(fmt: RtpPcmFormat) -> tuple[str, int, int]:
    """Identify one RTP encoding independently of packetization time."""

    return (fmt.encoding.upper(), fmt.sample_rate, fmt.channels)


def _dedupe_rtp_offer_encodings(
    formats: list[RtpPcmFormat] | tuple[RtpPcmFormat, ...],
) -> list[RtpPcmFormat]:
    """Keep the preferred packetization for each advertised RTP encoding."""

    seen: set[tuple[str, int, int]] = set()
    out: list[RtpPcmFormat] = []
    for fmt in formats:
        key = _rtp_encoding_key(fmt)
        if key in seen:
            continue
        seen.add(key)
        out.append(fmt)
    return out


def _same_rtp_audio_codec(left: RtpPcmFormat, right: RtpPcmFormat) -> bool:
    """Return whether two directional payloads identify the same codec."""

    return bool(
        left.encoding == right.encoding
        and left.sample_rate == right.sample_rate
        and left.channels == right.channels
    )


def offered_pcm_formats(
    sdp: str | bytes,
    *,
    allow_dahua_pcm: bool = False,
    capabilities: bool = False,
    include_auxiliary: bool = False,
) -> list[RtpPcmFormat]:
    parsed = parse_sdp(sdp, capabilities=capabilities)
    out: list[RtpPcmFormat] = []
    for pt in parsed["payload_order"]:
        spec = _offered_audio_mapping(parsed, pt)
        if spec is None:
            continue
        encoding, rate, channels = spec
        if encoding == "PCM":
            if (
                not allow_dahua_pcm
                or pt < 96
                or rate != 16000
                or channels != 1
            ):
                continue
        elif encoding not in CODECS or (CODECS[encoding].auxiliary and not include_auxiliary):
            continue
        out.append(
            RtpPcmFormat(
                pt,
                encoding,
                rate,
                channels,
                20 if encoding == "PCM" and not parsed["ptime"] else parsed["ptime"],
                parsed["minptime"],
                parsed["maxptime"],
                str(parsed["fmtp"].get(pt) or ""),
            )
        )
    return out


def _dtmf_events_from_fmtp(value: str) -> frozenset[int]:
    """Parse the RFC 4733 event list, defaulting to the DTMF range."""

    if not str(value or "").strip():
        return frozenset(range(16))
    events: set[int] = set()
    try:
        for item in str(value).split(","):
            bounds = item.strip().split("-", 1)
            start = int(bounds[0])
            end = int(bounds[-1])
            if not 0 <= start <= end <= 255:
                return frozenset()
            events.update(range(start, end + 1))
    except ValueError:
        return frozenset()
    return frozenset(events)


def _format_dtmf_events(events: frozenset[int]) -> str:
    """Render a compact RFC 4733 event list for SDP fmtp."""

    values = sorted(int(event) for event in events if 0 <= int(event) <= 255)
    if not values:
        return ""
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def offered_dtmf_formats(sdp: str | bytes) -> list[RtpDtmfFormat]:
    parsed = parse_sdp(sdp)
    out: list[RtpDtmfFormat] = []
    for pt in parsed["payload_order"]:
        spec = _offered_audio_mapping(parsed, pt)
        if spec is None:
            continue
        encoding, rate, _channels = spec
        if encoding == "TELEPHONE-EVENT":
            out.append(
                RtpDtmfFormat(
                    pt,
                    rate,
                    _dtmf_events_from_fmtp(parsed["fmtp"].get(pt, "")),
                )
            )
    return out


def first_offered_dtmf_format(
    sdp: str | bytes,
) -> RtpDtmfFormat | None:
    """Return the first telephone-event format in remote preference order."""

    formats = offered_dtmf_formats(sdp)
    return formats[0] if formats else None


def dtmf_format_for_clock(remote_sdp: str | bytes, clock_rate: int) -> RtpDtmfFormat | None:
    """P5: prefer the active audio clock, then a negotiated 8000 Hz alternative."""
    formats = offered_dtmf_formats(remote_sdp)
    for clock in dict.fromkeys((clock_rate, 8000)):
        for fmt in formats:
            if fmt.sample_rate == clock and fmt.events:
                return fmt
    return None


def offered_media_descriptions(sdp: str | bytes) -> list[str]:
    parsed = parse_sdp(sdp)
    out: list[str] = []
    for pt in parsed["payload_order"]:
        spec = _offered_audio_mapping(parsed, pt)
        if spec is None:
            out.append(f"pt={pt}")
            continue
        encoding, rate, channels = spec
        suffix = f"/{channels}" if channels != 1 else ""
        out.append(f"pt={pt}:{encoding}/{rate}{suffix}")
    if parsed["ptime"]:
        out.append(f"ptime={parsed['ptime']}")
    return out


def _offered_audio_mapping(parsed: dict, payload_type: int):
    """Resolve one RFC 3551 payload mapping without accepting PT remaps."""

    payload_type = int(payload_type)
    explicit = parsed["rtpmap"].get(payload_type)
    static = _STATIC_RTPMAP.get(payload_type)
    if payload_type < 96:
        if static is None or (explicit is not None and explicit != static):
            return None
        return static
    return explicit
