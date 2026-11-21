"""SDP media parsing and structural offer/answer validation."""

from __future__ import annotations

import ipaddress
import math
import re

from .imageattr import merge_image_attributes, parse_imageattr
from .media.codecs import STATIC_RTPMAP as STATIC_RTPMAP
from .sdp_document import SdpDocument
from .sdp_types import SdpError


def parse_connection_line(line: str) -> str:
    parts = line.split()
    if len(parts) != 3 or parts[0] != "c=IN" or parts[1] not in {"IP4", "IP6"}:
        raise SdpError(f"unsupported SDP connection: {line}")
    address = parts[2]
    try:
        literal = ipaddress.ip_address(address)
    except ValueError:
        return address
    if (literal.version == 6) != (parts[1] == "IP6"):
        raise SdpError(f"SDP connection address family mismatch: {line}")
    return address




SDP_DIRECTIONS = frozenset({"sendrecv", "sendonly", "recvonly", "inactive"})


def normalize_direction(value: str | None, *, default: str = "sendrecv") -> str:
    """Return one valid RFC 3264 media direction or raise ``SdpError``."""

    direction = str(value or default).strip().lower()
    if direction not in SDP_DIRECTIONS:
        raise SdpError(f"unsupported SDP media direction: {value}")
    return direction


def parse_packet_time(value: str) -> int | float:
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 1000:
        raise ValueError("packet time must be finite and within (0,1000] milliseconds")
    return int(number) if number.is_integer() else number


def parse_sdp(sdp: str | bytes, *, capabilities: bool = False) -> dict:
    document = SdpDocument.parse(sdp)
    session_conn = ""
    media_conn = ""
    media_port = 0
    payload_order: list[int] = []
    rtpmap: dict[int, tuple[str, int, int]] = {}
    fmtp: dict[int, str] = {}
    ptime = 0
    minptime = 0
    maxptime = 0
    saw_media = False
    selected_audio = False
    in_selected_audio = False
    session_direction = "sendrecv"
    media_direction = ""
    for line in document.lines:
        if line.startswith("m="):
            saw_media = True
            in_selected_audio = False
            parts = line.split()
            if (
                selected_audio
                or len(parts) < 4
                or parts[0] != "m=audio"
                or parts[2].upper() != "RTP/AVP"
            ):
                continue
            try:
                candidate_port = int(parts[1])
                candidate_payloads = [int(value) for value in parts[3:]]
            except ValueError as err:
                raise SdpError(f"bad audio media line: {line}") from err
            # Port zero rejects this media stream. Continue looking for the
            # first active RTP/AVP audio section that this single-stream bridge
            # can actually answer.
            if candidate_port == 0 and not capabilities:
                continue
            if not (0 if capabilities else 1) <= candidate_port <= 65535:
                raise SdpError(f"bad audio media port: {candidate_port}")
            if not candidate_payloads or any(
                not 0 <= pt <= 127 for pt in candidate_payloads
            ):
                raise SdpError(f"bad audio payload list: {line}")
            media_port = candidate_port
            payload_order = candidate_payloads
            selected_audio = True
            in_selected_audio = True
            continue
        if line.startswith("c="):
            if not saw_media:
                session_conn = parse_connection_line(line)
            elif in_selected_audio:
                media_conn = parse_connection_line(line)
            continue
        if line in {"a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"}:
            direction = line[2:]
            if not saw_media:
                session_direction = direction
            elif in_selected_audio:
                media_direction = direction
            continue
        if not in_selected_audio:
            continue
        try:
            if line.startswith("a=rtpmap:"):
                left, spec = line.removeprefix("a=rtpmap:").split(None, 1)
                pt = int(left)
                if not 0 <= pt <= 127:
                    raise SdpError(f"bad rtpmap payload type: {pt}")
                bits = spec.split("/")
                if len(bits) == 2:
                    encoding, rate = bits
                    channels = 1
                elif len(bits) == 3:
                    encoding, rate, channels_raw = bits
                    channels = int(channels_raw)
                else:
                    raise SdpError(f"bad rtpmap: {line}")
                rtpmap[pt] = (encoding.upper(), int(rate), channels)
            elif line.startswith("a=fmtp:"):
                left, spec = line.removeprefix("a=fmtp:").split(None, 1)
                fmtp[int(left)] = spec.strip()
            elif line.startswith("a=ptime:"):
                ptime_raw = line.removeprefix("a=ptime:").strip()
                ptime = (
                    0
                    if capabilities and ptime_raw == "0"
                    else parse_packet_time(ptime_raw)
                )
            elif line.startswith("a=minptime:"):
                minptime = parse_packet_time(line.removeprefix("a=minptime:").strip())
            elif line.startswith("a=maxptime:"):
                maxptime = parse_packet_time(line.removeprefix("a=maxptime:").strip())
        except ValueError as err:
            raise SdpError(f"bad SDP audio attribute: {line}") from err
    connection_ip = media_conn or session_conn
    if not connection_ip or (not media_port and not capabilities) or not payload_order:
        raise SdpError("SDP missing c=, m=audio port, or payload list")
    return {
        "connection_ip": connection_ip,
        "media_port": media_port,
        "payload_order": payload_order,
        "rtpmap": rtpmap,
        "fmtp": fmtp,
        "ptime": ptime,
        "minptime": minptime,
        "maxptime": maxptime,
        "direction": media_direction or session_direction,
        "connection_held": connection_ip == "0.0.0.0",
    }


