"""SIP server wire records, limits and validation helpers."""

from __future__ import annotations

from .core.media.payload_map import PayloadMap

import asyncio
from dataclasses import dataclass, field, replace
import secrets
from typing import Any, Awaitable, Callable, Protocol
from .sip_listener_request import SipRequestResult
from .core.audio_format import AudioFormat
from .core.audio_capabilities import AudioCapabilities
from .core import sdp, sip, sip_transfer
from .core.codec_policy import CodecPolicy
from .core.sip_auth import SipDigestCredentials
from .core.sip_dialog import PreparedDialogIdentity, DialogSignalingState, uas_request_matches_dialog
from .core.sip_transaction import SipInvite2xxTransaction


_MAX_SIP_UDP_TASKS = 32


_MAX_SIP_INVITE_TASKS = 24


_MAX_PENDING_INVITES = 64


_MAX_COMPLETED_TRANSACTIONS = 256


_MAX_REUSED_UDP_USAGES = 256


class _SipRequestNotSent(ConnectionError):
    """The transport rejected a request before accepting any bytes."""


@dataclass(frozen=True, slots=True)
class SipInvite:
    source_host: str
    source_port: int
    request_uri: sip.SipUri
    caller_uri: sip.SipUri | None
    target: str
    caller: str
    call_id: str
    cseq: str
    remote_sdp: bytes
    send_format: sdp.RtpPcmFormat
    recv_format: sdp.RtpPcmFormat
    remote_rtp_host: str
    remote_rtp_port: int
    remote_audio_direction: str = "sendrecv"
    local_audio_direction: str = "sendrecv"
    remote_audio_connection_held: bool = False
    video_format: sdp.RtpVideoFormat | None = None
    # ``video_format`` is the remote offer/local-TX contract.  The local
    # answer/local-RX contract can differ under H.264 level asymmetry and VP8
    # receiver-only fmtp limits.
    local_video_format: sdp.RtpVideoFormat | None = None
    video_answer_format: sdp.RtpVideoFormat | None = None
    remote_video_rtp_host: str = ""
    remote_video_rtp_port: int = 0
    remote_video_rtcp_host: str = ""
    remote_video_rtcp_port: int = 0
    remote_video_rtcp_mux: bool = False
    remote_video_payload_types: tuple[int, ...] = ()
    remote_video_connection_held: bool = False
    signaling_transport: str = "UDP"
    received_via_trunk: bool = False
    peer_profile: str = ""
    caller_route: str = ""
    target_route: str = ""
    initial_response_sent: bool = False
    trunk_id: str = ""
    called_number: str = ""
    called_number_raw: str = ""
    called_number_source: str = ""
    caller_number: str = ""
    caller_name: str = ""
    inbound_trunk_id: str = ""
    outbound_trunk_id: str = ""
    identified_source_endpoint_id: str = ""
    authenticated_username: str = ""
    max_forwards: int | None = None
    replaces: sip_transfer.SipReplaces | None = None
    codec_policy: CodecPolicy = CodecPolicy()
    local_recv_capabilities: tuple[AudioFormat, ...] = ()
    local_send_capabilities: tuple[AudioFormat, ...] = ()
    local_audio_capabilities: AudioCapabilities | None = None
    recv_formats: tuple[sdp.RtpPcmFormat, ...] = ()
    send_formats: tuple[sdp.RtpPcmFormat, ...] = ()
    dtmf_direction: sdp.RtpDtmfDirection | None = None
    audio_payloads: PayloadMap | None = None

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.recv_format

    def _events(self):
        return self.dtmf_direction

    @property
    def recv_dtmf_format(self):
        events = self._events()
        return events.recv if events is not None else None

    @property
    def send_dtmf_format(self):
        events = self._events()
        return events.send if events is not None else None

    @property
    def recv_dtmf_formats(self):
        events = self._events()
        return events.recv_formats if events is not None else ()

    @property
    def send_dtmf_formats(self):
        events = self._events()
        return events.send_formats if events is not None else ()

    @property
    def send_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.video_format

    @property
    def recv_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.local_video_format or self.video_format

    @property
    def answer_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.video_answer_format or self.recv_video_format

    @property
    def routing_caller(self) -> str:
        return str(
            self.caller_route
            or (self.caller_uri.user if self.caller_uri is not None else "")
            or self.caller
        ).strip()

    @property
    def routing_target(self) -> str:
        return str(self.target_route or self.target or self.request_uri.user).strip()


