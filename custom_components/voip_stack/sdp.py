"""SDP offer/answer helpers for RTP PCM used by the VoIP Stack profile."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction

from .audio_format import AudioFormat, PcmFormat, UDP_SAFE_PAYLOAD_BYTES
from .codec_capabilities import common_sip_codecs


class SdpError(ValueError):
    """Malformed or unsupported SDP."""


MAX_RTP_OFFER_FORMATS = 11
_PREFERRED_RTP_AUDIO_KEYS = {
    (48000, PcmFormat.S16LE, 1, 10): 0,
    (32000, PcmFormat.S16LE, 1, 16): 1,
    (32000, PcmFormat.S16LE, 1, 10): 2,
    (24000, PcmFormat.S16LE, 1, 20): 3,
    (16000, PcmFormat.S16LE, 1, 16): 4,
    (16000, PcmFormat.S16LE, 1, 10): 5,
    (16000, PcmFormat.S16LE, 1, 20): 6,
    (16000, PcmFormat.S16LE, 1, 32): 7,
    (8000, PcmFormat.S16LE, 1, 20): 8,
}
_STATIC_RTPMAP = {
    0: ("PCMU", 8000, 1),
    8: ("PCMA", 8000, 1),
    9: ("G722", 8000, 1),
}
_RFC6184_DEFAULT_PROFILE_LEVEL_ID = "42000a"
_SDP_DIRECTIONS = frozenset({"sendrecv", "sendonly", "recvonly", "inactive"})


def normalize_direction(value: str | None, *, default: str = "sendrecv") -> str:
    """Return one valid RFC 3264 media direction or raise ``SdpError``."""

    direction = str(value or default).strip().lower()
    if direction not in _SDP_DIRECTIONS:
        raise SdpError(f"unsupported SDP media direction: {value}")
    return direction


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


@dataclass(frozen=True, slots=True)
class RtpPcmFormat:
    payload_type: int
    encoding: str
    sample_rate: int
    channels: int
    frame_ms: int = 20
    min_frame_ms: int = 0
    max_frame_ms: int = 0

    @property
    def bits(self) -> int:
        if self.encoding in {"L16", "PCM"}:
            return 16
        if self.encoding == "L24":
            return 24
        if self.encoding in {"PCMA", "PCMU", "G722"}:
            return 8
        if self.encoding == "OPUS":
            return 0
        raise SdpError(f"unsupported RTP PCM encoding {self.encoding}")

    @property
    def audio_format(self) -> AudioFormat:
        if self.encoding in {"PCMA", "PCMU"}:
            return AudioFormat(
                self.sample_rate, PcmFormat.S16LE, self.channels, self.frame_ms or 20
            )
        if self.encoding == "G722":
            return AudioFormat(
                16000, PcmFormat.S16LE, self.channels, self.frame_ms or 20
            )
        if self.encoding == "OPUS":
            return AudioFormat(
                48000, PcmFormat.S16LE, self.channels, self.frame_ms or 20
            )
        pcm = (
            PcmFormat.S16LE
            if self.encoding in {"L16", "PCM"}
            else PcmFormat.S24LE
        )
        return AudioFormat(self.sample_rate, pcm, self.channels, self.frame_ms)

    @property
    def rtp_clock_rate(self) -> int:
        """RTP timestamp clock advertised in SDP."""

        return self.sample_rate

    @property
    def pcm_sample_rate(self) -> int:
        """Decoded PCM rate, which differs from RTP clock rate for G.722."""

        return self.audio_format.sample_rate

    @property
    def rtp_timestamp_step(self) -> int:
        return (self.rtp_clock_rate * (self.frame_ms or 20)) // 1000

    def wire_token(self) -> str:
        return f"pt={self.payload_type}:{self.encoding}/{self.sample_rate}/{self.channels}/{self.frame_ms}ms"


@dataclass(frozen=True, slots=True)
class RtpDtmfFormat:
    payload_type: int
    sample_rate: int
    events: frozenset[int] = frozenset(range(16))


@dataclass(frozen=True, slots=True)
class RtpPcmDirection:
    send: RtpPcmFormat
    recv: RtpPcmFormat

    @property
    def selected_format(self) -> RtpPcmFormat:
        return self.send


@dataclass(frozen=True, slots=True)
class RtpVideoFormat:
    """One negotiated RTP video format.

    The H.264 fields remain first-class because RFC 6184 needs them during
    packetization.  Other codecs keep their complete normalized ``fmtp`` so
    an exact relay never invents or discards endpoint parameters.
    """

    payload_type: int = 103
    # Baseline Level 3.1 covers the browser bridge's advertised receive
    # envelope (up to 1280x720) while retaining the broadly interoperable
    # 42/80 sub-profile used by SIP door stations.  Lower levels selected by
    # an answer are propagated to the browser encoder as hard constraints.
    profile_level_id: str = "42801f"
    packetization_mode: int = 1
    level_asymmetry_allowed: bool = True
    direction: str = "sendrecv"
    sprop_parameter_sets: str = ""
    encoding: str = "H264"
    clock_rate: int = 90000
    transport_profile: str = "RTP/AVP"
    fmtp: str = ""
    rtcp_feedback: tuple[str, ...] = ()

    def wire_token(self) -> str:
        token = f"pt={self.payload_type}:{self.encoding}/{self.clock_rate}"
        if self.encoding == "H264":
            token += (
                f";profile-level-id={self.profile_level_id};"
                f"packetization-mode={self.packetization_mode}"
            )
        elif self.fmtp:
            token += f";fmtp={self.fmtp}"
        return (
            f"{token};rtp-profile={self.transport_profile};direction={self.direction}"
        )

    @property
    def browser_codec(self) -> str:
        if self.encoding == "H264":
            return f"avc1.{self.profile_level_id.upper()}"
        return {
            "VP8": "vp8",
            "VP9": "vp09.00.10.08",
            "AV1": "av01.0.04M.08",
            "JPEG": "jpeg",
        }.get(self.encoding, "")


@dataclass(frozen=True, slots=True)
class RtpVideoDirection:
    """The negotiated video contracts from the local endpoint's perspective.

    RTP payload numbers normally match in both directions on one SDP media
    section, but codec parameters need not.  In particular, RFC 6184
    ``level-asymmetry-allowed=1`` makes the answer's H.264 level constrain the
    offerer's transmitted stream while the offer's level continues to
    constrain the answerer's transmitted stream.  VP8 ``max-fs``/``max-fr``
    parameters are receiver limits and are therefore directional as well.
    """

    send: RtpVideoFormat
    recv: RtpVideoFormat
    # A local answer combines local receive capabilities with local sender
    # parameter sets, so it is not generally identical to either stream
    # contract.  Outbound-answer processing leaves this unset.
    answer: RtpVideoFormat | None = None

    @property
    def selected_format(self) -> RtpVideoFormat:
        """Compatibility alias matching :class:`RtpPcmDirection`."""

        return self.send

    @property
    def answer_format(self) -> RtpVideoFormat:
        return self.answer or self.recv


@dataclass(frozen=True, slots=True)
class RemoteMediaTarget:
    """Normalized RTP/RTCP target projected from one remote media section."""

    rtp_host: str = ""
    rtp_port: int = 0
    rtcp_host: str = ""
    rtcp_port: int = 0
    rtcp_mux: bool = False
    payload_types: tuple[int, ...] = ()
    connection_held: bool = False

    @classmethod
    def from_section(
        cls,
        section: dict | None,
        *,
        rtcp_mux: bool | None = None,
    ) -> "RemoteMediaTarget":
        if section is None:
            return cls()
        rtp_host = str(section.get("connection_ip") or "")
        rtp_port = int(section.get("media_port") or 0)
        if not rtp_host or not 0 <= rtp_port <= 65535:
            raise SdpError("remote media section has an invalid RTP target")
        rtcp_host = str(section.get("rtcp_address") or rtp_host)
        raw_rtcp_port = section.get("rtcp_port")
        rtcp_port = int(raw_rtcp_port or (rtp_port + 1 if rtp_port else 0))
        if not 0 <= rtcp_port <= 65535:
            raise SdpError("remote media section has an invalid RTCP target")
        return cls(
            rtp_host=rtp_host,
            rtp_port=rtp_port,
            rtcp_host=rtcp_host,
            rtcp_port=rtcp_port,
            rtcp_mux=(
                bool(section.get("rtcp_mux"))
                if rtcp_mux is None
                else bool(rtcp_mux)
            ),
            payload_types=tuple(
                int(item) for item in section.get("payload_order") or ()
            ),
            connection_held=bool(section.get("connection_held")),
        )

    def as_remote_video_fields(self) -> dict[str, object]:
        return {
            "remote_video_rtp_host": self.rtp_host,
            "remote_video_rtp_port": self.rtp_port,
            "remote_video_rtcp_host": self.rtcp_host,
            "remote_video_rtcp_port": self.rtcp_port,
            "remote_video_rtcp_mux": self.rtcp_mux,
            "remote_video_payload_types": self.payload_types,
            "remote_video_connection_held": self.connection_held,
        }


# Kept as a public compatibility alias for integrations and tests that used
# the original H.264-only type before the video format model was widened.
RtpH264Format = RtpVideoFormat


DEFAULT_H264_FORMAT = RtpH264Format()
DEFAULT_VIDEO_FORMATS = (
    DEFAULT_H264_FORMAT,
    RtpVideoFormat(
        payload_type=104,
        encoding="VP8",
        fmtp="max-fr=20;max-fs=3600",
    ),
    RtpVideoFormat(payload_type=26, encoding="JPEG"),
)
_SUPPORTED_RTCP_FEEDBACK = frozenset({"nack pli", "ccm fir"})


def browser_video_send_supported(video_format: RtpVideoFormat | None) -> bool:
    """Return whether the browser bridge can packetize this negotiated codec."""

    if video_format is None:
        return False
    return video_format.encoding == "VP8" or (
        video_format.encoding == "H264" and video_format.packetization_mode == 1
    )


def browser_video_receive_supported(video_format: RtpVideoFormat | None) -> bool:
    """Return whether the direct browser bridge can render this codec."""

    return bool(
        video_format is not None and video_format.encoding in {"H264", "VP8", "JPEG"}
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
    return _fmtp_parameters(source.fmtp) == _fmtp_parameters(destination.fmtp)


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
    return _fmtp_parameters(previous.fmtp) == _fmtp_parameters(updated.fmtp)


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
            or video_formats_renegotiation_compatible(
                previous_send, updated_send
            )
        )
        and (
            not (recv_was_active and recv_is_active)
            or video_formats_renegotiation_compatible(
                previous_recv, updated_recv
            )
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
        return replace(offered, fmtp=answered.fmtp)
    if offered.encoding != "H264":
        return (
            offered
            if _fmtp_parameters(offered.fmtp) == _fmtp_parameters(answered.fmtp)
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
    if _fmtp_parameters(offered.fmtp) != _fmtp_parameters(answered.fmtp):
        return None
    return RtpVideoDirection(send=offered, recv=answered, answer=answered)


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
    raw = _fmtp_parameters(video_format.fmtp).get("max-recv-level", "")
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
    parameters = _fmtp_parameters(video_format.fmtp)
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


def audio_format_to_rtp(fmt: AudioFormat, payload_type: int) -> RtpPcmFormat:
    if not 96 <= int(payload_type) <= 127:
        raise SdpError("phase-1 PCM uses dynamic RTP payload types 96-127")
    if fmt.channels != 1:
        raise SdpError("phase-1 ESP RTP PCM is mono only")
    if not fmt.fits_udp_payload(UDP_SAFE_PAYLOAD_BYTES):
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
    if fmt.channels != 1:
        return False
    if not fmt.fits_udp_payload(UDP_SAFE_PAYLOAD_BYTES):
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
    frame_rank = {16: 0, 10: 1, 20: 2, 32: 3}.get(fmt.frame_ms, 9)
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
    """Return wire-identical formats supported in both local directions.

    One RFC 3264 ``sendrecv`` media description cannot assign one codec to
    each direction.  Its formats must therefore be usable for both sending
    and receiving.  ``S24LE_IN_S32`` and ``S24LE`` intentionally share the
    same RTP wire key.
    """

    recv_keys = {_format_key(fmt) for fmt in recv_formats}
    return _dedupe_formats(
        [fmt for fmt in send_formats if _format_key(fmt) in recv_keys]
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
    offered: RtpPcmFormat, local: AudioFormat
) -> RtpPcmFormat | None:
    if offered.frame_ms not in (0, local.frame_ms):
        min_frame_ms = offered.min_frame_ms or 0
        max_frame_ms = offered.max_frame_ms or 0
        if min_frame_ms and local.frame_ms < min_frame_ms:
            return None
        if max_frame_ms and local.frame_ms > max_frame_ms:
            return None
        if not min_frame_ms and not max_frame_ms:
            return None
        if min_frame_ms and not max_frame_ms and local.frame_ms > offered.frame_ms:
            return None
        if max_frame_ms and not min_frame_ms and local.frame_ms < offered.frame_ms:
            return None
    if (
        offered.frame_ms == 0
        and offered.min_frame_ms
        and local.frame_ms < offered.min_frame_ms
    ):
        return None
    if (
        offered.frame_ms == 0
        and offered.max_frame_ms
        and local.frame_ms > offered.max_frame_ms
    ):
        return None
    if offered.encoding == "OPUS":
        if (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == 48000
            and local.channels == offered.channels
            and local.frame_ms == 20
        ):
            return RtpPcmFormat(
                offered.payload_type, "OPUS", 48000, offered.channels, local.frame_ms
            )
        return None
    if offered.encoding == "G722":
        if (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == 16000
            and local.channels == 1
            and local.frame_ms == 20
        ):
            return RtpPcmFormat(
                offered.payload_type,
                "G722",
                8000,
                1,
                local.frame_ms,
            )
        return None
    if offered.encoding == "PCM":
        if (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == 16000
            and local.channels == 1
            and local.frame_ms == 20
            and offered.sample_rate == 16000
            and offered.channels == 1
        ):
            return RtpPcmFormat(
                offered.payload_type,
                "PCM",
                16000,
                1,
                20,
            )
        return None
    if not is_rtp_pcm_mappable(local):
        return None
    if offered.encoding in {"PCMA", "PCMU"}:
        if (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == offered.sample_rate
            and local.channels == offered.channels
        ):
            return RtpPcmFormat(
                offered.payload_type,
                offered.encoding,
                offered.sample_rate,
                offered.channels,
                local.frame_ms,
            )
        return None
    wanted = audio_format_to_rtp(local, offered.payload_type)
    if (
        offered.encoding == wanted.encoding
        and offered.sample_rate == wanted.sample_rate
        and offered.channels == wanted.channels
    ):
        return wanted
    return None


def _best_offered_match(
    offered: list[RtpPcmFormat], local_preferred: list[AudioFormat]
) -> RtpPcmFormat | None:
    for local in local_preferred:
        for offered_fmt in offered:
            selected = _rtp_compatible_audio(offered_fmt, local)
            if selected is not None:
                return selected
    return None


def _first_offered_match(
    offered: list[RtpPcmFormat],
    local_preferred: list[AudioFormat],
) -> RtpPcmFormat | None:
    for offered_fmt in offered:
        for local in local_preferred:
            selected = _rtp_compatible_audio(offered_fmt, local)
            if selected is not None:
                return selected
    return None


def _rtp_contract_key(fmt: RtpPcmFormat) -> tuple[int, str, int, int, int]:
    return (
        fmt.payload_type,
        fmt.encoding,
        fmt.sample_rate,
        fmt.channels,
        fmt.frame_ms,
    )


def _bidirectional_offered_match(
    offered: list[RtpPcmFormat],
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    prefer_offer_order: bool,
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
        send = _rtp_compatible_audio(offered_fmt, local_send)
        if send is None:
            continue
        for local_recv in local_recv_preferred:
            recv = _rtp_compatible_audio(offered_fmt, local_recv)
            if recv is not None and _rtp_contract_key(recv) == _rtp_contract_key(send):
                return RtpPcmDirection(send=send, recv=recv)
    return None


def _format_direction_for_remote(
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


def _negotiate_for_local_direction(
    offered: list[RtpPcmFormat],
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    direction: str,
    *,
    prefer_offer_order: bool,
) -> RtpPcmDirection | None:
    direction = normalize_direction(direction)
    if direction == "sendonly":
        selected = (
            _first_offered_match(offered, local_send_preferred)
            if prefer_offer_order
            else _best_offered_match(offered, local_send_preferred)
        )
        return (
            RtpPcmDirection(send=selected, recv=selected)
            if selected is not None
            else None
        )
    if direction == "recvonly":
        selected = (
            _first_offered_match(offered, local_recv_preferred)
            if prefer_offer_order
            else _best_offered_match(offered, local_recv_preferred)
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
            if direction == "sendonly":
                send = next(
                    (
                        selected
                        for local in local_send_preferred
                        if (
                            selected := _rtp_compatible_audio(
                                answered_format, local
                            )
                        )
                        is not None
                    ),
                    None,
                )
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
                recv = next(
                    (
                        selected
                        for local in local_recv_preferred
                        if (
                            selected := _rtp_compatible_audio(offered_format, local)
                        )
                        is not None
                    ),
                    None,
                )
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

            for local_send in local_send_preferred:
                send = _rtp_compatible_audio(answered_format, local_send)
                if send is None:
                    continue
                for local_recv in local_recv_preferred:
                    recv = _rtp_compatible_audio(offered_format, local_recv)
                    if recv is not None:
                        return RtpPcmDirection(send=send, recv=recv)
    return None


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
    include_dahua_pcm: bool = False,
    video_port: int = 0,
    video_format: RtpH264Format | None = None,
    video_formats: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat] | None = None,
    audio_direction: str = "sendrecv",
    video_direction: str = "sendrecv",
) -> str:
    audio_direction = normalize_direction(audio_direction)
    video_direction = normalize_direction(video_direction)
    capability_formats = _formats_for_local_direction(
        send_formats or [],
        recv_formats or [],
        audio_direction,
    )
    formats = rtp_offer_formats(capability_formats)
    if not formats:
        raise SdpError(
            f"SDP {audio_direction} offer requires at least one compatible "
            "RTP-mappable PCM format"
        )
    rtp_formats = (
        _common_codec_offer_formats(
            capability_formats,
            include_common_codecs=include_common_codecs,
            include_dahua_pcm=include_dahua_pcm,
        )
        if include_common_codecs or include_dahua_pcm
        else []
    )
    next_payload = 96
    used_payloads = {fmt.payload_type for fmt in rtp_formats}
    for fmt in formats:
        while next_payload in used_payloads:
            next_payload += 1
        rtp_formats.append(audio_format_to_rtp(fmt, next_payload))
        used_payloads.add(next_payload)
        next_payload += 1
    while next_payload in used_payloads:
        next_payload += 1
    dtmf_payload_type = next_payload
    payloads = " ".join(
        [*(str(fmt.payload_type) for fmt in rtp_formats), str(dtmf_payload_type)]
    )
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=VoIP Stack",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
        f"m=audio {int(media_port)} RTP/AVP {payloads}",
    ]
    for fmt in rtp_formats:
        lines.append(
            f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}"
        )
        if fmt.encoding == "OPUS":
            lines.append(
                f"a=fmtp:{fmt.payload_type} stereo=1;sprop-stereo=1;maxaveragebitrate=28000"
            )
    lines.append(f"a=rtpmap:{dtmf_payload_type} telephone-event/8000")
    lines.append(f"a=fmtp:{dtmf_payload_type} 0-15")
    lines.append(f"a=ptime:{rtp_formats[0].frame_ms}")
    lines.append(f"a=maxptime:{rtp_formats[0].frame_ms}")
    lines.append(f"a={audio_direction}")
    offered_video = tuple(
        video_formats or (() if video_format is None else (video_format,))
    )
    if offered_video and int(video_port) > 0:
        lines.extend(
            _video_media_lines_many(
                int(video_port), offered_video, direction=video_direction
            )
        )
    return "\r\n".join(lines) + "\r\n"


def _common_codec_offer_formats(
    formats: list[AudioFormat],
    *,
    include_common_codecs: bool = True,
    include_dahua_pcm: bool = False,
) -> list[RtpPcmFormat]:
    format_set = set(formats)
    capabilities = common_sip_codecs()
    out: list[RtpPcmFormat] = []
    if (
        include_dahua_pcm
        and AudioFormat(16000, PcmFormat.S16LE, 1, 20) in format_set
    ):
        # Dahua UAC endpoints use dynamic PT 97 for signed 16-bit
        # little-endian samples.  It is intentionally not RFC 3551 L16.
        out.append(RtpPcmFormat(97, "PCM", 16000, 1, 20))
    if (
        include_common_codecs
        and
        "OPUS" in capabilities
        and AudioFormat(48000, PcmFormat.S16LE, 2, 20) in format_set
    ):
        out.append(RtpPcmFormat(98, "OPUS", 48000, 2, 20))
    if (
        include_common_codecs
        and
        "G722" in capabilities
        and AudioFormat(16000, PcmFormat.S16LE, 1, 20) in format_set
    ):
        out.append(RtpPcmFormat(9, "G722", 8000, 1, 20))
    if (
        include_common_codecs
        and AudioFormat(8000, PcmFormat.S16LE, 1, 20) in format_set
    ):
        out.extend(
            (
                RtpPcmFormat(8, "PCMA", 8000, 1, 20),
                RtpPcmFormat(0, "PCMU", 8000, 1, 20),
            )
        )
    return out


def parse_sdp(sdp: str | bytes) -> dict:
    if isinstance(sdp, bytes):
        sdp = sdp.decode("utf-8", errors="strict")
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
    for raw in sdp.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
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
            if candidate_port == 0:
                continue
            if not 1 <= candidate_port <= 65535:
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
                if not line.startswith("c=IN IP4 "):
                    raise SdpError(f"unsupported SDP connection: {line}")
                session_conn = line.removeprefix("c=IN IP4 ").strip()
            elif in_selected_audio:
                if not line.startswith("c=IN IP4 "):
                    raise SdpError(f"unsupported SDP audio connection: {line}")
                media_conn = line.removeprefix("c=IN IP4 ").strip()
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
                ptime = int(line.removeprefix("a=ptime:").strip())
            elif line.startswith("a=minptime:"):
                minptime = int(line.removeprefix("a=minptime:").strip())
            elif line.startswith("a=maxptime:"):
                maxptime = int(line.removeprefix("a=maxptime:").strip())
        except ValueError as err:
            raise SdpError(f"bad SDP audio attribute: {line}") from err
    connection_ip = media_conn or session_conn
    if not connection_ip or not media_port or not payload_order:
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


def _parse_media_sections(sdp_body: str | bytes) -> tuple[str, str, list[dict]]:
    """Parse enough SDP structure to negotiate independent media sections."""

    if isinstance(sdp_body, bytes):
        sdp_body = sdp_body.decode("utf-8", errors="strict")
    session_connection = ""
    session_direction = "sendrecv"
    current: dict | None = None
    sections: list[dict] = []
    for raw in sdp_body.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
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
                "direction": session_direction,
            }
            sections.append(current)
            continue
        if line.startswith("c="):
            if current is None:
                if not line.startswith("c=IN IP4 "):
                    raise SdpError(f"unsupported SDP connection: {line}")
                address = line.removeprefix("c=IN IP4 ").strip()
                session_connection = address
            else:
                current["connection_seen"] = True
                if line.startswith("c=IN IP4 "):
                    current["connection_ip"] = line.removeprefix("c=IN IP4 ").strip()
                else:
                    # A media-level connection applies only to that media
                    # section.  Preserve a usable IPv4 audio section even if
                    # an additional video/data section advertises IP6 or an
                    # address family this deliberately small SIP profile does
                    # not implement.  The unsupported section is rejected in
                    # the answer instead of poisoning the entire audio call.
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
                if not 1 <= rtcp_port <= 65535:
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
        except ValueError as err:
            raise SdpError(f"bad SDP media attribute: {line}") from err
    for section in sections:
        if not section["connection_seen"]:
            section["connection_ip"] = session_connection
        section["connection_held"] = section["connection_ip"] == "0.0.0.0"
    return session_connection, session_direction, sections


def _section_rtpmap(section: dict, payload_type: int) -> tuple[str, int, int] | None:
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
    if media == "audio" and payload_type in _STATIC_RTPMAP:
        return _STATIC_RTPMAP[payload_type]
    if media == "video":
        if payload_type == 26:
            return ("JPEG", 90000, 1)
        if payload_type == 34:
            return ("H263", 90000, 1)
    return None


def _normalized_rtcp_feedback(values: list[str] | tuple[str, ...]) -> set[str]:
    """Normalize RFC 4585 feedback tokens for capability comparison."""

    return {" ".join(str(value).strip().lower().split()) for value in values}


def _equivalent_rtp_payloads(
    offered_section: dict,
    answered_section: dict,
) -> dict[int, set[int]]:
    """Map each answer payload to equivalent payloads in the offer.

    RFC 3264 recommends reusing the same payload type but explicitly permits
    a different number for the same codec.  The mapping remains directional:
    the offerer's RTP uses the answer number while the answerer's RTP uses the
    offer number.  A dynamic payload number itself, however, must never be
    remapped to a different codec within the media stream.
    """

    try:
        offered_payloads = {
            int(value) for value in offered_section.get("formats", ())
        }
        answered_payloads = {
            int(value) for value in answered_section.get("formats", ())
        }
    except ValueError as err:
        raise SdpError("RTP media formats must be numeric payload types") from err

    mappings: dict[int, set[int]] = {}
    for answer_payload in answered_payloads:
        answer_mapping = _section_rtpmap(answered_section, answer_payload)
        if answer_payload in offered_payloads:
            offered_mapping = _section_rtpmap(offered_section, answer_payload)
            if offered_mapping != answer_mapping:
                raise SdpError(
                    f"SDP answer remapped payload type {answer_payload}"
                )
        if answer_mapping is None:
            continue
        equivalent = {
            offer_payload
            for offer_payload in offered_payloads
            if _section_rtpmap(offered_section, offer_payload) == answer_mapping
        }
        if equivalent:
            mappings[answer_payload] = equivalent
    return mappings


def validate_sdp_answer(
    offer: str | bytes,
    answer: str | bytes,
    *,
    allow_omitted_trailing_media: bool = False,
) -> None:
    """Validate the RFC 3264 media-section and direction answer contract.

    An answer cannot add, remove, or reorder ``m=`` sections. Accepted media
    must retain the offered media type and transport profile and select at
    least one offered codec. RFC 3264 permits a different RTP payload number
    for that codec in the answer, so payload identity is kept directional.
    Direction is expressed from each endpoint's perspective, so one-way
    offers have only one legal active inverse.
    """

    _offer_connection, _offer_direction, offered = _parse_media_sections(offer)
    _answer_connection, _answer_direction, answered = _parse_media_sections(answer)
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
            raise SdpError(
                "SDP answer omitted a required audio media section"
            )
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
            equivalent_payloads = _equivalent_rtp_payloads(
                offered_section,
                answered_section,
            )
            if not equivalent_payloads:
                raise SdpError(
                    f"SDP answer media section {index} selected no offered codec"
                )
        else:
            offered_formats = {
                str(value) for value in offered_section["formats"]
            }
            if not answered_formats.intersection(offered_formats):
                raise SdpError(
                    f"SDP answer media section {index} selected an unoffered format"
                )
        if bool(answered_section["rtcp_mux"]) and not bool(
            offered_section["rtcp_mux"]
        ):
            raise SdpError(
                f"SDP answer media section {index} added unoffered rtcp-mux"
            )
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
            wildcard_feedback = _normalized_rtcp_feedback(
                offered_feedback.get("*", [])
            )
            answered_payloads = {
                int(value) for value in answered_formats if str(value).isdigit()
            }
            for key, values in answered_section["rtcp_feedback"].items():
                answer_feedback = _normalized_rtcp_feedback(values)
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
                            _normalized_rtcp_feedback(
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
            raise SdpError(
                f"SDP answer direction {answer_direction} is invalid for "
                f"{offer_direction} offer in media section {index}"
            )


def parse_video_sdp(sdp_body: str | bytes) -> dict | None:
    """Return the first video media section when it matches this profile."""

    _session_connection, _session_direction, sections = _parse_media_sections(sdp_body)
    for section in sections:
        if section["media"] != "video":
            continue
        # This deliberately small profile owns one video stream. Do not skip
        # an unsupported first m=video and accidentally answer a later format
        # in the first section's position.
        if (
            section["port"] == 0
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
            "direction": section["direction"],
            "connection_held": bool(section["connection_held"]),
        }
    return None


def _fmtp_parameters(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in str(value or "").split(";"):
        key, separator, raw_value = item.strip().partition("=")
        if key:
            out[key.lower()] = raw_value.strip() if separator else ""
    return out


_STATIC_VIDEO_RTPMAP = {
    26: "JPEG/90000",
    34: "H263/90000",
}
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


def offered_video_formats(sdp_body: str | bytes) -> list[RtpVideoFormat]:
    """List normalized formats from the first active AVP/AVPF video section."""

    parsed = parse_video_sdp(sdp_body)
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
        parameters = _fmtp_parameters(parsed["fmtp"].get(payload_type, ""))
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
                rtcp_feedback=(
                    feedback if str(parsed["transport_profile"]) == "RTP/AVPF" else ()
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


def _fmtp_without_parameters(value: str, excluded: set[str]) -> str:
    parameters = _fmtp_parameters(value)
    return ";".join(
        f"{key}={item}" if item else key
        for key, item in parameters.items()
        if key not in excluded
    )


def _h264_stream_contract(
    receiver: RtpVideoFormat,
    sender: RtpVideoFormat,
) -> RtpVideoFormat:
    """Combine receiver limits with parameter sets emitted by the sender."""

    # sprop-level-parameter-sets needs source-attribute negotiation that this
    # direct SIP profile does not implement.  Omit it so endpoints fall back
    # to the standards-defined in-band transport rather than associating
    # parameter sets with the wrong RTP source.
    receiver_fmtp = _fmtp_without_parameters(
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
            send=_h264_stream_contract(answer_receiver, local),
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
    if _fmtp_parameters(offered.fmtp) != _fmtp_parameters(answer.fmtp):
        # Generic exact-codec relay has no codec-specific way to reconcile
        # differing fmtp contracts.  Preserve the historical echo-answer for
        # an explicitly identical local capability only.
        return None
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
            _fmtp_parameters(candidate.fmtp) != _fmtp_parameters(selected.fmtp)
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
            recv=_h264_stream_contract(common, selected),
        )
    if candidate.encoding == "VP8":
        return RtpVideoDirection(
            send=selected,
            recv=replace(candidate, direction=selected.direction),
        )
    return RtpVideoDirection(
        send=selected,
        recv=replace(candidate, direction=selected.direction),
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
            for key, value in _fmtp_parameters(video_format.fmtp).items()
        )

    parsed = _fmtp_parameters(video_format.fmtp)
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
    if selected.transport_profile == "RTP/AVPF":
        for feedback in selected.rtcp_feedback:
            lines.append(f"a=rtcp-fb:{payload_type} {feedback}")
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
        if profile == "RTP/AVPF":
            feedback = item.rtcp_feedback or (
                ("nack pli", "ccm fir") if item.encoding in {"H264", "VP8"} else ()
            )
            for value in feedback:
                lines.append(f"a=rtcp-fb:{payload_type} {value}")
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
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=VoIP Stack",
        f"c=IN IP4 {media_ip}",
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


def offered_pcm_formats(
    sdp: str | bytes,
    *,
    allow_dahua_pcm: bool = False,
) -> list[RtpPcmFormat]:
    parsed = parse_sdp(sdp)
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
                or parsed["ptime"] not in {0, 20}
            ):
                continue
        elif encoding not in {"L16", "L24", "PCMA", "PCMU", "OPUS", "G722"}:
            continue
        out.append(
            RtpPcmFormat(
                pt,
                encoding,
                rate,
                channels,
                20
                if encoding == "PCM" and not parsed["ptime"]
                else parsed["ptime"],
                parsed["minptime"],
                parsed["maxptime"],
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


def negotiate_dtmf_answer(
    remote_sdp: str | bytes,
    local_offer_sdp: str | bytes,
) -> RtpDtmfFormat | None:
    """Return the receive-side telephone-event payload from the offer.

    As with audio/video codecs, an answer may assign another PT to the same
    telephone-event clock rate. The answerer still transmits using the PT in
    the offer, which is the value an outbound dialog must decode.
    """

    answered = offered_dtmf_formats(remote_sdp)
    offered = offered_dtmf_formats(local_offer_sdp)
    for answer_format in answered:
        for offer_format in offered:
            if answer_format.sample_rate == offer_format.sample_rate:
                events = answer_format.events & offer_format.events
                if events:
                    return RtpDtmfFormat(
                        offer_format.payload_type,
                        offer_format.sample_rate,
                        events,
                    )
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


def negotiate_directional(
    remote_sdp: str | bytes,
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    allow_dahua_pcm: bool = False,
) -> RtpPcmDirection | None:
    parsed = parse_sdp(remote_sdp)
    local_direction = local_direction_for_offer(
        parsed["direction"],
        remote_connection_held=bool(parsed["connection_held"]),
    )
    format_direction = _format_direction_for_remote(
        parsed["direction"],
        local_direction,
    )
    return _negotiate_for_local_direction(
        offered_pcm_formats(
            remote_sdp,
            allow_dahua_pcm=allow_dahua_pcm,
        ),
        local_send_preferred,
        local_recv_preferred,
        format_direction,
        prefer_offer_order=False,
    )


def negotiate_answer_directional(
    remote_sdp: str | bytes,
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    local_offer_direction: str = "sendrecv",
    local_offer_sdp: str | bytes | None = None,
    allow_dahua_pcm: bool = False,
) -> RtpPcmDirection | None:
    """Negotiate one standards-compliant media contract from an SDP answer."""

    parsed = parse_sdp(remote_sdp)
    local_direction = local_direction_for_offer(
        parsed["direction"],
        remote_connection_held=bool(parsed["connection_held"]),
    )
    format_direction = _format_direction_for_remote(
        parsed["direction"],
        local_direction,
        inactive_default=local_offer_direction,
    )
    answered_formats = offered_pcm_formats(
        remote_sdp,
        allow_dahua_pcm=allow_dahua_pcm,
    )
    if local_offer_sdp is not None:
        return _negotiate_answer_with_offer_payloads(
            answered_formats,
            offered_pcm_formats(
                local_offer_sdp,
                allow_dahua_pcm=allow_dahua_pcm,
            ),
            local_send_preferred,
            local_recv_preferred,
            format_direction,
        )
    return _negotiate_for_local_direction(
        answered_formats,
        local_send_preferred,
        local_recv_preferred,
        format_direction,
        prefer_offer_order=True,
    )


def build_answer_directional(
    origin_ip: str,
    media_ip: str,
    media_port: int,
    send: RtpPcmFormat,
    recv: RtpPcmFormat,
    *,
    dtmf: RtpDtmfFormat | None = None,
    remote_sdp: str | bytes | None = None,
    video_port: int = 0,
    video_format: RtpH264Format | None = None,
    audio_direction: str | None = None,
    video_direction: str | None = None,
) -> str:
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
    format_direction = _format_direction_for_remote(
        remote_audio["direction"] if remote_audio is not None else "sendrecv",
        audio_direction,
    )
    if format_direction == "sendonly":
        selected = [send]
    elif format_direction == "recvonly":
        selected = [recv]
    else:
        if _rtp_contract_key(send) != _rtp_contract_key(recv):
            raise SdpError(
                "SDP sendrecv answer requires one RTP payload usable for both TX and RX"
            )
        selected = [send]
    payload_values = [str(fmt.payload_type) for fmt in selected]
    if (
        dtmf is not None
        and dtmf.events
        and str(dtmf.payload_type) not in payload_values
    ):
        payload_values.append(str(dtmf.payload_type))
    payloads = " ".join(payload_values)
    audio_lines = [f"m=audio {int(media_port)} RTP/AVP {payloads}"]
    for fmt in selected:
        audio_lines.append(
            f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}"
        )
    if dtmf is not None and dtmf.events:
        audio_lines.append(
            f"a=rtpmap:{dtmf.payload_type} telephone-event/{dtmf.sample_rate}"
        )
        audio_lines.append(
            f"a=fmtp:{dtmf.payload_type} {_format_dtmf_events(dtmf.events)}"
        )
    audio_lines.extend(
        [
            f"a=ptime:{selected[0].frame_ms}",
            f"a=maxptime:{selected[0].frame_ms}",
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
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=VoIP Stack",
        f"c=IN IP4 {media_ip}",
        *(_offered_time_lines(remote_sdp) if remote_sdp else ["t=0 0"]),
        *audio_lines,
    ]
    return "\r\n".join(lines) + "\r\n"
