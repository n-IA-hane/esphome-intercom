"""Audio RTP capabilities and directional codec and packet-time negotiation."""

from __future__ import annotations

from dataclasses import replace

from . import codec_capabilities
from .audio_format import PACKET_MS_PREFERENCE, UDP_SAFE_PAYLOAD_BYTES, AudioFormat, PcmFormat
from .sdp_document import SdpDocument
from .media.codecs import CODECS
from .sdp_parser import (
    STATIC_RTPMAP as _STATIC_RTPMAP,
)
from .sdp_parser import (
    format_direction_for_remote,
    has_directional_flow_attributes,
    local_direction_for_offer,
    normalize_direction,
    parse_audio_flow_attributes,
    parse_sdp,
    supports_independent_audio_ptime,
)
from .sdp_parser import (
    parse_packet_time as _packet_time,
)
from .sdp_types import (
    MAX_RTP_OFFER_FORMATS,
    RtpDtmfDirection,
    RtpDtmfFormat,
    RtpPcmDirection,
    RtpPcmFormat,
    SdpError,
)

_PREFERRED_RTP_AUDIO_KEYS = {
    (rate, PcmFormat.S16LE, 1, frame_ms): rank
    for rank, (rate, frame_ms) in enumerate(
        (rate, frame_ms)
        for rate in (48000, 32000, 24000, 16000, 8000)
        for frame_ms in PACKET_MS_PREFERENCE[rate]
    )
}


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
        raise SdpError("phase-1 PCM uses dynamic RTP payload types 96-127")
    if _pcm_wire_bytes(fmt) > UDP_SAFE_PAYLOAD_BYTES:
        raise SdpError(
            f"RTP PCM frame too large for voip-pcm/1: "
            f"{fmt.wire_token()} is {fmt.nominal_frame_bytes} bytes; "
            f"max is {UDP_SAFE_PAYLOAD_BYTES}"
        )
    if fmt.pcm_format == PcmFormat.S16LE:
        encoding = "L16"
    elif fmt.pcm_format in (PcmFormat.S24LE, PcmFormat.S24LE_IN_S32):
        encoding = "L24"
    else:
        raise SdpError(f"{fmt.pcm_format.value} has no phase-1 RTP PCM mapping")
    return RtpPcmFormat(
        int(payload_type), encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms
    )


def is_rtp_pcm_mappable(fmt: AudioFormat) -> bool:
    if _pcm_wire_bytes(fmt) > UDP_SAFE_PAYLOAD_BYTES:
        return False
    return fmt.pcm_format in {PcmFormat.S16LE, PcmFormat.S24LE, PcmFormat.S24LE_IN_S32}


def rtp_mappable_formats(formats: list[AudioFormat]) -> list[AudioFormat]:
    return [fmt for fmt in formats if is_rtp_pcm_mappable(fmt)]


def _rtp_offer_rank(fmt: AudioFormat) -> tuple[int, int, int]:
    key = _format_key(fmt)
    if key in _PREFERRED_RTP_AUDIO_KEYS:
        return (0, _PREFERRED_RTP_AUDIO_KEYS[key], 0)
    pcm_rank = {
        PcmFormat.S16LE: 0,
        PcmFormat.S24LE: 1,
        PcmFormat.S24LE_IN_S32: 2,
        PcmFormat.S32LE: 3,
    }[fmt.pcm_format]
    generic = PACKET_MS_PREFERENCE[16000]
    frame_rank = generic.index(fmt.frame_ms) if fmt.frame_ms in generic else 9
    return (1 + pcm_rank, -fmt.sample_rate, frame_rank)


def rtp_offer_formats(formats: list[AudioFormat]) -> list[AudioFormat]:
    ranked = sorted(_dedupe_formats(rtp_mappable_formats(formats)), key=_rtp_offer_rank)
    if not ranked:
        return []
    # a=ptime is media-level in the SIP/SDP profile. Keep one packetization
    # interval per m=audio instead of smuggling per-payload ptime in fmtp.
    frame_ms = ranked[0].frame_ms
    return [fmt for fmt in ranked if fmt.frame_ms == frame_ms][:MAX_RTP_OFFER_FORMATS]


def _format_key(fmt: AudioFormat) -> tuple[int, PcmFormat, int, int]:
    wire_pcm = (
        PcmFormat.S24LE if fmt.pcm_format == PcmFormat.S24LE_IN_S32 else fmt.pcm_format
    )
    return (fmt.sample_rate, wire_pcm, fmt.channels, fmt.frame_ms)


