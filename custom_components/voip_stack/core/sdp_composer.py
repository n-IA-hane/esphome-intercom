"""Serialize complete SDP offers and answers from negotiated media contracts."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import replace

from .audio_format import AudioFormat
from .imageattr import serialize_imageattr
from .sdp_audio import (
    _common_codec_offer_formats,
    _dedupe_rtp_offer_encodings,
    _format_dtmf_events,
    _format_key,
    _formats_for_local_direction,
    _local_receive_match,
    _rtp_contract_key,
    _rtp_encoding_key,
    _same_rtp_audio_codec,
    audio_format_to_rtp,
    audio_offer_formats,
    offered_pcm_formats,
    parse_rtp_audio_capability,
    rtp_mappable_formats,
)
from .sdp_document import SdpDocument
from .sdp_parser import (
    format_direction_for_remote,
    has_directional_flow_attributes,
    local_direction_for_offer,
    local_direction_for_remote,
    normalize_direction,
    parse_audio_flow_attributes,
    parse_sdp,
    supports_independent_audio_ptime,
    suppress_local_send,
)
from .sdp_parser import (
    parse_media_sections as _parse_media_sections,
)
from .sdp_types import (
    MAX_RTP_OFFER_FORMATS,
    RtpDtmfFormat,
    RtpH264Format,
    RtpPcmFormat,
    RtpVideoFormat,
    SdpError,
)
from .sdp_video import (
    _serialized_video_fmtp,
    _unique_video_payloads,
    offered_video_formats,
)


def _address_type(address: str) -> str:
    """Return the SDP address type for one literal or local hostname."""

    try:
        return "IP6" if ipaddress.ip_address(address).version == 6 else "IP4"
    except ValueError:
        return "IP4"


def _connection_line(address: str) -> str:
    return f"c=IN {_address_type(address)} {address}"


def _origin_line(address: str, session_id: int = 0, version: int = 0) -> str:
    return f"o=- {session_id} {version} IN {_address_type(address)} {address}"


def rewrite_sdp_origin(
    sdp_body: str | bytes,
    session_id: int,
    session_version: int,
) -> str:
    """Apply one stable RFC 3264 origin identity to an SDP description."""

    if isinstance(sdp_body, bytes):
        sdp_body = sdp_body.decode("utf-8", errors="strict")
    session_id = int(session_id)
    session_version = int(session_version)
    if session_id < 0 or session_version < 0:
        raise SdpError("SDP origin session id/version must be non-negative")
    separator = "\r\n" if "\r\n" in sdp_body else "\n"
    lines = sdp_body.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(lines):
        if not line.startswith("o="):
            continue
        parts = line[2:].split()
        if len(parts) != 6:
            raise SdpError(f"bad SDP origin line: {line}")
        parts[1] = str(session_id)
        parts[2] = str(session_version)
        lines[index] = "o=" + " ".join(parts)
        rendered = separator.join(lines)
        if sdp_body.endswith(("\r\n", "\n")) and not rendered.endswith(separator):
            rendered += separator
        return rendered
    # Keep deliberately minimal test/legacy SDP bodies usable. Production
    # offer/answer builders always emit an origin line.
    return sdp_body


def sdp_description_changed(previous: str | bytes, updated: str | bytes) -> bool:
    """Compare local SDP while excluding the origin version itself."""

    def _description(value: str | bytes) -> tuple[str, ...]:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="strict")
        return tuple(
            line.strip()
            for line in value.replace("\r\n", "\n").split("\n")
            if line.strip() and not line.startswith("o=")
        )

    return _description(previous) != _description(updated)


def build_offer(
    origin_ip: str, media_ip: str, media_port: int, formats: list[AudioFormat]
) -> str:
    return build_offer_directional(origin_ip, media_ip, media_port, formats, formats)


def build_offer_directional(
    origin_ip: str,
    media_ip: str,
    media_port: int,
    send_formats: list[AudioFormat],
    recv_formats: list[AudioFormat],
    *,
    include_common_codecs: bool = False,
    audio_rtcp: bool = True,
    include_dahua_pcm: bool = False,
    video_port: int = 0,
    video_format: RtpH264Format | None = None,
    video_formats: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat] | None = None,
    audio_direction: str = "sendrecv",
    video_direction: str = "sendrecv",
    audio_rtp_formats: tuple[RtpPcmFormat, ...] | None = None,
    send_rtp_formats: tuple[RtpPcmFormat, ...] | None = None,
    recv_rtp_formats: tuple[RtpPcmFormat, ...] | None = None,
    allow_directional_payloads: bool = False,
    independent_audio_ptime: bool = False,
    codec_policy=None,
    local_capabilities=None,
) -> str:
    audio_direction = normalize_direction(audio_direction)
    video_direction = normalize_direction(video_direction)
    from .media import negotiator
    from .media.shadow import engine_mode
    if engine_mode() == "active":
        try:
            plan = negotiator.create_offer(send_formats, recv_formats, codec_policy=codec_policy,
                capabilities=local_capabilities, audio_rtp_formats=audio_rtp_formats,
                send_rtp_formats=send_rtp_formats, recv_rtp_formats=recv_rtp_formats,
                common=include_common_codecs, dahua=include_dahua_pcm,
                directional=allow_directional_payloads, independent=independent_audio_ptime,
                direction=audio_direction)
        except ValueError as error:
            raise SdpError(str(error)) from error
        return _write_offer_plan(plan, origin_ip, media_ip, media_port, audio_direction=audio_direction,
            audio_rtcp=audio_rtcp, directional=allow_directional_payloads,
            independent=independent_audio_ptime, video_port=video_port,
            video_formats=video_formats or (() if video_format is None else (video_format,)),
            video_direction=video_direction, codec_policy=codec_policy)
    receive_capabilities = recv_formats
    legacy_frame_ms = None
    if (
        allow_directional_payloads
        and not independent_audio_ptime
        and audio_direction == "sendrecv"
    ):
        # D1 originally required one packet time in both directions. Keep
        # that wire contract until the peer advertises the versioned upgrade.
        send_times = (
            send_rtp_formats if send_rtp_formats is not None else audio_rtp_formats
        )
        recv_times = (
            recv_rtp_formats if recv_rtp_formats is not None else audio_rtp_formats
        )
        send_times = (
            send_times
            if send_times is not None
            else tuple(
                audio_format_to_rtp(fmt, 96)
                for fmt in rtp_mappable_formats(send_formats)
            )
        )
        recv_times = (
            recv_times
            if recv_times is not None
            else tuple(
                audio_format_to_rtp(fmt, 96)
                for fmt in rtp_mappable_formats(recv_formats)
            )
        )
        first_sender = next(iter(send_times), None)
        selected_send_times = [
            fmt.frame_ms
            for fmt in send_times
            if first_sender is not None and _same_rtp_audio_codec(fmt, first_sender)
        ]
        legacy_frame_ms = next(
            (fmt.frame_ms for fmt in recv_times if fmt.frame_ms in selected_send_times),
            None,
        )
        if legacy_frame_ms is None:
            raise SdpError("Legacy directional audio requires a common packet time")
    directional_flows: dict[tuple[str, int, int], str] = {}
    preferred_frame_ms = legacy_frame_ms
    explicit_rtp_formats = bool(
        audio_rtp_formats is not None
        or send_rtp_formats is not None
        or recv_rtp_formats is not None
    )
    if (
        not explicit_rtp_formats
        and allow_directional_payloads
        and audio_direction == "sendrecv"
    ):
        # Packet time belongs to the receiving endpoint, not to an RTP codec
        # identity. Assign each encoding once, preserving each direction's
        # preferences, then reuse the explicit-capability offer builder.
        payload_ids = {}

        def assign(formats):
            result = []
            for fmt in rtp_mappable_formats(formats):
                key = _format_key(fmt)[:3]
                if key not in payload_ids:
                    if len(payload_ids) >= MAX_RTP_OFFER_FORMATS:
                        continue
                    payload_ids[key] = 96 + len(payload_ids)
                result.append(audio_format_to_rtp(fmt, payload_ids[key]))
            return tuple(result)

        send_rtp_formats = assign(send_formats)
        recv_rtp_formats = assign(recv_formats)
        explicit_rtp_formats = True
    if explicit_rtp_formats:
        if audio_rtp_formats is not None:
            explicit_all = list(audio_rtp_formats)
            send_audio_keys = {_format_key(fmt) for fmt in send_formats}
            recv_audio_keys = {_format_key(fmt) for fmt in recv_formats}
            explicit_send = [
                fmt
                for fmt in explicit_all
                if _format_key(fmt.audio_format) in send_audio_keys
            ]
            explicit_recv = [
                fmt
                for fmt in explicit_all
                if _format_key(fmt.audio_format) in recv_audio_keys
            ]
        else:
            explicit_send = list(send_rtp_formats or ())
            explicit_recv = list(recv_rtp_formats or ())
        send_keys = {_rtp_encoding_key(fmt) for fmt in explicit_send}
        recv_keys = {_rtp_encoding_key(fmt) for fmt in explicit_recv}
        if audio_direction == "sendonly":
            selected_explicit = explicit_send
        elif audio_direction == "recvonly":
            selected_explicit = explicit_recv
        elif allow_directional_payloads:
            # Keep the preferred format sent to the peer ahead of its common
            # lower-rate alternative. A first-compatible answerer otherwise
            # chooses 16k in both directions instead of the declared 48k/16k.
            selected_explicit = (
                [
                    fmt
                    for fmt in explicit_all
                    if _rtp_encoding_key(fmt) in send_keys | recv_keys
                ]
                if audio_rtp_formats is not None
                else [*explicit_send, *explicit_recv]
            )
        else:
            # A standard sendrecv offer needs codecs supported both ways.
            # Preserve the caller's transmit preference within that set.
            selected_explicit = [
                fmt for fmt in explicit_send if _rtp_encoding_key(fmt) in recv_keys
            ]
        rtp_formats = _dedupe_rtp_offer_encodings(selected_explicit)
        if not rtp_formats:
            raise SdpError("SDP offer requires at least one RTP audio format")
        if audio_direction != "sendonly":
            receiver = (
                explicit_recv[0]
                if allow_directional_payloads and explicit_recv
                else next(
                    (
                        fmt
                        for fmt in explicit_recv
                        if _same_rtp_audio_codec(fmt, rtp_formats[0])
                    ),
                    None,
                )
            )
            if receiver is not None:
                preferred_frame_ms = legacy_frame_ms or receiver.frame_ms
        if allow_directional_payloads and audio_direction == "sendrecv":
            for fmt in rtp_formats:
                key = _rtp_encoding_key(fmt)
                directional_flows[key] = (
                    "sendrecv"
                    if key in send_keys and key in recv_keys
                    else "send"
                    if key in send_keys
                    else "recv"
                    if key in recv_keys
                    else ""
                )
        if include_common_codecs or include_dahua_pcm:
            # Explicit RTP discovery orders preferences, while the local
            # codec policy still permits the authoritative answer to choose
            # an ordinary codec. Preserve every explicit mapping and ptime.
            common = _common_codec_offer_formats(
                _formats_for_local_direction(
                    send_formats, recv_formats, audio_direction
                ),
                include_common_codecs=include_common_codecs,
                include_dahua_pcm=include_dahua_pcm,
                preferred_frame_ms=preferred_frame_ms or rtp_formats[0].frame_ms,
            )
            used_payloads = {fmt.payload_type for fmt in rtp_formats}
            codec_keys = {_rtp_encoding_key(fmt) for fmt in rtp_formats}
            for fmt in common:
                key = _rtp_encoding_key(fmt)
                if key in codec_keys:
                    continue
                if fmt.payload_type in used_payloads:
                    payload = next(
                        (pt for pt in range(96, 128) if pt not in used_payloads), None
                    )
                    if payload is None:
                        raise SdpError(
                            "No RTP payload type available for codec alternative"
                        )
                    fmt = replace(fmt, payload_type=payload)
                rtp_formats.append(fmt)
                used_payloads.add(fmt.payload_type)
                codec_keys.add(key)
                if allow_directional_payloads and audio_direction == "sendrecv":
                    directional_flows[key] = "sendrecv"
    else:
        capability_formats = _formats_for_local_direction(
            send_formats or [],
            recv_formats or [],
            audio_direction,
        )
        pcm_formats = capability_formats
        rtp_formats = audio_offer_formats(
            pcm_formats,
            capability_formats=capability_formats,
            include_common_codecs=include_common_codecs,
            include_dahua_pcm=include_dahua_pcm,
            preferred_frame_ms=preferred_frame_ms,
        )
    if codec_policy is not None:
        rtp_formats = list(codec_policy.audio_formats(rtp_formats))
        if not rtp_formats:
            raise SdpError(
                "The selected audio codecs do not match this call's media capabilities"
            )
    if preferred_frame_ms is None and audio_direction != "sendonly":
        receiver = _local_receive_match([rtp_formats[0]], recv_formats)
        if receiver is not None:
            preferred_frame_ms = receiver.frame_ms
    next_payload = 96
    used_payloads = {fmt.payload_type for fmt in rtp_formats}
    while next_payload in used_payloads:
        next_payload += 1
    dtmf_payload_type = next_payload
    payloads = " ".join(
        [*(str(fmt.payload_type) for fmt in rtp_formats), str(dtmf_payload_type)]
    )
    lines = [
        "v=0",
        _origin_line(origin_ip),
        "s=VoIP Stack",
        _connection_line(media_ip),
        "t=0 0",
        f"m=audio {int(media_port)} RTP/AVP {payloads}",
    ]
    if not audio_rtcp:
        # RFC 3556 disables RTCP only for this audio media section; video
        # retains its own feedback transport and bandwidth policy.
        lines.extend(("b=RS:0", "b=RR:0"))
    for fmt in rtp_formats:
        lines.append(
            f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}"
        )
        if fmt.encoding == "OPUS":
            opus_fmtp = str(fmt.fmtp or "").strip() or (
                "stereo=0;sprop-stereo=0;maxaveragebitrate=28000"
            )
            lines.append(f"a=fmtp:{fmt.payload_type} {opus_fmtp}")
        if allow_directional_payloads and audio_direction == "sendrecv":
            flow = directional_flows.get(_rtp_encoding_key(fmt))
            if flow is not None:
                lines.append(f"a=x-voip-stack-flow:{fmt.payload_type} {flow}")
    lines.append(f"a=rtpmap:{dtmf_payload_type} telephone-event/8000")
    lines.append(f"a=fmtp:{dtmf_payload_type} 0-15")
    if allow_directional_payloads:
        lines.append("a=x-voip-stack-independent-ptime:1")
    packet_time = preferred_frame_ms or rtp_formats[0].frame_ms
    receive_packet_times = [
        receiver.frame_ms
        for fmt in rtp_formats
        if (receiver := _local_receive_match([fmt], receive_capabilities)) is not None
    ]
    if explicit_rtp_formats:
        receive_packet_times.extend(fmt.frame_ms for fmt in explicit_recv)
    max_packet_time = (
        max(packet_time, *receive_packet_times)
        if receive_packet_times and audio_direction != "sendonly"
        else packet_time
    )
    if (
        local_capabilities is not None
        and not explicit_rtp_formats
        and audio_direction != "sendonly"
    ):
        # Preference quanta do not limit the receiver. RFC 8866 maxptime is
        # media-wide, so every advertised receiving codec must fit its budget.
        receive_bounds = [
            local_capabilities.max_receive_packet_ms(receiver)
            for fmt in rtp_formats
            if (
                receiver := _local_receive_match(
                    [fmt], receive_capabilities, local_capabilities=local_capabilities
                )
            )
            is not None
        ]
        if receive_bounds:
            max_packet_time = min(receive_bounds)
    if explicit_rtp_formats and audio_direction != "sendonly":
        # maxptime is media-wide. Every advertised receiving mapping must fit,
        # rather than inheriting the largest duration from a different rate.
        receive_bounds = [
            fmt.max_frame_ms
            for fmt in explicit_recv
            if fmt.max_frame_ms
            and any(_same_rtp_audio_codec(fmt, wire) for wire in rtp_formats)
        ]
        if receive_bounds:
            max_packet_time = min(max_packet_time, *receive_bounds)
            packet_time = min(packet_time, max_packet_time)
    lines.append(f"a=ptime:{packet_time}")
    if (
        not allow_directional_payloads
        or max_packet_time > packet_time
        or any(fmt.frame_ms > max_packet_time for fmt in rtp_formats)
    ):
        lines.append(f"a=maxptime:{max_packet_time}")
    lines.append(f"a={audio_direction}")
    offered_video = _unique_video_payloads(
        video_formats or (() if video_format is None else (video_format,))
    )
    if codec_policy is not None:
        offered_video = codec_policy.video_formats(offered_video)
    if offered_video:
        lines.extend(
            _video_media_lines_many(
                int(video_port), offered_video, direction=video_direction
            )
        )
    return "\r\n".join(lines) + "\r\n"


def _write_offer_plan(plan, origin_ip, media_ip, media_port, *, audio_direction, audio_rtcp,
                      directional, independent, video_port, video_formats, video_direction, codec_policy):
    """Serialize decisions already owned by the negotiator, without choosing codecs."""
    lines = ["v=0", _origin_line(origin_ip), "s=VoIP Stack", _connection_line(media_ip), "t=0 0",
             f"m=audio {int(media_port)} RTP/AVP " + " ".join(str(number) for number, _mapping in plan.payloads)]
    if not audio_rtcp:
        lines.extend(("b=RS:0", "b=RR:0"))
    flows = dict(plan.flows)
    for fmt in plan.formats:
        lines.append(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}")
        if fmt.encoding == "OPUS":
            lines.append(f"a=fmtp:{fmt.payload_type} stereo=0;sprop-stereo=0;maxaveragebitrate=28000")
        if directional and audio_direction == "sendrecv" and fmt.payload_type in flows:
            lines.append(f"a=x-voip-stack-flow:{fmt.payload_type} {flows[fmt.payload_type]}")
    for event in plan.events:
        lines.extend((f"a=rtpmap:{event.payload_type} telephone-event/{event.sample_rate}",
                      f"a=fmtp:{event.payload_type} {_format_dtmf_events(event.events)}"))
    if directional:
        lines.append("a=x-voip-stack-independent-ptime:1")
    if plan.packet_ms:
        lines.append(f"a=ptime:{plan.packet_ms:g}")
    if plan.maximum_ms and (not directional or independent):
        lines.append(f"a=maxptime:{plan.maximum_ms:g}")
    lines.append(f"a={audio_direction}")
    video = _unique_video_payloads(video_formats)
    if codec_policy is not None:
        video = codec_policy.video_formats(video)
    if video:
        lines.extend(_video_media_lines_many(int(video_port), video, direction=video_direction))
    return "\r\n".join(lines) + "\r\n"


def build_video_reoffer(
    local_sdp: str,
    *,
    video_port: int,
    video_formats: tuple[RtpVideoFormat, ...],
    video_direction: str,
) -> str:
    """Update video while preserving the existing session and other media."""
    parse_sdp(local_sdp)
    replacement = _video_media_lines_many(
        int(video_port),
        _unique_video_payloads(video_formats),
        direction=normalize_direction(video_direction),
    )
    lines = []
    in_video = False
    replaced = False
    for line in local_sdp.replace("\r\n", "\n").splitlines():
        if line.startswith("m="):
            in_video = line.startswith("m=video ")
            if in_video:
                if replaced:
                    raise SdpError("Multiple video streams are not supported")
                lines.extend(replacement)
                replaced = True
        if not in_video:
            lines.append(line)
    if not replaced:
        lines.extend(replacement)
    return "\r\n".join(lines) + "\r\n"


def _video_media_lines(
    media_port: int,
    selected: RtpVideoFormat,
    *,
    direction: str,
) -> list[str]:
    payload_type = int(selected.payload_type)
    rtp_encoding = {"H263P": "H263-1998"}.get(selected.encoding, selected.encoding)
    lines = [
        f"m=video {int(media_port)} {selected.transport_profile} {payload_type}",
        f"a=rtpmap:{payload_type} {rtp_encoding}/{selected.clock_rate}",
    ]
    fmtp = _serialized_video_fmtp(selected)
    if fmtp:
        lines.append(f"a=fmtp:{payload_type} {fmtp}")
    for feedback in selected.rtcp_feedback:
        lines.append(f"a=rtcp-fb:{payload_type} {feedback}")
    if selected.max_framerate is not None:
        lines.append(f"a=framerate:{selected.max_framerate:g}")
    if (
        selected.encoding == "JPEG"
        and selected.image_attributes is not None
        and (
            selected.image_attributes.send is not None
            or selected.image_attributes.recv is not None
        )
    ):
        lines.append(
            "a=" + serialize_imageattr(payload_type, selected.image_attributes)
        )
    if int(media_port) > 0:
        lines.append(f"a=rtcp:{int(media_port) + 1}")
    lines.append(f"a={direction}")
    return lines


def _video_media_lines_many(
    media_port: int,
    selected: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat],
    *,
    direction: str,
) -> list[str]:
    """Build one offer m-line containing all supported video payloads."""

    formats = tuple(selected)
    if not formats:
        raise SdpError("video offer requires at least one format")
    profiles = {item.transport_profile for item in formats}
    if len(profiles) != 1:
        raise SdpError("one video media section cannot mix RTP profiles")
    profile = profiles.pop()
    if profile not in {"RTP/AVP", "RTP/AVPF"}:
        raise SdpError("unsupported video RTP profile")
    payloads = " ".join(str(item.payload_type) for item in formats)
    lines = [f"m=video {int(media_port)} {profile} {payloads}"]
    for item in formats:
        payload_type = int(item.payload_type)
        rtp_encoding = {"H263P": "H263-1998"}.get(item.encoding, item.encoding)
        lines.append(f"a=rtpmap:{payload_type} {rtp_encoding}/{item.clock_rate}")
        fmtp = _serialized_video_fmtp(item)
        if fmtp:
            lines.append(f"a=fmtp:{payload_type} {fmtp}")
        if (
            item.encoding == "JPEG"
            and item.image_attributes is not None
            and (
                item.image_attributes.send is not None
                or item.image_attributes.recv is not None
            )
        ):
            lines.append(
                "a=" + serialize_imageattr(payload_type, item.image_attributes)
            )
        if profile == "RTP/AVPF" or item.rtcp_feedback:
            feedback = item.rtcp_feedback or (
                ("nack pli", "ccm fir") if item.encoding in {"H264", "VP8"} else ()
            )
            for value in feedback:
                lines.append(f"a=rtcp-fb:{payload_type} {value}")
    frame_limits = [
        item.max_framerate for item in formats if item.max_framerate is not None
    ]
    if frame_limits:
        lines.append(f"a=framerate:{min(frame_limits):g}")
    if int(media_port) > 0:
        lines.append(f"a=rtcp:{int(media_port) + 1}")
    lines.append(f"a={direction}")
    return lines


def _rejected_media_line(section: dict) -> str:
    formats = " ".join(str(item) for item in section.get("formats") or ["0"])
    return f"m={section['media']} 0 {section['transport']} {formats}"


def _offered_time_lines(sdp_body: str | bytes) -> list[str]:
    """Return the offer's RFC 3264 session time description unchanged."""

    if isinstance(sdp_body, bytes):
        sdp_body = sdp_body.decode("utf-8", errors="strict")
    lines: list[str] = []
    for raw in sdp_body.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("m="):
            break
        if line.startswith(("t=", "r=")):
            lines.append(line)
    return lines or ["t=0 0"]


