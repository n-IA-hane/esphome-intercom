"""Wire contracts and validation for an outbound SIP dialog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable
from collections.abc import Mapping
from .core.media_address import MediaEndpoint
from .core.media.payload_map import PayloadConflict, PayloadMap, RtpMapping
from .core import sdp, sip, sip_transfer
from .core.sip_dialog import DialogSignalingState
from .sip_udp_io import SipDatagramQueueProtocol


def _explicit_uri_transport(uri: sip.SipUri) -> str:
    """Return an administratively selected SIP transport, if present."""

    return next(
        (
            str(value or "").strip().upper()
            for key, value in uri.params
            if key.strip().lower() == "transport"
        ),
        "",
    )


def _sip_decline_reason(msg: sip.SipMessage) -> str:
    # State follows the final SIP status, never untrusted diagnostic text.
    return sip.sip_failure_reason(int(msg.status_code or 0))




def _is_invite_progress_response(status_code: int | None) -> bool:
    return status_code is not None and 100 < int(status_code) < 200


@dataclass(slots=True)
class SipDialog(DialogSignalingState):
    target: str
    remote_host: str
    remote_sip_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    local_rtp_port: int
    call_id: str
    local_uri: str
    remote_uri: str
    send_format: sdp.RtpPcmFormat
    recv_format: sdp.RtpPcmFormat
    remote_tag: str = ""
    recv_dtmf_formats: tuple[sdp.RtpDtmfFormat, ...] = ()
    send_dtmf_formats: tuple[sdp.RtpDtmfFormat, ...] = ()
    dtmf_payload_type: int | None = None
    dtmf_clock_rate: int = 8000
    dtmf_events: frozenset[int] = frozenset(range(16))
    send_dtmf_payload_type: int | None = None
    send_dtmf_clock_rate: int | None = None
    send_dtmf_events: frozenset[int] | None = None
    remote_audio_direction: str = "sendrecv"
    local_audio_direction: str = "sendrecv"
    remote_audio_connection_held: bool = False
    video_format: sdp.RtpVideoFormat | None = None
    # ``video_format`` remains the backward-compatible local-TX contract
    # selected by the remote answer.  ``local_video_format`` is the distinct
    # local-RX contract retained from our offer (RFC 6184 level asymmetry and
    # VP8 receiver limits).
    local_video_format: sdp.RtpVideoFormat | None = None
    imageattr_fallback_needed: bool = False
    remote_video_rtp_host: str = ""
    remote_video_rtp_port: int = 0
    remote_video_rtcp_host: str = ""
    remote_video_rtcp_port: int = 0
    remote_video_rtcp_mux: bool = False
    remote_video_payload_types: tuple[int, ...] = ()
    remote_video_connection_held: bool = False
    local_video_rtp_port: int = 0
    local_video_direction: str = "inactive"
    recv_formats: tuple[sdp.RtpPcmFormat, ...] = ()
    send_formats: tuple[sdp.RtpPcmFormat, ...] = ()
    match_received_codec: bool = False
    audio_payloads: PayloadMap = field(default_factory=PayloadMap, repr=False)
    video_payloads: PayloadMap = field(default_factory=PayloadMap, repr=False)
    # The previous dialog retains its already discovered destinations until
    # its media owner finalizes or compensates a remotely accepted update.
    _local_media_endpoints: dict | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        self.audio_payloads = _audio_payload_update(self.audio_payloads,
            send=self.send_formats or (self.send_format,), receive=self.recv_formats or (self.recv_format,),
            send_dtmf=self.send_dtmf_payload_type, send_clock=self.send_dtmf_clock_rate or self.dtmf_clock_rate,
            receive_dtmf=self.dtmf_payload_type, receive_clock=self.dtmf_clock_rate,
            receive_events=self.recv_dtmf_formats, send_events=self.send_dtmf_formats)
        self.video_payloads = _video_payload_update(self.video_payloads, self.video_format,
                                                    self.local_video_format or self.video_format)

    @property
    def recv_payload_history(self) -> tuple[sdp.RtpPcmFormat, ...]:
        """Read-only compatibility view; the immutable map is the sole history."""
        return tuple(sdp.RtpPcmFormat(pt, encoding, clock, channels)
                     for pt, (encoding, clock, channels) in self.audio_payloads.receive_history.items()
                     if encoding != "TELEPHONE-EVENT")

    @property
    def dtmf_direction(self):
        """Project committed fields without parsing or selecting another SDP contract."""
        receive = (sdp.RtpDtmfFormat(self.dtmf_payload_type, self.dtmf_clock_rate, self.dtmf_events)
                   if self.dtmf_payload_type is not None else None)
        send = (sdp.RtpDtmfFormat(self.send_dtmf_payload_type,
                self.send_dtmf_clock_rate or self.dtmf_clock_rate,
                self.send_dtmf_events if self.send_dtmf_events is not None else self.dtmf_events)
                if self.send_dtmf_payload_type is not None else None)
        if send is None and receive is None:
            return None
        return sdp.RtpDtmfDirection(send, receive,
            self.send_dtmf_formats or ((send,) if send is not None else ()),
            self.recv_dtmf_formats or ((receive,) if receive is not None else ()))

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.send_format

    @property
    def send_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.video_format

    @property
    def recv_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.local_video_format or self.video_format


def _payload_numbers(formats) -> dict[int, RtpMapping]:
    result = {}
    for fmt in formats:
        if isinstance(fmt, sdp.RtpPcmFormat):
            identity = (fmt.encoding.upper(), fmt.sample_rate, fmt.channels)
        elif isinstance(fmt, sdp.RtpVideoFormat):
            identity = (fmt.encoding.upper(), fmt.clock_rate, 1)
        elif isinstance(fmt, sdp.RtpDtmfFormat):
            identity = ("TELEPHONE-EVENT", fmt.sample_rate, 1)
        else:
            continue  # A delayed-offer seed is decoded PCM, not an RTP mapping.
        previous = result.get(fmt.payload_type)
        if previous is not None and previous != identity:
            raise PayloadConflict("SDP assigned one payload to different negotiated codecs")
        result[fmt.payload_type] = identity
    return result


def _audio_payload_update(previous: PayloadMap, *, send, receive, send_dtmf=None, send_clock=8000,
                          receive_dtmf=None, receive_clock=8000, receive_events=(), send_events=()) -> PayloadMap:
    outgoing = (*send, *send_events, *((sdp.RtpDtmfFormat(send_dtmf, send_clock),) if send_dtmf is not None else ()))
    incoming = (*receive, *receive_events, *((sdp.RtpDtmfFormat(receive_dtmf, receive_clock),) if receive_dtmf is not None else ()))
    return previous.with_mappings(send=_payload_numbers(outgoing), receive=_payload_numbers(incoming))


def _video_payload_update(previous: PayloadMap, send, receive) -> PayloadMap:
    return previous.with_mappings(send=_payload_numbers((send,)), receive=_payload_numbers((receive,)))


@dataclass(frozen=True, slots=True)
class _DelayedRemoteOffer:
    request: sip.SipMessage
    offer_sdp: str
    remote_target_uri: str
    local_sdp_session_version: int


DialogMediaCommit = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PreparedDialogMediaUpdate:
    """Two-phase media update owned until its SIP response is committed."""

    commit: DialogMediaCommit
    rollback: DialogMediaCommit | None = None
    answer_video_format: sdp.RtpVideoFormat | None = None
    answer_video_rtp_port: int | None = None
    on_committed: Callable[[SipDialog], None] | None = None
    answer_media_endpoints: Mapping[str, MediaEndpoint] | None = None


DialogMediaUpdateHandler = Callable[
    [SipDialog, SipDialog, str],
    Awaitable[DialogMediaCommit | PreparedDialogMediaUpdate | None],
]


ReferHandler = Callable[[sip_transfer.SipReferTarget], Awaitable[int]]


SipTransferResult = sip_transfer.SipTransferResult


@dataclass(slots=True, frozen=True)
class _InDialogResponse:
    request: sip.SipMessage
    status: int
    reason: str
    extra_headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    to_tag: str = ""
    contact_uri: str = ""


_SipClientProtocol = SipDatagramQueueProtocol


_MIN_INVITE_RETRY_AFTER = 0.2


_MAX_INVITE_RETRY_AFTER = 2.0