def parse_media_sections(
    sdp_body: str | bytes,
    *,
    capabilities: bool = False,
) -> tuple[str, str, list[dict]]:
    """Parse enough SDP structure to negotiate independent media sections."""

    document = SdpDocument.parse(sdp_body)
    session_connection = ""
    session_direction = "sendrecv"
    current: dict | None = None
    sections: list[dict] = []
    for line in document.lines:
        if line.startswith("m="):
            parts = line[2:].split()
            if len(parts) < 4:
                raise SdpError(f"bad media line: {line}")
            try:
                port = int(parts[1])
            except ValueError as err:
                raise SdpError(f"bad media port: {line}") from err
            if not 0 <= port <= 65535:
                raise SdpError(f"bad media port: {port}")
            current = {
                "media": parts[0].lower(),
                "port": port,
                "transport": parts[2].upper(),
                "formats": parts[3:],
                "connection_ip": "",
                "connection_seen": False,
                "connection_supported": True,
                "rtpmap": {},
                "fmtp": {},
                "rtcp_feedback": {},
                "rtcp_port": 0,
                "rtcp_address": "",
                "rtcp_address_supported": True,
                "rtcp_mux": False,
                "rtcp_mux_only": False,
                "framerate": None,
                "image_attributes": {},
                "direction": session_direction,
            }
            sections.append(current)
            continue
        if line.startswith("c="):
            if current is None:
                session_connection = parse_connection_line(line)
            else:
                current["connection_seen"] = True
                try:
                    current["connection_ip"] = parse_connection_line(line)
                except SdpError:
                    # A media-level connection applies only to that media
                    # section. Preserve a usable audio section when another
                    # media section advertises an unsupported address family.
                    current["connection_ip"] = ""
                    current["connection_supported"] = False
            continue
        if line in {"a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"}:
            direction = line[2:]
            if current is None:
                session_direction = direction
            else:
                current["direction"] = direction
            continue
        if current is None:
            continue
        try:
            if line.startswith("a=rtpmap:"):
                left, spec = line.removeprefix("a=rtpmap:").split(None, 1)
                current["rtpmap"][int(left)] = spec.strip()
            elif line.startswith("a=fmtp:"):
                left, spec = line.removeprefix("a=fmtp:").split(None, 1)
                current["fmtp"][int(left)] = spec.strip()
            elif line.startswith("a=rtcp-fb:"):
                left, spec = line.removeprefix("a=rtcp-fb:").split(None, 1)
                key: int | str = "*" if left == "*" else int(left)
                current["rtcp_feedback"].setdefault(key, []).append(spec.strip())
            elif line.startswith("a=rtcp:"):
                parts = line.removeprefix("a=rtcp:").split()
                if len(parts) not in {1, 4}:
                    raise SdpError(f"bad SDP RTCP attribute: {line}")
                rtcp_port = int(parts[0])
                # RFC 3264 section 9 uses zero media ports for capabilities,
                # not transport endpoints. Accept the matching RTCP placeholder
                # only in that context; offer/answer validation stays strict.
                capability_placeholder = capabilities and current["port"] == 0
                if not (
                    1 <= rtcp_port <= 65535
                    or (rtcp_port == 0 and capability_placeholder)
                ):
                    raise SdpError(f"bad SDP RTCP port: {rtcp_port}")
                current["rtcp_port"] = rtcp_port
                if len(parts) == 4:
                    if parts[1].upper() == "IN" and parts[2].upper() == "IP4":
                        current["rtcp_address"] = parts[3]
                    else:
                        current["rtcp_address_supported"] = False
            elif line == "a=rtcp-mux":
                current["rtcp_mux"] = True
            elif line == "a=rtcp-mux-only":
                current["rtcp_mux_only"] = True
            elif line.startswith("a=imageattr:"):
                # Optional capabilities cannot invalidate an otherwise usable
                # audio/video SDP. A later valid PT-specific value wins.
                parsed_image = parse_imageattr(line.removeprefix("a=imageattr:"))
                if parsed_image is not None and len(current["image_attributes"]) < 32:
                    payload, attributes = parsed_image
                    current["image_attributes"][payload] = merge_image_attributes(
                        current["image_attributes"].get(payload), attributes
                    )
            elif line.startswith("a=framerate:"):
                framerate = float(line.removeprefix("a=framerate:").strip())
                if not math.isfinite(framerate) or framerate <= 0:
                    raise SdpError(f"bad SDP video framerate: {line}")
                current["framerate"] = framerate
        except ValueError as err:
            raise SdpError(f"bad SDP media attribute: {line}") from err
    for section in sections:
        if not section["connection_seen"]:
            section["connection_ip"] = session_connection
        section["connection_held"] = section["connection_ip"] == "0.0.0.0"
    return session_connection, session_direction, sections


