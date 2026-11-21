"""JPEG, H.264 and VP8 contracts and directional video negotiation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction

from .imageattr import (
    ANY_IMAGE_ATTRIBUTES,
    ImageAttributeFallbackRequired,
    ImageAttributes,
    constraints_usable,
    intersect_constraints,
    plan_jpeg_image,
    resolve_image_attributes,
)
from .sdp_parser import (
    constrained_media_direction,
    fmtp_parameters,
    fmtp_without_parameters,
    normalize_direction,
    parse_video_sdp,
)
from .sdp_types import RtpH264Format, RtpVideoDirection, RtpVideoFormat

_RFC6184_DEFAULT_PROFILE_LEVEL_ID = "42000a"


# Kept as a public compatibility alias for integrations and tests that used
# the original H.264-only type before the video format model was widened.


DEFAULT_H264_FORMAT = RtpH264Format()

CONSTRAINED_BASELINE_H264_FORMAT = RtpH264Format(
    payload_type=105,
    profile_level_id="42c01f",
)

DEFAULT_VIDEO_FORMATS = (
    DEFAULT_H264_FORMAT,
    CONSTRAINED_BASELINE_H264_FORMAT,
    RtpVideoFormat(
        payload_type=104,
        encoding="VP8",
        fmtp="max-fr=20;max-fs=3600",
    ),
    RtpVideoFormat(
        payload_type=26, encoding="JPEG", image_attributes=ANY_IMAGE_ATTRIBUTES
    ),
)

_SUPPORTED_RTCP_FEEDBACK = frozenset({"nack pli", "ccm fir"})


def video_offer_formats_for_target_codec(
    codec: str,
    available: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat] | None = None,
) -> tuple[RtpVideoFormat, ...]:
    """Return the narrowest honest offer for one advertised peer codec."""

    formats = tuple(DEFAULT_VIDEO_FORMATS if available is None else available)
    normalized = str(codec or "").strip().upper()
    if normalized == "JPEG":
        return tuple(fmt for fmt in formats if fmt.encoding == "JPEG")[:1]
    if normalized == "H264":
        matching = tuple(fmt for fmt in formats if fmt.encoding == "H264")
        if not matching:
            return ()
        constrained = next(
            (
                fmt
                for fmt in matching
                if fmt.profile_level_id.lower().startswith("42c0")
            ),
            None,
        )
        return (constrained or matching[0],)
    return formats


def browser_video_send_supported(video_format: RtpVideoFormat | None) -> bool:
    """Return whether the browser bridge can packetize this negotiated codec."""

    if video_format is None:
        return False
    return video_format.encoding in {"VP8", "JPEG"} or (
        video_format.encoding == "H264" and video_format.packetization_mode == 1
    )


def browser_video_receive_supported(video_format: RtpVideoFormat | None) -> bool:
    """Return whether the direct browser bridge can render this codec."""

    return bool(
        video_format is not None and video_format.encoding in {"H264", "VP8", "JPEG"}
    )


def remote_can_send(video_format: RtpVideoFormat | None) -> bool:
    """Return whether a remote endpoint may send media in its SDP direction."""

    return bool(
        video_format is not None and video_format.direction in {"sendonly", "sendrecv"}
    )


def remote_can_receive(
    video_format: RtpVideoFormat | None,
    *,
    connection_held: bool = False,
) -> bool:
    """Return whether a remote endpoint may receive media in its SDP direction."""

    return bool(
        video_format is not None
        and not connection_held
        and video_format.direction in {"recvonly", "sendrecv"}
    )


def video_formats_passthrough_compatible(
    source: RtpVideoFormat | None,
    destination: RtpVideoFormat | None,
) -> bool:
    """Return whether encoded RTP payloads may be relayed without transcoding.

    Payload type and direction are leg-local and therefore intentionally not
    compared.  H.264 packetization and profile bytes are bitstream contracts;
    forwarding a stream across a different contract can produce a call that
    negotiates successfully but cannot be decoded by the destination.
    """

    if source is None or destination is None:
        return False
    if (
        source.encoding != destination.encoding
        or source.clock_rate != destination.clock_rate
    ):
        return False
    if source.transport_profile != destination.transport_profile:
        return False
    if source.encoding == "H264":
        return bool(
            source.packetization_mode == destination.packetization_mode
            and _h264_stream_fits(source, destination)
        )
    if source.encoding == "VP8":
        # RFC 7741 defines max-fr/max-fs exclusively as receiver
        # capabilities. Different values on the two dialog legs constrain
        # the respective senders; they do not change the VP8 RTP payload
        # format and therefore do not require transcoding.
        return True
    return fmtp_parameters(source.fmtp) == fmtp_parameters(destination.fmtp)


def video_formats_renegotiation_compatible(
    previous: RtpVideoFormat | None,
    updated: RtpVideoFormat | None,
) -> bool:
    """Return whether live media can adopt an updated codec contract.

    Payload type and direction can be updated by the caller.  H.264 permits
    the level part to change while its sub-profile and packetization mode
    remain fixed; other encoded formats keep their complete fmtp contract.
    """

    if previous is None or updated is None:
        return False
    if (
        previous.encoding != updated.encoding
        or previous.clock_rate != updated.clock_rate
        or previous.transport_profile != updated.transport_profile
    ):
        return False
    if previous.encoding == "H264":
        return bool(
            previous.packetization_mode == updated.packetization_mode
            and _h264_subprofiles_compatible(previous, updated)
        )
    if previous.encoding == "VP8":
        return True
    return fmtp_parameters(previous.fmtp) == fmtp_parameters(updated.fmtp)


def directional_video_renegotiation_compatible(
    previous_send: RtpVideoFormat | None,
    previous_recv: RtpVideoFormat | None,
    updated_send: RtpVideoFormat | None,
    updated_recv: RtpVideoFormat | None,
) -> bool:
    """Validate only codec paths active before and after a new offer.

    Direction changes are normal offer/answer renegotiation, not transcoding.
    In particular, ``recvonly`` -> ``sendrecv`` activates a receive path for
    the first time, so its previous inactive candidate cannot veto the offer.
    """

    if previous_send is None or updated_send is None:
        return False
    previous_direction = normalize_direction(previous_send.direction)
    updated_direction = normalize_direction(updated_send.direction)
    send_was_active = previous_direction in {"sendrecv", "recvonly"}
    send_is_active = updated_direction in {"sendrecv", "recvonly"}
    recv_was_active = previous_direction in {"sendrecv", "sendonly"}
    recv_is_active = updated_direction in {"sendrecv", "sendonly"}
    return bool(
        (
            not (send_was_active and send_is_active)
            or video_formats_renegotiation_compatible(previous_send, updated_send)
        )
        and (
            not (recv_was_active and recv_is_active)
            or video_formats_renegotiation_compatible(previous_recv, updated_recv)
        )
    )


def video_answer_contract(
    offered: RtpVideoFormat,
    answered: RtpVideoFormat,
) -> RtpVideoFormat | None:
    """Project an accepted answer level onto the offerer's media leg.

    A B2BUA relaying encoded H.264 must answer its source leg with the level
    selected on the destination leg.  Keeping the offerer's payload type and
    equivalent sub-profile while copying only the changeable level prevents
    both over-advertising and accidental payload/constraint remapping.
    """

    if (
        offered.encoding != answered.encoding
        or offered.clock_rate != answered.clock_rate
        or offered.transport_profile != answered.transport_profile
    ):
        return None
    if offered.encoding == "VP8":
        # The answer describes the answering endpoint's decoder limits. A
        # B2BUA relays those limits to the original offerer while retaining
        # the original dialog leg's payload type.
        return replace(
            offered, fmtp=answered.fmtp, max_framerate=answered.max_framerate
        )
    if offered.encoding != "H264":
        return (
            replace(
                offered,
                max_framerate=answered.max_framerate,
                image_attributes=answered.image_attributes
                if offered.image_attributes is not None
                else None,
            )
            if fmtp_parameters(offered.fmtp) == fmtp_parameters(answered.fmtp)
            else None
        )
    if (
        offered.packetization_mode != answered.packetization_mode
        or not _h264_answer_compatible(offered, answered)
    ):
        return None
    profile_level_id = _h264_answer_level_for_offer(
        offered.profile_level_id,
        answered.profile_level_id,
    )
    if profile_level_id is None:
        return None
    receiver = replace(
        answered,
        payload_type=offered.payload_type,
        profile_level_id=profile_level_id,
        level_asymmetry_allowed=bool(
            offered.level_asymmetry_allowed and answered.level_asymmetry_allowed
        ),
        direction=offered.direction,
        transport_profile=offered.transport_profile,
    )
    return _h264_stream_contract(receiver, answered)


def video_offer_answer_directional(
    offered: RtpVideoFormat,
    answered: RtpVideoFormat,
) -> RtpVideoDirection | None:
    """Return local stream contracts for an already selected local answer."""

    if (
        offered.encoding != answered.encoding
        or offered.clock_rate != answered.clock_rate
        or offered.transport_profile != answered.transport_profile
    ):
        return None
    if offered.encoding == "H264":
        if (
            offered.packetization_mode != answered.packetization_mode
            or not _h264_answer_compatible(offered, answered)
        ):
            return None
        bilateral_asymmetry = bool(
            offered.level_asymmetry_allowed and answered.level_asymmetry_allowed
        )
        if bilateral_asymmetry:
            send_receiver = offered
            recv_receiver = answered
        else:
            common_profile = _h264_answer_level_for_offer(
                offered.profile_level_id,
                answered.profile_level_id,
            )
            if common_profile is None:
                return None
            send_receiver = replace(
                offered,
                profile_level_id=common_profile,
                level_asymmetry_allowed=False,
            )
            recv_receiver = replace(
                answered,
                profile_level_id=common_profile,
                level_asymmetry_allowed=False,
            )
        return RtpVideoDirection(
            send=_h264_stream_contract(send_receiver, answered),
            recv=_h264_stream_contract(recv_receiver, offered),
            answer=answered,
        )
    if offered.encoding == "VP8":
        return RtpVideoDirection(send=offered, recv=answered, answer=answered)
    if fmtp_parameters(offered.fmtp) != fmtp_parameters(answered.fmtp):
        return None
    if offered.encoding == "JPEG":
        return _jpeg_image_contract(offered, answered)
    return RtpVideoDirection(send=offered, recv=answered, answer=answered)


def jpeg_image_plan(
    video_format: RtpVideoFormat, native_width: int, native_height: int
):
    attributes = video_format.image_attributes
    return plan_jpeg_image(
        attributes.recv if attributes is not None else None, native_width, native_height
    )


def remove_image_attributes(value):
    """Build one legacy retry through the existing O/A owner, without new state."""
    if isinstance(value, RtpVideoFormat):
        return replace(value, image_attributes=None)
    if isinstance(value, bytes):
        return remove_image_attributes(value.decode()).encode()
    if isinstance(value, str):
        return (
            "\r\n".join(
                line
                for line in value.splitlines()
                if not line.startswith("a=imageattr:")
            )
            + "\r\n"
        )
    return tuple(remove_image_attributes(item) for item in value)


def _jpeg_image_contract(
    offered: RtpVideoFormat, answered: RtpVideoFormat, *, strict_answer: bool = False
) -> RtpVideoDirection:
    """Project remote receive preferences onto local TX, preserving both legs."""
    if offered.image_attributes is None or answered.image_attributes is None:
        return RtpVideoDirection(
            send=remove_image_attributes(offered),
            recv=remove_image_attributes(answered),
            answer=remove_image_attributes(answered),
        )
    send = intersect_constraints(
        offered.image_attributes.recv, answered.image_attributes.send
    )
    recv = intersect_constraints(
        answered.image_attributes.recv, offered.image_attributes.send
    )
    remote_direction = normalize_direction(offered.direction)
    send_active = remote_direction in {"sendrecv", "recvonly"}
    recv_active = remote_direction in {"sendrecv", "sendonly"}
    send_usable, recv_usable = constraints_usable(send), constraints_usable(recv)
    if (send_active and not send_usable) or (recv_active and not recv_usable):
        if strict_answer:
            raise ImageAttributeFallbackRequired(offered.payload_type)
        return RtpVideoDirection(
            send=remove_image_attributes(offered),
            recv=remove_image_attributes(answered),
            answer=remove_image_attributes(answered),
        )
    if not send_usable:
        send = None
    if not recv_usable:
        recv = None
    answer_attributes = (
        ImageAttributes(send=send, recv=recv)
        if send is not None or recv is not None
        else None
    )
    return RtpVideoDirection(
        send=replace(offered, image_attributes=ImageAttributes(recv=send)),
        recv=replace(answered, image_attributes=ImageAttributes(recv=recv)),
        answer=replace(answered, image_attributes=answer_attributes),
    )


_H264_SUBPROFILE_PATTERNS = (
    # RFC 6184, Table 5.  The mask removes each ``x`` bit from the
    # comparison, including constraint_set3_flag when it carries Level 1b.
    ("constrained-baseline", 0x42, 0x4F, 0x40),
    ("constrained-baseline", 0x4D, 0x8F, 0x80),
    ("constrained-baseline", 0x58, 0xCF, 0xC0),
    ("baseline", 0x42, 0x4F, 0x00),
    ("baseline", 0x58, 0xCF, 0x80),
    ("main", 0x4D, 0xAF, 0x00),
    ("extended", 0x58, 0xCF, 0x00),
    # H.264 constraint_set4_flag + constraint_set5_flag identify the
    # Constrained High subset used by WebRTC endpoints (for example 640c1f).
    # Keep it distinct from unconstrained High so passthrough matching cannot
    # silently claim compatibility between the two decoder contracts.
    ("constrained-high", 0x64, 0x0C, 0x0C),
    ("high", 0x64, 0xFF, 0x00),
    ("high-10", 0x6E, 0xFF, 0x00),
    ("high-4:2:2", 0x7A, 0xFF, 0x00),
    ("high-4:4:4", 0xF4, 0xFF, 0x00),
    ("high-10-intra", 0x6E, 0xFF, 0x10),
    ("high-4:2:2-intra", 0x7A, 0xFF, 0x10),
    ("high-4:4:4-intra", 0xF4, 0xFF, 0x10),
    ("cavlc-4:4:4-intra", 0x2C, 0xFF, 0x10),
)


def _h264_profile_level(profile_level_id: str) -> tuple[str, int] | None:
    """Return RFC 6184 sub-profile and an orderable level value."""

    try:
        profile_idc, profile_iop, level_idc = bytes.fromhex(profile_level_id)
    except (TypeError, ValueError):
        return None
    subprofile = next(
        (
            name
            for name, wanted_idc, mask, value in _H264_SUBPROFILE_PATTERNS
            if profile_idc == wanted_idc and profile_iop & mask == value
        ),
        None,
    )
    if subprofile is None:
        return None
    level_1b = (
        profile_idc in {0x42, 0x4D, 0x58} and level_idc == 11 and profile_iop & 0x10
    ) or (profile_idc not in {0x42, 0x4D, 0x58} and level_idc == 9)
    # Keep Level 1b strictly between Level 1.0 and Level 1.1.
    return subprofile, 105 if level_1b else level_idc * 10


def _h264_answer_level_for_offer(
    offered_profile_level_id: str,
    answered_profile_level_id: str,
) -> str | None:
    """Encode the answer's level using the offerer's equivalent sub-profile."""

    offered = _h264_profile_level(offered_profile_level_id.lower())
    answered = _h264_profile_level(answered_profile_level_id.lower())
    if offered is None or answered is None or offered[0] != answered[0]:
        return None
    try:
        profile_idc, profile_iop, _level_idc = bytes.fromhex(offered_profile_level_id)
        _answer_idc, _answer_iop, answer_level_idc = bytes.fromhex(
            answered_profile_level_id
        )
    except (TypeError, ValueError):
        return None
    is_level_1b = answered[1] == 105
    if profile_idc in {0x42, 0x4D, 0x58}:
        if is_level_1b:
            profile_iop |= 0x10
            answer_level_idc = 11
        elif answer_level_idc == 11:
            # For these profiles constraint_set3_flag differentiates Level
            # 1b from ordinary Level 1.1 and is therefore part of the level.
            profile_iop &= ~0x10
    elif is_level_1b:
        answer_level_idc = 9
    return bytes((profile_idc, profile_iop, answer_level_idc)).hex()


def _h264_subprofiles_compatible(
    first: RtpVideoFormat,
    second: RtpVideoFormat,
) -> bool:
    """Return whether RFC 6184 sees the same non-level configuration."""

    first_profile = _h264_profile_level(first.profile_level_id.lower())
    second_profile = _h264_profile_level(second.profile_level_id.lower())
    return bool(
        first_profile is not None
        and second_profile is not None
        and first_profile[0] == second_profile[0]
    )


def _h264_answer_compatible(
    offered: RtpVideoFormat,
    answered: RtpVideoFormat,
) -> bool:
    """Validate the changeable level part of an RFC 6184 answer."""

    offered_profile = _h264_profile_level(offered.profile_level_id.lower())
    answered_profile = _h264_profile_level(answered.profile_level_id.lower())
    if (
        offered_profile is None
        or answered_profile is None
        or offered_profile[0] != answered_profile[0]
    ):
        return False
    if offered.level_asymmetry_allowed and answered.level_asymmetry_allowed:
        return True
    # Without bilateral level asymmetry the answer selects the common lower
    # level.  A downgrade is valid; an upgrade is explicitly forbidden.
    return answered_profile[1] <= offered_profile[1]


def _h264_stream_fits(
    source: RtpVideoFormat,
    destination: RtpVideoFormat,
) -> bool:
    """Return whether a source stream fits the destination decoder contract."""

    source_profile = _h264_receiver_profile_level(source)
    destination_profile = _h264_receiver_profile_level(destination)
    if (
        source_profile is None
        or destination_profile is None
        or source_profile[0] != destination_profile[0]
    ):
        return False
    # level-asymmetry-allowed negotiates different levels per direction; it
    # never authorizes forwarding a higher-level bitstream to a lower-level
    # decoder.
    if source_profile[1] > destination_profile[1]:
        return False
    # RFC 6184 max-* values extend, rather than replace with a lower value,
    # the limits implied by the highest receive level.  Compare complete
    # effective envelopes so, for example, Level 2.1 + max-fs=3600 correctly
    # fits an unadorned Level 4 receiver (whose Table A-1 MaxFS is 8192).
    source_limits = _h264_receiver_limits(source)
    destination_limits = _h264_receiver_limits(destination)
    if source_limits is None or destination_limits is None:
        return False
    return all(
        value <= destination_limits[name] for name, value in source_limits.items()
    )


_H264_INTEGER_RECEIVER_LIMITS = (
    "max-mbps",
    "max-smbps",
    "max-fs",
    "max-cpb",
    "max-dpb",
    "max-br",
)


@dataclass(frozen=True, slots=True)
class _H264LevelLimits:
    """H.264 Table A-1 limits used by the RFC 6184 receiver parameters."""

    max_mbps: int
    max_fs: int
    max_dpb_mbs: int
    max_br: int
    max_cpb: int


# Indexed by the orderable level value returned by ``_h264_profile_level``.
# RFC 6184 references H.264 Table A-1 for these defaults.  MaxBR and MaxCPB
# are subsequently scaled for the negotiated profile as required by H.264's
# cpbBrVclFactor/cpbBrNALFactor rules.
_H264_LEVEL_LIMITS = {
    100: _H264LevelLimits(1485, 99, 396, 64, 175),
    105: _H264LevelLimits(1485, 99, 396, 128, 350),
    110: _H264LevelLimits(3000, 396, 900, 192, 500),
    120: _H264LevelLimits(6000, 396, 2376, 384, 1000),
    130: _H264LevelLimits(11880, 396, 2376, 768, 2000),
    200: _H264LevelLimits(11880, 396, 2376, 2000, 2000),
    210: _H264LevelLimits(19800, 792, 4752, 4000, 4000),
    220: _H264LevelLimits(20250, 1620, 8100, 4000, 4000),
    300: _H264LevelLimits(40500, 1620, 8100, 10000, 10000),
    310: _H264LevelLimits(108000, 3600, 18000, 14000, 14000),
    320: _H264LevelLimits(216000, 5120, 20480, 20000, 20000),
    400: _H264LevelLimits(245760, 8192, 32768, 20000, 25000),
    410: _H264LevelLimits(245760, 8192, 32768, 50000, 62500),
    420: _H264LevelLimits(522240, 8704, 34816, 50000, 62500),
    500: _H264LevelLimits(589824, 22080, 110400, 135000, 135000),
    510: _H264LevelLimits(983040, 36864, 184320, 240000, 240000),
    520: _H264LevelLimits(2073600, 36864, 184320, 240000, 240000),
}


def _h264_br_cpb_scale(subprofile: str) -> Fraction:
    """Return the profile-specific H.264 bitrate/CPB scaling factor."""

    if subprofile == "high":
        return Fraction(5, 4)
    if subprofile in {"high-10", "high-10-intra"}:
        return Fraction(3, 1)
    if subprofile in {
        "high-4:2:2",
        "high-4:4:4",
        "high-4:2:2-intra",
        "high-4:4:4-intra",
        "cavlc-4:4:4-intra",
    }:
        return Fraction(4, 1)
    return Fraction(1, 1)


def _h264_receiver_profile_level(
    video_format: RtpVideoFormat,
) -> tuple[str, int] | None:
    """Return the highest RFC 6184 level explicitly supported for receive."""

    default = _h264_profile_level(video_format.profile_level_id.lower())
    if default is None:
        return None
    raw = fmtp_parameters(video_format.fmtp).get("max-recv-level", "")
    if not raw:
        return default
    try:
        profile_idc = video_format.profile_level_id[:2]
        parsed = _h264_profile_level(f"{profile_idc}{raw.lower()}")
    except (TypeError, ValueError):
        return None
    # max-recv-level is valid only as a strictly higher level of the same
    # default sub-profile.
    if parsed is None or parsed[0] != default[0] or parsed[1] <= default[1]:
        return None
    return parsed


def _h264_receiver_limits(
    video_format: RtpVideoFormat,
) -> dict[str, Fraction] | None:
    """Return the complete effective RFC 6184 receive capability envelope."""

    profile = _h264_receiver_profile_level(video_format)
    if profile is None:
        return None
    level = _H264_LEVEL_LIMITS.get(profile[1])
    if level is None:
        return None
    br_cpb_scale = _h264_br_cpb_scale(profile[0])
    defaults = {
        "max-mbps": Fraction(level.max_mbps),
        # Without max-smbps, static macroblocks share the ordinary MaxMBPS
        # processing envelope.
        "max-smbps": Fraction(level.max_mbps),
        "max-fs": Fraction(level.max_fs),
        "max-cpb": Fraction(level.max_cpb) * br_cpb_scale,
        # max-dpb is signalled in units of 8/3 macroblocks.
        "max-dpb": Fraction(level.max_dpb_mbs * 3, 8),
        "max-br": Fraction(level.max_br) * br_cpb_scale,
    }
    parameters = fmtp_parameters(video_format.fmtp)
    limits = dict(defaults)
    explicit: set[str] = set()
    for name in _H264_INTEGER_RECEIVER_LIMITS:
        raw = parameters.get(name)
        if raw is None:
            continue
        try:
            value = int(raw, 10)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        candidate = Fraction(value)
        minimum = limits["max-mbps"] if name == "max-smbps" else defaults[name]
        # RFC 6184 only permits these parameters to extend the level's
        # required capability.  Lower values are malformed receiver claims.
        if candidate < minimum:
            return None
        limits[name] = candidate
        explicit.add(name)
    if "max-smbps" not in explicit:
        limits["max-smbps"] = limits["max-mbps"]
    # With max-br but no max-cpb, RFC 6184 scales the level's MaxCPB in the
    # same proportion as the bitrate extension.
    if "max-br" in explicit and "max-cpb" not in explicit:
        limits["max-cpb"] = defaults["max-cpb"] * limits["max-br"] / defaults["max-br"]
    return limits


def h264_receiver_limits(
    video_format: RtpVideoFormat,
) -> dict[str, Fraction] | None:
    """Expose the negotiated RFC 6184 receive envelope to media adapters."""

    return _h264_receiver_limits(video_format)


def _unique_video_payloads(
    formats: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat],
) -> tuple[RtpVideoFormat, ...]:
    """Keep one deterministic RTP mapping for each video payload type."""

    selected: dict[int, RtpVideoFormat] = {}
    for fmt in formats:
        selected.setdefault(int(fmt.payload_type), fmt)
    return tuple(selected.values())


_STATIC_VIDEO_RTPMAP = {
    26: "JPEG/90000",
    34: "H263/90000",
}


def video_static_rtp_mapping(payload_type: int) -> tuple[str, int, int] | None:
    """Expose the parser's static video identity facts to the dialog map owner."""
    value = _STATIC_VIDEO_RTPMAP.get(payload_type)
    if value is None:
        return None
    encoding, clock = value.split("/", 1)
    return encoding, int(clock), 1