def _answer_with_offered_media_order(
    *,
    origin_ip: str,
    media_ip: str,
    audio_lines: list[str],
    remote_sdp: str | bytes,
    video_port: int,
    video_format: RtpH264Format | None,
    video_direction: str | None = None,
) -> str:
    _session_connection, _session_direction, sections = _parse_media_sections(
        remote_sdp
    )
    lines = [
        "v=0",
        _origin_line(origin_ip),
        "s=VoIP Stack",
        _connection_line(media_ip),
        *_offered_time_lines(remote_sdp),
    ]
    used_audio = False
    used_video = False
    for section in sections:
        if (
            section["media"] == "audio"
            and not used_audio
            and section["port"] > 0
            and section["transport"] == "RTP/AVP"
            and section["connection_supported"]
        ):
            lines.extend(audio_lines)
            used_audio = True
            continue
        if section["media"] == "video" and not used_video:
            offered_payloads = {str(item) for item in section.get("formats") or []}
            if (
                video_format is not None
                and int(video_port) > 0
                and section["port"] > 0
                and section["transport"] in {"RTP/AVP", "RTP/AVPF"}
                and section["connection_supported"]
                and not section["rtcp_mux_only"]
                and str(video_format.payload_type) in offered_payloads
            ):
                lines.extend(
                    _video_media_lines(
                        int(video_port),
                        video_format,
                        direction=(
                            suppress_local_send(
                                video_direction
                                or local_direction_for_remote(video_format.direction)
                            )
                            if section["connection_held"]
                            else video_direction
                            or local_direction_for_remote(video_format.direction)
                        ),
                    )
                )
            else:
                lines.append(_rejected_media_line(section))
            used_video = True
            continue
        lines.append(_rejected_media_line(section))
    if not used_audio:
        lines.extend(audio_lines)
    return "\r\n".join(lines) + "\r\n"