def section_rtpmap(section: dict, payload_type: int) -> tuple[str, int, int] | None:
    """Normalize one RTP mapping used by offer/answer validation."""

    explicit = str(section["rtpmap"].get(payload_type) or "").strip()
    if explicit:
        parts = explicit.split("/")
        if len(parts) not in {2, 3}:
            raise SdpError(f"bad rtpmap for payload type {payload_type}")
        try:
            return (
                parts[0].strip().upper(),
                int(parts[1]),
                int(parts[2]) if len(parts) == 3 else 1,
            )
        except ValueError as err:
            raise SdpError(f"bad rtpmap for payload type {payload_type}") from err
    media = str(section["media"])
    if media == "audio" and payload_type in STATIC_RTPMAP:
        return STATIC_RTPMAP[payload_type]
    if media == "video":
        if payload_type == 26:
            return ("JPEG", 90000, 1)
        if payload_type == 34:
            return ("H263", 90000, 1)
    return None


def normalized_rtcp_feedback(values: list[str] | tuple[str, ...]) -> set[str]:
    """Normalize RFC 4585 feedback tokens for capability comparison."""

    return {" ".join(str(value).strip().lower().split()) for value in values}


def equivalent_rtp_payloads(
    offered_section: dict,
    answered_section: dict,
) -> dict[int, set[int]]:
    """Map each answer payload to equivalent payloads in the offer.

    RFC 3264 recommends reusing the same payload type but explicitly permits
    distinct mappings in each direction. The offerer's RTP uses the answer
    number while the answerer's RTP uses the offer number. Consequently the
    same dynamic number may identify different formats in the two payload
    spaces and must be compared by codec contract, not by number alone.
    """

    try:
        offered_payloads = {int(value) for value in offered_section.get("formats", ())}
        answered_payloads = {
            int(value) for value in answered_section.get("formats", ())
        }
    except ValueError as err:
        raise SdpError("RTP media formats must be numeric payload types") from err

    mappings: dict[int, set[int]] = {}
    for answer_payload in answered_payloads:
        answer_mapping = section_rtpmap(answered_section, answer_payload)
        if answer_mapping is None:
            continue
        equivalent = {
            offer_payload
            for offer_payload in offered_payloads
            if section_rtpmap(offered_section, offer_payload) == answer_mapping
        }
        if equivalent:
            mappings[answer_payload] = equivalent
    return mappings


