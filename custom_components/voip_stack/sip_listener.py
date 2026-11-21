"""SIP endpoint request handling for UDP and TCP listeners."""

from __future__ import annotations

import asyncio
import contextlib
import errno
from dataclasses import dataclass, field, replace
from functools import partial
import logging
import secrets
from typing import Any, Awaitable, Callable, Protocol

from .sip_capture import capture_io, send_sip_datagram
from .sip_tcp_server import SipTcpServer as _OwnedSipTcpServer
from .sip_listener_request import (
    SipFollowUpRequest,
    SipRequestResult,
    accepts_capability_sdp,
    dialog_extension_error,
    is_application_request,
    matches_cancel,
    matches_retransmission as _same_request_transaction,
    request_sequence,
)

from .core.audio_format import AudioFormat, HA_SIP_PCM_FORMATS
from .core.codec_capabilities import supports_dahua_pcm
from .const import VOIP_STACK_RTP_PORT
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES
from .core import sdp, sip, sip_transfer
from .core.sip_stun import binding_keepalive_response
from .core.codec_policy import CodecPolicy
from .core.sip_auth import DigestChallengeTracker, SipDigestCredentials
from .core.sip_transport import SIP_UDP_SAFE_REQUEST_BYTES, SipResponseMailbox, bind_flow_usage
from .core.media.shadow import shadow_inbound_offer
from .core.sip_dialog import (
    PreparedDialogIdentity,
    SubscriptionDialog,
    DialogKey,
    DialogSignalingState,
    apply_remote_offer_media,
    notify_media_commit,
    build_dialog_request,
    uas_request_matches_dialog,
)
from .sip_tcp_io import (
    read_sip_stream_message as _read_sip_stream_message,
)
from .core.sip_transaction import (
    SIP_T1 as _SIP_T1,
    SIP_T2 as _SIP_T2,
    SIP_TIMER_B as _INVITE_2XX_TIMEOUT,
    SIP_TIMER_H as _INVITE_NON2XX_TIMEOUT,
    SessionTimerDriver,
    SipInvite2xxTransaction,
    async_run_dialog_request_transaction,
    async_run_server_transaction,
    matches_invite_error_ack,
)
from .queue_utils import put_drop_oldest
from .dtmf import build_sip_info_dtmf_body
from .inbound_identity import identity_fields


_LOGGER = logging.getLogger(__name__)
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
    replaces: sip_transfer.SipReplaces | None = None
    codec_policy: CodecPolicy = CodecPolicy()
    local_recv_capabilities: tuple[AudioFormat, ...] = ()
    local_send_capabilities: tuple[AudioFormat, ...] = ()
    recv_formats: tuple[sdp.RtpPcmFormat, ...] = ()

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.recv_format

    @property
    def recv_dtmf_format(self) -> sdp.RtpDtmfFormat | None:
        """The event clock follows the peer's transmitted audio on this leg."""
        from .core.media.shadow import engine_mode
        return (sdp.dtmf_format_for_clock(self.remote_sdp, self.recv_format.sample_rate)
                if engine_mode() == "active" else sdp.first_offered_dtmf_format(self.remote_sdp))

    @property
    def send_dtmf_format(self) -> sdp.RtpDtmfFormat | None:
        from .core.media.shadow import engine_mode
        return (sdp.dtmf_format_for_clock(self.remote_sdp, self.send_format.sample_rate)
                if engine_mode() == "active" else sdp.first_offered_dtmf_format(self.remote_sdp))

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


@dataclass(slots=True)
class _CompletedRequest:
    request: sip.SipMessage
    addr: tuple[str, int]
    status: int
    reason: str


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