def _dedupe_formats(formats: list[AudioFormat]) -> list[AudioFormat]:
    seen: set[tuple[int, PcmFormat, int, int]] = set()
    out: list[AudioFormat] = []
    for fmt in formats:
        key = _format_key(fmt)
        if key in seen:
            continue
        seen.add(key)
        out.append(fmt)
    return out


def _bidirectional_formats(
    send_formats: list[AudioFormat],
    recv_formats: list[AudioFormat],
) -> list[AudioFormat]:
    """Return codecs supported in both local directions.

    One RFC 3264 ``sendrecv`` media description cannot assign one codec to
    each direction.  Its formats must therefore be usable for both sending
    and receiving.  ``S24LE_IN_S32`` and ``S24LE`` intentionally share the
    same RTP wire key. Each receiver chooses its own packetization interval.
    """

    recv_keys = {_format_key(fmt)[:3] for fmt in recv_formats}
    return _dedupe_formats(
        [fmt for fmt in send_formats if _format_key(fmt)[:3] in recv_keys]
    )


def _formats_for_local_direction(
    send_formats: list[AudioFormat],
    recv_formats: list[AudioFormat],
    direction: str,
) -> list[AudioFormat]:
    direction = normalize_direction(direction)
    if direction == "sendonly":
        return _dedupe_formats(send_formats)
    if direction == "recvonly":
        return _dedupe_formats(recv_formats)
    # An inactive offer describes the formats that can be used when the
    # stream resumes as sendrecv (RFC 3264 sections 5.1 and 6.1).
    return _bidirectional_formats(send_formats, recv_formats)


def _rtp_compatible_audio(
    offered: RtpPcmFormat,
    local: AudioFormat,
    *,
    local_capabilities=None,
    receive: bool = False,
) -> RtpPcmFormat | None:
    if offered.min_frame_ms and local.frame_ms < offered.min_frame_ms:
        return None
    if offered.max_frame_ms and local.frame_ms > offered.max_frame_ms:
        return None
    encoding = offered.encoding
    if encoding == "OPUS":
        valid = (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == 48000
            and local.channels == 1
            and offered.sample_rate == 48000
            and offered.channels == 2
        )
    elif encoding == "G722":
        valid = (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == 16000
            and local.channels == 1
            and offered.sample_rate == 8000
            and offered.channels == 1
        )
    elif encoding == "PCM":
        valid = (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == 16000
            and local.channels == 1
            and offered.sample_rate == 16000
            and offered.channels == 1
        )
    elif encoding in {"PCMA", "PCMU"}:
        valid = (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == offered.sample_rate
            and local.channels == offered.channels
        )
    else:
        wanted = (
            "L16"
            if local.pcm_format == PcmFormat.S16LE
            else "L24"
            if local.pcm_format in (PcmFormat.S24LE, PcmFormat.S24LE_IN_S32)
            else ""
        )
        valid = (
            encoding == wanted
            and offered.sample_rate == local.sample_rate
            and offered.channels == local.channels
        )
    if not valid:
        return None
    if (
        local_capabilities is None
        and encoding in {"L16", "L24"}
        and not is_rtp_pcm_mappable(local)
    ):
        return None
    selected = replace(offered, frame_ms=local.frame_ms)
    if local_capabilities is not None and not local_capabilities.supports_rtp(
        selected, receive=receive
    ):
        return None
    return selected


def _best_offered_match(
    offered: list[RtpPcmFormat],
    local_preferred: list[AudioFormat],
    *,
    local_capabilities=None,
) -> RtpPcmFormat | None:
    for local in local_preferred:
        for offered_fmt in offered:
            selected = _rtp_compatible_audio(
                offered_fmt, local, local_capabilities=local_capabilities
            )
            if selected is not None:
                return _first_offered_match(
                    [offered_fmt],
                    local_preferred,
                    local_capabilities=local_capabilities,
                )
    return None


def _first_offered_match(
    offered: list[RtpPcmFormat],
    local_preferred: list[AudioFormat],
    *,
    local_capabilities=None,
    receive: bool = False,
) -> RtpPcmFormat | None:
    for offered_fmt in offered:
        # ptime states the receiver's preference. A min/max range permits
        # alternatives; it must not displace an exactly supported preference.
        for local in sorted(
            local_preferred, key=lambda fmt: fmt.frame_ms != offered_fmt.frame_ms
        ):
            selected = _rtp_compatible_audio(
                offered_fmt,
                local,
                local_capabilities=local_capabilities,
                receive=receive,
            )
            if selected is not None:
                return _with_receiver_parameters(selected, offered_fmt)
    return None