def validate_sdp_answer(
    offer: str | bytes,
    answer: str | bytes,
    *,
    allow_omitted_trailing_media: bool = False,
    allow_inactive_rejected_media_port: bool = False,
    allow_video_direction_capability: bool = False,
) -> None:
    """Validate the RFC 3264 media-section and direction answer contract.

    An answer cannot add, remove, or reorder ``m=`` sections. Accepted media
    must retain the offered media type and transport profile and select at
    least one offered codec. RFC 3264 permits a different RTP payload number
    for that codec in the answer, so payload identity is kept directional.
    Direction is expressed from each endpoint's perspective, so one-way
    offers have only one legal active inverse.
    """

    _offer_connection, _offer_direction, offered = parse_media_sections(offer)
    _answer_connection, _answer_direction, answered = parse_media_sections(answer)
    if not offered:
        raise SdpError("SDP offer has no media sections")
    if len(answered) > len(offered) or (
        len(answered) < len(offered) and not allow_omitted_trailing_media
    ):
        raise SdpError(
            "SDP answer must preserve the offer's media-section count and order"
        )
    if allow_omitted_trailing_media and len(answered) < len(offered):
        # A few PSTN gateways return only the accepted leading audio section
        # instead of retaining later rejected media with port zero.  This is
        # not RFC 3264 compliant, but treating those *trailing* sections as
        # rejected is unambiguous and preserves useful audio interoperability.
        # Never allow an omitted leading/accepted audio section.
        omitted = offered[len(answered) :]
        if not answered or any(section["media"] == "audio" for section in omitted):
            raise SdpError("SDP answer omitted a required audio media section")
    allowed_directions = {
        "sendrecv": {"sendrecv", "sendonly", "recvonly", "inactive"},
        "sendonly": {"recvonly", "inactive"},
        "recvonly": {"sendonly", "inactive"},
        "inactive": {"inactive"},
    }
    for index, (offered_section, answered_section) in enumerate(
        zip(offered, answered, strict=False)
    ):
        media = str(offered_section["media"])
        if str(answered_section["media"]) != media:
            raise SdpError(
                f"SDP answer media section {index} changed {media} to "
                f"{answered_section['media']}"
            )
        if str(answered_section["transport"]) != str(offered_section["transport"]):
            raise SdpError(
                f"SDP answer media section {index} changed the transport profile"
            )
        if int(offered_section["port"]) == 0 and int(answered_section["port"]) != 0:
            if not (
                allow_inactive_rejected_media_port
                and normalize_direction(str(answered_section["direction"]))
                == "inactive"
            ):
                raise SdpError(
                    f"SDP answer media section {index} accepted a rejected offer"
                )
        answered_formats = {str(value) for value in answered_section["formats"]}
        if not answered_formats:
            raise SdpError(f"SDP answer media section {index} has no format")
        if int(answered_section["port"]) == 0:
            continue

        transport = str(answered_section["transport"]).upper()
        equivalent_payloads: dict[int, set[int]] = {}
        if transport.startswith("RTP/"):
            equivalent_payloads = equivalent_rtp_payloads(
                offered_section,
                answered_section,
            )
            if not equivalent_payloads:
                raise SdpError(
                    f"SDP answer media section {index} selected no offered codec"
                )
        else:
            offered_formats = {str(value) for value in offered_section["formats"]}
            if not answered_formats.intersection(offered_formats):
                raise SdpError(
                    f"SDP answer media section {index} selected an unoffered format"
                )
        # Some deployed SIP UAs, including Zoiper, add ``a=rtcp-mux`` to an
        # RTP/AVP answer even when the offer omitted it. RFC 5761 requires the
        # attribute to be offered first, but rejecting an otherwise compatible
        # audio answer tears down the established dialog. We do not advertise
        # mux support and safely ignore the unsolicited answer attribute.
        if bool(answered_section["rtcp_mux_only"]) and not bool(
            offered_section["rtcp_mux_only"]
        ):
            raise SdpError(
                f"SDP answer media section {index} added unoffered rtcp-mux-only"
            )
        # RFC 4585 defines rtcp-fb only for RTP/AVPF. Some otherwise useful
        # SIP peers serialize those attributes on RTP/AVP as well; they have
        # no negotiated meaning there and are ignored, matching
        # ``offered_video_formats``. AVPF remains strict offer/answer.
        if transport == "RTP/AVPF":
            offered_feedback = offered_section["rtcp_feedback"]
            wildcard_feedback = normalized_rtcp_feedback(offered_feedback.get("*", []))
            answered_payloads = {
                int(value) for value in answered_formats if str(value).isdigit()
            }
            for key, values in answered_section["rtcp_feedback"].items():
                answer_feedback = normalized_rtcp_feedback(values)
                if key == "*":
                    allowed_feedback = wildcard_feedback
                else:
                    if int(key) not in answered_payloads:
                        raise SdpError(
                            f"SDP answer media section {index} attached RTCP "
                            "feedback to an unselected payload type"
                        )
                    allowed_feedback = set(wildcard_feedback)
                    for offered_payload in equivalent_payloads.get(int(key), ()):
                        allowed_feedback.update(
                            normalized_rtcp_feedback(
                                offered_feedback.get(offered_payload, [])
                            )
                        )
                if not answer_feedback.issubset(allowed_feedback):
                    raise SdpError(
                        f"SDP answer media section {index} added unoffered "
                        "RTCP feedback"
                    )
        offer_direction = normalize_direction(str(offered_section["direction"]))
        answer_direction = normalize_direction(str(answered_section["direction"]))
        if answer_direction not in allowed_directions[offer_direction]:
            # Some UAs report video capabilities rather than an effective
            # answer direction. Opting-in callers must intersect with their
            # offer, as PJSIP does, including inactive for equal one-way sides.
            if (
                allow_video_direction_capability
                and media == "video"
                and offer_direction in {"sendonly", "recvonly"}
                and answer_direction in {"sendrecv", offer_direction}
            ):
                continue
            raise SdpError(
                f"SDP answer direction {answer_direction} is invalid for "
                f"{offer_direction} offer in media section {index}"
            )