def video_static_rtp_payload(mapping: tuple[str, int, int]) -> int | None:
    """Return the same registry's preferred static number for an offered codec."""
    return next((number for number in _STATIC_VIDEO_RTPMAP if video_static_rtp_mapping(number) == mapping), None)

_VIDEO_ENCODING_ALIASES = {
    "H264": "H264",
    "H265": "H265",
    "HEVC": "H265",
    "VP8": "VP8",
    "VP9": "VP9",
    "AV1": "AV1",
    "JPEG": "JPEG",
    "MJPEG": "JPEG",
    "H263": "H263",
    "H263-1998": "H263P",
    "H263-2000": "H263P",
}


def offered_video_formats(
    sdp_body: str | bytes, *, capabilities: bool = False
) -> list[RtpVideoFormat]:
    """List normalized formats from the first active AVP/AVPF video section."""

    parsed = parse_video_sdp(sdp_body, capabilities=capabilities)
    if parsed is None:
        return []
    out: list[RtpVideoFormat] = []
    for payload_type in parsed["payload_order"]:
        payload_type = int(payload_type)
        explicit_mapping = str(parsed["rtpmap"].get(payload_type) or "")
        static_mapping = _STATIC_VIDEO_RTPMAP.get(payload_type)
        if payload_type < 96:
            # RFC 3551 static payload types cannot be reassigned by rtpmap.
            if static_mapping is None or (
                explicit_mapping and explicit_mapping.upper() != static_mapping.upper()
            ):
                continue
            mapping = static_mapping
        else:
            mapping = explicit_mapping
        if not mapping:
            continue
        bits = mapping.upper().split("/")
        if len(bits) < 2:
            continue
        encoding = _VIDEO_ENCODING_ALIASES.get(bits[0])
        if encoding is None:
            continue
        try:
            clock_rate = int(bits[1])
        except ValueError:
            continue
        if clock_rate != 90000:
            continue
        parameters = fmtp_parameters(parsed["fmtp"].get(payload_type, ""))
        profile = parameters.get(
            "profile-level-id", _RFC6184_DEFAULT_PROFILE_LEVEL_ID
        ).lower()
        packetization_mode = 1
        sprop_parameter_sets = ""
        if encoding == "H264":
            try:
                packetization_mode = int(parameters.get("packetization-mode", "0") or 0)
            except ValueError:
                continue
            if packetization_mode not in {0, 1} or len(profile) != 6:
                continue
            if _h264_profile_level(profile) is None:
                continue
            sprop_parameter_sets = parameters.get("sprop-parameter-sets", "")
        feedback = tuple(
            value
            for value in dict.fromkeys(
                [
                    *parsed["rtcp_feedback"].get("*", []),
                    *parsed["rtcp_feedback"].get(payload_type, []),
                ]
            )
            if value in _SUPPORTED_RTCP_FEEDBACK
        )
        out.append(
            RtpVideoFormat(
                payload_type=payload_type,
                profile_level_id=profile,
                packetization_mode=packetization_mode,
                level_asymmetry_allowed=(
                    parameters.get("level-asymmetry-allowed", "0") == "1"
                ),
                direction=str(parsed["direction"] or "sendrecv"),
                sprop_parameter_sets=sprop_parameter_sets,
                encoding=encoding,
                clock_rate=clock_rate,
                transport_profile=str(parsed["transport_profile"]),
                fmtp=str(parsed["fmtp"].get(payload_type, "")).strip(),
                # PJSIP and PBXs also signal explicit feedback over AVP.
                # Retain advertised capabilities without inventing any.
                rtcp_feedback=feedback,
                max_framerate=parsed["framerate"],
                image_attributes=(
                    resolve_image_attributes(
                        parsed["image_attributes"].get(None),
                        parsed["image_attributes"].get(payload_type),
                    )
                    if encoding == "JPEG"
                    else None
                ),
            )
        )
    return out