def _with_receiver_parameters(
    selected: RtpPcmFormat, receiver: RtpPcmFormat
) -> RtpPcmFormat:
    """Keep the receiver's SDP bounds without retiming the validated format."""
    return replace(
        selected,
        min_frame_ms=receiver.min_frame_ms,
        max_frame_ms=receiver.max_frame_ms,
        fmtp=receiver.fmtp,
    )


def _local_receive_match(
    offered: list[RtpPcmFormat],
    local_preferred: list[AudioFormat],
    receiver_formats: list[RtpPcmFormat] | None = None,
    *,
    local_capabilities=None,
) -> RtpPcmFormat | None:
    """Match RX against our own receiver, never the remote receiver's ptime."""
    for local in local_preferred:
        for candidate in offered:
            receiver = (
                next(
                    (
                        fmt
                        for fmt in receiver_formats
                        if _same_rtp_audio_codec(fmt, candidate)
                    ),
                    None,
                )
                if receiver_formats is not None
                else replace(candidate, frame_ms=0, min_frame_ms=0, max_frame_ms=0)
            )
            if receiver is None:
                continue
            selected = _rtp_compatible_audio(
                receiver, local, local_capabilities=local_capabilities, receive=True
            )
            if selected is not None:
                return _with_receiver_parameters(
                    replace(selected, payload_type=candidate.payload_type),
                    receiver,
                )
    return None


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


def _bidirectional_offered_match(
    offered: list[RtpPcmFormat],
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    prefer_offer_order: bool,
    local_capabilities=None,
) -> RtpPcmDirection | None:
    """Select one offered payload that is usable in both RTP directions."""

    candidates = (
        (
            (offered_fmt, local_send)
            for offered_fmt in offered
            for local_send in local_send_preferred
        )
        if prefer_offer_order
        else (
            (offered_fmt, local_send)
            for local_send in local_send_preferred
            for offered_fmt in offered
        )
    )
    for offered_fmt, local_send in candidates:
        if (
            _rtp_compatible_audio(
                offered_fmt, local_send, local_capabilities=local_capabilities
            )
            is None
        ):
            continue
        send = _first_offered_match(
            [offered_fmt], local_send_preferred, local_capabilities=local_capabilities
        )
        recv = _local_receive_match(
            [offered_fmt], local_recv_preferred, local_capabilities=local_capabilities
        )
        if send is not None and recv is not None:
            return RtpPcmDirection(
                send=_with_receiver_parameters(send, offered_fmt), recv=recv
            )
    return None


def _negotiate_for_local_direction(
    offered: list[RtpPcmFormat],
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    direction: str,
    *,
    prefer_offer_order: bool,
    local_capabilities=None,
) -> RtpPcmDirection | None:
    direction = normalize_direction(direction)
    if direction == "sendonly":
        selected = (
            _first_offered_match(
                offered, local_send_preferred, local_capabilities=local_capabilities
            )
            if prefer_offer_order
            else _best_offered_match(
                offered, local_send_preferred, local_capabilities=local_capabilities
            )
        )
        return (
            RtpPcmDirection(send=selected, recv=selected)
            if selected is not None
            else None
        )
    if direction == "recvonly":
        selected = _local_receive_match(
            offered, local_recv_preferred, local_capabilities=local_capabilities
        )
        return (
            RtpPcmDirection(send=selected, recv=selected)
            if selected is not None
            else None
        )
    return _bidirectional_offered_match(
        offered,
        local_send_preferred,
        local_recv_preferred,
        prefer_offer_order=prefer_offer_order,
        local_capabilities=local_capabilities,
    )


def _same_rtp_audio_codec(left: RtpPcmFormat, right: RtpPcmFormat) -> bool:
    """Return whether two directional payloads identify the same codec."""

    return bool(
        left.encoding == right.encoding
        and left.sample_rate == right.sample_rate
        and left.channels == right.channels
    )