def answered_media_contract(invite: SipInvite, answer_sdp: str | bytes) -> SipInvite:
    """Project the chosen local video answer before sending, without codec selection.

    Audio is retained verbatim. Ports and direction come from the owner's
    generated answer, never hardware metadata or another negotiation pass.
    """
    if not answer_sdp:
        return invite
    _connection, _direction, sections = sdp.parse_media_sections(answer_sdp)
    section = next((value for value in sections if value["media"] == "video"), None)
    if section is None or not section["port"]:
        if invite.video_format is None and invite.local_video_format is None:
            return invite
        return replace(invite, video_format=None, local_video_format=None, video_answer_format=None)
    selected = invite.answer_video_format
    if selected is None:
        raise sdp.SdpError("active video answer requires its chosen media contract")
    actual = next((value for value in sdp.offered_video_formats(answer_sdp)
                   if (value.payload_type, value.encoding, value.clock_rate) ==
                      (selected.payload_type, selected.encoding, selected.clock_rate)), None)
    if actual is None:
        raise sdp.SdpError("video answer changed the chosen codec mapping")
    remote_direction = sdp.local_direction_for_remote(section["direction"])
    send = replace(invite.send_video_format, direction=remote_direction) if invite.send_video_format else None
    receive = replace(actual, direction=remote_direction)
    return replace(invite, video_format=send, local_video_format=receive, video_answer_format=actual)


@dataclass(frozen=True, slots=True)
class SipInitialInvite:
    """Signaling identity for an initial INVITE that carries no SDP offer."""

    source_host: str
    source_port: int
    request_uri: sip.SipUri
    caller_uri: sip.SipUri | None
    target: str
    caller: str
    call_id: str
    cseq: str
    signaling_transport: str = "UDP"
    received_via_trunk: bool = False
    peer_profile: str = ""
    caller_route: str = ""
    target_route: str = ""
    trunk_id: str = ""
    called_number: str = ""
    called_number_raw: str = ""
    called_number_source: str = ""
    caller_number: str = ""
    caller_name: str = ""
    inbound_trunk_id: str = ""
    outbound_trunk_id: str = ""
    identified_source_endpoint_id: str = ""
    authenticated_username: str = ""
    max_forwards: int | None = None
    replaces: sip_transfer.SipReplaces | None = None
    codec_policy: CodecPolicy = CodecPolicy()

    @property
    def routing_caller(self) -> str:
        return str(
            self.caller_route
            or (self.caller_uri.user if self.caller_uri is not None else "")
            or self.caller
        ).strip()

    @property
    def routing_target(self) -> str:
        return str(self.target_route or self.target or self.request_uri.user).strip()


@dataclass(frozen=True, slots=True)
class SipInviteResult:
    """Prepared SIP response with an optional atomic media transition.

    Handlers may reserve resources while building this result, but must defer
    mutations of the active media contract to ``commit``.  ``rollback`` must
    release every prepared resource when signaling fails, the dialog ends, or
    the commit cannot complete.

    Changed answers carry ``media_contract`` from their preparing owner. A
    legacy handler may omit it only for an unchanged compatible answer; the
    server validates that description and never reconstructs a ranked choice.
    """

    status: int
    reason: str
    answer_sdp: str = ""
    to_tag: str = ""
    defer_final: bool = False
    decline_reason: str = ""
    commit: Callable[[], Awaitable[None]] | None = None
    rollback: Callable[[], Awaitable[None]] | None = None
    dialog_identity: PreparedDialogIdentity | None = None
    finalize: Callable[[], Awaitable[None]] | None = None
    connected_identity_name: str = ""
    connected_identity_user: str = ""
    on_committed: Callable[[SipInvite], None] | None = None
    media_contract: SipInvite | None = None

    def __post_init__(self):
        if self.media_contract is not None and self.answer_sdp:
            object.__setattr__(self, "media_contract",
                answered_media_contract(self.media_contract, self.answer_sdp))