def offered_h264_formats(sdp_body: str | bytes) -> list[RtpH264Format]:
    """Compatibility wrapper returning only RFC 6184 formats."""

    return [fmt for fmt in offered_video_formats(sdp_body) if fmt.encoding == "H264"]


def negotiate_video(
    remote_sdp: str | bytes,
    *,
    accepted_encodings: tuple[str, ...] = ("H264", "VP8", "JPEG"),
    prefer_browser_send: bool = False,
) -> RtpVideoFormat | None:
    """Select the best remote format accepted by the configured media path.

    Preserve the endpoint's order within each capability class, but prefer a
    codec the browser can consume directly over one that needs the optional
    FFmpeg bridge.  When camera transmission is requested, prefer the smaller
    subset that the browser can also packetize.
    """

    accepted = {item.upper() for item in accepted_encodings}
    compatible = [
        fmt for fmt in offered_video_formats(remote_sdp) if fmt.encoding in accepted
    ]
    if prefer_browser_send:
        selected = next(
            (fmt for fmt in compatible if browser_video_send_supported(fmt)),
            None,
        )
        if selected is not None:
            return selected
    return next(
        (fmt for fmt in compatible if browser_video_receive_supported(fmt)),
        None,
    ) or next(iter(compatible), None)


def _matching_local_video_capability(
    offered: RtpVideoFormat,
    local_formats: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat],
) -> RtpVideoFormat | None:
    """Return a local receive capability compatible with one offered codec."""

    for local in local_formats:
        if local.encoding != offered.encoding or local.clock_rate != offered.clock_rate:
            continue
        if offered.encoding == "H264" and (
            local.packetization_mode != offered.packetization_mode
            or not _h264_subprofiles_compatible(offered, local)
        ):
            continue
        return local
    return None