def options_capabilities(body: str | bytes) -> dict:
    """Read advisory audio hints, without turning absent ptime into a format."""
    body = SdpDocument.parse(body)
    parsed = parse_sdp(body, capabilities=True)
    formats = offered_pcm_formats(body, capabilities=True)
    # Scope the private attributes to the selected audio section. Other media
    # sections cannot establish directional audio support.
    text = body.text
    sections = re.split(r"(?=^m=)", text.replace("\r\n", "\n"), flags=re.MULTILINE)
    audio = next(
        (
            part
            for part in sections
            if part.startswith("m=audio ")
            and part.splitlines()[0].split()[2].upper() == "RTP/AVP"
        ),
        "",
    )
    flows = parse_audio_flow_attributes(audio)
    direction = parsed["direction"]

    def permits(fmt, sending):
        flow = flows.get(fmt.payload_type)
        if flow:
            return flow in ({"send", "sendrecv"} if sending else {"recv", "sendrecv"})
        return direction in (
            {"sendrecv", "sendonly"} if sending else {"sendrecv", "recvonly"}
        )

    def tokens(sending):
        if independent:
            prefix = "a=x-voip-stack-audio-" + ("tx:" if sending else "rx:")
            advertised = [
                line.strip()[len(prefix) :]
                for line in audio.splitlines()
                if line.strip().startswith(prefix)
            ]
            if advertised:
                if len(advertised) != 1 or len(advertised[0]) > 512:
                    raise SdpError("Invalid directional audio capability list")
                values = advertised[0].split(",")
                if not 1 <= len(values) <= 8:
                    raise SdpError("Too many directional audio capabilities")
                result = []
                for value in values:
                    capability = parse_rtp_audio_capability(value)
                    if not any(
                        _same_rtp_audio_codec(capability, fmt) and permits(fmt, sending)
                        for fmt in formats
                    ):
                        raise SdpError(
                            "Directional capability has no advertised RTP encoding"
                        )
                    token = (
                        f"{capability.encoding}/{capability.sample_rate}/"
                        f"{capability.channels}/{capability.frame_ms}"
                    )
                    if token not in result:
                        result.append(token)
                return result
            if sending:
                # ptime describes this peer's receiver, not its transmitter.
                return []
        if not parsed["ptime"]:
            return []
        result = []
        for fmt in formats:
            if not permits(fmt, sending):
                continue
            token = f"{fmt.encoding}/{fmt.sample_rate}/{fmt.channels}/{parsed['ptime']}"
            try:
                parse_rtp_audio_capability(token, fmt.payload_type)
            except SdpError:
                continue
            result.append(token)
        return result

    # A D1 hint is explicit wire evidence even when its packet time is unknown.
    advertised_flows = {flows.get(fmt.payload_type) for fmt in formats}
    d1 = bool(advertised_flows & {"send", "sendrecv"}) and bool(
        advertised_flows & {"recv", "sendrecv"}
    )
    independent = d1 and supports_independent_audio_ptime(body)
    features = ["directional_audio_v1"] if d1 else []
    if independent:
        features.append("independent_audio_ptime_v1")
    result = {
        "sip_audio_tx_formats": tokens(True),
        "sip_audio_rx_formats": tokens(False),
        "sdp_features": features,
    }
    if (independent
            and any(line.startswith("a=x-voip-stack-audio-tx:") for line in audio.splitlines())
            and any(line.startswith("a=x-voip-stack-audio-rx:") for line in audio.splitlines())):
        result["sip_audio_capabilities_complete"] = True
    sends = any(permits(fmt, True) for fmt in formats)
    receives = any(permits(fmt, False) for fmt in formats)
    if sends or receives:
        result["audio_mode"] = (
            "full_duplex"
            if sends and receives
            else "mic_only"
            if sends
            else "speaker_only"
        )
    # OPTIONS uses port zero for capabilities. Ordinary offer/answer parsing
    # continues to reject zero-port video; discovery reuses its codec checks.
    try:
        video = next(
            (
                fmt
                for fmt in offered_video_formats(body, capabilities=True)
                if fmt.encoding in {"JPEG", "H264"}
            ),
            None,
        )
    except SdpError:
        # An unusable optional video section must not discard valid audio.
        video = None
    if video is not None:
        result["sip_video_codec"] = video.encoding.lower()
    return result