DelayedOfferAnswer = Callable[[SipInvite], Awaitable[SipInviteResult]]


@dataclass(frozen=True, slots=True)
class UasDelayedOfferPlan:
    """One local offer and the application continuation for its ACK answer."""

    offer_sdp: str
    accept_answer: DelayedOfferAnswer
    rollback: Callable[[], Awaitable[None]] | None = None


@dataclass(slots=True)
class _PendingInvite:
    request: sip.SipMessage
    addr: tuple[str, int]
    to_tag: str
    transport: str
    status: int = 100
    reason: str = "Trying"
    answer_sdp: str = ""
    decline_reason: str = ""
    cancelled: bool = False
    reliable_rseq: int = 0
    expiry_task: asyncio.Task[None] | None = None
    final_task: asyncio.Task[None] | None = None
    final_retransmissions: int = 0
    local_sdp_session_id: int = field(default_factory=lambda: secrets.randbits(63) or 1)
    local_sdp_session_version: int = 0
    connected_identity_name: str = ""
    connected_identity_user: str = ""
    delayed_offer_plan: UasDelayedOfferPlan | None = None
    identity: SipInvite | SipInitialInvite | None = None
    media_contract: SipInvite | None = None
    codec_policy: CodecPolicy = CodecPolicy()


@dataclass(frozen=True, slots=True)
class _DeferredInviteFinal:
    status: int
    reason: str
    answer_sdp: str
    decline_reason: str
    connected_identity_name: str
    connected_identity_user: str


@dataclass(slots=True)
class _ReliableProvisional:
    request: sip.SipMessage
    addr: tuple[str, int]
    to_tag: str
    rseq: int
    response_status: int
    response_reason: str
    answer_sdp: str = ""
    task: asyncio.Task[None] | None = None
    deferred_final: _DeferredInviteFinal | None = None
    deferred_progress: _DeferredInviteFinal | None = None


@dataclass(slots=True)
class _DialogResponse:
    request: sip.SipMessage
    addr: tuple[str, int]
    status: int
    reason: str
    answer_sdp: str = ""


@dataclass(slots=True)
class _PendingDelayedOffer:
    """Local offer awaiting its answer in an INVITE ACK."""

    request: sip.SipMessage
    addr: tuple[str, int]
    offer_sdp: str
    previous_invite: SipInvite | None
    initial_invite: SipInitialInvite | None
    remote_target_uri: str
    accept_answer: DelayedOfferAnswer | None = None
    rollback: Callable[[], Awaitable[None]] | None = None
    settled: bool = False


@dataclass(slots=True)
class _ActiveDialog(DialogSignalingState):
    request: sip.SipMessage
    addr: tuple[str, int]
    to_tag: str
    cseq: int
    transport: str
    status: int = 200
    reason: str = "OK"
    answer_sdp: str = ""
    invite: SipInvite | None = None
    last_request: sip.SipMessage | None = None
    last_status: int = 200
    last_reason: str = "OK"
    last_response_sdp: str = ""
    renegotiations: int = 0
    update_in_progress: bool = False
    media_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    local_cseq: int = 1
    invite_2xx: SipInvite2xxTransaction = field(
        default_factory=SipInvite2xxTransaction
    )
    response_cache: list[_DialogResponse] = field(default_factory=list)
    connected_identity_name: str = ""
    connected_identity_user: str = ""
    connected_identity_sent: bool = False
    connected_identity_task: asyncio.Task[None] | None = None
    local_uri: str = ""
    local_display_name: str = ""
    remote_uri: str = ""
    remote_display_name: str = ""
    update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_timer_task: asyncio.Task[None] | None = None
    refer_task: asyncio.Task[None] | None = None
    refer_notifier: sip_transfer.ReferNotifier | None = None
    incoming_refer_count: int = 0
    refer_subscription: sip_transfer.ReferSubscription | None = None
    refer_count: int = 0
    termination_task: asyncio.Task[bool] | None = None
    delayed_offer: _PendingDelayedOffer | None = None
    prepared_video_reinvite: tuple[SipInvite, SipInvite, str] | None = None
    local_offer_in_progress: bool = False
    local_video_reinvite_status: int = 0
    local_video_reinvite_reason: str = ""
    codec_policy: CodecPolicy = CodecPolicy()
    event_credentials: Callable[[], SipDigestCredentials | None] | None = None
    audio_payloads: PayloadMap = field(default_factory=PayloadMap)
    authenticated_flow: asyncio.StreamWriter | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.invite is not None:
            self.install_invite(self.invite)

    def install_invite(self, invite):
        """Commit one media generation without retiring its payload meanings."""
        from .core.media.negotiator import payload_history

        candidate = payload_history(invite.audio_payloads or self.audio_payloads,
            sdp.RtpPcmDirection(invite.send_format, invite.recv_format, invite.recv_formats, invite.send_formats),
            invite._events())
        history = self.audio_payloads.with_mappings(
            receive=candidate.receive_history, send=candidate.send_history)
        self.audio_payloads = history.with_mappings(receive=candidate.receive, send=candidate.send)
        self.invite = invite