def _h264_stream_contract(
    receiver: RtpVideoFormat,
    sender: RtpVideoFormat,
) -> RtpVideoFormat:
    """Combine receiver limits with parameter sets emitted by the sender."""

    # sprop-level-parameter-sets needs source-attribute negotiation that this
    # direct SIP profile does not implement.  Omit it so endpoints fall back
    # to the standards-defined in-band transport rather than associating
    # parameter sets with the wrong RTP source.
    receiver_fmtp = fmtp_without_parameters(
        receiver.fmtp,
        {"sprop-parameter-sets", "sprop-level-parameter-sets"},
    )
    return replace(
        receiver,
        sprop_parameter_sets=sender.sprop_parameter_sets,
        fmtp=receiver_fmtp,
    )


def _project_local_video_answer(
    offered: RtpVideoFormat,
    local: RtpVideoFormat,
) -> RtpVideoDirection | None:
    """Build directional contracts for a local answer to a remote offer."""

    if offered.encoding == "H264":
        if (
            offered.packetization_mode != local.packetization_mode
            or not _h264_subprofiles_compatible(offered, local)
        ):
            return None
        bilateral_asymmetry = bool(
            offered.level_asymmetry_allowed and local.level_asymmetry_allowed
        )
        offered_profile = _h264_profile_level(offered.profile_level_id.lower())
        local_profile = _h264_profile_level(local.profile_level_id.lower())
        if offered_profile is None or local_profile is None:
            return None
        if bilateral_asymmetry:
            answer_level_source = local
        else:
            answer_level_source = (
                offered if offered_profile[1] <= local_profile[1] else local
            )
        answer_profile = _h264_answer_level_for_offer(
            offered.profile_level_id,
            answer_level_source.profile_level_id,
        )
        if answer_profile is None:
            return None
        answer_receiver = replace(
            offered,
            profile_level_id=answer_profile,
            level_asymmetry_allowed=bilateral_asymmetry,
            fmtp=local.fmtp,
            max_framerate=local.max_framerate,
        )
        answer = _h264_stream_contract(answer_receiver, local)
        if bilateral_asymmetry:
            return RtpVideoDirection(
                send=_h264_stream_contract(
                    replace(offered, level_asymmetry_allowed=True),
                    local,
                ),
                recv=_h264_stream_contract(answer_receiver, offered),
                answer=answer,
            )
        # Without bilateral level asymmetry RFC 6184 selects one common level
        # for both directions, even though other receiver-only parameters may
        # remain directional.
        return RtpVideoDirection(
            send=_h264_stream_contract(
                replace(answer_receiver, max_framerate=offered.max_framerate), local
            ),
            recv=_h264_stream_contract(answer_receiver, offered),
            answer=answer,
        )

    answer = replace(
        local,
        payload_type=offered.payload_type,
        direction=offered.direction,
        transport_profile=offered.transport_profile,
        rtcp_feedback=offered.rtcp_feedback,
    )
    if offered.encoding == "VP8":
        # RFC 7741 max-fs/max-fr are receive limits.  The remote offer limits
        # local TX, while the local answer independently limits remote TX.
        return RtpVideoDirection(send=offered, recv=answer, answer=answer)
    if fmtp_parameters(offered.fmtp) != fmtp_parameters(answer.fmtp):
        # Generic exact-codec relay has no codec-specific way to reconcile
        # differing fmtp contracts.  Preserve the historical echo-answer for
        # an explicitly identical local capability only.
        return None
    if offered.encoding == "JPEG":
        return _jpeg_image_contract(offered, answer)
    return RtpVideoDirection(send=offered, recv=answer, answer=answer)