def parse_video_sdp(
    sdp_body: str | bytes, *, capabilities: bool = False
) -> dict | None:
    """Return the first video media section when it matches this profile."""

    _session_connection, _session_direction, sections = parse_media_sections(
        sdp_body,
        capabilities=capabilities,
    )
    for section in sections:
        if section["media"] != "video":
            continue
        # This deliberately small profile owns one video stream. Do not skip
        # an unsupported first m=video and accidentally answer a later format
        # in the first section's position.
        if (
            (section["port"] == 0 and not capabilities)
            or section["transport"] not in {"RTP/AVP", "RTP/AVPF"}
            or not section["connection_supported"]
            or not section["rtcp_address_supported"]
            or section["rtcp_mux_only"]
        ):
            return None
        if not section["connection_ip"]:
            raise SdpError("SDP video section has no connection address")
        payload_order: list[int] = []
        try:
            payload_order = [int(item) for item in section["formats"]]
        except ValueError as err:
            raise SdpError("bad video payload list") from err
        if not payload_order or any(not 0 <= item <= 127 for item in payload_order):
            raise SdpError("bad video payload list")
        return {
            "connection_ip": section["connection_ip"],
            "media_port": section["port"],
            "transport_profile": section["transport"],
            "payload_order": payload_order,
            "rtpmap": dict(section["rtpmap"]),
            "fmtp": dict(section["fmtp"]),
            "rtcp_feedback": dict(section["rtcp_feedback"]),
            "rtcp_port": int(section["rtcp_port"] or 0),
            "rtcp_address": str(section["rtcp_address"] or ""),
            "rtcp_mux": bool(section["rtcp_mux"]),
            "rtcp_mux_only": bool(section["rtcp_mux_only"]),
            "framerate": section["framerate"],
            "image_attributes": dict(section["image_attributes"]),
            "direction": section["direction"],
            "connection_held": bool(section["connection_held"]),
        }
    return None