def _negotiate_answer_with_offer_payloads(
    answered: list[RtpPcmFormat],
    offered: list[RtpPcmFormat],
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    direction: str,
    *,
    local_capabilities=None,
) -> RtpPcmDirection | None:
    """Negotiate the two RTP payload spaces of an RFC 3264 answer.

    The answer payload identifies packets sent by the local offerer.  The
    matching payload from the original offer identifies packets sent by the
    remote answerer.  Keeping those independently prevents a legal payload
    renumbering from silently decoding or transmitting with the wrong PT.
    """

    direction = normalize_direction(direction)
    for answered_format in answered:
        for offered_format in offered:
            if not _same_rtp_audio_codec(answered_format, offered_format):
                continue
            send = _first_offered_match(
                [answered_format],
                local_send_preferred,
                local_capabilities=local_capabilities,
            )
            recv = _first_offered_match(
                [offered_format],
                local_recv_preferred,
                local_capabilities=local_capabilities,
                receive=True,
            )
            if direction == "sendonly":
                if send is not None:
                    return RtpPcmDirection(
                        send=send,
                        recv=replace(
                            send,
                            payload_type=offered_format.payload_type,
                            frame_ms=offered_format.frame_ms,
                        ),
                    )
                continue
            if direction == "recvonly":
                if recv is not None:
                    return RtpPcmDirection(
                        send=replace(
                            recv,
                            payload_type=answered_format.payload_type,
                            frame_ms=answered_format.frame_ms,
                        ),
                        recv=recv,
                    )
                continue

            if send is not None and recv is not None:
                return RtpPcmDirection(send=send, recv=recv)
    return None


def audio_offer_formats(
    formats: list[AudioFormat],
    *,
    capability_formats: list[AudioFormat] | None = None,
    include_common_codecs: bool = False,
    include_dahua_pcm: bool = False,
    preferred_frame_ms: int | None = None,
) -> list[RtpPcmFormat]:
    """Build the canonical RTP candidates shared by SDP and bridge planning."""
    pcm_formats = rtp_offer_formats(formats)
    if not pcm_formats:
        raise SdpError("SDP offer requires at least one RTP-mappable audio format")
    if preferred_frame_ms is None:
        preferred_frame_ms = pcm_formats[0].frame_ms
    result = (
        _common_codec_offer_formats(
            capability_formats if capability_formats is not None else formats,
            include_common_codecs=include_common_codecs,
            include_dahua_pcm=include_dahua_pcm,
            preferred_frame_ms=preferred_frame_ms,
        )
        if include_common_codecs or include_dahua_pcm
        else []
    )
    next_payload = 96
    used_payloads = {fmt.payload_type for fmt in result}
    for fmt in pcm_formats:
        while next_payload in used_payloads:
            next_payload += 1
        result.append(audio_format_to_rtp(fmt, next_payload))
        used_payloads.add(next_payload)
        next_payload += 1
    return result


def _common_codec_offer_formats(
    formats: list[AudioFormat],
    *,
    include_common_codecs: bool = True,
    include_dahua_pcm: bool = False,
    preferred_frame_ms: int | None = None,
) -> list[RtpPcmFormat]:
    format_set = set(formats)
    capabilities = codec_capabilities.common_sip_codecs()
    out: list[RtpPcmFormat] = []
    if include_dahua_pcm and AudioFormat(16000, PcmFormat.S16LE, 1, 20) in format_set:
        # Dahua UAC endpoints use dynamic PT 97 for signed 16-bit
        # little-endian samples.  It is intentionally not RFC 3551 L16.
        out.append(RtpPcmFormat(97, "PCM", 16000, 1, 20))
    if include_common_codecs and "OPUS" in capabilities:
        # Keep the packet time of the available PCM profile. Native SIP offers
        # use 10 ms; adding Opus must not retime their existing PCM payloads.
        for frame_ms in dict.fromkeys((preferred_frame_ms, 20, 10)):
            if frame_ms not in (2.5, 5, 10, 20, 40, 60):
                continue
            if any(
                AudioFormat(48000, PcmFormat.S16LE, channels, frame_ms) in format_set
                for channels in (1, 2)
            ):
                out.append(RtpPcmFormat(98, "OPUS", 48000, 2, frame_ms))
                break
    if include_common_codecs and "G722" in capabilities:
        for frame_ms in dict.fromkeys((preferred_frame_ms, 20, 10, 16, 32)):
            if (
                frame_ms is not None
                and AudioFormat(16000, PcmFormat.S16LE, 1, frame_ms) in format_set
            ):
                out.append(RtpPcmFormat(9, "G722", 8000, 1, frame_ms))
                break
    g711_frames = [
        fmt.frame_ms
        for fmt in formats
        if fmt.sample_rate == 8000
        and fmt.pcm_format == PcmFormat.S16LE
        and fmt.channels == 1
    ]
    if include_common_codecs and g711_frames:
        out.extend(
            (
                RtpPcmFormat(8, "PCMA", 8000, 1, max(g711_frames)),
                RtpPcmFormat(0, "PCMU", 8000, 1, max(g711_frames)),
            )
        )
    return out


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


