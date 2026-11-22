"""Audio RTP capabilities and directional codec and packet-time negotiation."""

from __future__ import annotations

from dataclasses import replace

from . import codec_capabilities
from .audio_format import PACKET_MS_PREFERENCE, AudioFormat, PcmFormat
from .sdp_audio_facts import (
    _dedupe_rtp_offer_encodings as _dedupe_rtp_offer_encodings,
    _dtmf_events_from_fmtp as _dtmf_events_from_fmtp,
    _format_dtmf_events as _format_dtmf_events,
    _offered_audio_mapping as _offered_audio_mapping,
    _pcm_wire_bytes as _pcm_wire_bytes,
    _rtp_contract_key as _rtp_contract_key,
    _rtp_encoding_key as _rtp_encoding_key,
    _same_rtp_audio_codec as _same_rtp_audio_codec,
    audio_format_to_rtp as audio_format_to_rtp,
    dtmf_format_for_clock as dtmf_format_for_clock,
    first_offered_dtmf_format as first_offered_dtmf_format,
    is_rtp_pcm_mappable as is_rtp_pcm_mappable,
    offered_dtmf_formats as offered_dtmf_formats,
    offered_media_descriptions as offered_media_descriptions,
    offered_pcm_formats as offered_pcm_formats,
    parse_rtp_audio_capability as parse_rtp_audio_capability,
)
from .sdp_document import SdpDocument
from .sdp_parser import (
    format_direction_for_remote,
    has_directional_flow_attributes,
    local_direction_for_offer,
    normalize_direction,
    parse_audio_flow_attributes,
    parse_sdp,
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

    remote_sdp = SdpDocument.parse(remote_sdp)
    previous = (negotiator.LegContract(established.send, established.recv, None)
                if established is not None else None)
    contract = negotiator.answer_offer(remote_sdp, local_send_preferred, local_recv_preferred,
        capabilities=local_capabilities, codec_policy=codec_policy,
        dahua=allow_dahua_pcm, established=previous, receive_capabilities=local_recv_capabilities)
    return negotiator.as_direction(contract, remote_sdp)


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
    """Apply an exact answer, preserving the capability-only API when no offer is supplied."""
    from .media import negotiator

    remote_sdp = SdpDocument.parse(remote_sdp)
    if local_offer_sdp is not None:
        local_offer_sdp = SdpDocument.parse(local_offer_sdp)
    format_direction = None
    if local_offer_sdp is None:
        parsed = parse_sdp(remote_sdp)
        local_direction = local_direction_for_offer(parsed["direction"],
            remote_connection_held=bool(parsed["connection_held"]))
        format_direction = format_direction_for_remote(parsed["direction"], local_direction,
            inactive_default=local_offer_direction)
    contract = negotiator.apply_answer(local_offer_sdp, remote_sdp,
        local_send_preferred, local_recv_preferred, capabilities=local_capabilities,
        codec_policy=codec_policy, dahua=allow_dahua_pcm, format_direction=format_direction)
    selected = negotiator.as_direction(contract, remote_sdp)
    # Compatibility callers have no exact offer authorizing transmit alternatives.
    return replace(selected, send_formats=()) if selected is not None and local_offer_sdp is None else selected


def has_directional_audio_flow_attributes(remote_sdp: str | bytes) -> bool:
    """Return whether SDP declares usable directional audio payloads."""

    return has_directional_flow_attributes(remote_sdp)


def audio_flow_attributes(remote_sdp: str | bytes) -> dict[int, str]:
    """Return explicitly advertised per-payload audio directions."""

    return parse_audio_flow_attributes(remote_sdp)
