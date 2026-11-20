"""Immutable RTP media values shared by SDP negotiation and media consumers."""

from __future__ import annotations

from dataclasses import dataclass

from .audio_format import AudioFormat, PcmFormat
from .imageattr import ImageAttributes


class SdpError(ValueError):
    """Malformed or unsupported SDP."""


@dataclass(frozen=True, slots=True)
class RtpPcmFormat:
    payload_type: int
    encoding: str
    sample_rate: int
    channels: int
    frame_ms: int = 20
    min_frame_ms: int = 0
    max_frame_ms: int = 0
    fmtp: str = ""

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
            return AudioFormat(48000, PcmFormat.S16LE, 1, self.frame_ms or 20)
        pcm = PcmFormat.S16LE if self.encoding in {"L16", "PCM"} else PcmFormat.S24LE
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
        return round(self.rtp_clock_rate * (self.frame_ms or 20) / 1000)

    def wire_token(self) -> str:
        return f"pt={self.payload_type}:{self.encoding}/{self.sample_rate}/{self.channels}/{self.frame_ms}ms"


@dataclass(frozen=True, slots=True)
class RtpDtmfFormat:
    payload_type: int
    sample_rate: int
    events: frozenset[int] = frozenset(range(16))


@dataclass(frozen=True, slots=True)
class RtpDtmfDirection:
    """Directional RFC 4733 payload mappings for one negotiated dialog."""

    send: RtpDtmfFormat
    recv: RtpDtmfFormat


@dataclass(frozen=True, slots=True)
class RtpPcmDirection:
    send: RtpPcmFormat
    recv: RtpPcmFormat
    recv_formats: tuple[RtpPcmFormat, ...] = ()
    send_formats: tuple[RtpPcmFormat, ...] = ()

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
    # RFC 8866 section 6.13 media-level maximum frame-rate recommendation.
    # Keep it in the directional media contract so a browser sender cannot
    # silently exceed the receive envelope selected by a SIP endpoint.
    max_framerate: float | None = None
    image_attributes: ImageAttributes | None = None

    def wire_token(self) -> str:
        token = f"pt={self.payload_type}:{self.encoding}/{self.clock_rate}"
        if self.encoding == "H264":
            token += (
                f";profile-level-id={self.profile_level_id};"
                f"packetization-mode={self.packetization_mode}"
            )
        elif self.fmtp:
            token += f";fmtp={self.fmtp}"
        if self.max_framerate is not None:
            token += f";framerate={self.max_framerate:g}"
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
                bool(section.get("rtcp_mux")) if rtcp_mux is None else bool(rtcp_mux)
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


RtpH264Format = RtpVideoFormat


MAX_RTP_OFFER_FORMATS = 11