def negotiate_video_offer_directional(
    remote_sdp: str | bytes,
    *,
    local_formats: tuple[RtpVideoFormat, ...]
    | list[RtpVideoFormat] = DEFAULT_VIDEO_FORMATS,
    accepted_encodings: tuple[str, ...] = ("H264", "VP8", "JPEG"),
    prefer_browser_send: bool = False,
    allow_passthrough_fallback: bool = False,
) -> RtpVideoDirection | None:
    """Negotiate a remote video offer without collapsing receive limits.

    ``send`` is the codec contract for packets sent by this endpoint and
    ``recv`` is the format that must be serialized in the local answer and
    enforced for packets received from the offerer.
    """

    accepted = {item.upper() for item in accepted_encodings}
    negotiated: list[RtpVideoDirection] = []
    for offered in offered_video_formats(remote_sdp):
        if offered.encoding not in accepted:
            continue
        local = _matching_local_video_capability(offered, local_formats)
        if local is None and allow_passthrough_fallback:
            local = offered
        if local is None:
            continue
        directional = _project_local_video_answer(offered, local)
        if directional is not None:
            negotiated.append(directional)
    if prefer_browser_send:
        selected = next(
            (item for item in negotiated if browser_video_send_supported(item.send)),
            None,
        )
        if selected is not None:
            return selected
    return next(
        (item for item in negotiated if browser_video_receive_supported(item.recv)),
        None,
    ) or next(iter(negotiated), None)