def negotiate_dtmf_answer_directional(
    remote_sdp: str | bytes,
    local_offer_sdp: str | bytes,
    *,
    send_clock_rate: int | None = None,
    receive_clock_rate: int | None = None,
    audio: RtpPcmDirection | None = None,
) -> RtpDtmfDirection | None:
    """Return directional RFC 4733 mappings selected by an SDP answer.

    As with audio and video, the local offerer transmits with the answer's PT
    and receives with the original offer's PT. This matters for softphones
    that reuse one dynamic number for another clock rate in their answer and
    publish the compatible telephone-event format on a different number.
    """

    answered = offered_dtmf_formats(remote_sdp)
    offered = offered_dtmf_formats(local_offer_sdp)
    if audio is not None:
        from .media.shadow import engine_mode
        if engine_mode() == "active":
            send_clock_rate, receive_clock_rate = audio.send.sample_rate, audio.recv.sample_rate
    if send_clock_rate is not None or receive_clock_rate is not None:
        def selected(clock, receive):
            for wanted in dict.fromkeys((clock, 8000)):
                for answer in answered:
                    for offer in offered:
                        if answer.sample_rate == offer.sample_rate == wanted and (events := answer.events & offer.events):
                            chosen = offer if receive else answer
                            return replace(chosen, events=events)
            return None
        send = selected(send_clock_rate, False)
        recv = selected(receive_clock_rate, True)
        return RtpDtmfDirection(send, recv) if send is not None or recv is not None else None
    for answer_format in answered:
        for offer_format in offered:
            if answer_format.sample_rate == offer_format.sample_rate:
                events = answer_format.events & offer_format.events
                if events:
                    return RtpDtmfDirection(
                        send=RtpDtmfFormat(
                            answer_format.payload_type,
                            answer_format.sample_rate,
                            events,
                        ),
                        recv=RtpDtmfFormat(
                            offer_format.payload_type,
                            offer_format.sample_rate,
                            events,
                        ),
                    )
    return None


def negotiate_dtmf_answer(
    remote_sdp: str | bytes,
    local_offer_sdp: str | bytes,
) -> RtpDtmfFormat | None:
    """Return the receive-side RFC 4733 mapping for compatibility callers."""

    selected = negotiate_dtmf_answer_directional(remote_sdp, local_offer_sdp)
    return selected.recv if selected is not None else None


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


def _local_receive_budget(fmt: RtpPcmFormat, capabilities) -> RtpPcmFormat:
    if capabilities is None:
        return fmt
    maximum = capabilities.max_receive_packet_ms(fmt)
    if fmt.max_frame_ms:
        maximum = min(maximum, fmt.max_frame_ms)
    return replace(fmt, max_frame_ms=maximum)