@dataclass(slots=True)
class _CompletedRequest:
    request: sip.SipMessage
    addr: tuple[str, int]
    status: int
    reason: str
    response: bytes = b""


InviteHandler = Callable[[SipInvite], Awaitable[SipInviteResult]]


InviteAuthorizer = Callable[
    [sip.SipMessage, SipInitialInvite, tuple[str, int], str], SipInitialInvite | SipRequestResult
]


OfferlessInviteHandler = Callable[
    [SipInitialInvite], Awaitable[UasDelayedOfferPlan | None]
]


TerminateHandler = Callable[[str, str], Awaitable[None]]


RegisterHandler = Callable[[sip.SipMessage, tuple[str, int], str], Awaitable[Any]]


InfoHandler = Callable[[sip.SipMessage, tuple[str, int], str], Awaitable[None]]


MediaUpdateHandler = Callable[[SipInvite, SipInvite, str], Awaitable[SipInviteResult]]


ReferHandler = Callable[[str, sip_transfer.SipReferTarget], Awaitable[int]]


FollowUpSender = Callable[
    [sip.SipMessage, tuple[str, int], SipRequestResult],
    Awaitable[sip.SipMessage | None],
]


RequestHandler = Callable[
    [sip.SipMessage, tuple[str, int], str, FollowUpSender],
    Awaitable[SipRequestResult],
]


SendHandler = Callable[[bytes, tuple[str, int]], bool | None]


TcpDialogSender = Callable[[bytes], bool | None]


def _uri_from_header(header: str) -> sip.SipUri | None:
    value = (header or "").strip()
    if "<" in value and ">" in value:
        value = value[value.index("<") + 1:value.index(">")]
    try:
        return sip.parse_sip_uri(value)
    except Exception:
        return None


def _uri_text_from_header(header: str) -> str:
    value = str(header or "").strip()
    if "<" in value and ">" in value:
        value = value[value.index("<") + 1:value.index(">")]
    elif value.lower().startswith("tel:"):
        value = value.split(";tag=", 1)[0]
    if value.lower().startswith("tel:"):
        return sip.identity_uri(value)
    uri = _uri_from_header(header)
    return str(uri) if uri is not None else ""


def _identity_header(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch not in "\r\n").strip()


def _cseq_number(value: str) -> int:
    try:
        return sip.parse_cseq(value).number
    except Exception:
        return 1


def _same_dialog_request(request: sip.SipMessage, dialog: _ActiveDialog, _addr: tuple[str, int]) -> bool:
    """Match a new in-dialog request, not an INVITE retransmission."""
    try:
        cseq = sip.parse_cseq(request.header("CSeq"))
    except (TypeError, ValueError, sip.SipError):
        return False
    return bool(
        cseq.number >= dialog.cseq
        and uas_request_matches_dialog(
            request,
            dialog.request,
            local_tag=dialog.to_tag,
        )
    )