def negotiate_h264(remote_sdp: str | bytes) -> RtpH264Format | None:
    """Select the first supported H.264 mode-1 format from the offer/answer."""

    offered = offered_h264_formats(remote_sdp)
    return offered[0] if offered else None


def _matching_offered_video_answer(
    selected: RtpVideoFormat,
    offered_formats: tuple[RtpVideoFormat, ...],
) -> RtpVideoFormat | None:
    """Return the offered codec contract accepted by one answer payload."""

    exact_payload = next(
        (
            item
            for item in offered_formats
            if int(item.payload_type) == int(selected.payload_type)
        ),
        None,
    )
    # Reusing an offered PT for another codec is a remap, not directional
    # renumbering. Do not match it against a different offered payload.
    candidates = (exact_payload,) if exact_payload is not None else offered_formats
    for candidate in candidates:
        if candidate is None or (
            candidate.encoding != selected.encoding
            or candidate.clock_rate != selected.clock_rate
            or candidate.transport_profile != selected.transport_profile
        ):
            continue
        if candidate.encoding == "H264":
            if candidate.packetization_mode != selected.packetization_mode:
                continue
            if not _h264_answer_compatible(candidate, selected):
                continue
        elif candidate.encoding != "VP8" and (
            fmtp_parameters(candidate.fmtp) != fmtp_parameters(selected.fmtp)
        ):
            continue
        return candidate
    return None