class SipUdpEndpoint(asyncio.DatagramProtocol):
    """Small SIP/UDP user agent.

    The protocol object is intentionally policy-light: it validates SIP/SDP and
    delegates call routing to `on_invite`. It sends only standards-compliant SIP
    responses.
    """

    def __init__(
        self,
        *,
        local_ip: str,
        local_rtp_port: int,
        local_sip_port: int = 5060,
        supported_formats: list[AudioFormat],
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        on_invite: InviteHandler,
        on_offerless_invite: OfferlessInviteHandler | None = None,
        codec_policy_resolver=None,
        invite_identity_resolver=None,
        invite_authorizer: InviteAuthorizer | None = None,
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        on_media_update: MediaUpdateHandler | None = None,
        on_refer: ReferHandler | None = None,
        on_request: RequestHandler | None = None,
        request_router: ApplicationRequestRouter | None = None,
        send_override: SendHandler | None = None,
        request_route: Callable[[sip.SipUri, tuple[str, int]], Awaitable[tuple[str, int]]] | None = None,
        transaction_flow: Callable[[tuple[str, int]], object] | None = None,
        signaling_transport: str = "UDP",
        flow_trunk_id: str = "",
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
        trusted_trunk: bool = False,
        trusted_trunk_id: str = "",
        max_pending_invites: int = _MAX_PENDING_INVITES,
        deferred_invite_timeout: float | None = None,
    ) -> None:
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port or 5060)
        self.local_rtp_port = local_rtp_port or VOIP_STACK_RTP_PORT
        base_formats = supported_formats or list(HA_SIP_PCM_FORMATS)
        self.supported_send_formats = supported_send_formats or base_formats
        self.supported_recv_formats = supported_recv_formats or base_formats
        self.on_invite = on_invite
        self.on_offerless_invite = on_offerless_invite
        self.codec_policy_resolver = codec_policy_resolver
        self.invite_identity_resolver = invite_identity_resolver
        self.invite_authorizer = invite_authorizer
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.on_media_update = on_media_update
        self.on_refer = on_refer
        self.on_request = on_request
        self.request_router = request_router
        self.send_override = send_override
        self.request_route = request_route
        self.transaction_flow = transaction_flow
        self.signaling_transport = (signaling_transport or "UDP").upper()
        self.application_tcp_endpoint: SipUdpEndpoint | None = None
        self.application_udp_endpoint: SipUdpEndpoint | None = None
        self.flow_trunk_id = flow_trunk_id
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        self.trusted_trunk = bool(trusted_trunk)
        self.trusted_trunk_id = str(trusted_trunk_id)
        self.max_pending_invites = max(1, int(max_pending_invites))
        self.deferred_invite_timeout = (
            None if deferred_invite_timeout is None else max(0.01, float(deferred_invite_timeout))
        )
        self.transport: asyncio.DatagramTransport | None = None
        self._closed_waiter: asyncio.Future[None] | None = None
        self.pending_invites: dict[str, _PendingInvite] = {}
        self._reliable_provisionals: dict[str, _ReliableProvisional] = {}
        self.completed_invites: dict[str, _PendingInvite] = {}
        self.active_dialogs: dict[str, _ActiveDialog] = {}
        self._outside_refer: dict[str, sip_transfer.ReferSubscription] = {}
        self._event_dialogs: dict[str, _ActiveDialog] = {}
        self.completed_byes: dict[str, _CompletedRequest] = {}
        self.completed_infos: dict[tuple[str, int], _CompletedRequest] = {}
        self.completed_pracks: dict[tuple[str, int], _CompletedRequest] = {}
        self.completed_application_requests: dict[
            tuple[str, int, str], tuple[_CompletedRequest, SipRequestResult]
        ] = {}
        self._logged_incompatible_invites: set[str] = set()
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._invite_tasks: set[asyncio.Task[None]] = set()
        self._maintenance_tasks: set[asyncio.Task[Any]] = set()
        self._client_transaction_responses: dict[str, SipResponseMailbox] = {}
        self._dialog_queues: dict[tuple[tuple[str, int], str], asyncio.Queue] = {}
        self._reused_dialog_keys: dict[asyncio.Queue, set[tuple[tuple[str, int], str]]] = {}
        self.dropped_datagrams = 0
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self._closed_waiter = asyncio.get_running_loop().create_future()
        _LOGGER.info("SIP UDP listener ready on %s", transport.get_extra_info("sockname"))

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            _LOGGER.warning("SIP UDP listener closed with error: %s", exc)
        else:
            _LOGGER.info("SIP UDP listener closed")
        self.transport = None
        for queue in self._reused_dialog_keys:
            put_drop_oldest(queue, None)
        self._dialog_queues.clear()
        self._reused_dialog_keys.clear()
        self.cancel_request_tasks()
        if self._closed_waiter is not None and not self._closed_waiter.done():
            self._closed_waiter.set_result(None)

    def close_transaction_flow(self, flow: object, error: BaseException | None) -> None:
        """Settle only transactions owned by this exact transport connection.

        A local framing/read timeout has no transport error and must not be
        converted into an EOF recovery attempt on a different connection.
        """
        for responses in tuple(self._client_transaction_responses.values()):
            if responses.flow is flow:
                if error is not None:
                    responses.fail(error)
                else:
                    responses.deliver(None)

    async def wait_closed(self) -> None:
        waiter = self._closed_waiter
        if waiter is not None and not waiter.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter, timeout=1.0)
        tasks = tuple(self._request_tasks | self._maintenance_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def datagram_received(self, data: bytes, addr) -> None:
        capture_io(data, self.transport, addr)
        self.submit_datagram(data, addr)

    def submit_datagram(self, data: bytes, addr) -> bool:
        """Schedule one message while preserving capacity for dialog control."""

        if self.signaling_transport == "UDP" and data[:1] in (b"\0", b"\1"):
            response = binding_keepalive_response(data, addr)
            if response is None or self.transport is None:
                self.dropped_datagrams += 1
                return False
            send_sip_datagram(self.transport, response, addr)
            return True
        try:
            message = sip.parse_message(data)
        except (ValueError, sip.SipError):
            self.dropped_datagrams += 1
            return False
        queue = self._dialog_queues.get(((str(addr[0]), int(addr[1])), message.header("Call-ID")))
        if queue is not None:
            put_drop_oldest(queue, (data, (str(addr[0]), int(addr[1]))) if self.signaling_transport == "UDP" else data)
            return True
        if message.is_response:
            try:
                branch = sip.parse_via(message.header("Via")).branch
            except sip.SipError:
                return False
            mailbox = self._client_transaction_responses.get(branch)
            if mailbox is not None and mailbox.matches(message):
                mailbox.deliver((message, addr))
                return True
            self.dropped_datagrams += 1
            return False
        is_invite = message.method == "INVITE"
        is_control = self._known_control(message, addr)
        if (
            len(self._request_tasks) >= _MAX_SIP_UDP_TASKS
            or (not is_control and len(self._request_tasks) >= _MAX_SIP_INVITE_TASKS)
            or (is_invite and len(self._invite_tasks) >= _MAX_SIP_INVITE_TASKS)
        ):
            self.dropped_datagrams += 1
            if self.dropped_datagrams & (self.dropped_datagrams - 1) == 0:
                _LOGGER.warning(
                    "SIP UDP handler saturated; dropped %d datagrams",
                    self.dropped_datagrams,
                )
            return False
        task = asyncio.create_task(self._handle_datagram_guarded(data, addr))
        self._request_tasks.add(task)
        if is_invite:
            self._invite_tasks.add(task)
        task.add_done_callback(self._request_task_done)
        return True

    def open_reused_dialog(self, addr: tuple[str, int], call_id: str, *, flow_active, routed=False):
        """Lease this socket to one reader, optionally following routed targets."""
        key = (addr, call_id)
        transport = self.transport
        if (transport is None or not call_id or key in self._dialog_queues
                or len(self._dialog_queues) >= _MAX_REUSED_UDP_USAGES):
            return None
        queue = asyncio.Queue(maxsize=64)
        self._dialog_queues[key] = queue
        self._reused_dialog_keys[queue] = {key}

        def send(raw, destination=None):
            if (self.transport is not transport or self._dialog_queues.get(key) is not queue
                    or not flow_active()):
                return False
            destination = addr if destination is None else destination
            if destination != addr:
                try:
                    usage_id = sip.parse_message(raw).header("Call-ID")
                except sip.SipError:
                    return False
                if self._dialog_queues.get((addr, usage_id)) is not queue:
                    return False
                target_key = (destination, usage_id)
                current = self._dialog_queues.get(target_key)
                if (not routed or (current is not None and current is not queue)
                        or (current is None and len(self._dialog_queues) >= _MAX_REUSED_UDP_USAGES)):
                    return False
                self._dialog_queues[target_key] = queue
                self._reused_dialog_keys[queue].add(target_key)
            return self._send(raw, destination)

        return send, queue

    def bind_reused_usage(self, addr, call_id, usage_id):
        queue = self._dialog_queues.get((addr, call_id))
        if queue is None:
            raise ConnectionError("SIP flow owner is no longer registered")
        if usage_id == call_id:
            return lambda: None
        if len(self._dialog_queues) >= _MAX_REUSED_UDP_USAGES:
            raise RuntimeError("SIP UDP dialog usage capacity reached")
        release = bind_flow_usage(self._dialog_queues, (addr, call_id), (addr, usage_id))
        keys = self._reused_dialog_keys[queue]
        keys.add((addr, usage_id))

        def close():
            release()
            for key in tuple(keys):
                if key[1] == usage_id:
                    if self._dialog_queues.get(key) is queue:
                        self._dialog_queues.pop(key, None)
                    keys.discard(key)

        return close

    def close_reused_dialog(self, addr, call_id, *, queue=None):
        current = self._dialog_queues.get((addr, call_id))
        if current is None or (queue is not None and queue is not current):
            return
        for key in self._reused_dialog_keys.pop(current, ()):
            if self._dialog_queues.get(key) is current:
                self._dialog_queues.pop(key, None)

    def _next_hop_target(self, value: str) -> sip.SipUri:
        target = sip.parse_sip_uri(value)
        return self.request_router.resolve_local_target(target) if self.request_router is not None else target

    def _contact_for_request(self, request, *, transport):
        if self.request_router is not None:
            return self.request_router.local_contact_uri(request, self.local_ip, self.local_sip_port,
                                                         transport, flow_trunk_id=self.flow_trunk_id)
        return _response_contact_uri(request, local_ip=self.local_ip, local_sip_port=self.local_sip_port, transport=transport)

    def _known_control(self, request, addr):
        if self.request_router is not None and self.request_router.known_control(request, addr, self.signaling_transport):
            return True
        call_id = request.header("Call-ID")
        method = request.method
        subscription = self._outside_refer.get(call_id)
        if method == "NOTIFY" and subscription is not None:
            key = subscription.dialog.key
            return (sip.extract_tag(request.header("To")) == key.local_tag
                    and (not key.remote_tag or sip.extract_tag(request.header("From")) == key.remote_tag))
        dialog = self.active_dialogs.get(call_id) or self._event_dialogs.get(call_id)
        if method == "SUBSCRIBE" and dialog is not None and dialog.refer_notifier is not None:
            return DialogKey.from_uas_request(request) == dialog.refer_notifier.dialog.key
        if method in {"ACK", "BYE", "UPDATE", "NOTIFY"} and dialog is not None:
            return bool(uas_request_matches_dialog(request, dialog.request, local_tag=dialog.to_tag)
                        and (method != "NOTIFY" or dialog.refer_subscription is not None))
        if method == "CANCEL":
            pending = self.pending_invites.get(call_id)
            if pending is None:
                return False
            try:
                return (sip.parse_cseq(request.header("CSeq")).number == sip.parse_cseq(pending.request.header("CSeq")).number
                        and sip.parse_via(request.header("Via")).branch == sip.parse_via(pending.request.header("Via")).branch
                        and request.header("From") == pending.request.header("From"))
            except sip.SipError:
                return False
        if method == "PRACK":
            reliable = self._reliable_provisionals.get(call_id)
            if reliable is None:
                return False
            try:
                return (sip.parse_rack(request.header("RAck")) == sip.SipRAck(
                    reliable.rseq, sip.parse_cseq(reliable.request.header("CSeq")).number, "INVITE")
                    and uas_request_matches_dialog(request, reliable.request, local_tag=reliable.to_tag))
            except sip.SipError:
                return False
        return False

    def _request_task_done(self, task: asyncio.Task[None]) -> None:
        self._request_tasks.discard(task)
        self._invite_tasks.discard(task)

    @staticmethod
    def _remember_completed(store: dict, key: str, value: Any) -> None:
        if key not in store and len(store) >= _MAX_COMPLETED_TRANSACTIONS:
            store.pop(next(iter(store)))
        store[key] = value

    def _remember_terminated_invite(self, dialog: _ActiveDialog) -> None:
        """Reject late retransmissions after the dialog has been torn down."""
        invite_addr = dialog.addr
        for cached in dialog.response_cache:
            if cached.request is dialog.request:
                invite_addr = cached.addr
                break
        self._remember_completed(
            self.completed_invites,
            dialog.request.header("Call-ID"),
            _PendingInvite(
                dialog.request,
                invite_addr,
                dialog.to_tag,
                dialog.transport,
                status=481,
                reason="Call/Transaction Does Not Exist",
            ),
        )

    @staticmethod
    def _find_dialog_response(
        dialog: _ActiveDialog,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> _DialogResponse | None:
        for cached in reversed(dialog.response_cache):
            if _same_request_transaction(request, cached.request, addr, cached.addr):
                return cached
        return None

    @staticmethod
    def _remember_dialog_response(
        dialog: _ActiveDialog,
        request: sip.SipMessage,
        addr: tuple[str, int],
        status: int,
        reason: str,
        answer_sdp: str = "",
    ) -> None:
        dialog.response_cache.append(
            _DialogResponse(request, addr, int(status), str(reason), str(answer_sdp or ""))
        )
        del dialog.response_cache[:-16]

    def cancel_request_tasks(self) -> None:
        for subscription in self._outside_refer.values():
            subscription.close()
        self._outside_refer.clear()
        for dialog in self._event_dialogs.values():
            self._cancel_refer(dialog)
        self._event_dialogs.clear()
        for task in tuple(self._request_tasks | self._maintenance_tasks):
            task.cancel()
        for dialog in tuple(self.active_dialogs.values()):
            dialog.invite_2xx.cancel()
            self._cancel_connected_identity(dialog)
            self._cancel_session_timer(dialog)
            self._cancel_refer(dialog)
        for pending in tuple(self.pending_invites.values()):
            self._cancel_pending_expiry(pending)
        for reliable in tuple(self._reliable_provisionals.values()):
            self._cancel_reliable_provisional(reliable)
        self._reliable_provisionals.clear()
        for completed in tuple(self.completed_invites.values()):
            self._cancel_invite_non2xx(completed)

    @staticmethod
    def _cancel_pending_expiry(pending: _PendingInvite) -> None:
        task = pending.expiry_task
        pending.expiry_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    @staticmethod
    def _cancel_reliable_provisional(reliable: _ReliableProvisional) -> None:
        task = reliable.task
        reliable.task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _send_reliable_provisional(
        self,
        call_id: str,
        pending: _PendingInvite,
    ) -> bool:
        """Send and own one RFC 3262 provisional transaction."""

        pending.reliable_rseq = pending.reliable_rseq + 1 if pending.reliable_rseq else secrets.randbelow(0x7FFFFFFE) + 1
        reliable = _ReliableProvisional(
            request=pending.request,
            addr=pending.addr,
            to_tag=pending.to_tag,
            rseq=pending.reliable_rseq,
            response_status=pending.status,
            response_reason=pending.reason,
            answer_sdp=pending.answer_sdp,
        )
        self._reliable_provisionals[call_id] = reliable

        if not self._transmit_reliable_provisional(reliable):
            self._reliable_provisionals.pop(call_id, None)
            return False

        async def run() -> None:
            interval = _SIP_T1
            deadline = asyncio.get_running_loop().time() + _INVITE_2XX_TIMEOUT
            try:
                while self._reliable_provisionals.get(call_id) is reliable:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    if self.pending_invites.get(call_id) is not pending:
                        await asyncio.sleep(remaining)
                        continue
                    await asyncio.sleep(min(interval, remaining))
                    if self._reliable_provisionals.get(call_id) is not reliable:
                        return
                    if self.pending_invites.get(call_id) is pending:
                        self._transmit_reliable_provisional(reliable)
                        interval *= 2
                if (
                    self._reliable_provisionals.pop(call_id, None) is reliable
                    and self.pending_invites.get(call_id) is pending
                ):
                    self._send_final_response_now(
                        call_id,
                        504,
                        "Server Time-out",
                        decline_reason="prack_timeout",
                    )
                    if self.on_terminated is not None:
                        await self.on_terminated(call_id, "prack_timeout")
            except asyncio.CancelledError:
                return
            finally:
                if reliable.task is asyncio.current_task():
                    reliable.task = None

        task = asyncio.create_task(run(), name=f"voip-sip-prack-{call_id}")
        reliable.task = task
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_tasks.discard)
        return True

    def _transmit_reliable_provisional(
        self,
        reliable: _ReliableProvisional,
    ) -> bool:
        return self._send_response(
            reliable.request,
            reliable.addr,
            reliable.response_status,
            reliable.response_reason,
            body=reliable.answer_sdp.encode() if reliable.answer_sdp else b"",
            to_tag=reliable.to_tag,
            extra_headers=(("Require", "100rel"), ("RSeq", str(reliable.rseq))),
        )

    def _arm_pending_expiry(self, call_id: str, pending: _PendingInvite) -> None:
        self._cancel_pending_expiry(pending)
        if self.deferred_invite_timeout is None:
            return

        async def _expire() -> None:
            try:
                await asyncio.sleep(self.deferred_invite_timeout)
                if self.pending_invites.get(call_id) is not pending:
                    return
                sent = self.send_final_response(
                    call_id,
                    480,
                    "Temporarily Unavailable",
                    decline_reason="no_answer",
                )
                if not sent:
                    self.pending_invites.pop(call_id, None)
                if self.on_terminated is not None:
                    await self.on_terminated(call_id, "no_answer")
            except asyncio.CancelledError:
                return
            finally:
                if pending.expiry_task is asyncio.current_task():
                    pending.expiry_task = None

        task = asyncio.create_task(
            _expire(),
            name=f"voip-sip-invite-expiry-{call_id}",
        )
        pending.expiry_task = task
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_tasks.discard)

    @staticmethod
    def _cancel_connected_identity(dialog: _ActiveDialog) -> None:
        task = dialog.connected_identity_task
        dialog.connected_identity_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    @staticmethod
    def _cancel_session_timer(dialog: _ActiveDialog) -> None:
        task = dialog.session_timer_task
        dialog.session_timer_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    @staticmethod
    def _cancel_refer(dialog: _ActiveDialog) -> None:
        if dialog.refer_notifier is not None:
            dialog.refer_notifier.cancel()
        if dialog.refer_subscription is not None:
            dialog.refer_subscription.close()
            dialog.refer_subscription = None
        task = dialog.refer_task
        dialog.refer_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _retire_dialog(self, call_id: str) -> _PendingDelayedOffer | None:
        """Detach one dialog and every signaling owner exactly once."""

        current = self.active_dialogs.get(call_id)
        if current is None:
            return None
        current.invite_2xx.cancel()
        self._cancel_connected_identity(current)
        self._cancel_session_timer(current)
        if ((current.refer_task is not None and not current.refer_task.done())
                or (current.refer_subscription is not None and not current.refer_subscription.result.done())):
            self._event_dialogs[call_id] = current
        else:
            self._cancel_refer(current)
        termination_task = current.termination_task
        current.termination_task = None
        if (
            termination_task is not None
            and termination_task is not asyncio.current_task()
        ):
            termination_task.cancel()
        delayed = current.delayed_offer
        current.delayed_offer = None
        self._remember_terminated_invite(current)
        self.active_dialogs.pop(call_id, None)
        return delayed

    async def _terminate_dialog(self, call_id: str, reason: str) -> None:
        """Signal and retire a confirmed dialog, then notify its owner."""

        await self.async_send_bye(
            call_id,
            timeout=min(1.5, _INVITE_2XX_TIMEOUT),
        )
        if self.on_terminated is not None:
            await self.on_terminated(call_id, reason)

    def _activate_dialog(
        self,
        call_id: str,
        pending: _PendingInvite,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> _ActiveDialog:
        """Build and activate one UAS dialog from its authoritative INVITE."""

        invite = self._parse_invite(request, addr, codec_policy=pending.codec_policy) if request.body else None
        if invite is not None and pending.answer_sdp:
            # The routed answer may select a common codec with the other leg.
            # Retain that accepted contract instead of repeating HA's initial
            # preference over the caller's original offer.
            accepted = sdp.negotiate_answer_directional(
                pending.answer_sdp, self.supported_recv_formats, self.supported_send_formats,
                local_offer_sdp=request.body, allow_dahua_pcm=invite.peer_profile == "dahua",
                local_capabilities=HA_AUDIO_CAPABILITIES, codec_policy=pending.codec_policy,
            )
            if accepted is not None:
                from .core.media.leg import receive_auxiliary_formats
                from .core.media.negotiator import policy_for_leg
                auxiliary = receive_auxiliary_formats(request.body, accepted.send,
                    policy_for_leg(self.supported_send_formats, self.supported_recv_formats,
                                   codec_policy=pending.codec_policy, capabilities=HA_AUDIO_CAPABILITIES),
                    own_description=pending.answer_sdp)
                invite = replace(invite, send_format=accepted.recv, recv_format=accepted.send,
                                 recv_formats=(accepted.send, *auxiliary))
        initial_invite = (
            self._parse_initial_invite(request, addr) if not request.body else None
        )
        if pending.identity is not None:
            if invite is not None:
                invite = replace(invite, **identity_fields(pending.identity),
                                 received_via_trunk=pending.identity.received_via_trunk,
                                 trunk_id=pending.identity.trunk_id,
                                 outbound_trunk_id=pending.identity.outbound_trunk_id,
                                 identified_source_endpoint_id=pending.identity.identified_source_endpoint_id,
                                 authenticated_username=pending.identity.authenticated_username)
            if initial_invite is not None:
                initial_invite = replace(initial_invite, **identity_fields(pending.identity),
                                         received_via_trunk=pending.identity.received_via_trunk,
                                         trunk_id=pending.identity.trunk_id,
                                         outbound_trunk_id=pending.identity.outbound_trunk_id,
                                         identified_source_endpoint_id=pending.identity.identified_source_endpoint_id,
                                         authenticated_username=pending.identity.authenticated_username)
        signaling_invite = invite or initial_invite
        if signaling_invite is None:
            raise sip.SipError("cannot activate dialog from invalid INVITE")
        remote_target = (
            sip.contact_target_uri(request)
            or _uri_text_from_header(request.header("From"))
        )
        dialog = _ActiveDialog(
            request=request,
            addr=addr,
            to_tag=pending.to_tag,
            cseq=_cseq_number(request.header("CSeq")) + 1,
            transport=pending.transport,
            codec_policy=pending.codec_policy,
            remote_target_uri=remote_target,
            route_set=sip.record_route_set(request),
            status=pending.status,
            reason=pending.reason,
            answer_sdp=pending.answer_sdp,
            invite=invite,
            last_request=request,
            last_status=pending.status,
            last_reason=pending.reason,
            last_response_sdp=pending.answer_sdp,
            local_sdp_session_id=pending.local_sdp_session_id,
            local_sdp_session_version=pending.local_sdp_session_version,
            connected_identity_name=(
                pending.connected_identity_name or signaling_invite.target
            ),
            connected_identity_user=(
                pending.connected_identity_user or signaling_invite.routing_target
            ),
            peer_supports_from_change=sip.supports_option(request, "from-change"),
        )
        plan = pending.delayed_offer_plan
        if plan is not None:
            dialog.delayed_offer = _PendingDelayedOffer(
                request=request,
                addr=addr,
                offer_sdp=pending.answer_sdp,
                previous_invite=None,
                initial_invite=initial_invite,
                remote_target_uri=remote_target,
                accept_answer=plan.accept_answer,
                rollback=plan.rollback,
            )
        self.active_dialogs[call_id] = dialog
        self._arm_session_timer(
            call_id,
            dialog,
            sip.negotiate_uas_session_timer(request),
            local_role="uas",
        )
        return dialog

    @staticmethod
    def _accept_remote_connected_identity(
        dialog: _ActiveDialog,
        request: sip.SipMessage,
    ) -> None:
        """Commit the peer identity carried by one accepted mid-dialog request."""

        try:
            remote_uri = str(sip.parse_sip_uri(request.header("From")))
        except (TypeError, ValueError, sip.SipError):
            return
        dialog.remote_uri = remote_uri
        dialog.remote_display_name = sip.name_addr_display_name(
            request.header("From")
        )

    def _arm_connected_identity(self, call_id: str, dialog: _ActiveDialog) -> None:
        """Send the RFC 4916 connected identity after the initial ACK."""

        if (
            dialog.connected_identity_sent
            or not dialog.peer_supports_from_change
            or self.active_dialogs.get(call_id) is not dialog
        ):
            return
        remote_uri = dialog.remote_uri or _uri_text_from_header(
            dialog.request.header("From")
        )
        original_local_uri = _uri_from_header(dialog.request.header("To"))
        remote_tag = sip.extract_tag(dialog.request.header("From"))
        identity_user = str(
            dialog.connected_identity_user
            or (original_local_uri.user if original_local_uri is not None else "voip")
        ).strip()
        if (
            not remote_uri
            or original_local_uri is None
            or not remote_tag
            or not identity_user
        ):
            return
        identity_uri = str(
            sip.SipUri(
                identity_user,
                original_local_uri.host,
                original_local_uri.port,
                params=original_local_uri.params,
            )
        )
        async def _run() -> None:
            try:
                response = await self._send_dialog_request(
                    call_id,
                    dialog,
                    "UPDATE",
                    local_uri=identity_uri,
                    local_display_name=dialog.connected_identity_name,
                )
                dialog.connected_identity_sent = response is not None
            except asyncio.CancelledError:
                return
            finally:
                if dialog.connected_identity_task is asyncio.current_task():
                    dialog.connected_identity_task = None

        task = asyncio.create_task(
            _run(),
            name=f"voip-sip-connected-identity-{call_id}",
        )
        dialog.connected_identity_task = task
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_tasks.discard)

    def _dialog_request_current(self, call_id, dialog, method):
        return (self.active_dialogs.get(call_id) is dialog or
                (method in {"REFER", "NOTIFY", "SUBSCRIBE"} and self._event_dialogs.get(call_id) is dialog))

    def _release_event_dialog(self, call_id, dialog):
        if dialog.refer_subscription is not None and not dialog.refer_subscription.result.done():
            return
        task = dialog.refer_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            return
        if self._event_dialogs.get(call_id) is dialog:
            self._event_dialogs.pop(call_id, None)

    async def _send_dialog_request(
        self,
        call_id: str,
        dialog: _ActiveDialog,
        method: str,
        *,
        local_uri: str = "",
        local_display_name: str = "",
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
        content_type: str = "",
        timeout: float = _INVITE_2XX_TIMEOUT,
    ) -> sip.SipMessage | None:
        """Send one serialized non-INVITE request on an owned dialog."""

        async with dialog.update_lock:
            if not self._dialog_request_current(call_id, dialog, method):
                return None
            remote_uri = dialog.remote_uri or _uri_text_from_header(
                dialog.request.header("From")
            )
            remote_target_uri = (
                dialog.remote_target_uri
                or _uri_text_from_header(dialog.request.header("Contact"))
                or remote_uri
            )
            remote_tag = sip.extract_tag(dialog.request.header("From"))
            local_uri = local_uri or _uri_text_from_header(dialog.request.header("To"))
            try:
                request = build_dialog_request(
                    method,
                    call_id=call_id,
                    local_tag=dialog.to_tag,
                    remote_tag=remote_tag,
                    cseq=dialog.local_cseq,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    remote_target_uri=remote_target_uri,
                    route_set=dialog.route_set,
                    contact_uri=self._contact_for_request(dialog.request, transport=dialog.transport),
                    transport=dialog.transport,
                    local_display_name=local_display_name,
                    remote_display_name=dialog.remote_display_name,
                    extra_headers=extra_headers,
                    content_type=content_type,
                    body=body,
                )
                target_addr = dialog.addr
                if dialog.transport == "UDP":
                    target = self._next_hop_target(request.routing.next_hop_uri)
                    target_addr = (target.host, int(target.port or 5060))
            except (TypeError, ValueError, sip.SipError) as err:
                _LOGGER.warning(
                    "SIP %s routing rejected call_id=%s: %s", method, call_id, err
                )
                return None

            if method == "REFER" and dialog.refer_subscription is not None:
                dialog.refer_subscription.bind(request.ids.cseq, first=dialog.refer_count == 0)
                dialog.refer_count += 1
            dialog.local_cseq += 1
            response = await self._run_client_transaction(
                raw=request.raw,
                addr=target_addr,
                ids=request.ids,
                method=method,
                transport=dialog.transport,
                active=lambda: self._dialog_request_current(call_id, dialog, method),
                timeout=timeout,
            )
            if method == "INVITE" and response is not None:
                self._send_dialog_invite_ack(
                    dialog,
                    request,
                    response,
                    target_addr,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                )
            return response

    async def async_options(self, uri: str, addr: tuple[str, int], *, timeout: float = 3.0):
        """Query one peer on the existing listener, without a call or RTP owner."""
        return await self.async_contact_request("OPTIONS", uri, addr,
            headers=(("Accept", "application/sdp"),), timeout=timeout)

    async def async_contact_request(self, method, uri, addr, *, headers, timeout, active=lambda: True, on_sent=None):
        """Run an out-of-dialog request on a specific contact's existing flow."""
        ids = sip.SipDialogIds(call_id=sip.make_call_id(method.lower()), local_tag=sip.make_tag(),
            remote_tag="", cseq=1, branch=sip.make_branch())
        local_uri = str(sip.SipUri("voip-stack", self.local_ip, self.local_sip_port))
        request_headers = sip.dialog_headers(request_uri=uri, local_uri=local_uri, remote_uri=uri,
            dialog=ids, method=method, contact_uri=local_uri, transport=self.signaling_transport)
        request_headers.extend(headers)
        return await self._run_client_transaction(raw=sip.build_request(method, uri, request_headers),
            addr=addr, ids=ids, method=method, transport=self.signaling_transport,
            timeout=timeout, creating_dialog=True, response_addr=addr, active=active, on_sent=on_sent)

    async def _run_client_transaction(
        self,
        *,
        raw: bytes,
        addr: tuple[str, int],
        ids: sip.SipDialogIds,
        method: str,
        transport: str,
        active: Callable[[], bool] = lambda: True,
        timeout: float = _INVITE_2XX_TIMEOUT,
        creating_dialog: bool = False,
        response_addr: tuple[str, int] | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> sip.SipMessage | None:
        """Run the shared non-INVITE transaction for this listener transport."""

        responses = SipResponseMailbox(
            lambda message: sip.response_matches_dialog_transaction(message, ids, method, creating_dialog=creating_dialog),
            flow=self.transaction_flow(addr) if self.transaction_flow is not None else None,
        )
        self._client_transaction_responses[ids.branch] = responses

        def send():
            sent = self._send(raw, addr)
            if sent is False and self.signaling_transport == "TCP":
                raise _SipRequestNotSent("SIP TCP flow is unavailable before send")
            return sent

        def sent():
            sip.mark_sip_event(self, method)
            if on_sent is not None:
                on_sent()

        async def read_response(timeout):
            # Advisory metadata belongs to the queried flow. A matching branch
            # received from another socket is not evidence about this binding.
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                item = await responses.receive(remaining)
                if item is None or response_addr is None or item[1] == response_addr:
                    return item

        try:
            return await async_run_dialog_request_transaction(
                send=send,
                read=read_response if response_addr is not None else responses.receive,
                matches=lambda response: sip.response_matches_dialog_transaction(
                    response, ids, method, creating_dialog=creating_dialog
                ),
                active=active,
                transport=transport,
                timeout=timeout,
                on_sent=sent,
            )
        finally:
            self._client_transaction_responses.pop(ids.branch, None)

    async def _send_application_follow_up(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
        result: SipRequestResult,
    ) -> sip.SipMessage | None:
        """Run one client transaction in an application-created dialog."""

        lock = result.follow_up_lock
        if lock is not None:
            async with lock:
                return await self._send_application_follow_up_unlocked(
                    request, addr, result
                )
        return await self._send_application_follow_up_unlocked(request, addr, result)

    async def _send_application_follow_up_unlocked(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
        result: SipRequestResult,
    ) -> sip.SipMessage | None:
        follow_up = result.follow_up
        if follow_up is None or not result.to_tag:
            return None
        try:
            state = result.follow_up_dialog
            local_uri = state.local_uri if state else _uri_text_from_header(request.header("To"))
            remote_uri = state.remote_uri if state else _uri_text_from_header(request.header("From"))
            remote_target = (
                state.remote_target_uri if state else _uri_text_from_header(request.header("Contact")) or remote_uri
            )
            routing = sip.dialog_request_routing(
                remote_target,
                state.route_set if state else tuple(request.header_values("Record-Route")),
            )
            ids = sip.SipDialogIds(
                call_id=state.key.call_id if state else request.header("Call-ID"),
                local_tag=state.key.local_tag if state else result.to_tag,
                remote_tag=state.key.remote_tag if state else sip.extract_tag(request.header("From")),
                cseq=follow_up.cseq,
                branch=sip.make_branch(),
            )
            contact_uri = self._contact_for_request(request, transport=self.signaling_transport)
            headers = sip.dialog_headers(
                request_uri=routing.request_uri,
                local_uri=local_uri,
                remote_uri=remote_uri,
                dialog=ids,
                method=follow_up.method,
                contact_uri=contact_uri,
                content_type=follow_up.content_type,
                transport=self.signaling_transport,
            )
            headers.extend(("Route", value) for value in routing.route_headers)
            headers.extend(follow_up.headers)
            raw = sip.build_request(
                follow_up.method,
                routing.request_uri,
                headers,
                follow_up.body,
            )
        except (TypeError, ValueError, sip.SipError) as err:
            _LOGGER.warning("SIP application follow-up rejected: %s", err)
            return None

        target = self._next_hop_target(routing.next_hop_uri)
        next_hop = (target.host, sip.sip_default_port(target))
        deadline = asyncio.get_running_loop().time() + _INVITE_2XX_TIMEOUT
        authentication = DigestChallengeTracker()
        credentials = result.follow_up_credentials
        target_revision = (state.remote_cseq, state.remote_target_uri) if state is not None else None
        flow_selector = getattr(self.request_router, "application_follow_up_flow", None)
        pinned_flow = flow_selector(request, state) if flow_selector is not None else None
        requested_transport = next((str(value or "").upper() for key, value in target.params
                                    if key.casefold() == "transport"), "UDP")
        selected_transport = pinned_flow[0] if pinned_flow is not None else requested_transport
        if selected_transport not in {"UDP", "TCP"}:
            raise ConnectionError("SIP application next hop requires unsupported transport")
        transaction_endpoint = (self if self.signaling_transport == selected_transport else
                                self.application_tcp_endpoint if selected_transport == "TCP" else
                                self.application_udp_endpoint)
        if transaction_endpoint is None:
            raise ConnectionError("SIP application next-hop transport is unavailable")

        def use_transport(endpoint):
            nonlocal headers, raw, contact_uri
            contact_uri = endpoint._contact_for_request(request, transport=endpoint.signaling_transport)
            transport_headers = sip.dialog_headers(request_uri=routing.request_uri,
                local_uri=local_uri, remote_uri=remote_uri, dialog=ids,
                method=follow_up.method, contact_uri=contact_uri, transport=endpoint.signaling_transport)
            headers = [(key, value) for key, value in headers if key.casefold() not in {"via", "contact"}]
            headers.extend((key, value) for key, value in transport_headers if key.casefold() in {"via", "contact"})
            raw = sip.build_request(follow_up.method, routing.request_uri, headers, follow_up.body)

        if transaction_endpoint is not self:
            use_transport(transaction_endpoint)
        if pinned_flow is not None:
            next_hop = pinned_flow[1]
        elif transaction_endpoint.request_route is not None:
            async with asyncio.timeout(max(0, deadline - asyncio.get_running_loop().time())):
                next_hop = await transaction_endpoint.request_route(target, addr)
        udp_fallback_used = False
        tcp_reconnected = False
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            if (transaction_endpoint.signaling_transport == "UDP"
                    and (requested_transport == "TCP" or len(raw) > SIP_UDP_SAFE_REQUEST_BYTES)
                    and not udp_fallback_used and pinned_flow is None):
                tcp = self.application_tcp_endpoint
                if tcp is None or tcp.request_route is None:
                    _LOGGER.warning("SIP application request needs unavailable TCP transport method=%s bytes=%s",
                                    follow_up.method, len(raw))
                    return None
                try:
                    async with asyncio.timeout(remaining):
                        tcp_hop = await tcp.request_route(target, addr)
                except OSError as err:
                    # Only setup failure before any request bytes were sent may
                    # retry UDP (RFC3261 section18.1.1), never a response timeout.
                    if requested_transport == "TCP" or err.errno not in {
                            errno.ECONNREFUSED, errno.ECONNRESET, errno.ENOPROTOOPT}:
                        raise
                    udp_fallback_used = True
                else:
                    transaction_endpoint = tcp
                    next_hop = tcp_hop
                    use_transport(tcp)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                response = await transaction_endpoint._run_client_transaction(
                    raw=raw, addr=next_hop, ids=ids, method=follow_up.method,
                    transport=transaction_endpoint.signaling_transport, creating_dialog=not ids.remote_tag,
                    timeout=remaining,
                )
            except _SipRequestNotSent:
                if (follow_up.method != "NOTIFY" or transaction_endpoint.signaling_transport != "TCP"
                        or tcp_reconnected or pinned_flow is not None or transaction_endpoint.request_route is None):
                    raise
                # One pre-send rejection may reopen the same resolved peer.
                # Once queued/written, EOF or timeout has uncertain delivery
                # and must never replay a NOTIFY on another connection.
                tcp_reconnected = True
                pinned_target = replace(target, host=next_hop[0], port=next_hop[1])
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                async with asyncio.timeout(remaining):
                    next_hop = await transaction_endpoint.request_route(pinned_target, addr)
                continue
            if response is None:
                return None
            status = int(response.status_code or 0)
            if status not in {401, 407}:
                if (200 <= status < 300 and state is not None
                        and target_revision == (state.remote_cseq, state.remote_target_uri)):
                    try:
                        contact = sip.contact_target_uri(response)
                    except sip.SipError:
                        return None
                    if contact:
                        state.remote_target_uri = contact
                return response
            if credentials is None or state is None:
                return response
            try:
                header, _challenge, authorization = authentication.authorize(
                    response, username=credentials.username, password=credentials.password,
                    auth_username=credentials.auth_username, method=follow_up.method,
                    uri=routing.request_uri, body=follow_up.body,
                )
            except ValueError:
                return response
            state.local_cseq = max(state.local_cseq, ids.cseq + 1)
            ids = replace(ids, cseq=state.next_cseq(), branch=sip.make_branch())
            if result.follow_up_cseq_changed is not None:
                result.follow_up_cseq_changed(ids.cseq)
            headers = [(key, value) for key, value in headers if key.casefold() not in
                       {header.casefold(), "via", "cseq"}]
            transaction_headers = sip.dialog_headers(
                request_uri=routing.request_uri, local_uri=local_uri, remote_uri=remote_uri,
                dialog=ids, method=follow_up.method, contact_uri=contact_uri,
                transport=transaction_endpoint.signaling_transport,
            )
            headers.extend((key, value) for key, value in transaction_headers if key.casefold() in {"via", "cseq"})
            headers.append((header, authorization))
            raw = sip.build_request(follow_up.method, routing.request_uri, headers, follow_up.body)


    def _send_dialog_invite_ack(
        self,
        dialog: _ActiveDialog,
        request,
        response: sip.SipMessage,
        target_addr: tuple[str, int],
        *,
        local_uri: str,
        remote_uri: str,
    ) -> bool:
        """ACK one final response to a locally initiated in-dialog INVITE."""

        status = int(response.status_code or 0)
        remote_tag = sip.extract_tag(response.header("To"))
        try:
            if 200 <= status < 300:
                remote_target = (
                    sip.contact_target_uri(response)
                    or dialog.remote_target_uri
                    or remote_uri
                )
                ack = build_dialog_request(
                    "ACK",
                    call_id=request.ids.call_id,
                    local_tag=request.ids.local_tag,
                    remote_tag=remote_tag,
                    cseq=request.ids.cseq,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    remote_target_uri=remote_target,
                    route_set=dialog.route_set,
                    contact_uri=local_uri,
                    transport=dialog.transport,
                )
                if dialog.transport == "UDP":
                    next_hop = self._next_hop_target(ack.routing.next_hop_uri)
                    target_addr = (next_hop.host, int(next_hop.port or 5060))
                raw = ack.raw
            else:
                ids = sip.SipDialogIds(
                    call_id=request.ids.call_id,
                    local_tag=request.ids.local_tag,
                    remote_tag=remote_tag,
                    cseq=request.ids.cseq,
                    branch=request.ids.branch,
                )
                headers = sip.dialog_headers(
                    request_uri=request.routing.request_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    dialog=ids,
                    method="ACK",
                    contact_uri=local_uri,
                    transport=dialog.transport,
                )
                headers.extend(
                    ("Route", value) for value in request.routing.route_headers
                )
                raw = sip.build_request(
                    "ACK", request.routing.request_uri, headers, b""
                )
        except (TypeError, ValueError, sip.SipError):
            return False
        return self._send(raw, target_addr) is not False

    async def async_prepare_video_reinvite(
        self,
        call_id: str,
        *,
        local_video_rtp_port: int,
        video_formats: tuple[sdp.RtpVideoFormat, ...],
        video_direction: str = "sendrecv",
        timeout: float = 8.0,
    ) -> SipInvite | None:
        """Negotiate one local video offer without publishing its media state."""

        dialog = self.active_dialogs.get(call_id)
        if (
            dialog is None
            or dialog.invite is None
            or dialog.prepared_video_reinvite is not None
            or dialog.update_in_progress
            or dialog.local_offer_in_progress
            or dialog.media_lock.locked()
            or not video_formats
            or int(local_video_rtp_port) < 0
        ):
            return None
        previous = dialog.invite
        direction = (
            "inactive" if int(local_video_rtp_port) == 0 else video_direction
        )
        prepared = False
        dialog.local_offer_in_progress = True
        dialog.local_video_reinvite_status = 0
        dialog.local_video_reinvite_reason = ""
        try:
            current_local_sdp = dialog.local_sdp_body or dialog.answer_sdp
            current_audio_formats = tuple(sdp.offered_pcm_formats(
                current_local_sdp, allow_dahua_pcm=previous.peer_profile == "dahua",
            ))
            if not current_audio_formats:
                return None
            offer = sdp.build_video_reoffer(
                current_local_sdp,
                video_port=int(local_video_rtp_port),
                video_formats=dialog.codec_policy.video_formats(tuple(video_formats)),
                video_direction=direction,
            )
            next_version = int(dialog.local_sdp_session_version)
            offer = sdp.rewrite_sdp_origin(
                offer, dialog.local_sdp_session_id, next_version
            )
            if sdp.sdp_description_changed(dialog.local_sdp_body, offer):
                next_version += 1
                offer = sdp.rewrite_sdp_origin(
                    offer, dialog.local_sdp_session_id, next_version
                )
            response = await self._send_dialog_request(
                call_id,
                dialog,
                "INVITE",
                body=offer.encode(),
                content_type="application/sdp",
                timeout=timeout,
            )
            dialog.local_video_reinvite_status = int(
                response.status_code or 0
            ) if response is not None else 408
            dialog.local_video_reinvite_reason = (
                str(response.reason or "") if response is not None else "Request Timeout"
            )
            if (
                response is None
                or not 200 <= int(response.status_code or 0) < 300
                or not response.body
            ):
                _LOGGER.warning(
                    "SIP local video re-INVITE missing final answer call_id=%s "
                    "status=%s body=%s",
                    call_id,
                    response.status_code if response is not None else None,
                    bool(response is not None and response.body),
                )
                return None
            sdp.validate_sdp_answer(
                offer,
                response.body,
                allow_inactive_rejected_media_port=int(local_video_rtp_port) == 0,
            )
            audio = sdp.negotiate_answer_directional(
                response.body,
                [fmt.audio_format for fmt in current_audio_formats],
                [fmt.audio_format for fmt in current_audio_formats],
                local_offer_direction=previous.local_audio_direction,
                local_offer_sdp=offer,
                allow_dahua_pcm=previous.peer_profile == "dahua",
                local_capabilities=HA_AUDIO_CAPABILITIES,
            )
            if audio is None:
                _LOGGER.warning(
                    "SIP local video re-INVITE audio answer incompatible call_id=%s "
                    "offer=[%s] answer=[%s] expected_tx=%s expected_rx=%s direction=%s",
                    call_id,
                    ", ".join(sdp.offered_media_descriptions(offer)),
                    ", ".join(sdp.offered_media_descriptions(response.body)),
                    ",".join(fmt.wire_token() for fmt in current_audio_formats),
                    ",".join(fmt.wire_token() for fmt in current_audio_formats),
                    previous.local_audio_direction,
                )
                return None
            parsed = sdp.parse_sdp(response.body)
            video_pair = (
                sdp.negotiate_video_answer_directional(
                    response.body, video_formats
                )
                if int(local_video_rtp_port) > 0
                else None
            )
            video_send = video_pair.send if video_pair is not None else None
            video_recv = video_pair.recv if video_pair is not None else None
            remote_video = (
                sdp.parse_video_sdp(response.body)
                if video_send is not None
                else None
            )
            if int(local_video_rtp_port) > 0 and remote_video is None:
                _LOGGER.warning(
                    "SIP local video re-INVITE video answer incompatible call_id=%s",
                    call_id,
                )
                return None
            candidate = replace(
                previous,
                remote_sdp=response.body,
                send_format=audio.send,
                recv_format=audio.recv,
                remote_rtp_host=str(parsed["connection_ip"]),
                remote_rtp_port=int(parsed["media_port"]),
                remote_audio_direction=str(parsed["direction"]),
                local_audio_direction=sdp.local_direction_for_offer(
                    str(parsed["direction"]),
                    remote_connection_held=bool(parsed["connection_held"]),
                ),
                remote_audio_connection_held=bool(parsed["connection_held"]),
                video_format=video_send,
                local_video_format=video_recv,
                video_answer_format=video_recv,
                **sdp.RemoteMediaTarget.from_section(
                    remote_video, rtcp_mux=False
                ).as_remote_video_fields(),
            )
            dialog.prepared_video_reinvite = (previous, candidate, offer)
            dialog.local_sdp_session_version = next_version
            prepared = True
            return candidate
        except (TypeError, ValueError, sdp.SdpError, sip.SipError):
            _LOGGER.exception(
                "SIP local video re-INVITE answer rejected call_id=%s",
                call_id,
            )
            return None
        finally:
            if not prepared:
                dialog.local_offer_in_progress = False

    def commit_prepared_reinvite(
        self,
        call_id: str,
        previous: SipInvite,
        candidate: SipInvite,
    ) -> bool:
        """Publish exactly one locally negotiated UAS dialog generation."""

        dialog = self.active_dialogs.get(call_id)
        if (
            dialog is None
            or dialog.invite is not previous
            or dialog.prepared_video_reinvite is None
            or dialog.prepared_video_reinvite[:2] != (previous, candidate)
        ):
            return False
        dialog.invite = candidate
        dialog.local_sdp_body = dialog.prepared_video_reinvite[2]
        dialog.prepared_video_reinvite = None
        dialog.local_offer_in_progress = False
        dialog.renegotiations += 1
        return True

    def abort_prepared_reinvite(
        self,
        call_id: str,
        previous: SipInvite,
        candidate: SipInvite,
    ) -> None:
        """Retire a dialog whose accepted local offer cannot be committed."""

        dialog = self.active_dialogs.get(call_id)
        if dialog is None or dialog.prepared_video_reinvite is None:
            return
        if dialog.prepared_video_reinvite[:2] != (previous, candidate):
            return
        dialog.prepared_video_reinvite = None
        dialog.local_offer_in_progress = False
        self.send_bye(call_id)

    def _event_credentials(self, call_id, dialog):
        if self.request_router is None:
            return None
        binder = getattr(self.request_router, "bind_dialog_credentials", None)
        if binder is None:
            return self.request_router.dialog_credentials(call_id, dialog.addr, dialog.transport)
        if dialog.event_credentials is None:
            dialog.event_credentials = binder(call_id, dialog.addr, dialog.transport)
        return dialog.event_credentials()

    async def _handle_dialog_refer(
        self,
        call_id: str,
        dialog: _ActiveDialog,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> None:
        """Accept one REFER and report its owner-provided result with NOTIFY."""

        try:
            refer_cseq = sip.parse_cseq(request.header("CSeq"))
            target = sip_transfer.request_refer_to(request)
        except (TypeError, ValueError, sip.SipError):
            self._send_response(
                request, addr, 400, "Bad Request", to_tag=dialog.to_tag
            )
            return
        if self.on_refer is None or (
            dialog.refer_task is not None and not dialog.refer_task.done()
        ):
            self._send_response(request, addr, 603, "Decline", to_tag=dialog.to_tag)
            self._remember_dialog_response(dialog, request, addr, 603, "Decline")
            return
        usage = SubscriptionDialog.from_request(dialog.request, dialog.to_tag)
        usage.remote_cseq = refer_cseq.number
        usage.remote_target_uri = dialog.remote_target_uri or usage.remote_target_uri
        usage.route_set = dialog.route_set
        dialog.incoming_refer_count += 1
        self._event_credentials(call_id, dialog)

        async def send(headers, body):
            async with dialog.update_lock:
                usage.local_cseq = max(usage.local_cseq, dialog.local_cseq)
                credentials = self._event_credentials(call_id, dialog)
                try:
                    return await self._send_application_follow_up_unlocked(dialog.request, dialog.addr,
                        SipRequestResult(to_tag=dialog.to_tag, follow_up_dialog=usage,
                            follow_up_credentials=credentials,
                            follow_up=SipFollowUpRequest(method="NOTIFY", cseq=usage.next_cseq(),
                                headers=headers, body=body, content_type="message/sipfrag")))
                finally:
                    dialog.local_cseq = max(dialog.local_cseq, usage.local_cseq)

        notifier = sip_transfer.ReferNotifier(usage, refer_cseq.number, send,
                                            allow_missing_id=dialog.incoming_refer_count == 1)
        dialog.refer_notifier = notifier
        self._send_response(request, addr, 200, "OK", to_tag=dialog.to_tag)
        self._remember_dialog_response(dialog, request, addr, 200, "OK")

        async def run() -> None:
            async def action():
                return await self.on_refer(call_id, target)
            try:
                await sip_transfer.run_refer_action(action, notifier.report)
            finally:
                await notifier.close()
                if dialog.refer_notifier is notifier:
                    dialog.refer_notifier = None
                if dialog.refer_task is asyncio.current_task():
                    dialog.refer_task = None
                self._release_event_dialog(call_id, dialog)

        task = asyncio.create_task(run(), name=f"voip-sip-refer-{call_id}")
        dialog.refer_task = task
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_tasks.discard)

    async def _accept_delayed_offer_ack(
        self,
        call_id: str,
        dialog: _ActiveDialog,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> bool:
        """Commit an offerless INVITE only after its ACK carries an answer."""

        delayed = dialog.delayed_offer
        if delayed is None:
            return True
        dialog.delayed_offer = None
        dialog.update_in_progress = False
        content_type = request.header("Content-Type").split(";", 1)[0].lower()
        updated_invite = (
            self._parse_invite(
                request,
                addr,
                peer_profile=(
                    delayed.previous_invite.peer_profile
                    if delayed.previous_invite is not None
                    else (
                        delayed.initial_invite.peer_profile
                        if delayed.initial_invite is not None
                        else ""
                    )
                ),
                local_offer_sdp=delayed.offer_sdp,
            )
            if request.body and content_type == "application/sdp"
            else None
        )
        if updated_invite is not None and delayed.initial_invite is not None:
            initial = delayed.initial_invite
            updated_invite = replace(
                updated_invite,
                source_host=initial.source_host,
                source_port=initial.source_port,
                request_uri=initial.request_uri,
                caller_uri=initial.caller_uri,
                target=initial.target,
                caller=initial.caller,
                call_id=initial.call_id,
                cseq=initial.cseq,
                signaling_transport=initial.signaling_transport,
                received_via_trunk=initial.received_via_trunk,
                trunk_id=initial.trunk_id,
                outbound_trunk_id=initial.outbound_trunk_id,
                identified_source_endpoint_id=initial.identified_source_endpoint_id,
                authenticated_username=initial.authenticated_username,
                **identity_fields(initial),
                peer_profile=initial.peer_profile,
                caller_route=initial.caller_route,
                target_route=initial.target_route,
                initial_response_sent=True,
            )
        result = SipInviteResult(488, "Not Acceptable Here")
        accept_answer = delayed.accept_answer
        if accept_answer is None and self.on_media_update is not None:
            previous = delayed.previous_invite

            async def accept_answer(updated: SipInvite) -> SipInviteResult:
                if previous is None:
                    return SipInviteResult(488, "Not Acceptable Here")
                return await self.on_media_update(previous, updated, "INVITE")

        if updated_invite is not None and accept_answer is not None:
            try:
                result = await accept_answer(updated_invite)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "SIP delayed offer commit failed call_id=%s", call_id
                )
                result = SipInviteResult(500, "Server Internal Error")
        if not 200 <= int(result.status) < 300 or updated_invite is None:
            if result.rollback is not None:
                await result.rollback()
            await self._rollback_delayed_offer(delayed)
            await self._terminate_dialog(call_id, "media_incompatible")
            return False
        if not await apply_remote_offer_media(result.commit, result.rollback, result.finalize):
            _LOGGER.error("SIP delayed offer media commit failed call_id=%s", call_id)
            await self._rollback_delayed_offer(delayed)
            await self._terminate_dialog(call_id, "media_update_failed")
            return False
        delayed.settled = True
        dialog.addr = addr
        if delayed.remote_target_uri:
            dialog.remote_target_uri = delayed.remote_target_uri
        dialog.invite = updated_invite
        dialog.answer_sdp = delayed.offer_sdp
        dialog.renegotiations += 1
        notify_media_commit(result.on_committed, updated_invite)
        self._arm_session_timer(
            call_id,
            dialog,
            sip.negotiate_uas_session_timer(delayed.request),
            local_role="uas",
        )
        return True

    @staticmethod
    async def _rollback_delayed_offer(delayed: _PendingDelayedOffer) -> None:
        """Release one uncommitted offer continuation at most once."""

        if delayed.settled:
            return
        delayed.settled = True
        if delayed.rollback is not None:
            await delayed.rollback()

    def _arm_session_timer(
        self,
        call_id: str,
        dialog: _ActiveDialog,
        timer: sip.SipSessionExpires | None,
        *,
        local_role: str,
    ) -> None:
        """Own one RFC 4028 refresh or expiry timer on the dialog."""

        self._cancel_session_timer(dialog)
        dialog.session_timer.configure(
            timer,
            local_role=local_role,
            now=asyncio.get_running_loop().time(),
        )
        if timer is None:
            return

        async def _run() -> None:
            try:
                outcome = await SessionTimerDriver(
                    dialog.session_timer,
                    partial(self._send_dialog_request, call_id, dialog),
                    local_role,
                    asyncio.get_running_loop().time,
                ).run()
                if (
                    outcome != "disabled"
                    and self.active_dialogs.get(call_id) is dialog
                ):
                    await self._terminate_dialog(call_id, outcome)
            except asyncio.CancelledError:
                return
            finally:
                if dialog.session_timer_task is asyncio.current_task():
                    dialog.session_timer_task = None

        task = asyncio.create_task(
            _run(),
            name=f"voip-sip-session-timer-{call_id}",
        )
        dialog.session_timer_task = task
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_tasks.discard)

    @staticmethod
    def _cancel_invite_non2xx(completed: _PendingInvite) -> None:
        task = completed.final_task
        completed.final_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _arm_invite_non2xx(self, call_id: str, completed: _PendingInvite) -> None:
        """Run RFC 3261 Timer G/H for one non-2xx INVITE final response."""

        if completed.request.method != "INVITE" or int(completed.status) < 300:
            return
        self._cancel_invite_non2xx(completed)
        completed.final_retransmissions = 0

        async def _run() -> None:
            def _retransmit_final() -> bool:
                sent = self._send_response(
                    completed.request,
                    completed.addr,
                    completed.status,
                    completed.reason,
                    to_tag=completed.to_tag,
                    decline_reason=completed.decline_reason,
                )
                if sent:
                    completed.final_retransmissions += 1
                return sent

            try:
                await async_run_server_transaction(
                    send=_retransmit_final,
                    active=lambda: self.completed_invites.get(call_id) is completed,
                    transport=completed.transport,
                    timeout=_INVITE_NON2XX_TIMEOUT,
                    t1=_SIP_T1,
                    t2=_SIP_T2,
                )
            except asyncio.CancelledError:
                return
            finally:
                if completed.final_task is asyncio.current_task():
                    completed.final_task = None
            if self.completed_invites.get(call_id) is completed:
                self.completed_invites.pop(call_id, None)

        completed.final_task = asyncio.create_task(
            _run(),
            name=f"voip-sip-invite-final-{call_id}",
        )

    def _arm_invite_2xx(
        self,
        dialog: _ActiveDialog,
        request: sip.SipMessage,
        addr: tuple[str, int],
        status: int,
        reason: str,
        answer_sdp: str,
    ) -> None:
        """Retransmit an INVITE 2xx over UDP until its matching ACK arrives."""

        if request.method != "INVITE" or not 200 <= int(status) < 300:
            return
        call_id = request.header("Call-ID")
        body = answer_sdp.encode("utf-8") if answer_sdp else b""

        async def ack_timeout() -> None:
            dialog.invite_2xx.cancel()
            delayed = dialog.delayed_offer
            dialog.delayed_offer = None
            if delayed is not None:
                await self._rollback_delayed_offer(delayed)
            await self._terminate_dialog(call_id, "ack_timeout")

        dialog.invite_2xx.start(
            request,
            transport=dialog.transport,
            send=lambda: self._send_response(
                request,
                addr,
                status,
                reason,
                body=body,
                to_tag=dialog.to_tag,
            ),
            on_timeout=ack_timeout,
            still_owned=lambda: self.active_dialogs.get(call_id) is dialog,
            timeout=_INVITE_2XX_TIMEOUT,
            t1=_SIP_T1,
            t2=_SIP_T2,
            task_name=f"voip-sip-invite-2xx-{call_id}",
        )

    async def _handle_datagram_guarded(self, data: bytes, addr) -> None:
        try:
            await self._handle_datagram(data, addr)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("SIP UDP handler failed for %s:%s", addr[0], addr[1])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("SIP UDP socket error: %s", exc)

    def _send(self, data: bytes, addr) -> bool:
        try:
            if self.send_override is not None:
                return self.send_override(data, addr) is not False
            if self.transport is not None:
                send_sip_datagram(self.transport, data, addr)
                return True
        except (ConnectionError, OSError, RuntimeError) as err:
            _LOGGER.debug("SIP send failed for %s:%s: %s", addr[0], addr[1], err)
        return False

    def _send_response(
        self,
        request: sip.SipMessage,
        addr,
        status: int,
        reason: str,
        *,
        body: bytes = b"",
        to_tag: str = "",
        decline_reason: str = "",
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> bool:
        clean_reason = _identity_header(decline_reason)
        raw = sip.build_uas_response(
            request,
            status,
            reason,
            contact_uri=self._contact_for_request(request, transport=self.signaling_transport),
            to_tag=to_tag,
            top_via=_response_via_header(request, addr),
            body=body,
            extra_headers=extra_headers,
            decline_reason=clean_reason,
        )
        if not self._send(raw, addr):
            _LOGGER.warning("SIP TX %s %s dropped for %s:%s", status, reason, addr[0], addr[1])
            return False
        _LOGGER.info("SIP TX %s %s to %s:%s", status, reason, addr[0], addr[1])
        sip.mark_sip_event(self, "SIP_RESPONSE", int(status), reason)
        return True

    async def _handle_datagram(self, data: bytes, addr) -> None:
        try:
            request = sip.parse_message(data)
        except Exception as err:
            _LOGGER.info("SIP RX malformed from %s:%s: %s", addr[0], addr[1], err)
            return
        dialog = self.active_dialogs.get(request.header("Call-ID"))
        media_request = (request.method == "INVITE" or request.method in {"UPDATE", "ACK"} and request.body)
        if (dialog is None or not media_request
                or not uas_request_matches_dialog(request, dialog.request, local_tag=dialog.to_tag)):
            return await self._handle_parsed_message(request, addr, message_size=len(data))
        if dialog.media_lock.locked():
            cached = self._find_dialog_response(dialog, request, addr)
            if cached is not None or (dialog.last_request is not None and
                    _same_request_transaction(request, dialog.last_request, addr, dialog.addr)):
                return await self._handle_parsed_message(request, addr, message_size=len(data))
            if request.method != "ACK":
                self._send_response(request, addr, 500, "Server Internal Error", to_tag=dialog.to_tag,
                                    extra_headers=(("Retry-After", str(secrets.randbelow(11))),))
            return
        async with dialog.media_lock:
            return await self._handle_parsed_message(request, addr, message_size=len(data))

    async def _handle_parsed_message(self, request: sip.SipMessage, addr, *, message_size: int) -> None:
        if not request.is_request:
            self._receive_response(request, addr)
            return

        _LOGGER.info("SIP RX %s %s from %s:%s", request.method, request.uri, addr[0], addr[1])
        sip.mark_sip_event(self, request.method or "SIP_REQUEST")
        request_cseq = request_sequence(request)
        if isinstance(request_cseq, SipRequestResult):
            self._send_response(request, addr, request_cseq.status, request_cseq.reason, extra_headers=request_cseq.headers)
            return
        if self.request_router is not None and await self.request_router.route_owned_request(
                request, addr, self.signaling_transport, lambda raw: self._send(raw, addr)):
            return
        if self._handle_refer_subscription_request(request, addr, request_cseq):
            return
        if error := dialog_extension_error(request):
            self._send_response(request, addr, error.status, error.reason, extra_headers=error.headers)
            return

        match request.method:
            case "OPTIONS":
                self._handle_options(request, addr)
            case "REGISTER":
                await self._handle_register(request, addr)
            case "PRACK":
                await self._handle_prack(request, addr, request_cseq)
            case "INFO":
                await self._handle_info(request, addr, request_cseq)
            case "CANCEL":
                await self._handle_cancel(request, addr)
            case "BYE":
                await self._handle_bye(request, addr)
            case "ACK":
                await self._handle_ack(request, addr)
            case _ if is_application_request(request):
                await self._handle_application_request(request, addr, request_cseq, message_size)
            case _:
                call_id = request.header("Call-ID")
                dialog = self.active_dialogs.get(call_id)
                if dialog is None and request.method == "NOTIFY":
                    dialog = self._event_dialogs.get(call_id)
                if dialog is not None:
                    await self._handle_dialog_request(request, addr, request_cseq, call_id, dialog)
                elif request.method == "UPDATE":
                    self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
                else:
                    await self._handle_initial_invite(request, addr, call_id)

    def _receive_response(self, request, addr) -> None:
        sip.mark_sip_event(self, "SIP_RESPONSE", int(request.status_code or 0), request.reason)
        try:
            via_values = request.header_values("Via")
            branch = sip.parse_via(via_values[0] if via_values else "").branch
        except (TypeError, ValueError, sip.SipError):
            branch = ""
        mailbox = self._client_transaction_responses.get(branch)
        if mailbox is not None and mailbox.matches(request):
            mailbox.deliver((request, addr))
            return
        _LOGGER.info("SIP RX response ignored from %s:%s", addr[0], addr[1])

    def _handle_refer_subscription_request(self, request, addr, request_cseq) -> bool:
        """Let an existing REFER subscription consume its SUBSCRIBE or NOTIFY."""
        if request.method == "SUBSCRIBE":
            identity = request.header("Call-ID")
            dialog = self.active_dialogs.get(identity) or self._event_dialogs.get(identity)
            notifier = dialog.refer_notifier if dialog is not None else None
            if notifier is not None:
                key = (identity, request_cseq.number, "SUBSCRIBE")
                cached = self.completed_application_requests.get(key)
                if cached is not None and _same_request_transaction(request, cached[0].request, addr, cached[0].addr):
                    result = cached[1]
                else:
                    notifier.dialog.remote_cseq = max(notifier.dialog.remote_cseq, dialog.cseq - 1)
                    status, reason, headers = notifier.refresh(request)
                    result = SipRequestResult(status, reason, headers=headers, to_tag=dialog.to_tag)
                    self._remember_completed(self.completed_application_requests, key,
                        (_CompletedRequest(request, addr, status, reason), result))
                    if status == 200:
                        dialog.cseq = notifier.dialog.remote_cseq + 1
                        dialog.remote_target_uri = notifier.dialog.remote_target_uri
                self._send_response(request, addr, result.status, result.reason,
                                    to_tag=result.to_tag, extra_headers=result.headers)
                return True
        if request.method == "NOTIFY" and request.header("Call-ID") in self._outside_refer:
            subscription = self._outside_refer[request.header("Call-ID")]
            key = (request.header("Call-ID"), request_cseq.number, "NOTIFY")
            cached = self.completed_application_requests.get(key)
            if cached is not None and _same_request_transaction(request, cached[0].request, addr, cached[0].addr):
                result = cached[1]
            else:
                status, reason = subscription.receive_dialog_notify(request)
                result = SipRequestResult(status, reason, to_tag=subscription.dialog.key.local_tag)
                self._remember_completed(self.completed_application_requests, key,
                    (_CompletedRequest(request, addr, status, reason), result))
            self._send_response(request, addr, result.status, result.reason, to_tag=result.to_tag)
            return True
        return False

    def _handle_options(self, request, addr) -> None:
        # RFC 3261 11.2 defaults an absent Accept to application/sdp.
        # Capability descriptions use zero ports and never reserve media.
        body = sdp.build_offer_directional(self.local_ip, self.local_ip, 0,
            self.supported_send_formats, self.supported_recv_formats,
            include_common_codecs=True, allow_directional_payloads=True,
            independent_audio_ptime=True) if accepts_capability_sdp(request) else ""
        body = sdp.rewrite_sdp_origin(body, secrets.randbits(63) or 1, 0).encode() if body else b""
        self._send_response(request, addr, 200, "OK", body=body,
            extra_headers=(("Content-Type", "application/sdp"),) if body else ())

    async def _handle_register(self, request, addr) -> None:
        if self.on_register is not None:
            result = await self.on_register(request, addr, self.signaling_transport)
            raw = sip.build_uas_response(
                request,
                int(result.status),
                str(result.reason),
                top_via=_response_via_header(request, addr),
                extra_headers=tuple(getattr(result, "headers", ()) or ()),
            )
            if self._send(raw, addr):
                _LOGGER.info("SIP TX %s %s to %s:%s", result.status, result.reason, addr[0], addr[1])
                sip.mark_sip_event(self, "SIP_RESPONSE", int(result.status), str(result.reason))
            else:
                _LOGGER.warning("SIP REGISTER response dropped for %s:%s", addr[0], addr[1])
            return
        self._send_response(request, addr, 405, "Method Not Allowed")

    async def _handle_application_request(self, request, addr, request_cseq, message_size) -> None:
        if (
            request.method == "MESSAGE"
            and self.signaling_transport == "UDP"
            and message_size > 1300
        ):
            self._send_response(request, addr, 513, "Message Too Large")
            return
        if self.on_request is None:
            self._send_response(request, addr, 405, "Method Not Allowed")
            return
        request_key = (
            request.header("Call-ID"),
            request_cseq.number,
            request.method,
        )
        completed_application = self.completed_application_requests.get(
            request_key
        )
        if completed_application is not None:
            completed_request, completed_result = completed_application
            if _same_request_transaction(
                request,
                completed_request.request,
                addr,
                completed_request.addr,
            ):
                self._send_response(
                    request,
                    addr,
                    completed_result.status,
                    completed_result.reason,
                    to_tag=completed_result.to_tag,
                    extra_headers=completed_result.headers,
                )
            else:
                self._send_response(
                    request, addr, 481, "Call/Transaction Does Not Exist"
                )
            return
        result = await self.on_request(
            request,
            addr,
            self.signaling_transport,
            self._send_application_follow_up,
            **({"flow_trunk_id": self.flow_trunk_id} if self.flow_trunk_id and request.method == "REFER" else {}),
        )
        self._remember_completed(
            self.completed_application_requests,
            request_key,
            (_CompletedRequest(request, addr, result.status, result.reason), result),
        )
        self._send_response(
            request,
            addr,
            result.status,
            result.reason,
            to_tag=result.to_tag,
            extra_headers=result.headers,
        )
        if result.follow_up_scheduler is not None and 200 <= result.status < 300:
            result.follow_up_scheduler()
        elif result.follow_up is not None and 200 <= result.status < 300:
            task = asyncio.create_task(
                self._send_application_follow_up(request, addr, result),
                name=f"voip-sip-{result.follow_up.method.lower()}-{request.header('Call-ID')}",
            )
            self._maintenance_tasks.add(task)
            task.add_done_callback(self._maintenance_tasks.discard)

    def _replay_completed_request(self, request, addr, completed: _CompletedRequest) -> None:
        if _same_request_transaction(request, completed.request, addr, completed.addr):
            self._send_response(request, addr, completed.status, completed.reason)
        else:
            self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")

    async def _handle_prack(self, request, addr, request_cseq) -> None:
        call_id = request.header("Call-ID")
        prack_key = (call_id, request_cseq.number)
        completed = self.completed_pracks.get(prack_key)
        if completed is not None:
            self._replay_completed_request(request, addr, completed)
            return
        reliable = self._reliable_provisionals.get(call_id)
        try:
            rack = sip.parse_rack(request.header("RAck"))
            invite_cseq = sip.parse_cseq(reliable.request.header("CSeq"))
            matches = bool(
                reliable is not None
                and reliable.addr[0] == addr[0]
                and sip.extract_tag(request.header("From"))
                == sip.extract_tag(reliable.request.header("From"))
                and sip.extract_tag(request.header("To")) == reliable.to_tag
                and rack
                == sip.SipRAck(reliable.rseq, invite_cseq.number, "INVITE")
            )
        except (AttributeError, TypeError, ValueError, sip.SipError):
            matches = False
        if not matches or reliable is None:
            self._send_response(
                request,
                addr,
                481,
                "Call/Transaction Does Not Exist",
            )
            return
        prack_result: SipInviteResult | None = None
        updated = None
        if request.body:
            if (
                request.header("Content-Type").split(";", 1)[0].strip().lower()
                != "application/sdp"
            ):
                self._send_response(
                    request,
                    addr,
                    415,
                    "Unsupported Media Type",
                    extra_headers=(("Accept", "application/sdp"),),
                )
                return
            previous = self._parse_invite(reliable.request, reliable.addr)
            updated = self._parse_invite(request, addr)
            if (
                previous is not None
                and updated is not None
                and self.on_media_update is not None
            ):
                try:
                    prack_result = await self.on_media_update(
                        previous,
                        updated,
                        "PRACK",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception(
                        "SIP PRACK media update failed call_id=%s",
                        call_id,
                    )
            if prack_result is None or not 200 <= prack_result.status < 300:
                self._reliable_provisionals.pop(call_id, None)
                self._cancel_reliable_provisional(reliable)
                self._send_response(request, addr, 488, "Not Acceptable Here")
                self._send_final_response_now(
                    call_id,
                    488,
                    "Not Acceptable Here",
                    decline_reason="media_incompatible",
                )
                return
        self._reliable_provisionals.pop(call_id, None)
        self._cancel_reliable_provisional(reliable)
        self._remember_completed(
            self.completed_pracks,
            prack_key,
            _CompletedRequest(request, addr, 200, "OK"),
        )
        sent = self._send_response(
            request,
            addr,
            200,
            "OK",
            body=(
                prack_result.answer_sdp.encode()
                if prack_result is not None and prack_result.answer_sdp
                else b""
            ),
        )
        if not sent:
            if prack_result is not None and prack_result.rollback is not None:
                await prack_result.rollback()
            return
        if prack_result is not None and not await apply_remote_offer_media(
            prack_result.commit, prack_result.rollback, prack_result.finalize
        ):
            _LOGGER.error("SIP PRACK commit failed call_id=%s", call_id)
            self._send_final_response_now(
                call_id, 500, "Server Internal Error",
                decline_reason="media_update_failed",
            )
            return
        if prack_result is not None and updated is not None:
            notify_media_commit(prack_result.on_committed, updated)
        if reliable.deferred_progress is not None:
            progress = reliable.deferred_progress
            self.send_final_response(call_id, progress.status, progress.reason,
                answer_sdp=progress.answer_sdp, decline_reason=progress.decline_reason)
        if reliable.deferred_final is not None:
            final = reliable.deferred_final
            self.send_final_response(
                call_id,
                final.status,
                final.reason,
                answer_sdp=final.answer_sdp,
                decline_reason=final.decline_reason,
                connected_identity_name=final.connected_identity_name,
                connected_identity_user=final.connected_identity_user,
            )

    async def _handle_info(self, request, addr, request_cseq) -> None:
        call_id = request.header("Call-ID")
        info_key = (call_id, request_cseq.number)
        completed_info = self.completed_infos.get(info_key)
        if completed_info is not None:
            self._replay_completed_request(request, addr, completed_info)
            return
        dialog = self.active_dialogs.get(call_id)
        if dialog is None or not _same_dialog_request(request, dialog, addr):
            self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
            return
        if self.on_info is not None:
            await self.on_info(request, addr, self.signaling_transport)
        self._remember_completed(
            self.completed_infos,
            info_key,
            _CompletedRequest(request, addr, 200, "OK"),
        )
        dialog.cseq = max(dialog.cseq, request_cseq.number + 1)
        self._send_response(request, addr, 200, "OK")

    async def _handle_cancel(self, request, addr) -> None:
        call_id = request.header("Call-ID")
        pending = self.pending_invites.get(call_id)
        completed = self.completed_invites.get(call_id)
        invite_transaction = pending or (completed if completed is not None and completed.cancelled else None)
        # Match the transaction and originating host, but do not pin the
        # source port: NATs and SIP proxies may legitimately rewrite it.
        if (invite_transaction is None or invite_transaction.addr[0] != addr[0]
                or not matches_cancel(request, invite_transaction.request)):
            self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
            return
        if pending is None:
            self._send_response(request, addr, 200, "OK")
            return
        pending.cancelled = True
        self._cancel_pending_expiry(pending)
        pending.status = 487
        pending.reason = "Request Terminated"
        pending.decline_reason = "cancelled"
        self.pending_invites.pop(call_id, None)
        self._remember_completed(self.completed_invites, call_id, pending)
        self._send_response(request, addr, 200, "OK")
        final_sent = self._send_response(
            pending.request,
            pending.addr,
            487,
            "Request Terminated",
            to_tag=pending.to_tag,
            decline_reason="cancelled",
        )
        if final_sent:
            self._arm_invite_non2xx(call_id, pending)
        if self.on_terminated is not None:
            await self.on_terminated(call_id, "cancelled")

    async def _handle_bye(self, request, addr) -> None:
        call_id = request.header("Call-ID")
        dialog = self.active_dialogs.get(call_id)
        if dialog is None:
            completed = self.completed_byes.get(call_id)
            if completed is None:
                self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
            else:
                self._replay_completed_request(request, addr, completed)
            return
        same_dialog = _same_dialog_request(request, dialog, addr)
        if not same_dialog:
            self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
            return
        delayed = self._retire_dialog(call_id)
        if delayed is not None:
            await self._rollback_delayed_offer(delayed)
        self._remember_completed(
            self.completed_byes,
            call_id,
            _CompletedRequest(request, addr, 200, "OK"),
        )
        self._send_response(request, addr, 200, "OK")
        if self.on_terminated is not None:
            await self.on_terminated(call_id, "remote_hangup")

    async def _handle_ack(self, request, addr) -> None:
        call_id = request.header("Call-ID")
        completed = self.completed_invites.get(call_id)
        if (
            completed is not None
            and completed.status >= 300
            and matches_invite_error_ack(request, completed.request)
        ):
            self._cancel_invite_non2xx(completed)
            self.completed_invites.pop(call_id, None)
            return
        dialog = self.active_dialogs.get(call_id)
        if dialog is not None and dialog.invite_2xx.acknowledge(
            request,
            lambda ack: _same_dialog_ack(ack, dialog, addr),
        ):
            if not await self._accept_delayed_offer_ack(
                call_id, dialog, request, addr
            ):
                return
            self._arm_connected_identity(call_id, dialog)

    async def _handle_dialog_request(self, request, addr, request_cseq, call_id, dialog) -> None:
        cached_response = self._find_dialog_response(dialog, request, addr)
        if cached_response is not None:
            body = (
                cached_response.answer_sdp.encode("utf-8")
                if cached_response.answer_sdp
                else b""
            )
            self._send_response(
                request,
                addr,
                cached_response.status,
                cached_response.reason,
                body=body,
                to_tag=dialog.to_tag,
            )
            return
        if (request.method == "NOTIFY" and request_cseq.number < dialog.cseq
                and uas_request_matches_dialog(request, dialog.request, local_tag=dialog.to_tag)):
            self._send_response(request, addr, 500, "Server Internal Error",
                                to_tag=dialog.to_tag, extra_headers=(("Retry-After", "1"),))
            return
        if not _same_dialog_request(request, dialog, addr):
            self._send_response(request, addr, 481, "Call/Transaction Does Not Exist", to_tag=dialog.to_tag)
            return
        if request.method == "NOTIFY":
            subscription = dialog.refer_subscription
            if subscription is not None and subscription.dialog is not None:
                if subscription.dialog.key.call_id == call_id:
                    subscription.dialog.remote_cseq = max(subscription.dialog.remote_cseq, dialog.cseq - 1)
                status, reason = subscription.receive_dialog_notify(request)
            else:
                status, reason = 481, "Subscription Does Not Exist"
            self._send_response(request, addr, status, reason, to_tag=dialog.to_tag)
            self._remember_dialog_response(dialog, request, addr, status, reason)
            if status == 200:
                dialog.cseq = sip.parse_cseq(request.header("CSeq")).number + 1
            return
        if request.method == "REFER":
            await self._handle_dialog_refer(
                call_id, dialog, request, addr
            )
            return
        if request.method not in {"INVITE", "UPDATE"}:
            self._send_response(request, addr, 405, "Method Not Allowed", to_tag=dialog.to_tag)
            return
        await self._handle_dialog_offer(request, addr, request_cseq, call_id, dialog)

    async def _handle_dialog_offer(self, request, addr, request_cseq, call_id, dialog) -> None:
        if dialog.update_in_progress:
            # This UAS already has an inbound offer pending.  RFC 3261
            # section 14.2 uses 500 + Retry-After for this case; 491 is for
            # a UAS that currently owns a locally generated offer.
            self._send_response(
                request,
                addr,
                500,
                "Server Internal Error",
                to_tag=dialog.to_tag,
                extra_headers=(("Retry-After", "1"),),
            )
            return
        if dialog.local_offer_in_progress:
            self._send_response(
                request,
                addr,
                491,
                "Request Pending",
                to_tag=dialog.to_tag,
            )
            return

        try:
            refreshed_remote_target = sip.contact_target_uri(request)
        except sip.SipError:
            self._send_response(
                request,
                addr,
                400,
                "Bad Request",
                to_tag=dialog.to_tag,
            )
            return

        if not request.body:
            if request.method == "INVITE":
                self._offer_in_dialog(request, addr, request_cseq, dialog, refreshed_remote_target)
            else:
                self._refresh_dialog(request, addr, request_cseq, call_id, dialog, refreshed_remote_target)
            return

        if request.header("Content-Type").split(";", 1)[0].strip().lower() != "application/sdp":
            self._send_response(
                request,
                addr,
                415,
                "Unsupported Media Type",
                to_tag=dialog.to_tag,
            )
            return
        previous_invite = dialog.invite or self._parse_invite(
            dialog.request, dialog.addr
        )
        updated_invite = self._parse_invite(
            request,
            addr,
            peer_profile=(
                previous_invite.peer_profile
                if previous_invite is not None
                else ""
            ),
        )
        media_unchanged = bool(
            previous_invite is not None
            and updated_invite is not None
            and _same_audio_media(previous_invite, updated_invite)
            and _same_video_media(previous_invite, updated_invite)
        )
        if previous_invite is None or updated_invite is None:
            result = SipInviteResult(488, "Not Acceptable Here")
        elif self.on_media_update is not None:
            dialog.update_in_progress = True
            dialog.last_request = request
            dialog.last_status = 100
            dialog.last_reason = "Trying"
            dialog.last_response_sdp = ""
            if request.method == "INVITE":
                self._send_response(
                    request,
                    addr,
                    100,
                    "Trying",
                    to_tag=dialog.to_tag,
                )
            try:
                result = await self.on_media_update(
                    previous_invite,
                    updated_invite,
                    request.method,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "SIP in-dialog media update failed call_id=%s method=%s",
                    call_id,
                    request.method,
                )
                result = SipInviteResult(500, "Server Internal Error")
            finally:
                dialog.update_in_progress = False
            if self.active_dialogs.get(call_id) is not dialog:
                if result.rollback is not None:
                    await result.rollback()
                terminated = _PendingInvite(
                    request,
                    addr,
                    dialog.to_tag,
                    dialog.transport,
                    status=487,
                    reason="Request Terminated",
                    decline_reason="remote_hangup",
                )
                self._remember_completed(self.completed_invites, call_id, terminated)
                final_sent = self._send_response(
                    request,
                    addr,
                    487,
                    "Request Terminated",
                    to_tag=dialog.to_tag,
                    decline_reason="remote_hangup",
                )
                if final_sent:
                    self._arm_invite_non2xx(call_id, terminated)
                return
        elif media_unchanged:
            result = SipInviteResult(200, "OK", answer_sdp=dialog.answer_sdp)
        else:
            result = SipInviteResult(488, "Not Acceptable Here")
        await self._answer_dialog_offer(
            request, addr, request_cseq, call_id, dialog, refreshed_remote_target, updated_invite, result
        )

    def _offer_in_dialog(self, request, addr, request_cseq, dialog, refreshed_remote_target) -> None:
        previous_invite = dialog.invite or self._parse_invite(
            dialog.request, dialog.addr
        )
        if previous_invite is None or not dialog.answer_sdp:
            self._send_response(
                request,
                addr,
                488,
                "Not Acceptable Here",
                to_tag=dialog.to_tag,
            )
            return
        self._send_response(
            request,
            addr,
            100,
            "Trying",
            to_tag=dialog.to_tag,
        )
        next_sdp_version = dialog.local_sdp_session_version + 1
        offer_sdp = sdp.rewrite_sdp_origin(
            dialog.answer_sdp,
            dialog.local_sdp_session_id,
            next_sdp_version,
        )
        sent = self._send_response(
            request,
            addr,
            200,
            "OK",
            body=offer_sdp.encode(),
            to_tag=dialog.to_tag,
        )
        if not sent:
            return
        dialog.update_in_progress = True
        dialog.delayed_offer = _PendingDelayedOffer(
            request=request,
            addr=addr,
            offer_sdp=offer_sdp,
            previous_invite=previous_invite,
            initial_invite=None,
            remote_target_uri=refreshed_remote_target,
        )
        dialog.last_request = request
        dialog.last_status = 200
        dialog.last_reason = "OK"
        dialog.last_response_sdp = offer_sdp
        dialog.cseq = request_cseq.number + 1
        dialog.local_sdp_session_version = next_sdp_version
        self._remember_dialog_response(
            dialog,
            request,
            addr,
            200,
            "OK",
            offer_sdp,
        )
        self._arm_invite_2xx(
            dialog,
            request,
            addr,
            200,
            "OK",
            offer_sdp,
        )

    def _refresh_dialog(self, request, addr, request_cseq, call_id, dialog, refreshed_remote_target) -> None:
        # An offerless UPDATE is a session refresh and does not start
        # a new offer/answer exchange.
        sent = self._send_response(
            request,
            addr,
            200,
            "OK",
            to_tag=dialog.to_tag,
        )
        if sent:
            dialog.last_request = request
            dialog.last_status = 200
            dialog.last_reason = "OK"
            dialog.last_response_sdp = ""
            dialog.cseq = request_cseq.number + 1
            dialog.addr = addr
            if refreshed_remote_target:
                dialog.remote_target_uri = refreshed_remote_target
            self._accept_remote_connected_identity(dialog, request)
            dialog.renegotiations += 1
            self._arm_session_timer(
                call_id,
                dialog,
                sip.negotiate_uas_session_timer(request),
                local_role="uas",
            )
            self._remember_dialog_response(
                dialog,
                request,
                addr,
                200,
                "OK",
            )

    async def _answer_dialog_offer(self, request, addr, request_cseq, call_id, dialog, refreshed_remote_target, updated_invite, result) -> None:
        status = int(result.status)
        reason = str(result.reason)
        answer_sdp = str(result.answer_sdp or "")
        next_sdp_version = int(dialog.local_sdp_session_version)
        if answer_sdp:
            answer_sdp = sdp.rewrite_sdp_origin(
                answer_sdp,
                dialog.local_sdp_session_id,
                next_sdp_version,
            )
            if sdp.sdp_description_changed(
                dialog.answer_sdp, answer_sdp
            ):
                next_sdp_version += 1
                answer_sdp = sdp.rewrite_sdp_origin(
                    answer_sdp,
                    dialog.local_sdp_session_id,
                    next_sdp_version,
                )
        if 200 <= status < 300 and not answer_sdp:
            status = 500
            reason = "Server Internal Error"
        body = answer_sdp.encode("utf-8") if 200 <= status < 300 else b""
        sent = self._send_response(
            request,
            addr,
            status,
            reason,
            body=body,
            to_tag=dialog.to_tag,
            decline_reason=result.decline_reason,
        )
        if not sent and result.rollback is not None:
            await result.rollback()
        if sent:
            dialog.last_request = request
            dialog.last_status = status
            dialog.last_reason = reason
            dialog.last_response_sdp = answer_sdp if 200 <= status < 300 else ""
            dialog.cseq = request_cseq.number + 1
            self._remember_dialog_response(
                dialog,
                request,
                addr,
                status,
                reason,
                answer_sdp if 200 <= status < 300 else "",
            )
            if 200 <= status < 300 and updated_invite is not None:
                # Arm ACK handling before an async media commit can yield.
                # A fast UAC is allowed to ACK immediately after the 2xx.
                self._arm_invite_2xx(
                    dialog,
                    request,
                    addr,
                    status,
                    reason,
                    answer_sdp,
                )
                if not await apply_remote_offer_media(
                    result.commit, result.rollback, result.finalize
                ):
                    _LOGGER.error(
                        "SIP in-dialog media commit failed call_id=%s method=%s",
                        call_id,
                        request.method,
                    )
                    await self._terminate_dialog(call_id, "media_update_failed")
                    return
                dialog.addr = addr
                if refreshed_remote_target:
                    dialog.remote_target_uri = refreshed_remote_target
                self._accept_remote_connected_identity(
                    dialog,
                    request,
                )
                dialog.answer_sdp = answer_sdp
                dialog.invite = updated_invite
                dialog.local_sdp_session_version = next_sdp_version
                dialog.renegotiations += 1
                notify_media_commit(result.on_committed, updated_invite)
                self._arm_session_timer(
                    call_id,
                    dialog,
                    sip.negotiate_uas_session_timer(request),
                    local_role="uas",
                )
                _LOGGER.info(
                    "SIP in-dialog %s accepted call_id=%s remote_rtp=%s:%s audio_direction=%s",
                    request.method,
                    call_id,
                    updated_invite.remote_rtp_host,
                    updated_invite.remote_rtp_port,
                    updated_invite.remote_audio_direction,
                )

    async def _handle_initial_invite(self, request, addr, call_id) -> None:
        try:
            sip.contact_target_uri(request)
        except sip.SipError:
            self._send_response(request, addr, 400, "Bad Request")
            return
        try:
            sip.record_route_set(request)
        except sip.SipError:
            self._send_response(request, addr, 400, "Bad Request")
            return
        if (
            request.body
            and request.header("Content-Type").split(";", 1)[0].strip().lower()
            != "application/sdp"
        ):
            self._send_response(
                request,
                addr,
                415,
                "Unsupported Media Type",
                extra_headers=(("Accept", "application/sdp"),),
            )
            return
        if self._replay_initial_invite(request, addr, call_id):
            return

        initial_invite = self._parse_initial_invite(request, addr, authorize=True)
        if isinstance(initial_invite, SipRequestResult):
            self._send_response(request, addr, initial_invite.status, initial_invite.reason,
                                to_tag=initial_invite.to_tag, extra_headers=initial_invite.headers)
            return
        if initial_invite is None:
            self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=sip.make_tag())
            return
        invite = self._parse_invite(request, addr, initial=initial_invite) if request.body else None
        if (request.body and invite is None) or (not request.body and self.on_offerless_invite is None):
            self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=sip.make_tag())
            return
        signaling_invite = invite or initial_invite
        if len(self.pending_invites) >= self.max_pending_invites:
            self._send_response(
                request,
                addr,
                503,
                "Service Unavailable",
                extra_headers=(("Retry-After", "1"),),
            )
            return
        to_tag = sip.make_tag()
        pending = _PendingInvite(request, addr, to_tag, self.signaling_transport, codec_policy=signaling_invite.codec_policy, identity=signaling_invite)
        self.pending_invites[call_id] = pending
        self._send_response(request, addr, 100, "Trying")
        delayed_plan: UasDelayedOfferPlan | None = None
        try:
            if invite is not None:
                result = await self.on_invite(invite)
            else:
                assert initial_invite is not None
                assert self.on_offerless_invite is not None
                delayed_plan = await self.on_offerless_invite(initial_invite)
                if delayed_plan is None:
                    result = SipInviteResult(488, "Not Acceptable Here")
                else:
                    offer_sdp = str(delayed_plan.offer_sdp or "").strip()
                    if not offer_sdp:
                        raise ValueError("delayed offer plan must provide SDP")
                    sdp.parse_sdp(offer_sdp.encode())
                    pending.delayed_offer_plan = delayed_plan
                    result = SipInviteResult(200, "OK", answer_sdp=offer_sdp)
        except BaseException:
            if self.pending_invites.get(call_id) is pending:
                self.pending_invites.pop(call_id, None)
            if delayed_plan is not None and delayed_plan.rollback is not None:
                await delayed_plan.rollback()
            raise
        if self.pending_invites.get(call_id) is not pending:
            # A CANCEL/final decision won while routing awaited. Clean up any
            # resources the completed policy path may just have published.
            if result.rollback is not None:
                await result.rollback()
            if pending.cancelled and self.on_terminated is not None:
                await self.on_terminated(call_id, "cancelled")
            if delayed_plan is not None and delayed_plan.rollback is not None:
                await delayed_plan.rollback()
            return
        await self._finish_initial_invite(request, addr, call_id, pending, result)

    def _replay_initial_invite(self, request, addr, call_id) -> bool:
        """Answer a pending or completed server transaction before admitting a new INVITE."""
        existing_pending = self.pending_invites.get(call_id)
        if existing_pending is not None:
            if not _same_request_transaction(request, existing_pending.request, addr, existing_pending.addr):
                self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=existing_pending.to_tag)
                return True
            reliable = self._reliable_provisionals.get(call_id)
            body = existing_pending.answer_sdp.encode("utf-8") if existing_pending.answer_sdp else b""
            sent = (
                self._transmit_reliable_provisional(reliable)
                if reliable is not None and existing_pending.status < 200
                else self._send_response(
                    request,
                    addr,
                    existing_pending.status,
                    existing_pending.reason,
                    body=body,
                    to_tag=existing_pending.to_tag,
                    decline_reason=existing_pending.decline_reason,
                )
            )
            if sent and existing_pending.status >= 200:
                self.pending_invites.pop(call_id, None)
                if existing_pending.status < 300:
                    dialog = self._activate_dialog(
                        call_id,
                        existing_pending,
                        request,
                        addr,
                    )
                    self._remember_dialog_response(
                        dialog,
                        request,
                        addr,
                        existing_pending.status,
                        existing_pending.reason,
                        existing_pending.answer_sdp,
                    )
                    self._arm_invite_2xx(
                        dialog,
                        request,
                        addr,
                        existing_pending.status,
                        existing_pending.reason,
                        existing_pending.answer_sdp,
                    )
                else:
                    self._remember_completed(self.completed_invites, call_id, existing_pending)
                    self._arm_invite_non2xx(call_id, existing_pending)
            return True
        completed_invite = self.completed_invites.get(call_id)
        if completed_invite is not None:
            if not _same_request_transaction(request, completed_invite.request, addr, completed_invite.addr):
                self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=completed_invite.to_tag)
                return True
            body = completed_invite.answer_sdp.encode("utf-8") if completed_invite.answer_sdp else b""
            self._send_response(
                request,
                addr,
                completed_invite.status,
                completed_invite.reason,
                body=body,
                to_tag=completed_invite.to_tag,
                decline_reason=completed_invite.decline_reason,
            )
            return True
        return False

    async def _finish_initial_invite(self, request, addr, call_id, pending, result) -> None:
        media_cleanup_owned = False
        try:
            to_tag = result.to_tag or pending.to_tag
            answer_sdp = (
                sdp.rewrite_sdp_origin(
                    result.answer_sdp,
                    pending.local_sdp_session_id,
                    pending.local_sdp_session_version,
                )
                if result.answer_sdp
                else ""
            )
            if result.defer_final:
                pending.to_tag = to_tag
                self._send_pending_progress(call_id, pending, result.status, result.reason,
                    answer_sdp=answer_sdp, decline_reason=result.decline_reason)
                return

            pending.to_tag = to_tag
            pending.connected_identity_name = _identity_header(result.connected_identity_name)
            pending.connected_identity_user = _identity_header(result.connected_identity_user)
            pending.status = int(result.status)
            pending.reason = str(result.reason)
            pending.answer_sdp = answer_sdp
            pending.decline_reason = result.decline_reason
            body = answer_sdp.encode("utf-8") if answer_sdp else b""
            sent = self._send_response(
                request,
                addr,
                result.status,
                result.reason,
                body=body,
                to_tag=to_tag,
                decline_reason=result.decline_reason,
            )
            if sent:
                self.pending_invites.pop(call_id, None)
            if sent and 200 <= int(result.status) < 300:
                dialog = self._activate_dialog(call_id, pending, request, addr)
                self._remember_dialog_response(
                    dialog,
                    request,
                    addr,
                    int(result.status),
                    str(result.reason),
                    answer_sdp,
                )
                self._arm_invite_2xx(
                    dialog,
                    request,
                    addr,
                    int(result.status),
                    str(result.reason),
                    answer_sdp,
                )
                media_cleanup_owned = True
                if not await apply_remote_offer_media(result.commit, result.rollback, result.finalize):
                    await self._terminate_dialog(call_id, "media_update_failed")
                elif dialog.invite is not None:
                    notify_media_commit(result.on_committed, dialog.invite)
            elif sent and int(result.status) >= 300:
                if result.rollback is not None:
                    media_cleanup_owned = True
                    await result.rollback()
                self._remember_completed(self.completed_invites, call_id, pending)
                self._arm_invite_non2xx(call_id, pending)
                # The server transaction remains cached until ACK or Timer H, but
                # the logical call has already reached a final response. Retire
                # its runtime ownership now instead of keeping the phone busy for
                # the lifetime of the transaction cache.
                if self.on_terminated is not None:
                    await self.on_terminated(
                        call_id,
                        result.decline_reason or "declined",
                    )
            elif not sent:
                if result.rollback is not None:
                    media_cleanup_owned = True
                    await result.rollback()
                # Retain only the bounded server transaction. A retransmission
                # cannot accept media that has already been rolled back.
                if pending.status < 300:
                    pending.status = 500
                    pending.reason = "Server Internal Error"
                    pending.answer_sdp = ""
                self.pending_invites.pop(call_id, None)
                self._remember_completed(self.completed_invites, call_id, pending)
                self._arm_invite_non2xx(call_id, pending)
                if self.on_terminated is not None:
                    await self.on_terminated(call_id, "transport_unreachable")
        except BaseException:
            try:
                if not media_cleanup_owned and result.rollback is not None:
                    await result.rollback()
            finally:
                self.pending_invites.pop(call_id, None)
                if call_id in self.active_dialogs:
                    await self._terminate_dialog(call_id, "media_update_failed")
                elif self.on_terminated is not None:
                    await self.on_terminated(call_id, "media_update_failed")
            raise

    def _send_pending_progress(self, call_id, pending, status, reason, *, answer_sdp="", decline_reason="") -> bool:
        """Publish one progress response through the pending INVITE owner."""
        rewritten = (sdp.rewrite_sdp_origin(answer_sdp, pending.local_sdp_session_id,
            pending.local_sdp_session_version) if answer_sdp else "")
        if pending.status == int(status) and pending.reason == str(reason) and pending.answer_sdp == rewritten:
            if pending.expiry_task is None:
                self._arm_pending_expiry(call_id, pending)
            return True
        reliable = self._reliable_provisionals.get(call_id)
        if reliable is not None:
            reliable.deferred_progress = _DeferredInviteFinal(int(status), str(reason), str(answer_sdp),
                str(decline_reason), "", "")
            return True
        pending.status, pending.reason = int(status), str(reason)
        pending.answer_sdp = rewritten
        pending.decline_reason = decline_reason
        use_reliable = (101 <= int(status) < 200 and (
            sip.supports_option(pending.request, "100rel") or "100rel" in sip.option_tags(pending.request, "Require")))
        sent = (self._send_reliable_provisional(call_id, pending) if use_reliable else
            self._send_response(pending.request, pending.addr, int(status), str(reason),
                body=pending.answer_sdp.encode(), to_tag=pending.to_tag, decline_reason=decline_reason))
        self._arm_pending_expiry(call_id, pending)
        return sent

    def send_final_response(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
        connected_identity_name: str = "",
        connected_identity_user: str = "",
    ) -> bool:
        if int(status) < 200:
            pending = self.pending_invites.get(call_id)
            return pending is not None and self._send_pending_progress(call_id, pending, status, reason,
                answer_sdp=answer_sdp, decline_reason=decline_reason)
        reliable = self._reliable_provisionals.get(call_id)
        if (
            reliable is not None
            and (reliable.answer_sdp or (
                reliable.deferred_progress is not None
                and reliable.deferred_progress.answer_sdp
            ))
            and 200 <= int(status) < 300
        ):
            reliable.deferred_final = _DeferredInviteFinal(
                int(status),
                str(reason),
                str(answer_sdp),
                str(decline_reason),
                str(connected_identity_name),
                str(connected_identity_user),
            )
            return True
        return self._send_final_response_now(
            call_id,
            status,
            reason,
            answer_sdp=answer_sdp,
            decline_reason=decline_reason,
            connected_identity_name=connected_identity_name,
            connected_identity_user=connected_identity_user,
        )

    def _send_final_response_now(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
        connected_identity_name: str = "",
        connected_identity_user: str = "",
    ) -> bool:
        pending = self.pending_invites.get(call_id)
        if pending is None:
            return False
        self._cancel_pending_expiry(pending)
        if answer_sdp:
            answer_sdp = sdp.rewrite_sdp_origin(
                answer_sdp,
                pending.local_sdp_session_id,
                pending.local_sdp_session_version,
            )
        pending.status = int(status)
        pending.reason = str(reason)
        pending.answer_sdp = answer_sdp
        pending.decline_reason = decline_reason
        pending.connected_identity_name = _identity_header(connected_identity_name)
        pending.connected_identity_user = _identity_header(connected_identity_user)
        body = answer_sdp.encode("utf-8") if answer_sdp else b""
        if not self._send_response(
            pending.request,
            pending.addr,
            status,
            reason,
            body=body,
            to_tag=pending.to_tag,
            decline_reason=decline_reason,
        ):
            return False
        if int(status) < 200:
            return True
        self.pending_invites.pop(call_id, None)
        if int(status) < 300:
            dialog = self._activate_dialog(
                call_id,
                pending,
                pending.request,
                pending.addr,
            )
            self._remember_dialog_response(
                dialog,
                pending.request,
                pending.addr,
                int(status),
                str(reason),
                answer_sdp,
            )
            self._arm_invite_2xx(
                dialog,
                pending.request,
                pending.addr,
                int(status),
                str(reason),
                answer_sdp,
            )
        else:
            self._remember_completed(self.completed_invites, call_id, pending)
            self._arm_invite_non2xx(call_id, pending)
        return True

    def _dialog_for_bye(self, call_id: str) -> tuple[str, _ActiveDialog] | None:
        if not call_id and len(self.active_dialogs) == 1:
            call_id = next(iter(self.active_dialogs))
        dialog = self.active_dialogs.get(call_id) if call_id else None
        if dialog is None:
            return None
        return call_id, dialog

    async def async_send_bye(self, call_id: str = "", *, timeout: float = 1.5) -> bool:
        """Run one owned BYE transaction, then retire the dialog exactly once."""

        resolved = self._dialog_for_bye(call_id)
        if resolved is None:
            return False
        call_id, dialog = resolved
        current_task = asyncio.current_task()
        owner = dialog.termination_task
        if owner is not None and owner is not current_task:
            return await asyncio.shield(owner)
        if current_task is not None and owner is None:
            dialog.termination_task = current_task  # type: ignore[assignment]
        try:
            response = await self._send_dialog_request(
                call_id,
                dialog,
                "BYE",
                timeout=timeout,
            )
            if response is None:
                _LOGGER.info("SIP BYE completed without final response call_id=%s", call_id)
        finally:
            delayed = self._retire_dialog(call_id)
            if delayed is not None and not delayed.settled:
                await self._rollback_delayed_offer(delayed)
        return True

    async def request_video_keyframe(
        self, call_id: str, *, timeout: float = 3.0
    ) -> bool:
        """Request a full intra frame with RFC 5168 media control."""

        dialog = self.active_dialogs.get(call_id)
        if dialog is None:
            return False
        response = await self._send_dialog_request(
            call_id,
            dialog,
            "INFO",
            body=sip.RFC5168_PICTURE_FAST_UPDATE_BODY,
            content_type="application/media_control+xml",
            timeout=timeout,
        )
        return bool(
            response is not None
            and 200 <= int(response.status_code or 0) < 300
        )

    async def acquire_replacement_dialog(self, replaces: sip_transfer.SipReplaces):
        """Keep the matched media contract stable through replacement commit."""
        if self.invite_for_replaces(replaces) is None:
            return None
        dialog = self.active_dialogs[replaces.call_id]
        await asyncio.wait_for(dialog.media_lock.acquire(), timeout=8.0)
        if (self.active_dialogs.get(replaces.call_id) is not dialog
                or self.invite_for_replaces(replaces) is None):
            dialog.media_lock.release()
            return None
        if dialog.update_in_progress or dialog.local_offer_in_progress:
            dialog.media_lock.release()
            raise TimeoutError("dialog offer/answer is still pending")
        released = False
        def release():
            nonlocal released
            if not released:
                released = True
                dialog.media_lock.release()
        return dialog.invite, release

    def invite_for_replaces(self, replaces: sip_transfer.SipReplaces) -> SipInvite | None:
        """Match all three dialog identifiers in their UAS orientation."""
        dialog = self.active_dialogs.get(replaces.call_id)
        if (dialog is None or dialog.to_tag != replaces.to_tag
                or sip.extract_tag(dialog.request.header("From")) != replaces.from_tag):
            return None
        return dialog.invite

    def invite_for_call(self, call_id: str) -> SipInvite | None:
        dialog = self.active_dialogs.get(call_id)
        return dialog.invite if dialog is not None else None

    def dialog_key(self, call_id: str) -> DialogKey | None:
        dialog = self.active_dialogs.get(call_id)
        return DialogKey.for_uas_dialog(dialog.request, local_tag=dialog.to_tag) if dialog is not None else None

    def local_uri_for_call(self, call_id: str) -> str:
        dialog = self.active_dialogs.get(call_id)
        return _uri_text_from_header(dialog.request.header("To")) if dialog is not None else ""

    def remote_target_for_call(self, call_id: str) -> str:
        dialog = self.active_dialogs.get(call_id)
        return (dialog.remote_target_uri or dialog.remote_uri) if dialog is not None else ""

    def remote_uri_for_call(self, call_id: str) -> str:
        dialog = self.active_dialogs.get(call_id)
        return (
            dialog.remote_uri or _uri_text_from_header(dialog.request.header("From"))
            if dialog is not None else ""
        )

    async def async_refer(
        self, call_id: str, target: sip_transfer.SipReferTarget, *, timeout: float = 30.0,
    ) -> sip_transfer.SipTransferResult | None:
        """Request transfer through the existing owner of an inbound dialog."""
        dialog = self.active_dialogs.get(call_id)
        if dialog is None:
            return None
        if dialog.refer_subscription is not None:
            return sip_transfer.SipTransferResult(False, 0, "unavailable")
        self._event_credentials(call_id, dialog)
        subscription = sip_transfer.ReferSubscription()
        dialog.refer_subscription = subscription
        subscription.dialog = SubscriptionDialog.from_request(dialog.request, dialog.to_tag)
        subscription.dialog.remote_cseq = dialog.cseq - 1
        subscription.dialog.remote_target_uri = dialog.remote_target_uri or dialog.remote_uri
        subscription.dialog.route_set = dialog.route_set
        peer_uri = sip.parse_sip_uri(dialog.remote_target_uri or dialog.remote_uri)
        usage = None
        if any(key.casefold() == "gr" for key, _ in peer_uri.params):
            usage = SubscriptionDialog(key=DialogKey(sip.make_call_id("refer"), sip.make_tag(), ""),
                local_uri=_uri_text_from_header(dialog.request.header("To")),
                remote_uri=dialog.remote_uri, remote_target_uri=str(peer_uri), remote_cseq=0)
            subscription.dialog = usage
            self._outside_refer[usage.key.call_id] = subscription
        try:
            async with asyncio.timeout(timeout):
                return await self._await_refer_outcome(call_id, dialog, target, subscription, usage, timeout)
        except TimeoutError:
            return sip_transfer.SipTransferResult(False, 0, "timeout")
        finally:
            subscription.close()
            if usage is not None:
                self._outside_refer.pop(usage.key.call_id, None)
            if dialog.refer_subscription is subscription:
                dialog.refer_subscription = None
            self._release_event_dialog(call_id, dialog)

    async def _await_refer_outcome(self, call_id, dialog, target, subscription, usage, timeout):
        if usage is not None:
            subscription.bind(usage.local_cseq, first=True)
            follow_up = SipFollowUpRequest(method="REFER", cseq=usage.next_cseq(),
                headers=(("Refer-To", target.as_header()),
                    ("Target-Dialog", str(sip_transfer.SipTargetDialog(call_id,
                        sip.extract_tag(dialog.request.header("From")), dialog.to_tag))),
                    ("Require", "tdialog"),
                    *((("Referred-By", target.referred_by),) if target.referred_by else ()),
                    *target.referral_body_headers),
                body=target.referral_body, content_type=target.referral_content_type)
            response = await self._send_application_follow_up_unlocked(dialog.request, dialog.addr,
                SipRequestResult(to_tag=usage.key.local_tag, follow_up=follow_up, follow_up_dialog=usage,
                    follow_up_credentials=self._event_credentials(call_id, dialog),
                    follow_up_cseq_changed=lambda sequence: subscription.bind(sequence, first=True)))
        else:
            response = await self._send_dialog_request(
            call_id, dialog, "REFER",
            extra_headers=(("Refer-To", target.as_header()),
                           *((("Referred-By", target.referred_by),) if target.referred_by else ()),
                           *target.referral_body_headers),
            body=target.referral_body, content_type=target.referral_content_type,
            timeout=min(timeout, 8.0),
        )
        status = int(response.status_code or 0) if response is not None else 0
        if not 200 <= status < 300:
            return sip_transfer.SipTransferResult(False, status, "rejected")
        try:
            return await asyncio.wait_for(subscription.result, timeout)
        except TimeoutError:
            return sip_transfer.SipTransferResult(False, 0, "timeout")

    async def send_dtmf_info(
        self,
        call_id: str,
        digit: str,
        *,
        duration_ms: int = 160,
        timeout: float = 3.0,
    ) -> bool:
        """Send one DTMF digit on an owned inbound dialog using SIP INFO."""

        dialog = self.active_dialogs.get(call_id)
        if dialog is None:
            return False
        try:
            body = build_sip_info_dtmf_body(digit, duration_ms=duration_ms)
        except ValueError:
            return False
        response = await self._send_dialog_request(
            call_id,
            dialog,
            "INFO",
            body=body,
            content_type="application/dtmf-relay",
            timeout=timeout,
        )
        return bool(
            response is not None
            and 200 <= int(response.status_code or 0) < 300
        )

    def send_bye(self, call_id: str = "") -> bool:
        """Compatibility entry point backed by the asynchronous BYE owner."""

        resolved = self._dialog_for_bye(call_id)
        if resolved is None:
            return False
        call_id, dialog = resolved
        if dialog.termination_task is not None:
            return True
        task = asyncio.create_task(
            self.async_send_bye(call_id),
            name=f"voip-sip-listener-bye-{call_id}",
        )
        dialog.termination_task = task
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_tasks.discard)
        return True

    def snapshot(self) -> dict[str, Any]:
        renegotiations = sum(dialog.renegotiations for dialog in self.active_dialogs.values())
        pending_invite_acks = sum(
            1 for dialog in self.active_dialogs.values() if dialog.invite_2xx.active
        )
        invite_2xx_retransmissions = sum(
            dialog.invite_2xx.retransmissions for dialog in self.active_dialogs.values()
        )
        pending_invite_error_acks = sum(
            1
            for completed in self.completed_invites.values()
            if completed.status >= 300 and completed.final_task is not None
        )
        invite_error_retransmissions = sum(
            completed.final_retransmissions for completed in self.completed_invites.values()
        )
        return {
            "transport": self.signaling_transport.lower(),
            "pending_transactions": len(self.pending_invites),
            "active_dialogs": len(self.active_dialogs),
            "pending_call_ids": sorted(self.pending_invites),
            "active_call_ids": sorted(self.active_dialogs),
            "media_renegotiations": renegotiations,
            "media_update_in_progress": sum(
                1 for dialog in self.active_dialogs.values() if dialog.update_in_progress
            ),
            "pending_invite_acks": pending_invite_acks,
            "invite_2xx_retransmissions": invite_2xx_retransmissions,
            "pending_invite_error_acks": pending_invite_error_acks,
            "invite_error_retransmissions": invite_error_retransmissions,
            "last_sip_event": self.last_sip_event,
            "last_sip_status_code": self.last_sip_status_code,
            "last_sip_reason": self.last_sip_reason,
        }

    def _parse_initial_invite(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
        *,
        peer_profile: str = "",
        authorize: bool = False,
    ) -> SipInitialInvite | SipRequestResult | None:
        """Parse routing identity without inventing an unnegotiated media leg."""

        try:
            request_uri = sip.parse_sip_uri(request.uri)
            from_uri = _uri_from_header(request.header("From"))
            profile = str(peer_profile or "").strip().casefold()
            if not profile and supports_dahua_pcm(request.header("User-Agent")):
                profile = "dahua"
            caller_name = sip.name_addr_display_name(request.header("From"))
            caller = caller_name or (from_uri.user if from_uri is not None else "")
            target = sip.name_addr_display_name(request.header("To"))
            if not target:
                target = _identity_header(request.header("X-Voip-Stack-Dest-Name"))
            if not target:
                target = request_uri.user
            caller_route = (
                from_uri.user
                if from_uri is not None and from_uri.user
                else _identity_header(request.header("X-Voip-Stack-Caller-Route"))
                or caller
            )
            target_route = (
                request_uri.user
                or _identity_header(request.header("X-Voip-Stack-Dest-Route"))
                or target
            )
            initial = SipInitialInvite(
                source_host=str(addr[0]),
                source_port=int(addr[1]),
                request_uri=request_uri,
                caller_uri=from_uri,
                target=target,
                caller=caller,
                caller_number=from_uri.user if from_uri is not None else caller_route,
                caller_name=caller_name,
                call_id=request.header("Call-ID"),
                cseq=request.header("CSeq"),
                signaling_transport=self.signaling_transport,
                received_via_trunk=self.trusted_trunk,
                trunk_id=self.trusted_trunk_id if self.trusted_trunk else "",
                peer_profile=profile,
                replaces=sip_transfer.request_replaces(request),
                caller_route=caller_route,
                target_route=target_route,
            )
            if authorize and self.invite_authorizer is not None:
                initial = self.invite_authorizer(request, initial, addr, self.signaling_transport)
                if isinstance(initial, SipRequestResult):
                    return initial
            previous = self.active_dialogs.get(initial.call_id) or self.pending_invites.get(initial.call_id)
            original = getattr(previous, "invite", None) or getattr(previous, "identity", None)
            if original is None:
                delayed = getattr(previous, "delayed_offer", None)
                original = delayed.initial_invite if delayed is not None else None
            if original is not None:
                initial = replace(initial, **identity_fields(original), caller=original.caller,
                                  received_via_trunk=original.received_via_trunk,
                                  trunk_id=original.trunk_id,
                                  outbound_trunk_id=original.outbound_trunk_id,
                                  identified_source_endpoint_id=original.identified_source_endpoint_id,
                                  authenticated_username=original.authenticated_username)
            elif self.invite_identity_resolver is not None:
                initial = self.invite_identity_resolver(request, initial)
            policy = previous.codec_policy if previous is not None else (
                self.codec_policy_resolver(initial) if self.codec_policy_resolver is not None else CodecPolicy()
            )
            return replace(initial, codec_policy=policy)
        except Exception as err:
            _LOGGER.info("SIP initial INVITE parse failed: %s", err)
            return None

    def _parse_invite(
        self,
        request: sip.SipMessage,
        addr,
        *,
        peer_profile: str = "",
        local_offer_sdp: str = "",
        codec_policy=None,
        initial: SipInitialInvite | None = None,
    ) -> SipInvite | None:
        try:
            initial = initial or self._parse_initial_invite(
                request,
                addr,
                peer_profile=peer_profile,
            )
            if initial is None:
                return None
            peer_profile = initial.peer_profile
            codec_policy = codec_policy or initial.codec_policy
            selected = (
                sdp.negotiate_answer_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    local_offer_sdp=local_offer_sdp,
                    codec_policy=codec_policy,
                    allow_dahua_pcm=peer_profile == "dahua",
                    local_capabilities=HA_AUDIO_CAPABILITIES,
                )
                if local_offer_sdp
                else sdp.negotiate_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    allow_dahua_pcm=peer_profile == "dahua",
                    codec_policy=codec_policy,
                    local_capabilities=HA_AUDIO_CAPABILITIES,
                )
            )
            if not local_offer_sdp:
                # The optional shadow comparison cannot change the decision
                # already made by the selected negotiation engine above.
                shadow_inbound_offer(
                    request.header("Call-ID"), request.body, selected,
                    dahua=peer_profile == "dahua", codec_policy=codec_policy)
            if selected is None:
                call_id = request.header("Call-ID")
                if call_id not in self._logged_incompatible_invites:
                    if len(self._logged_incompatible_invites) >= 256:
                        self._logged_incompatible_invites.pop()
                    self._logged_incompatible_invites.add(call_id)
                    try:
                        offered = ", ".join(sdp.offered_media_descriptions(request.body))
                    except Exception as err:
                        offered = f"unparseable SDP media: {err}"
                    _LOGGER.info(
                        "SIP INVITE rejected: no compatible PCM media in SDP call_id=%s offered=[%s] local_send=[%s] local_recv=[%s]",
                        call_id,
                        offered,
                        ", ".join(fmt.wire_token() for fmt in self.supported_send_formats),
                        ", ".join(fmt.wire_token() for fmt in self.supported_recv_formats),
                    )
                return None
            _LOGGER.info(
                "SIP INVITE media selected call_id=%s tx=%s rx=%s offered=[%s]",
                request.header("Call-ID"),
                selected.send.wire_token(),
                selected.recv.wire_token(),
                ", ".join(sdp.offered_media_descriptions(request.body)),
            )
            remote = sdp.parse_sdp(request.body)
            video_directional = (
                (
                    sdp.negotiate_video_answer_directional(
                        request.body,
                        tuple(sdp.offered_video_formats(local_offer_sdp)),
                    )
                    if local_offer_sdp
                    else sdp.negotiate_video_offer_directional(
                        request.body,
                        accepted_encodings=tuple(codec for codec in (
                            ("H264", "VP8", "JPEG", "H263", "H263P", "H265")
                            if self.enable_video_transcoding else ("H264", "VP8", "JPEG")
                        ) if codec_policy.video is None or codec in codec_policy.video),
                        prefer_browser_send=self.prefer_browser_video_send,
                        allow_passthrough_fallback=self.enable_video_transcoding,
                    )
                )
                if self.enable_video
                else None
            )
            video_format = (
                video_directional.send if video_directional is not None else None
            )
            local_video_format = (
                video_directional.recv if video_directional is not None else None
            )
            video_answer_format = (
                video_directional.answer_format
                if video_directional is not None
                else None
            )
            remote_video = sdp.parse_video_sdp(request.body) if video_format is not None else None
            remote_video_target = sdp.RemoteMediaTarget.from_section(
                remote_video,
                rtcp_mux=False,
            )
            _LOGGER.info(
                "SIP INVITE video negotiation call_id=%s enabled=%s selected=%s remote=%s",
                request.header("Call-ID"),
                self.enable_video,
                video_format.wire_token() if video_format is not None else "none",
                (
                    f"{remote_video['connection_ip']}:{remote_video['media_port']}"
                    if remote_video is not None
                    else "none"
                ),
            )
            return SipInvite(
                source_host=initial.source_host,
                source_port=initial.source_port,
                request_uri=initial.request_uri,
                caller_uri=initial.caller_uri,
                target=initial.target,
                caller=initial.caller,
                caller_route=initial.caller_route,
                target_route=initial.target_route,
                call_id=initial.call_id,
                cseq=initial.cseq,
                remote_sdp=request.body,
                send_format=selected.send,
                recv_format=selected.recv,
                recv_formats=selected.recv_formats,
                local_recv_capabilities=tuple(self.supported_recv_formats),
                local_send_capabilities=tuple(self.supported_send_formats),
                remote_rtp_host=remote["connection_ip"],
                remote_rtp_port=remote["media_port"],
                remote_audio_direction=str(remote["direction"]),
                local_audio_direction=sdp.local_direction_for_offer(
                    remote["direction"],
                    remote_connection_held=bool(remote["connection_held"]),
                ),
                remote_audio_connection_held=bool(remote["connection_held"]),
                video_format=video_format,
                local_video_format=local_video_format,
                video_answer_format=video_answer_format,
                # This UAS deliberately answers with separate RTP/RTCP ports.
                # RFC 5761 requires the offerer to stop multiplexing when the
                # answer omits a=rtcp-mux, regardless of the original offer.
                signaling_transport=initial.signaling_transport,
                received_via_trunk=initial.received_via_trunk,
                trunk_id=initial.trunk_id,
                outbound_trunk_id=initial.outbound_trunk_id,
                identified_source_endpoint_id=initial.identified_source_endpoint_id,
                authenticated_username=initial.authenticated_username,
                **identity_fields(initial),
                peer_profile=peer_profile,
                codec_policy=codec_policy,
                replaces=initial.replaces,
                **remote_video_target.as_remote_video_fields(),
            )
        except Exception as err:
            _LOGGER.info("SIP INVITE parse failed: %s", err)
            return None


class SipUdpServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        local_ip: str,
        local_rtp_port: int,
        supported_formats: list[AudioFormat],
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        on_invite: InviteHandler,
        on_offerless_invite: OfferlessInviteHandler | None = None,
        codec_policy_resolver=None,
        invite_identity_resolver=None,
        invite_authorizer: InviteAuthorizer | None = None,
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        on_media_update: MediaUpdateHandler | None = None,
        on_refer: ReferHandler | None = None,
        on_request: RequestHandler | None = None,
        request_router: ApplicationRequestRouter | None = None,
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.local_ip = local_ip
        self.local_rtp_port = local_rtp_port
        self.supported_formats = supported_formats
        self.supported_send_formats = supported_send_formats
        self.supported_recv_formats = supported_recv_formats
        self.on_invite = on_invite
        self.on_offerless_invite = on_offerless_invite
        self.codec_policy_resolver = codec_policy_resolver
        self.invite_identity_resolver = invite_identity_resolver
        self.invite_authorizer = invite_authorizer
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.on_media_update = on_media_update
        self.on_refer = on_refer
        self.on_request = on_request
        self.request_router = request_router
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        self.transport: asyncio.DatagramTransport | None = None
        self.endpoint: SipUdpEndpoint | None = None

    async def start(self) -> bool:
        if self.transport is not None:
            return True
        loop = asyncio.get_running_loop()
        try:
            def _factory() -> SipUdpEndpoint:
                self.endpoint = SipUdpEndpoint(
                    local_ip=self.local_ip,
                    local_sip_port=self.port,
                    local_rtp_port=self.local_rtp_port,
                    supported_formats=self.supported_formats,
                    supported_send_formats=self.supported_send_formats,
                    supported_recv_formats=self.supported_recv_formats,
                    on_invite=self.on_invite,
                    on_offerless_invite=self.on_offerless_invite,
                    codec_policy_resolver=self.codec_policy_resolver,
                    invite_identity_resolver=self.invite_identity_resolver,
                    invite_authorizer=self.invite_authorizer,
                    on_terminated=self.on_terminated,
                    on_register=self.on_register,
                    on_info=self.on_info,
                    on_media_update=self.on_media_update,
                    on_refer=self.on_refer,
                    on_request=self.on_request,
                    request_router=self.request_router,
                    signaling_transport="UDP",
                    enable_video=self.enable_video,
                    enable_video_transcoding=self.enable_video_transcoding,
                    prefer_browser_video_send=self.prefer_browser_video_send,
                )
                return self.endpoint

            transport, _ = await loop.create_datagram_endpoint(
                _factory,
                local_addr=(self.host, self.port),
            )
        except OSError as err:
            _LOGGER.error("Failed to bind SIP UDP %s:%s: %s", self.host, self.port, err)
            return False
        self.transport = transport  # type: ignore[assignment]
        return True

    def send_final_response(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
        connected_identity_name: str = "",
        connected_identity_user: str = "",
    ) -> bool:
        return self.endpoint is not None and self.endpoint.send_final_response(
            call_id,
            status,
            reason,
            answer_sdp=answer_sdp,
            decline_reason=decline_reason,
            connected_identity_name=connected_identity_name,
            connected_identity_user=connected_identity_user,
        )

    def send_bye(self, call_id: str = "") -> bool:
        return self.endpoint is not None and self.endpoint.send_bye(call_id)

    async def async_send_bye(self, call_id: str = "") -> bool:
        return self.endpoint is not None and await self.endpoint.async_send_bye(
            call_id
        )

    async def stop(self) -> None:
        if self.transport is not None:
            endpoint = self.endpoint
            self.transport.close()
            self.transport = None
            if endpoint is not None:
                await endpoint.wait_closed()
        self.endpoint = None