def _same_dialog_ack(
    request: sip.SipMessage,
    dialog: _ActiveDialog,
    _addr: tuple[str, int],
) -> bool:
    """Match the ACK that confirms the dialog's most recent INVITE 2xx."""

    try:
        cseq = sip.parse_cseq(request.header("CSeq"))
        from_tag = sip.extract_tag(request.header("From"))
        to_tag = sip.extract_tag(request.header("To"))
        remote_tag = sip.extract_tag(dialog.request.header("From"))
    except (TypeError, ValueError, sip.SipError):
        return False
    return bool(
        request.header("Call-ID") == dialog.request.header("Call-ID")
        and cseq.method == "ACK"
        and from_tag
        and from_tag == remote_tag
        and to_tag
        and to_tag == dialog.to_tag
    )


def _same_video_media(previous: SipInvite, updated: SipInvite) -> bool:
    """Return whether an in-dialog request leaves video media unchanged.

    The current SIP profile does not renegotiate an established video RTP
    attachment. Accept session refreshes, but reject changes that the active
    browser socket or B2BUA relay cannot apply atomically.
    """

    if previous.video_format is None or updated.video_format is None:
        return previous.video_format is updated.video_format
    return bool(
        previous.video_format == updated.video_format
        and previous.local_video_format == updated.local_video_format
        and previous.video_answer_format == updated.video_answer_format
        and previous.remote_video_rtp_host == updated.remote_video_rtp_host
        and previous.remote_video_rtp_port == updated.remote_video_rtp_port
        and previous.remote_video_rtcp_host == updated.remote_video_rtcp_host
        and previous.remote_video_rtcp_port == updated.remote_video_rtcp_port
        and previous.remote_video_rtcp_mux == updated.remote_video_rtcp_mux
    )


def _same_audio_media(previous: SipInvite, updated: SipInvite) -> bool:
    """Return whether two offers describe the same negotiated audio stream."""

    return bool(
        previous.send_format.wire_token() == updated.send_format.wire_token()
        and previous.recv_format.wire_token() == updated.recv_format.wire_token()
        and previous.remote_rtp_host == updated.remote_rtp_host
        and previous.remote_rtp_port == updated.remote_rtp_port
        and previous.remote_audio_direction == updated.remote_audio_direction
    )


def _response_via_header(request: sip.SipMessage, addr) -> str:
    values = request.header_values("Via")
    value = values[0] if values else ""
    if not value:
        return ""
    try:
        parsed = sip.parse_via(value)
    except Exception:
        return value
    has_rport = any(key == "rport" for key, _ in parsed.params)
    source_host = str(addr[0]).strip("[]").lower()
    sent_by_host = str(parsed.host).strip("[]").lower()
    add_received = source_host != sent_by_host
    if not has_rport and not add_received:
        return value

    sent_by = parsed.host
    if parsed.port:
        sent_by = f"{sent_by}:{parsed.port}"
    rendered = f"SIP/2.0/{parsed.transport} {sent_by}"
    for key, val in parsed.params:
        if key in {"rport", "received"}:
            continue
        rendered += f";{key}" if val is None else f";{key}={val}"
    if add_received:
        rendered += f";received={addr[0]}"
    if has_rport:
        rendered += f";rport={int(addr[1])}"
    return rendered


def _response_contact_uri(request: sip.SipMessage, *, local_ip: str, local_sip_port: int, transport: str) -> str:
    try:
        request_uri = sip.parse_sip_uri(request.uri)
        user = request_uri.user or "voip"
    except Exception:
        user = "voip"
    port = int(local_sip_port or 5060)
    return str(sip.SipUri(user, local_ip, port, params=(("transport", transport.lower()),)))


class ApplicationRequestRouter(Protocol):
    def resolve_local_target(self, uri: sip.SipUri) -> sip.SipUri: ...
    def local_contact_uri(self, request: sip.SipMessage, host: str, port: int, transport: str,
                          *, flow_trunk_id: str = "") -> str: ...
    def known_control(self, request: sip.SipMessage, addr: tuple[str, int], transport: str) -> bool: ...
    async def route_owned_request(self, request: sip.SipMessage, addr: tuple[str, int], transport: str,
                                  reply: Callable[[bytes], bool]) -> bool: ...