def _negotiate_video_answer(
    remote_sdp: str | bytes,
    offered: RtpVideoFormat | tuple[RtpVideoFormat, ...],
) -> RtpVideoFormat | None:
    """Accept a codec from our offer, including a directional answer PT."""

    offered_formats = (
        (offered,) if isinstance(offered, RtpVideoFormat) else tuple(offered)
    )
    for selected in offered_video_formats(remote_sdp):
        if _matching_offered_video_answer(selected, offered_formats) is not None:
            return selected
    return None


def negotiate_video_answer_directional(
    remote_sdp: str | bytes,
    offered: RtpVideoFormat | tuple[RtpVideoFormat, ...],
    *,
    allow_h264_level_capability: bool = False,
) -> RtpVideoDirection | None:
    """Validate an answer and retain the offer/answer receive limits.

    For an outbound offer the remote answer constrains local TX.  The local
    offer continues to constrain remote TX when H.264 level asymmetry is
    negotiated bilaterally and for VP8's receiver-only fmtp parameters.
    """

    offered_formats = (
        (offered,) if isinstance(offered, RtpVideoFormat) else tuple(offered)
    )
    selected = _negotiate_video_answer(remote_sdp, offered_formats)
    if selected is None and allow_h264_level_capability:
        # Some UAs repeat their decoder capability instead of the selected
        # H.264 level. As in PJSIP's permissive negotiation, retain our offered
        # limit. Never relax the sub-profile, packetization mode or RTP profile.
        for answer in offered_video_formats(remote_sdp):
            if (
                answer.encoding != "H264"
                or _h264_profile_level(answer.profile_level_id) is None
            ):
                continue
            for offer in offered_formats:
                if offer.encoding != "H264" or not _h264_subprofiles_compatible(
                    offer, answer
                ):
                    continue
                bounded = replace(answer, profile_level_id=offer.profile_level_id)
                if _matching_offered_video_answer(bounded, (offer,)) is not None:
                    selected = bounded
                    break
            if selected is not None:
                break
    if selected is None:
        return None
    candidate = _matching_offered_video_answer(selected, offered_formats)
    if candidate is None:
        return None
    if candidate.encoding == "H264":
        bilateral_asymmetry = bool(
            candidate.level_asymmetry_allowed and selected.level_asymmetry_allowed
        )
        if bilateral_asymmetry:
            local_receive = _h264_stream_contract(
                replace(
                    candidate,
                    direction=selected.direction,
                    level_asymmetry_allowed=True,
                ),
                selected,
            )
            local_send = _h264_stream_contract(
                replace(selected, level_asymmetry_allowed=True),
                candidate,
            )
            return RtpVideoDirection(
                send=local_send,
                recv=local_receive,
            )
        common = video_answer_contract(candidate, selected)
        if common is None:
            return None
        common = replace(common, direction=selected.direction)
        return RtpVideoDirection(
            send=_h264_stream_contract(
                replace(common, payload_type=selected.payload_type),
                candidate,
            ),
            recv=_h264_stream_contract(
                replace(common, max_framerate=candidate.max_framerate), selected
            ),
        )
    if candidate.encoding == "VP8":
        return RtpVideoDirection(
            send=selected,
            recv=replace(candidate, direction=selected.direction),
        )
    if candidate.encoding == "JPEG":
        contract = _jpeg_image_contract(selected, candidate, strict_answer=True)
        return RtpVideoDirection(
            send=contract.send,
            recv=replace(contract.recv, direction=selected.direction),
        )
    return RtpVideoDirection(
        send=selected,
        recv=replace(candidate, direction=selected.direction),
    )


