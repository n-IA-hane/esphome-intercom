"""Serialize complete SDP offers and answers from negotiated media contracts."""

from __future__ import annotations

import ipaddress

from .audio_format import AudioFormat
from .media.capability import MediaCompatibility
from .media.codecs import DEFAULT_OPUS_RECEIVE_FMTP
from .imageattr import serialize_imageattr
from .sdp_audio_facts import (
    _format_dtmf_events,
    offered_pcm_formats,
    parse_rtp_audio_capability,
)
from .sdp_document import SdpDocument
from .sdp_parser import (
    audio_rtcp_allowed,
    local_direction_for_offer,
    local_direction_for_remote,
    normalize_direction,
    parse_sdp,
    suppress_local_send,
)
from .sdp_parser import (
    parse_media_sections as _parse_media_sections,
)
from .sdp_types import (
    RtpDtmfFormat,
    RtpH264Format,
    RtpPcmFormat,
    RtpPcmDirection,
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
    # A description without an origin has no version field to replace.
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
    audio_rtcp: bool = False,
    compatibility: MediaCompatibility = MediaCompatibility(),
    video_port: int = 0,
    video_format: RtpH264Format | None = None,
    video_formats: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat] | None = None,
    audio_direction: str = "sendrecv",
    video_direction: str = "sendrecv",
    audio_rtp_formats: tuple[RtpPcmFormat, ...] | None = None,
    send_rtp_formats: tuple[RtpPcmFormat, ...] | None = None,
    recv_rtp_formats: tuple[RtpPcmFormat, ...] | None = None,
    codec_policy=None,
    local_capabilities=None,
    previous_payloads=None,
) -> str:
    audio_direction = normalize_direction(audio_direction)
    video_direction = normalize_direction(video_direction)
    from .media import negotiator
    try:
        plan = negotiator.create_offer(send_formats, recv_formats, codec_policy=codec_policy,
            capabilities=local_capabilities, audio_rtp_formats=audio_rtp_formats,
            send_rtp_formats=send_rtp_formats, recv_rtp_formats=recv_rtp_formats,
            common=include_common_codecs, compatibility=compatibility,
            direction=audio_direction, previous=previous_payloads)
    except ValueError as error:
        raise SdpError(str(error)) from error
    return _write_offer_plan(plan, origin_ip, media_ip, media_port, audio_direction=audio_direction,
        audio_rtcp=audio_rtcp, video_port=video_port,
        video_formats=video_formats or (() if video_format is None else (video_format,)),
        video_direction=video_direction, codec_policy=codec_policy)


def _write_offer_plan(plan, origin_ip, media_ip, media_port, *, audio_direction, audio_rtcp,
                      video_port, video_formats, video_direction, codec_policy):
    """Serialize decisions already owned by the negotiator, without choosing codecs."""
    lines = ["v=0", _origin_line(origin_ip), "s=VoIP Stack", _connection_line(media_ip), "t=0 0",
             f"m=audio {int(media_port)} RTP/AVP " + " ".join(str(number) for number, _mapping in plan.payloads)]
    if not audio_rtcp:
        lines.extend(("b=RS:0", "b=RR:0"))
    for fmt in plan.formats:
        lines.append(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}")
        parameters = str(fmt.fmtp or "").strip()
        if not parameters and fmt.encoding == "OPUS":
            parameters = DEFAULT_OPUS_RECEIVE_FMTP
        if parameters:
            lines.append(f"a=fmtp:{fmt.payload_type} {parameters}")
    for event in plan.events:
        lines.extend((f"a=rtpmap:{event.payload_type} telephone-event/{event.sample_rate}",
                      f"a=fmtp:{event.payload_type} {_format_dtmf_events(event.events)}"))
    if plan.packet_ms:
        lines.append(f"a=ptime:{plan.packet_ms:g}")
    if plan.maximum_ms:
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