def negotiate_directional(
    remote_sdp: str | bytes,
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    allow_dahua_pcm: bool = False,
    codec_policy=None,
    local_recv_capabilities: tuple[AudioFormat, ...] | None = None,
    local_capabilities=None,
    established: RtpPcmDirection | None = None,
) -> RtpPcmDirection | None:
    from .media import negotiator
    from .media.shadow import engine_mode

    if engine_mode() == "active":
        previous = (negotiator.LegContract(established.send, established.recv, None)
                    if established is not None else None)
        contract = negotiator.answer_offer(remote_sdp, local_send_preferred, local_recv_preferred,
            capabilities=local_capabilities, codec_policy=codec_policy,
            dahua=allow_dahua_pcm, established=previous)
        return negotiator.as_direction(contract, remote_sdp)
    remote_sdp = SdpDocument.parse(remote_sdp)
    parsed = parse_sdp(remote_sdp)
    local_direction = local_direction_for_offer(
        parsed["direction"],
        remote_connection_held=bool(parsed["connection_held"]),
    )
    format_direction = format_direction_for_remote(
        parsed["direction"],
        local_direction,
    )
    offered = offered_pcm_formats(remote_sdp, allow_dahua_pcm=allow_dahua_pcm)
    if codec_policy is not None:
        offered = list(codec_policy.audio_formats(offered))
    if local_capabilities is not None:
        local_send_preferred = local_capabilities.expand(local_send_preferred, offered)
        local_recv_preferred = local_capabilities.expand(
            local_recv_preferred, offered, receive=True
        )
    explicit = _negotiate_audio_flow_attributes(
        remote_sdp,
        offered,
        local_send_preferred,
        local_recv_preferred,
        local_capabilities=local_capabilities,
    )
    selected = explicit or _negotiate_for_local_direction(
        offered,
        local_send_preferred,
        local_recv_preferred,
        format_direction,
        prefer_offer_order=False,
        local_capabilities=local_capabilities,
    )
    if selected is None:
        return None
    # The remote ptime is a recommendation, not our decoder's receive budget.
    # Retain the selected PCM quantum, and derive capacity only from locally
    # supported shapes for this same codec/rate/channels. Peer RX bounds apply
    # to our transmitter and must never become our own receive capacity.
    receive_shape = _format_key(selected.recv.audio_format)[:3]
    receive_capabilities = (
        local_recv_preferred
        if local_recv_capabilities is None
        else local_recv_capabilities
    )
    maximum = (
        local_capabilities.max_receive_packet_ms(selected.recv)
        if local_capabilities is not None
        else max(
            [
                selected.recv.frame_ms,
                *(
                    local.frame_ms
                    for local in receive_capabilities
                    if _format_key(local)[:3] == receive_shape
                ),
            ]
        )
    )
    return replace(selected, recv=replace(selected.recv, max_frame_ms=maximum))