def constrained_video_direction(
    remote_direction: str,
    *,
    allow_send: bool,
    allow_receive: bool = True,
) -> str:
    """Compatibility wrapper for video direction negotiation."""

    return constrained_media_direction(
        remote_direction,
        allow_send=allow_send,
        allow_receive=allow_receive,
    )


def _serialized_video_fmtp(video_format: RtpVideoFormat) -> str:
    """Serialize the complete negotiated codec contract deterministically.

    RFC 6184 parameters outside the three fields modeled directly by
    :class:`RtpVideoFormat` are still part of the wire contract.  Preserve
    them across an answer or a B2BUA leg while making the normalized fields
    authoritative.  This is particularly important for parameter sets that
    an endpoint supplies only in SDP and for receiver limits such as
    ``max-fs``/``max-mbps``.
    """

    if video_format.encoding != "H264":
        return ";".join(
            f"{key}={value}" if value else key
            for key, value in fmtp_parameters(video_format.fmtp).items()
        )

    parsed = fmtp_parameters(video_format.fmtp)
    extras = {
        key: value
        for key, value in parsed.items()
        if key
        not in {
            "profile-level-id",
            "packetization-mode",
            "level-asymmetry-allowed",
            "sprop-parameter-sets",
        }
    }
    ordered: list[tuple[str, str]] = [
        ("profile-level-id", video_format.profile_level_id.lower()),
        ("packetization-mode", str(int(video_format.packetization_mode))),
    ]
    if video_format.level_asymmetry_allowed:
        ordered.append(("level-asymmetry-allowed", "1"))
    sprop_parameter_sets = str(
        video_format.sprop_parameter_sets or parsed.get("sprop-parameter-sets", "")
    ).strip()
    if sprop_parameter_sets:
        ordered.append(("sprop-parameter-sets", sprop_parameter_sets))
    ordered.extend(extras.items())
    return ";".join(f"{key}={value}" if value else key for key, value in ordered)


def serialized_video_fmtp(video_format: RtpVideoFormat) -> str:
    """Serialize one negotiated video format for an RTP SDP adapter."""

    return _serialized_video_fmtp(video_format)


def video_fmtp_parameters(video_format: RtpVideoFormat) -> dict[str, str]:
    """Return normalized codec parameters without exposing parser internals."""

    return fmtp_parameters(video_format.fmtp)