def _offered_time_lines(sdp_body: str | bytes | SdpDocument) -> list[str]:
    """Return the offer's RFC 3264 session time description unchanged."""

    lines: list[str] = []
    for line in SdpDocument.parse(sdp_body).lines:
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
    remote_sdp: str | bytes | SdpDocument,
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
    direction = parsed["direction"]
    sends = direction in {"sendrecv", "sendonly"}
    receives = direction in {"sendrecv", "recvonly"}
    # Standard ptime is a receiver preference. OPTIONS cannot establish the
    # transmitter's producible durations or a complete directional catalog.
    # Keep codec geometry evidence separate from packet-time preferences.
    receive_tokens = []
    transmit_tokens = [f"{fmt.encoding}/{fmt.sample_rate}/{fmt.channels}" for fmt in formats] if sends else []
    if receives and parsed["ptime"]:
        for fmt in formats:
            token = f"{fmt.encoding}/{fmt.sample_rate}/{fmt.channels}/{parsed['ptime']}"
            try:
                parse_rtp_audio_capability(token, fmt.payload_type)
            except SdpError:
                continue
            receive_tokens.append(token)
    elif receives:
        receive_tokens = [f"{fmt.encoding}/{fmt.sample_rate}/{fmt.channels}" for fmt in formats]
    result = {
        "sip_audio_tx_formats": transmit_tokens,
        "sip_audio_rx_formats": receive_tokens,
        "sip_audio_receiver_ptime": parsed["ptime"],
        "sip_audio_receiver_maxptime": parsed["maxptime"],
    }
    if sends or receives:
        result["audio_mode"] = (
            "full_duplex" if sends and receives else "mic_only" if sends else "speaker_only")
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
    recv_formats: tuple[RtpPcmFormat, ...] = (),
    send_formats: tuple[RtpPcmFormat, ...] = (),
    dtmf: RtpDtmfFormat | None = None,
    dtmf_direction=None,
    audio_rtcp: bool = False,
    remote_sdp: str | bytes | SdpDocument | None = None,
    video_port: int = 0,
    video_format: RtpH264Format | None = None,
    audio_direction: str | None = None,
    video_direction: str | None = None,
) -> str:
    from .media.negotiator import answer_audio_projection

    # Legacy callers may supply one already chosen mapping. Current owners
    # supply the complete direction; serialization never negotiates it again.
    event_formats = (dtmf,) if dtmf is not None and dtmf.events else ()
    if dtmf_direction is not None:
        event_formats = dtmf_direction.recv_formats or ((dtmf_direction.recv,) if dtmf_direction.recv is not None else ())
    remote_audio = parse_sdp(remote_sdp) if remote_sdp else None
    audio_rtcp = bool(audio_rtcp and (not remote_sdp or audio_rtcp_allowed(remote_sdp)))
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
        # about the zero-address connection hold.
        audio_direction = suppress_local_send(audio_direction)
    audio_direction = normalize_direction(audio_direction)
    selected, packet_ms, receive_maxptime = answer_audio_projection(
        RtpPcmDirection(send, recv, recv_formats=recv_formats, send_formats=send_formats),
        direction=audio_direction, remote_sdp=remote_sdp)
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
                DEFAULT_OPUS_RECEIVE_FMTP
            )
            audio_lines.append(f"a=fmtp:{fmt.payload_type} {opus_fmtp}")
    for event in event_formats:
        audio_lines.append(
            f"a=rtpmap:{event.payload_type} telephone-event/{event.sample_rate}"
        )
        audio_lines.append(
            f"a=fmtp:{event.payload_type} {_format_dtmf_events(event.events)}"
        )
    audio_lines.extend(
        [
            f"a=ptime:{packet_ms}",
            f"a=maxptime:{receive_maxptime}",
            f"a={audio_direction}",
        ]
    )
    if remote_sdp:
        _connection, _direction, sections = _parse_media_sections(remote_sdp)
        # RFC 3264 answers retain the offer's media-section count and order.
        # The single-audio fast path is valid only for exactly one
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