def format_direction_for_remote(
    remote_direction: str,
    effective_local_direction: str,
    *,
    inactive_default: str = "sendrecv",
) -> str:
    """Return which local capabilities determine an SDP media format list."""

    effective_local_direction = normalize_direction(effective_local_direction)
    if effective_local_direction != "inactive":
        return effective_local_direction
    remote_direction = normalize_direction(remote_direction)
    if remote_direction == "sendonly":
        return "recvonly"
    if remote_direction == "recvonly":
        return "sendonly"
    inactive_default = normalize_direction(inactive_default)
    return inactive_default if inactive_default != "inactive" else "sendrecv"


def fmtp_parameters(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in str(value or "").split(";"):
        key, separator, raw_value = item.strip().partition("=")
        if key:
            out[key.lower()] = raw_value.strip() if separator else ""
    return out


def fmtp_without_parameters(value: str, excluded: set[str]) -> str:
    parameters = fmtp_parameters(value)
    return ";".join(
        f"{key}={item}" if item else key
        for key, item in parameters.items()
        if key not in excluded
    )


def local_direction_for_remote(remote_direction: str) -> str:
    """Return the RFC 3264 answer/local direction for a remote direction."""

    remote_direction = normalize_direction(remote_direction)
    return {
        "sendonly": "recvonly",
        "recvonly": "sendonly",
        "inactive": "inactive",
    }.get(str(remote_direction or "sendrecv").lower(), "sendrecv")


def local_direction_for_offer(
    remote_direction: str,
    *,
    remote_connection_held: bool = False,
) -> str:
    """Return the local direction, suppressing TX for legacy c=0 hold."""

    return constrained_media_direction(
        remote_direction,
        allow_send=not remote_connection_held,
        allow_receive=True,
    )


def suppress_local_send(direction: str) -> str:
    """Remove the local send bit while preserving receive permission."""

    direction = normalize_direction(direction)
    return {
        "sendrecv": "recvonly",
        "sendonly": "inactive",
    }.get(direction, direction)


def constrained_media_direction(
    remote_direction: str,
    *,
    allow_send: bool,
    allow_receive: bool = True,
) -> str:
    """Intersect an RFC 3264 offer direction with local media capabilities."""

    remote = normalize_direction(remote_direction)
    can_send = bool(allow_send and remote in {"sendrecv", "recvonly"})
    can_receive = bool(allow_receive and remote in {"sendrecv", "sendonly"})
    if can_send and can_receive:
        return "sendrecv"
    if can_send:
        return "sendonly"
    if can_receive:
        return "recvonly"
    return "inactive"


def supports_independent_audio_ptime(body: str | bytes) -> bool:
    """Read the versioned capability only from the advertised audio media."""
    text = (
        body.text
        if isinstance(body, SdpDocument)
        else (
            body.decode("utf-8", errors="strict") if isinstance(body, bytes) else body
        )
    )
    sections = re.split(r"(?=^m=)", text.replace("\r\n", "\n"), flags=re.MULTILINE)
    audio = next(
        (
            part
            for part in sections
            if part.startswith("m=audio ")
            and len(part.splitlines()[0].split()) >= 4
            and part.splitlines()[0].split()[2].upper() in {"RTP/AVP", "RTP/AVPF"}
        ),
        "",
    )
    return "a=x-voip-stack-independent-ptime:1" in {
        line.strip() for line in audio.splitlines()[1:]
    }


def parse_audio_flow_attributes(remote_sdp: str | bytes) -> dict[int, str]:
    text = (
        remote_sdp.text
        if isinstance(remote_sdp, SdpDocument)
        else (
            remote_sdp.decode("utf-8", errors="strict")
            if isinstance(remote_sdp, bytes)
            else remote_sdp
        )
    )
    return {
        int(match.group(1)): match.group(2).lower()
        for match in re.finditer(
            r"^a=x-voip-stack-flow:(\d+)\s+(send|recv|sendrecv)\s*$",
            text.replace("\r\n", "\n"),
            flags=re.IGNORECASE | re.MULTILINE,
        )
    }


def has_directional_flow_attributes(remote_sdp: str | bytes) -> bool:
    flows = set(parse_audio_flow_attributes(remote_sdp).values())
    can_send = bool(flows & {"send", "sendrecv"})
    can_recv = bool(flows & {"recv", "sendrecv"})
    return can_send and can_recv