def build_answer_directional(
    origin_ip: str,
    media_ip: str,
    media_port: int,
    send: RtpPcmFormat,
    recv: RtpPcmFormat,
    *,
    dtmf: RtpDtmfFormat | None = None,
    audio_rtcp: bool = True,
    remote_sdp: str | bytes | None = None,
    video_port: int = 0,
    video_format: RtpH264Format | None = None,
    audio_direction: str | None = None,
    video_direction: str | None = None,
) -> str:
    from .media.shadow import engine_mode
    from .sdp_audio import dtmf_format_for_clock

    event_formats = (dtmf,) if dtmf is not None and dtmf.events else ()
    if event_formats and remote_sdp and engine_mode() == "active":
        event_formats = tuple(dict.fromkeys(
            replace(item, events=item.events & dtmf.events)
            for clock in (send.sample_rate, recv.sample_rate)
            if (item := dtmf_format_for_clock(remote_sdp, clock)) is not None
            and item.events & dtmf.events
        ))
    remote_audio = parse_sdp(remote_sdp) if remote_sdp else None
    if audio_direction is None:
        audio_direction = (
            local_direction_for_offer(
                remote_audio["direction"],
                remote_connection_held=bool(remote_audio["connection_held"]),
            )
            if remote_audio is not None
            else "sendrecv"
        )
    elif remote_audio is not None and remote_audio["connection_held"]:
        # Fail safe for callers that pre-compute a direction without knowing
        # about the legacy connection hold.
        audio_direction = suppress_local_send(audio_direction)
    audio_direction = normalize_direction(audio_direction)
    format_direction = format_direction_for_remote(
        remote_audio["direction"] if remote_audio is not None else "sendrecv",
        audio_direction,
    )
    directional_payloads = bool(
        remote_sdp and has_directional_flow_attributes(remote_sdp)
    )
    if format_direction == "sendonly":
        selected = [send]
    elif format_direction == "recvonly":
        selected = [recv]
    else:
        if (
            _rtp_contract_key(send)[:4] != _rtp_contract_key(recv)[:4]
            and not directional_payloads
        ):
            raise SdpError(
                "SDP sendrecv answer requires one RTP payload usable for both TX and RX"
            )
        selected = (
            [recv]
            if _rtp_contract_key(send)[:4] == _rtp_contract_key(recv)[:4]
            else [recv, send]
        )
    payload_values = [str(fmt.payload_type) for fmt in selected]
    for event in event_formats:
        if str(event.payload_type) not in payload_values:
            payload_values.append(str(event.payload_type))
    payloads = " ".join(payload_values)
    audio_lines = [f"m=audio {int(media_port)} RTP/AVP {payloads}"]
    if not audio_rtcp:
        audio_lines.extend(("b=RS:0", "b=RR:0"))
    for fmt in selected:
        audio_lines.append(
            f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}"
        )
        if fmt.encoding == "OPUS":
            opus_fmtp = str(fmt.fmtp or "").strip() or (
                "stereo=0;sprop-stereo=0;maxaveragebitrate=28000"
            )
            audio_lines.append(f"a=fmtp:{fmt.payload_type} {opus_fmtp}")
        if format_direction == "sendrecv" and directional_payloads:
            send_match = _rtp_contract_key(fmt)[:4] == _rtp_contract_key(send)[:4]
            recv_match = _rtp_contract_key(fmt)[:4] == _rtp_contract_key(recv)[:4]
            flow = (
                "sendrecv"
                if send_match and recv_match
                else "send"
                if send_match
                else "recv"
            )
            audio_lines.append(f"a=x-voip-stack-flow:{fmt.payload_type} {flow}")
    for event in event_formats:
        audio_lines.append(
            f"a=rtpmap:{event.payload_type} telephone-event/{event.sample_rate}"
        )
        audio_lines.append(
            f"a=fmtp:{event.payload_type} {_format_dtmf_events(event.events)}"
        )
    if directional_payloads:
        audio_lines.append("a=x-voip-stack-independent-ptime:1")
    receive_maxptime = (
        max(recv.frame_ms, recv.max_frame_ms)
        if format_direction in {"recvonly", "sendrecv"}
        else send.frame_ms
    )
    audio_lines.extend(
        [
            f"a=ptime:{selected[0].frame_ms}",
            f"a=maxptime:{receive_maxptime}",
            f"a={audio_direction}",
        ]
    )
    if remote_sdp:
        _connection, _direction, sections = _parse_media_sections(remote_sdp)
        # RFC 3264 answers retain the offer's media-section count and order.
        # The legacy single-audio fast path is valid only for exactly one
        # audio section; every extra audio/video/data section must be answered
        # explicitly, with port zero when this profile does not select it.
        if len(sections) != 1 or sections[0]["media"] != "audio":
            return _answer_with_offered_media_order(
                origin_ip=origin_ip,
                media_ip=media_ip,
                audio_lines=audio_lines,
                remote_sdp=remote_sdp,
                video_port=video_port,
                video_format=video_format,
                video_direction=video_direction,
            )
    lines = [
        "v=0",
        _origin_line(origin_ip),
        "s=VoIP Stack",
        _connection_line(media_ip),
        *(_offered_time_lines(remote_sdp) if remote_sdp else ["t=0 0"]),
        *audio_lines,
    ]
    return "\r\n".join(lines) + "\r\n"