def negotiate_answer_directional(
    remote_sdp: str | bytes,
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    local_offer_direction: str = "sendrecv",
    local_offer_sdp: str | bytes | None = None,
    allow_dahua_pcm: bool = False,
    local_capabilities=None,
    codec_policy=None,
) -> RtpPcmDirection | None:
    """Negotiate one standards-compliant media contract from an SDP answer."""

    from .media import negotiator
    from .media.shadow import engine_mode

    if engine_mode() == "active" and local_offer_sdp is not None:
        contract = negotiator.apply_answer(local_offer_sdp, remote_sdp,
            local_send_preferred, local_recv_preferred, capabilities=local_capabilities,
            codec_policy=codec_policy, dahua=allow_dahua_pcm)
        return negotiator.as_direction(contract, remote_sdp)

    remote_sdp = SdpDocument.parse(remote_sdp)
    parsed = parse_sdp(remote_sdp)
    local_direction = local_direction_for_offer(
        parsed["direction"],
        remote_connection_held=bool(parsed["connection_held"]),
    )
    format_direction = format_direction_for_remote(
        parsed["direction"],
        local_direction,
        inactive_default=local_offer_direction,
    )
    answered_formats = offered_pcm_formats(
        remote_sdp,
        allow_dahua_pcm=allow_dahua_pcm,
    )
    if local_capabilities is not None:
        local_send_preferred = local_capabilities.expand(
            local_send_preferred, answered_formats
        )
        local_recv_preferred = local_capabilities.expand(
            local_recv_preferred, answered_formats, receive=True
        )
    if local_offer_sdp is not None:
        offered_formats = offered_pcm_formats(
            local_offer_sdp, allow_dahua_pcm=allow_dahua_pcm
        )
        if local_capabilities is not None:
            local_recv_preferred = local_capabilities.expand(
                local_recv_preferred, offered_formats, receive=True
            )
        explicit = _negotiate_audio_flow_attributes(
            remote_sdp,
            answered_formats,
            local_send_preferred,
            local_recv_preferred,
            receiver_formats=offered_formats,
            local_capabilities=local_capabilities,
        )
        if explicit is not None:
            # The ESP directional extension has a separate per-payload contract.
            # The answer describes the peer receiver; HA's receive bounds are
            # still those advertised in its own offer, not the peer's maxptime.
            peer_receiver = next(
                (
                    fmt
                    for fmt in answered_formats
                    if fmt.payload_type == explicit.send.payload_type
                    and _same_rtp_audio_codec(fmt, explicit.send)
                ),
                None,
            )
            local_receiver = next(
                (
                    fmt
                    for fmt in offered_formats
                    if _same_rtp_audio_codec(fmt, explicit.recv)
                ),
                None,
            )
            if peer_receiver is None or local_receiver is None:
                return None
            explicit = replace(
                explicit,
                send=_with_receiver_parameters(explicit.send, peer_receiver),
                recv=_with_receiver_parameters(explicit.recv, local_receiver),
            )
            explicit = replace(
                explicit, recv=_local_receive_budget(explicit.recv, local_capabilities)
            )
            return replace(
                explicit,
                recv_formats=(explicit.recv,)
                if local_direction in {"recvonly", "sendrecv"}
                else (),
            )
        selected = _negotiate_answer_with_offer_payloads(
            answered_formats,
            offered_formats,
            local_send_preferred,
            local_recv_preferred,
            format_direction,
            local_capabilities=local_capabilities,
        )
        if selected is None:
            return None
        receive = []
        if local_direction in {"recvonly", "sendrecv"}:
            # RFC 3264 6.1: the answerer sends any common codec using the
            # offer's PT. Its receive preference does not select our sole RX.
            for offered in offered_formats:
                if not any(
                    _same_rtp_audio_codec(offered, answer)
                    for answer in answered_formats
                ):
                    continue
                compatible = _first_offered_match(
                    [offered],
                    local_recv_preferred,
                    local_capabilities=local_capabilities,
                    receive=True,
                )
                if compatible is not None:
                    receive.append(replace(offered, frame_ms=compatible.frame_ms))
        transmit = []
        if local_direction in {"sendonly", "sendrecv"}:
            for answer in answered_formats:
                if not any(
                    _same_rtp_audio_codec(answer, offered)
                    for offered in offered_formats
                ):
                    continue
                compatible = _first_offered_match(
                    [answer],
                    local_send_preferred,
                    local_capabilities=local_capabilities,
                )
                if compatible is not None:
                    transmit.append(replace(answer, frame_ms=compatible.frame_ms))
        return replace(
            selected,
            recv=_local_receive_budget(selected.recv, local_capabilities),
            recv_formats=tuple(
                _local_receive_budget(fmt, local_capabilities) for fmt in receive
            ),
            send_formats=tuple(transmit),
        )
    selected = _negotiate_for_local_direction(
        answered_formats,
        local_send_preferred,
        local_recv_preferred,
        format_direction,
        prefer_offer_order=True,
        local_capabilities=local_capabilities,
    )
    if selected is None:
        return None
    selected = replace(
        selected, recv=_local_receive_budget(selected.recv, local_capabilities)
    )
    return replace(
        selected,
        recv_formats=(selected.recv,)
        if local_direction in {"recvonly", "sendrecv"}
        else (),
    )


def _negotiate_audio_flow_attributes(
    remote_sdp: str | bytes,
    answered: list[RtpPcmFormat],
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    receiver_formats: list[RtpPcmFormat] | None = None,
    local_capabilities=None,
) -> RtpPcmDirection | None:
    """Apply the ESPHome directional payload extension when both flows exist."""

    flows = parse_audio_flow_attributes(remote_sdp)
    remote_recv = [
        item
        for item in answered
        if flows.get(item.payload_type) in {"recv", "sendrecv"}
    ]
    remote_send = [
        item
        for item in answered
        if flows.get(item.payload_type) in {"send", "sendrecv"}
    ]
    if not remote_recv or not remote_send:
        return None
    send = _best_offered_match(
        remote_recv, local_send_preferred, local_capabilities=local_capabilities
    )
    if send is None:
        return None
    if not supports_independent_audio_ptime(remote_sdp):
        local_recv_preferred = [
            fmt for fmt in local_recv_preferred if fmt.frame_ms == send.frame_ms
        ]
    recv = _local_receive_match(
        remote_send,
        local_recv_preferred,
        receiver_formats,
        local_capabilities=local_capabilities,
    )
    return (
        RtpPcmDirection(send, recv) if send is not None and recv is not None else None
    )


def has_directional_audio_flow_attributes(remote_sdp: str | bytes) -> bool:
    """Return whether SDP declares usable directional audio payloads."""

    return has_directional_flow_attributes(remote_sdp)


def audio_flow_attributes(remote_sdp: str | bytes) -> dict[int, str]:
    """Return explicitly advertised per-payload audio directions."""

    return parse_audio_flow_attributes(remote_sdp)