class SipTcpServer(_OwnedSipTcpServer):
    """Preserve the legacy constructor while the transport owner is separate.

    SipEndpointManager composes the owner directly. Existing imports from
    sip_listener still construct the same protocol with these public defaults.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        local_ip: str,
        local_rtp_port: int,
        supported_formats: list[AudioFormat],
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        on_invite: InviteHandler,
        on_offerless_invite: OfferlessInviteHandler | None = None,
        codec_policy_resolver=None,
        invite_identity_resolver=None,
        invite_authorizer: InviteAuthorizer | None = None,
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        on_media_update: MediaUpdateHandler | None = None,
        on_refer: ReferHandler | None = None,
        on_request: RequestHandler | None = None,
        request_router: ApplicationRequestRouter | None = None,
        on_flow_closed: Callable[[str, int, str], None] | None = None,
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
        max_connections: int = 128,
        max_connections_per_host: int = 16,
        initial_message_timeout: float = 15.0,
        frame_timeout: float = 10.0,
    ) -> None:
        super().__init__(
            endpoint_factory=SipUdpEndpoint,
            host=host,
            port=port,
            local_ip=local_ip,
            local_rtp_port=local_rtp_port,
            supported_formats=supported_formats,
            supported_send_formats=supported_send_formats,
            supported_recv_formats=supported_recv_formats,
            on_invite=on_invite,
            on_offerless_invite=on_offerless_invite,
            codec_policy_resolver=codec_policy_resolver,
            invite_identity_resolver=invite_identity_resolver,
            invite_authorizer=invite_authorizer,
            on_terminated=on_terminated,
            on_register=on_register,
            on_info=on_info,
            on_media_update=on_media_update,
            on_refer=on_refer,
            on_request=on_request,
            request_router=request_router,
            on_flow_closed=on_flow_closed,
            enable_video=enable_video,
            enable_video_transcoding=enable_video_transcoding,
            prefer_browser_video_send=prefer_browser_video_send,
            max_connections=max_connections,
            max_connections_per_host=max_connections_per_host,
            initial_message_timeout=initial_message_timeout,
            frame_timeout=frame_timeout,
        )
