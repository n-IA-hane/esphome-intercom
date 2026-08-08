"""Async SIP/UDP endpoint for the phase-1 VoIP Stack profile."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field, replace
import logging
import secrets
from typing import Any, Awaitable, Callable

from .core.audio_format import AudioFormat, HA_SIP_PCM_FORMATS
from .core.codec_capabilities import supports_dahua_pcm
from .const import VOIP_STACK_RTP_PORT
from .core import sdp, sip, sip_transfer
from .core.sip_dialog import uas_request_matches_dialog
from .sip_tcp_io import SipTcpWriter, read_sip_stream_message as _read_sip_stream_message
from .core.sip_transaction import (
    SIP_T1 as _SIP_T1,
    SIP_T2 as _SIP_T2,
    SIP_TIMER_B as _INVITE_2XX_TIMEOUT,
    SIP_TIMER_H as _INVITE_NON2XX_TIMEOUT,
    SipClientTransaction,
    SipInvite2xxTransaction,
    async_run_server_transaction,
    matches_invite_error_ack,
    same_request_transaction,
)
from .queue_utils import put_drop_oldest


_LOGGER = logging.getLogger(__name__)
_MAX_SIP_UDP_TASKS = 32
_MAX_SIP_INVITE_TASKS = 24
_MAX_PENDING_INVITES = 64
_MAX_COMPLETED_TRANSACTIONS = 256
_DEFERRED_INVITE_TIMEOUT = 60.0


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

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.recv_format

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


DelayedOfferAnswer = Callable[[SipInvite], Awaitable[SipInviteResult]]


@dataclass(frozen=True, slots=True)
class UasDelayedOfferPlan:
    """One local offer and the application continuation for its ACK answer."""

    offer_sdp: str
    accept_answer: DelayedOfferAnswer
    rollback: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class SipRequestResult:
    """Application response for one non-call SIP request."""

    status: int = 200
    reason: str = "OK"
    headers: tuple[tuple[str, str], ...] = ()
    to_tag: str = ""
    follow_up: "SipFollowUpRequest | None" = None
    follow_up_lock: asyncio.Lock | None = None


@dataclass(frozen=True, slots=True)
class SipFollowUpRequest:
    """One request sent in the dialog established by an application method."""

    method: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    content_type: str = ""
    cseq: int = 1


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
    expiry_task: asyncio.Task[None] | None = None
    final_task: asyncio.Task[None] | None = None
    final_retransmissions: int = 0
    local_sdp_session_id: int = field(default_factory=lambda: secrets.randbits(63) or 1)
    local_sdp_session_version: int = 0
    connected_identity_name: str = ""
    connected_identity_user: str = ""
    delayed_offer_plan: UasDelayedOfferPlan | None = None


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
class _ActiveDialog:
    request: sip.SipMessage
    addr: tuple[str, int]
    to_tag: str
    cseq: int
    transport: str
    status: int = 200
    reason: str = "OK"
    answer_sdp: str = ""
    remote_target_uri: str = ""
    route_set: tuple[str, ...] = ()
    invite: SipInvite | None = None
    last_request: sip.SipMessage | None = None
    last_status: int = 200
    last_reason: str = "OK"
    last_response_sdp: str = ""
    renegotiations: int = 0
    update_in_progress: bool = False
    local_cseq: int = 1
    invite_2xx: SipInvite2xxTransaction = field(
        default_factory=SipInvite2xxTransaction
    )
    response_cache: list[_DialogResponse] = field(default_factory=list)
    local_sdp_session_id: int = 0
    local_sdp_session_version: int = 0
    connected_identity_name: str = ""
    connected_identity_user: str = ""
    connected_identity_sent: bool = False
    peer_supports_from_change: bool = False
    connected_identity_task: asyncio.Task[None] | None = None
    remote_uri: str = ""
    remote_display_name: str = ""
    update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_timer: sip.SipSessionTimer = field(default_factory=sip.SipSessionTimer)
    session_timer_task: asyncio.Task[None] | None = None
    refer_task: asyncio.Task[None] | None = None
    delayed_offer: _PendingDelayedOffer | None = None


@dataclass(slots=True)
class _CompletedRequest:
    request: sip.SipMessage
    addr: tuple[str, int]
    status: int
    reason: str


InviteHandler = Callable[[SipInvite], Awaitable[SipInviteResult]]
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


def _same_request_transaction(
    request: sip.SipMessage,
    original: sip.SipMessage,
    addr: tuple[str, int],
    original_addr: tuple[str, int],
) -> bool:
    """Match a request retransmission while allowing a NAT port rebind."""

    if addr[0] != original_addr[0]:
        return False
    return same_request_transaction(request, original)


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


def _response_headers(request: sip.SipMessage, *, addr=None, to_tag: str = "") -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    via_values = request.header_values("Via")
    if via_values:
        top_via = _response_via_header(request, addr) if addr is not None else via_values[0]
        headers.append(("Via", top_via))
        headers.extend(("Via", value) for value in via_values[1:])
    for name in ("From", "Call-ID", "CSeq"):
        value = request.header(name)
        if value:
            headers.append((name, value))
    to_value = request.header("To")
    if to_value and to_tag and not sip.extract_tag(to_value):
        to_value = f"{to_value};tag={to_tag}"
    if to_value:
        headers.insert(2, ("To", to_value))
    if request.method == "INVITE":
        headers.extend(
            ("Record-Route", value)
            for value in request.header_values("Record-Route")
        )
    return headers


def _response_contact_uri(request: sip.SipMessage, *, local_ip: str, local_sip_port: int, transport: str) -> str:
    try:
        request_uri = sip.parse_sip_uri(request.uri)
        user = request_uri.user or "voip"
    except Exception:
        user = "voip"
    port = int(local_sip_port or 5060)
    return str(sip.SipUri(user, local_ip, port, params=(("transport", transport.lower()),)))


def _unsupported_method_response(method: str) -> tuple[int, str]:
    if method in sip.KNOWN_UNSUPPORTED_METHODS:
        return 405, "Method Not Allowed"
    return 501, "Not Implemented"


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
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        on_media_update: MediaUpdateHandler | None = None,
        on_refer: ReferHandler | None = None,
        on_request: RequestHandler | None = None,
        send_override: SendHandler | None = None,
        signaling_transport: str = "UDP",
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
        trusted_trunk: bool = False,
        max_pending_invites: int = _MAX_PENDING_INVITES,
        deferred_invite_timeout: float = _DEFERRED_INVITE_TIMEOUT,
    ) -> None:
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port or 5060)
        self.local_rtp_port = local_rtp_port or VOIP_STACK_RTP_PORT
        base_formats = supported_formats or list(HA_SIP_PCM_FORMATS)
        self.supported_send_formats = supported_send_formats or base_formats
        self.supported_recv_formats = supported_recv_formats or base_formats
        self.on_invite = on_invite
        self.on_offerless_invite = on_offerless_invite
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.on_media_update = on_media_update
        self.on_refer = on_refer
        self.on_request = on_request
        self.send_override = send_override
        self.signaling_transport = (signaling_transport or "UDP").upper()
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        self.trusted_trunk = bool(trusted_trunk)
        self.max_pending_invites = max(1, int(max_pending_invites))
        self.deferred_invite_timeout = max(0.01, float(deferred_invite_timeout))
        self.transport: asyncio.DatagramTransport | None = None
        self._closed_waiter: asyncio.Future[None] | None = None
        self.pending_invites: dict[str, _PendingInvite] = {}
        self._reliable_provisionals: dict[str, _ReliableProvisional] = {}
        self.completed_invites: dict[str, _PendingInvite] = {}
        self.active_dialogs: dict[str, _ActiveDialog] = {}
        self.completed_byes: dict[str, _CompletedRequest] = {}
        self.completed_infos: dict[tuple[str, int], _CompletedRequest] = {}
        self.completed_pracks: dict[tuple[str, int], _CompletedRequest] = {}
        self.completed_application_requests: dict[
            tuple[str, int, str], tuple[_CompletedRequest, SipRequestResult]
        ] = {}
        self._logged_incompatible_invites: set[str] = set()
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._invite_tasks: set[asyncio.Task[None]] = set()
        self._maintenance_tasks: set[asyncio.Task[None]] = set()
        self._client_transaction_responses: dict[
            str, asyncio.Queue[tuple[sip.SipMessage, tuple[str, int]]]
        ] = {}
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
        self.cancel_request_tasks()
        if self._closed_waiter is not None and not self._closed_waiter.done():
            self._closed_waiter.set_result(None)

    async def wait_closed(self) -> None:
        waiter = self._closed_waiter
        if waiter is not None and not waiter.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter, timeout=1.0)
        tasks = tuple(self._request_tasks | self._maintenance_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def datagram_received(self, data: bytes, addr) -> None:
        self.submit_datagram(data, addr)

    def submit_datagram(self, data: bytes, addr) -> bool:
        """Schedule one message while preserving capacity for dialog control."""

        is_invite = data[:7].upper() == b"INVITE "
        is_control = any(
            data[: len(prefix)].upper() == prefix
            for prefix in (b"ACK ", b"BYE ", b"CANCEL ", b"UPDATE ")
        )
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
        for task in tuple(self._request_tasks):
            task.cancel()
        for dialog in tuple(self.active_dialogs.values()):
            self._cancel_invite_2xx(dialog)
            self._cancel_connected_identity(dialog)
            self._cancel_session_timer(dialog)
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

        reliable = _ReliableProvisional(
            request=pending.request,
            addr=pending.addr,
            to_tag=pending.to_tag,
            rseq=secrets.randbelow(0x7FFFFFFE) + 1,
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
    def _cancel_invite_2xx(dialog: _ActiveDialog) -> None:
        dialog.invite_2xx.cancel()

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
        task = dialog.refer_task
        dialog.refer_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _activate_dialog(
        self,
        call_id: str,
        dialog: _ActiveDialog,
        request: sip.SipMessage,
    ) -> None:
        self.active_dialogs[call_id] = dialog
        self._arm_session_timer(
            call_id,
            dialog,
            sip.negotiate_uas_session_timer(request),
            local_role="uas",
        )

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
    ) -> sip.SipMessage | None:
        """Send one serialized non-INVITE request on an owned dialog."""

        async with dialog.update_lock:
            if self.active_dialogs.get(call_id) is not dialog:
                return None
            remote_uri = dialog.remote_uri or _uri_text_from_header(
                dialog.request.header("From")
            )
            remote_tag = sip.extract_tag(dialog.request.header("From"))
            local_uri = local_uri or _uri_text_from_header(dialog.request.header("To"))
            try:
                routing = sip.dialog_request_routing(
                    dialog.remote_target_uri or remote_uri,
                    dialog.route_set,
                )
                ids = sip.SipDialogIds(
                    call_id=call_id,
                    local_tag=dialog.to_tag,
                    remote_tag=remote_tag,
                    cseq=dialog.local_cseq,
                    branch=sip.make_branch(),
                )
                headers = sip.dialog_headers(
                    request_uri=routing.request_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    dialog=ids,
                    method=method,
                    contact_uri=_response_contact_uri(
                        dialog.request,
                        local_ip=self.local_ip,
                        local_sip_port=self.local_sip_port,
                        transport=dialog.transport,
                    ),
                    content_type=content_type,
                    transport=dialog.transport,
                    local_display_name=local_display_name,
                    remote_display_name=dialog.remote_display_name,
                )
                headers.extend(("Route", value) for value in routing.route_headers)
                headers.extend(extra_headers)
                raw = sip.build_request(method, routing.request_uri, headers, body)
                target_addr = dialog.addr
                if dialog.transport == "UDP":
                    target = sip.parse_sip_uri(routing.next_hop_uri)
                    target_addr = (target.host, int(target.port or 5060))
            except (TypeError, ValueError, sip.SipError) as err:
                _LOGGER.warning("SIP %s routing rejected call_id=%s: %s", method, call_id, err)
                return None

            dialog.local_cseq += 1
            return await self._run_client_transaction(
                raw=raw,
                addr=target_addr,
                ids=ids,
                method=method,
                transport=dialog.transport,
                active=lambda: self.active_dialogs.get(call_id) is dialog,
            )

    async def _run_client_transaction(
        self,
        *,
        raw: bytes,
        addr: tuple[str, int],
        ids: sip.SipDialogIds,
        method: str,
        transport: str,
        active: Callable[[], bool] = lambda: True,
    ) -> sip.SipMessage | None:
        """Run the shared non-INVITE transaction for this listener transport."""

        responses: asyncio.Queue[tuple[sip.SipMessage, tuple[str, int]]] = (
            asyncio.Queue(maxsize=8)
        )
        self._client_transaction_responses[ids.branch] = responses
        transaction = SipClientTransaction[tuple[sip.SipMessage, tuple[str, int]]](
            transport=transport,
            timeout=_INVITE_2XX_TIMEOUT,
            t1=_SIP_T1,
            t2=_SIP_T2,
        )

        async def _read(timeout: float):
            return await asyncio.wait_for(responses.get(), timeout=timeout)

        async def _retransmit() -> None:
            if active():
                self._send(raw, addr)

        try:
            if not active() or not self._send(raw, addr):
                return None
            sip.mark_sip_event(self, method)
            while active():
                received = await transaction.receive(_read, _retransmit)
                if received is None:
                    return None
                response, _response_addr = received
                if (
                    sip.response_matches_dialog_transaction(response, ids, method)
                    and int(response.status_code or 0) >= 200
                ):
                    return response
        finally:
            self._client_transaction_responses.pop(ids.branch, None)
        return None

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
            local_uri = _uri_text_from_header(request.header("To"))
            remote_uri = _uri_text_from_header(request.header("From"))
            remote_target = (
                _uri_text_from_header(request.header("Contact")) or remote_uri
            )
            routing = sip.dialog_request_routing(
                remote_target,
                tuple(request.header_values("Record-Route")),
            )
            ids = sip.SipDialogIds(
                call_id=request.header("Call-ID"),
                local_tag=result.to_tag,
                remote_tag=sip.extract_tag(request.header("From")),
                cseq=follow_up.cseq,
                branch=sip.make_branch(),
            )
            contact_uri = _response_contact_uri(
                request,
                local_ip=self.local_ip,
                local_sip_port=self.local_sip_port,
                transport=self.signaling_transport,
            )
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

        return await self._run_client_transaction(
            raw=raw,
            addr=addr,
            ids=ids,
            method=follow_up.method,
            transport=self.signaling_transport,
        )

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
            target = sip_transfer.parse_refer_to(request.header("Refer-To"))
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
        self._send_response(request, addr, 202, "Accepted", to_tag=dialog.to_tag)
        self._remember_dialog_response(dialog, request, addr, 202, "Accepted")

        async def run() -> None:
            try:
                response = await self._send_dialog_request(
                    call_id,
                    dialog,
                    "NOTIFY",
                    extra_headers=(
                        ("Event", f"refer;id={refer_cseq.number}"),
                        ("Subscription-State", "active;expires=60"),
                    ),
                    body=b"SIP/2.0 100 Trying\r\n",
                    content_type="message/sipfrag",
                )
                if response is None or not 200 <= int(response.status_code or 0) < 300:
                    return
                try:
                    status = int(await self.on_refer(call_id, target))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception("SIP REFER handler failed call_id=%s", call_id)
                    status = 500
                status = status if 200 <= status <= 699 else 500
                reason = "OK" if status < 300 else "Transfer Failed"
                await self._send_dialog_request(
                    call_id,
                    dialog,
                    "NOTIFY",
                    extra_headers=(
                        ("Event", f"refer;id={refer_cseq.number}"),
                        ("Subscription-State", "terminated;reason=noresource"),
                    ),
                    body=f"SIP/2.0 {status} {reason}\r\n".encode(),
                    content_type="message/sipfrag",
                )
            finally:
                if dialog.refer_task is asyncio.current_task():
                    dialog.refer_task = None

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
            if not self.send_bye(call_id):
                self.active_dialogs.pop(call_id, None)
            if self.on_terminated is not None:
                await self.on_terminated(call_id, "media_incompatible")
            return False
        if result.commit is not None:
            try:
                await result.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "SIP delayed offer media commit failed call_id=%s", call_id
                )
                if result.rollback is not None:
                    await result.rollback()
                await self._rollback_delayed_offer(delayed)
                if not self.send_bye(call_id):
                    self.active_dialogs.pop(call_id, None)
                if self.on_terminated is not None:
                    await self.on_terminated(call_id, "media_update_failed")
                return False
        delayed.settled = True
        dialog.addr = addr
        if delayed.remote_target_uri:
            dialog.remote_target_uri = delayed.remote_target_uri
        dialog.invite = updated_invite
        dialog.answer_sdp = delayed.offer_sdp
        dialog.renegotiations += 1
        if delayed.request.header("Session-Expires"):
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
        dialog.session_timer.configure(timer, local_role=local_role)
        if timer is None:
            return

        async def _run() -> None:
            try:
                while self.active_dialogs.get(call_id) is dialog:
                    await asyncio.sleep(
                        dialog.session_timer.interval
                        * (0.5 if dialog.session_timer.local_refresher else 2 / 3)
                    )
                    if self.active_dialogs.get(call_id) is not dialog:
                        return
                    if not dialog.session_timer.local_refresher:
                        break
                    refresh = "failed"
                    for _attempt in range(2):
                        response = await self._send_dialog_request(
                            call_id,
                            dialog,
                            "UPDATE",
                            extra_headers=(
                                ("Supported", "timer"),
                                (
                                    "Session-Expires",
                                    f"{dialog.session_timer.interval};refresher=uac",
                                ),
                            ),
                        )
                        refresh = sip.apply_session_refresh_response(
                            dialog.session_timer,
                            response,
                            local_role="uac",
                        )
                        if refresh != "retry":
                            break
                    if refresh != "refreshed":
                        break
                if self.active_dialogs.get(call_id) is dialog:
                    if not self.send_bye(call_id):
                        self.active_dialogs.pop(call_id, None)
                    if self.on_terminated is not None:
                        await self.on_terminated(call_id, "session_timer_expired")
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
            if not self.send_bye(call_id):
                self.active_dialogs.pop(call_id, None)
            if self.on_terminated is not None:
                await self.on_terminated(call_id, "ack_timeout")

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
                self.transport.sendto(data, addr)
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
        headers = _response_headers(request, addr=addr, to_tag=to_tag)
        if request.method in {"INVITE", "UPDATE"} and 200 <= int(status) < 300:
            headers.append((
                "Contact",
                f"<{_response_contact_uri(request, local_ip=self.local_ip, local_sip_port=self.local_sip_port, transport=self.signaling_transport)}>",
            ))
            headers.extend(sip.session_timer_response_headers(request))
        if request.method == "INVITE" and 101 <= int(status) < 300:
            headers.append(
                ("Supported", ", ".join(sorted(sip.SUPPORTED_OPTION_TAGS)))
            )
        if body:
            headers.append(("Content-Type", "application/sdp"))
        if int(status) == 405:
            headers.append(("Allow", ", ".join(sorted(sip.SUPPORTED_METHODS))))
        headers.extend(extra_headers)
        clean_reason = _identity_header(decline_reason)
        if clean_reason and int(status) >= 300:
            quoted = clean_reason.replace("\\", "\\\\").replace('"', '\\"')
            headers.append(("Reason", f'X-Voip-Stack;cause={int(status)};text="{quoted}"'))
            headers.append(("X-Voip-Stack-Decline-Reason", clean_reason))
        raw = sip.build_response(status, reason, headers, body)
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
        if not request.is_request:
            sip.mark_sip_event(self, "SIP_RESPONSE", int(request.status_code or 0), request.reason)
            try:
                via_values = request.header_values("Via")
                branch = sip.parse_via(via_values[0] if via_values else "").branch
            except (TypeError, ValueError, sip.SipError):
                branch = ""
            queue = self._client_transaction_responses.get(branch)
            if queue is not None:
                put_drop_oldest(queue, (request, addr))
                return
            _LOGGER.info("SIP RX response ignored from %s:%s", addr[0], addr[1])
            return

        _LOGGER.info("SIP RX %s %s from %s:%s", request.method, request.uri, addr[0], addr[1])
        sip.mark_sip_event(self, request.method or "SIP_REQUEST")
        if request.method not in sip.SUPPORTED_METHODS:
            status, reason = _unsupported_method_response(request.method or "")
            self._send_response(request, addr, status, reason)
            return
        if unsupported := sip.unsupported_required_options(request):
            sent = self._send_response(
                request,
                addr,
                420,
                "Bad Extension",
                extra_headers=(("Unsupported", ", ".join(unsupported)),),
            )
            return
        try:
            request_cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            self._send_response(request, addr, 400, "Bad Request")
            return
        if request_cseq.method != request.method:
            self._send_response(request, addr, 400, "Bad Request")
            return
        if request.method in {"INVITE", "UPDATE"}:
            try:
                sip.negotiate_uas_session_timer(request)
            except sip.SipSessionIntervalTooSmall as err:
                self._send_response(
                    request,
                    addr,
                    422,
                    "Session Interval Too Small",
                    extra_headers=(("Min-SE", str(err.minimum)),),
                )
                return
            except sip.SipError:
                self._send_response(request, addr, 400, "Bad Request")
                return
        if request.method == "OPTIONS":
            self._send_response(request, addr, 200, "OK")
            return
        if request.method == "REGISTER":
            if self.on_register is not None:
                result = await self.on_register(request, addr, self.signaling_transport)
                headers = _response_headers(request, addr=addr, to_tag="")
                headers.extend(tuple(getattr(result, "headers", ()) or ()))
                raw = sip.build_response(int(result.status), str(result.reason), headers, b"")
                if self._send(raw, addr):
                    _LOGGER.info("SIP TX %s %s to %s:%s", result.status, result.reason, addr[0], addr[1])
                    sip.mark_sip_event(self, "SIP_RESPONSE", int(result.status), str(result.reason))
                else:
                    _LOGGER.warning("SIP REGISTER response dropped for %s:%s", addr[0], addr[1])
                return
            self._send_response(request, addr, 405, "Method Not Allowed")
            return
        if request.method in {"MESSAGE", "PUBLISH", "SUBSCRIBE"}:
            if (
                request.method == "MESSAGE"
                and self.signaling_transport == "UDP"
                and len(data) > 1300
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
            if result.follow_up is not None and 200 <= result.status < 300:
                task = asyncio.create_task(
                    self._send_application_follow_up(request, addr, result),
                    name=f"voip-sip-{result.follow_up.method.lower()}-{request.header('Call-ID')}",
                )
                self._maintenance_tasks.add(task)
                task.add_done_callback(self._maintenance_tasks.discard)
            return
        if request.method == "PRACK":
            call_id = request.header("Call-ID")
            prack_key = (call_id, request_cseq.number)
            completed = self.completed_pracks.get(prack_key)
            if completed is not None:
                if _same_request_transaction(
                    request,
                    completed.request,
                    addr,
                    completed.addr,
                ):
                    self._send_response(
                        request,
                        addr,
                        completed.status,
                        completed.reason,
                    )
                else:
                    self._send_response(
                        request,
                        addr,
                        481,
                        "Call/Transaction Does Not Exist",
                    )
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
            if prack_result is not None and prack_result.commit is not None:
                try:
                    await prack_result.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception("SIP PRACK commit failed call_id=%s", call_id)
                    if prack_result.rollback is not None:
                        await prack_result.rollback()
                    self._send_final_response_now(
                        call_id,
                        500,
                        "Server Internal Error",
                        decline_reason="media_update_failed",
                    )
                    return
            if reliable.deferred_final is not None:
                final = reliable.deferred_final
                self._send_final_response_now(
                    call_id,
                    final.status,
                    final.reason,
                    answer_sdp=final.answer_sdp,
                    decline_reason=final.decline_reason,
                    connected_identity_name=final.connected_identity_name,
                    connected_identity_user=final.connected_identity_user,
                )
            return
        if request.method == "INFO":
            call_id = request.header("Call-ID")
            info_key = (call_id, request_cseq.number)
            completed_info = self.completed_infos.get(info_key)
            if completed_info is not None:
                if _same_request_transaction(request, completed_info.request, addr, completed_info.addr):
                    self._send_response(request, addr, completed_info.status, completed_info.reason)
                else:
                    self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
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
            return
        if request.method == "CANCEL":
            call_id = request.header("Call-ID")
            pending = self.pending_invites.get(call_id)
            completed = self.completed_invites.get(call_id)
            invite_transaction = pending or (completed if completed is not None and completed.cancelled else None)
            # Match the transaction and originating host, but do not pin the
            # source port: NATs and SIP proxies may legitimately rewrite it.
            try:
                incoming_cseq = sip.parse_cseq(request.header("CSeq"))
                pending_cseq = (
                    sip.parse_cseq(invite_transaction.request.header("CSeq"))
                    if invite_transaction is not None
                    else None
                )
                incoming_vias = request.header_values("Via")
                incoming_branch = sip.parse_via(incoming_vias[0] if incoming_vias else "").branch
                pending_vias = invite_transaction.request.header_values("Via") if invite_transaction is not None else []
                pending_branch = (
                    sip.parse_via(pending_vias[0] if pending_vias else "").branch
                    if invite_transaction is not None
                    else ""
                )
                same_transaction = (
                    pending_cseq is not None
                    and incoming_cseq.method == "CANCEL"
                    and pending_cseq.method == "INVITE"
                    and incoming_cseq.number == pending_cseq.number
                    and bool(incoming_branch)
                    and incoming_branch == pending_branch
                )
            except (TypeError, ValueError, sip.SipError):
                same_transaction = False
            if invite_transaction is None or invite_transaction.addr[0] != addr[0] or not same_transaction:
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
            return
        if request.method == "BYE":
            call_id = request.header("Call-ID")
            dialog = self.active_dialogs.get(call_id)
            if dialog is None:
                completed = self.completed_byes.get(call_id)
                if completed is not None and _same_request_transaction(
                    request,
                    completed.request,
                    addr,
                    completed.addr,
                ):
                    self._send_response(request, addr, completed.status, completed.reason)
                else:
                    self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
                return
            same_dialog = _same_dialog_request(request, dialog, addr)
            if not same_dialog:
                self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
                return
            self._cancel_invite_2xx(dialog)
            self._cancel_connected_identity(dialog)
            self._cancel_session_timer(dialog)
            self._cancel_refer(dialog)
            delayed = dialog.delayed_offer
            dialog.delayed_offer = None
            if delayed is not None:
                await self._rollback_delayed_offer(delayed)
            self._remember_terminated_invite(dialog)
            self.active_dialogs.pop(call_id, None)
            self._remember_completed(
                self.completed_byes,
                call_id,
                _CompletedRequest(request, addr, 200, "OK"),
            )
            self._send_response(request, addr, 200, "OK")
            if self.on_terminated is not None:
                await self.on_terminated(call_id, "remote_hangup")
            return
        if request.method == "ACK":
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
            return
        call_id = request.header("Call-ID")
        existing_dialog = self.active_dialogs.get(call_id)
        if existing_dialog is not None:
            cached_response = self._find_dialog_response(existing_dialog, request, addr)
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
                    to_tag=existing_dialog.to_tag,
                )
                return
            if not _same_dialog_request(request, existing_dialog, addr):
                self._send_response(request, addr, 481, "Call/Transaction Does Not Exist", to_tag=existing_dialog.to_tag)
                return
            if request.method == "REFER":
                await self._handle_dialog_refer(
                    call_id, existing_dialog, request, addr
                )
                return
            if request.method not in {"INVITE", "UPDATE"}:
                self._send_response(request, addr, 405, "Method Not Allowed", to_tag=existing_dialog.to_tag)
                return
            if existing_dialog.update_in_progress:
                # This UAS already has an inbound offer pending.  RFC 3261
                # section 14.2 uses 500 + Retry-After for this case; 491 is for
                # a UAS that currently owns a locally generated offer.
                self._send_response(
                    request,
                    addr,
                    500,
                    "Server Internal Error",
                    to_tag=existing_dialog.to_tag,
                    extra_headers=(("Retry-After", "1"),),
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
                    to_tag=existing_dialog.to_tag,
                )
                return

            if not request.body:
                if request.method == "INVITE":
                    previous_invite = existing_dialog.invite or self._parse_invite(
                        existing_dialog.request, existing_dialog.addr
                    )
                    if previous_invite is None or not existing_dialog.answer_sdp:
                        self._send_response(
                            request,
                            addr,
                            488,
                            "Not Acceptable Here",
                            to_tag=existing_dialog.to_tag,
                        )
                        return
                    self._send_response(
                        request,
                        addr,
                        100,
                        "Trying",
                        to_tag=existing_dialog.to_tag,
                    )
                    next_sdp_version = existing_dialog.local_sdp_session_version + 1
                    offer_sdp = sdp.rewrite_sdp_origin(
                        existing_dialog.answer_sdp,
                        existing_dialog.local_sdp_session_id,
                        next_sdp_version,
                    )
                    sent = self._send_response(
                        request,
                        addr,
                        200,
                        "OK",
                        body=offer_sdp.encode(),
                        to_tag=existing_dialog.to_tag,
                    )
                    if not sent:
                        return
                    existing_dialog.update_in_progress = True
                    existing_dialog.delayed_offer = _PendingDelayedOffer(
                        request=request,
                        addr=addr,
                        offer_sdp=offer_sdp,
                        previous_invite=previous_invite,
                        initial_invite=None,
                        remote_target_uri=refreshed_remote_target,
                    )
                    existing_dialog.last_request = request
                    existing_dialog.last_status = 200
                    existing_dialog.last_reason = "OK"
                    existing_dialog.last_response_sdp = offer_sdp
                    existing_dialog.cseq = request_cseq.number + 1
                    existing_dialog.local_sdp_session_version = next_sdp_version
                    self._remember_dialog_response(
                        existing_dialog,
                        request,
                        addr,
                        200,
                        "OK",
                        offer_sdp,
                    )
                    self._arm_invite_2xx(
                        existing_dialog,
                        request,
                        addr,
                        200,
                        "OK",
                        offer_sdp,
                    )
                    return

                # An offerless UPDATE is a session refresh and does not start
                # a new offer/answer exchange.
                sent = self._send_response(
                    request,
                    addr,
                    200,
                    "OK",
                    to_tag=existing_dialog.to_tag,
                )
                if sent:
                    existing_dialog.last_request = request
                    existing_dialog.last_status = 200
                    existing_dialog.last_reason = "OK"
                    existing_dialog.last_response_sdp = ""
                    existing_dialog.cseq = request_cseq.number + 1
                    existing_dialog.addr = addr
                    if refreshed_remote_target:
                        existing_dialog.remote_target_uri = refreshed_remote_target
                    self._accept_remote_connected_identity(existing_dialog, request)
                    existing_dialog.renegotiations += 1
                    if request.header("Session-Expires"):
                        self._arm_session_timer(
                            call_id,
                            existing_dialog,
                            sip.negotiate_uas_session_timer(request),
                            local_role="uas",
                        )
                    self._remember_dialog_response(
                        existing_dialog,
                        request,
                        addr,
                        200,
                        "OK",
                    )
                return

            if request.header("Content-Type").split(";", 1)[0].strip().lower() != "application/sdp":
                self._send_response(
                    request,
                    addr,
                    415,
                    "Unsupported Media Type",
                    to_tag=existing_dialog.to_tag,
                )
                return
            previous_invite = existing_dialog.invite or self._parse_invite(
                existing_dialog.request, existing_dialog.addr
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
                existing_dialog.update_in_progress = True
                existing_dialog.last_request = request
                existing_dialog.last_status = 100
                existing_dialog.last_reason = "Trying"
                existing_dialog.last_response_sdp = ""
                if request.method == "INVITE":
                    self._send_response(
                        request,
                        addr,
                        100,
                        "Trying",
                        to_tag=existing_dialog.to_tag,
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
                    existing_dialog.update_in_progress = False
                if self.active_dialogs.get(call_id) is not existing_dialog:
                    if result.rollback is not None:
                        await result.rollback()
                    terminated = _PendingInvite(
                        request,
                        addr,
                        existing_dialog.to_tag,
                        existing_dialog.transport,
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
                        to_tag=existing_dialog.to_tag,
                        decline_reason="remote_hangup",
                    )
                    if final_sent:
                        self._arm_invite_non2xx(call_id, terminated)
                    return
            elif media_unchanged:
                result = SipInviteResult(200, "OK", answer_sdp=existing_dialog.answer_sdp)
            else:
                result = SipInviteResult(488, "Not Acceptable Here")
            status = int(result.status)
            reason = str(result.reason)
            answer_sdp = str(result.answer_sdp or "")
            next_sdp_version = int(existing_dialog.local_sdp_session_version)
            if answer_sdp:
                answer_sdp = sdp.rewrite_sdp_origin(
                    answer_sdp,
                    existing_dialog.local_sdp_session_id,
                    next_sdp_version,
                )
                if sdp.sdp_description_changed(
                    existing_dialog.answer_sdp, answer_sdp
                ):
                    next_sdp_version += 1
                    answer_sdp = sdp.rewrite_sdp_origin(
                        answer_sdp,
                        existing_dialog.local_sdp_session_id,
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
                to_tag=existing_dialog.to_tag,
                decline_reason=result.decline_reason,
            )
            if not sent and result.rollback is not None:
                await result.rollback()
            if sent:
                existing_dialog.last_request = request
                existing_dialog.last_status = status
                existing_dialog.last_reason = reason
                existing_dialog.last_response_sdp = answer_sdp if 200 <= status < 300 else ""
                existing_dialog.cseq = request_cseq.number + 1
                self._remember_dialog_response(
                    existing_dialog,
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
                        existing_dialog,
                        request,
                        addr,
                        status,
                        reason,
                        answer_sdp,
                    )
                    if result.commit is not None:
                        try:
                            await result.commit()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            _LOGGER.exception(
                                "SIP in-dialog media commit failed after response call_id=%s method=%s",
                                call_id,
                                request.method,
                            )
                            if result.rollback is not None:
                                await result.rollback()
                            # The 2xx has committed the offer/answer exchange on
                            # the wire. If the local media owner cannot commit,
                            # terminate the now-confirmed dialog explicitly.
                            if not self.send_bye(call_id):
                                self.active_dialogs.pop(call_id, None)
                            if self.on_terminated is not None:
                                await self.on_terminated(call_id, "media_update_failed")
                            return
                    existing_dialog.addr = addr
                    if refreshed_remote_target:
                        existing_dialog.remote_target_uri = refreshed_remote_target
                    self._accept_remote_connected_identity(
                        existing_dialog,
                        request,
                    )
                    existing_dialog.answer_sdp = answer_sdp
                    existing_dialog.invite = updated_invite
                    existing_dialog.local_sdp_session_version = next_sdp_version
                    existing_dialog.renegotiations += 1
                    if request.header("Session-Expires"):
                        self._arm_session_timer(
                            call_id,
                            existing_dialog,
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
            return
        if request.method == "UPDATE":
            self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
            return
        try:
            initial_remote_target = sip.contact_target_uri(request)
        except sip.SipError:
            self._send_response(request, addr, 400, "Bad Request")
            return
        try:
            initial_route_set = sip.record_route_set(request)
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
        initial_invite = (
            self._parse_initial_invite(request, addr) if not request.body else None
        )
        invite = self._parse_invite(request, addr) if request.body else None
        if initial_invite is not None and self.on_offerless_invite is None:
            self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=sip.make_tag())
            return
        if invite is None and initial_invite is None:
            self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=sip.make_tag())
            return
        signaling_invite = invite or initial_invite
        assert signaling_invite is not None
        call_id = signaling_invite.call_id
        existing_pending = self.pending_invites.get(call_id)
        if existing_pending is not None:
            if not _same_request_transaction(request, existing_pending.request, addr, existing_pending.addr):
                self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=existing_pending.to_tag)
                return
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
                    dialog = _ActiveDialog(
                        request=request,
                        addr=addr,
                        to_tag=existing_pending.to_tag,
                        cseq=_cseq_number(request.header("CSeq")) + 1,
                        transport=existing_pending.transport,
                        remote_target_uri=(
                            initial_remote_target
                            or _uri_text_from_header(request.header("From"))
                        ),
                        route_set=initial_route_set,
                        status=existing_pending.status,
                        reason=existing_pending.reason,
                        answer_sdp=existing_pending.answer_sdp,
                        invite=invite,
                        last_request=request,
                        last_status=existing_pending.status,
                        last_reason=existing_pending.reason,
                        last_response_sdp=existing_pending.answer_sdp,
                        local_sdp_session_id=existing_pending.local_sdp_session_id,
                        local_sdp_session_version=existing_pending.local_sdp_session_version,
                        connected_identity_name=(
                            existing_pending.connected_identity_name
                            or signaling_invite.target
                        ),
                        connected_identity_user=(
                            existing_pending.connected_identity_user
                            or signaling_invite.routing_target
                        ),
                        peer_supports_from_change=sip.supports_option(
                            request, "from-change"
                        ),
                    )
                    if existing_pending.delayed_offer_plan is not None:
                        plan = existing_pending.delayed_offer_plan
                        dialog.delayed_offer = _PendingDelayedOffer(
                            request=request,
                            addr=addr,
                            offer_sdp=existing_pending.answer_sdp,
                            previous_invite=None,
                            initial_invite=initial_invite,
                            remote_target_uri=initial_remote_target,
                            accept_answer=plan.accept_answer,
                            rollback=plan.rollback,
                        )
                    self._activate_dialog(call_id, dialog, request)
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
            return
        completed_invite = self.completed_invites.get(call_id)
        if completed_invite is not None:
            if not _same_request_transaction(request, completed_invite.request, addr, completed_invite.addr):
                self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=completed_invite.to_tag)
                return
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
            return
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
        pending = _PendingInvite(request, addr, to_tag, self.signaling_transport)
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
            if pending.cancelled and self.on_terminated is not None:
                await self.on_terminated(call_id, "cancelled")
            if delayed_plan is not None and delayed_plan.rollback is not None:
                await delayed_plan.rollback()
            return
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
            pending.status = int(result.status)
            pending.reason = str(result.reason)
            pending.answer_sdp = answer_sdp
            pending.decline_reason = result.decline_reason
            reliable = (
                101 <= int(result.status) < 200
                and (
                    sip.supports_option(request, "100rel")
                    or "100rel" in sip.option_tags(request, "Require")
                )
            )
            if reliable:
                self._send_reliable_provisional(call_id, pending)
            else:
                body = answer_sdp.encode("utf-8") if answer_sdp else b""
                self._send_response(
                    request,
                    addr,
                    result.status,
                    result.reason,
                    body=body,
                    to_tag=to_tag,
                )
            self._arm_pending_expiry(call_id, pending)
            return

        pending.to_tag = to_tag
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
            dialog = _ActiveDialog(
                request=request,
                addr=addr,
                to_tag=to_tag,
                cseq=_cseq_number(request.header("CSeq")) + 1,
                transport=self.signaling_transport,
                remote_target_uri=(
                    initial_remote_target
                    or _uri_text_from_header(request.header("From"))
                ),
                route_set=initial_route_set,
                status=int(result.status),
                reason=str(result.reason),
                answer_sdp=answer_sdp,
                invite=invite,
                last_request=request,
                last_status=int(result.status),
                last_reason=str(result.reason),
                last_response_sdp=answer_sdp,
                local_sdp_session_id=pending.local_sdp_session_id,
                local_sdp_session_version=pending.local_sdp_session_version,
                connected_identity_name=(
                    pending.connected_identity_name or signaling_invite.target
                ),
                connected_identity_user=(
                    pending.connected_identity_user or signaling_invite.routing_target
                ),
                peer_supports_from_change=sip.supports_option(
                    request, "from-change"
                ),
            )
            if delayed_plan is not None:
                dialog.delayed_offer = _PendingDelayedOffer(
                    request=request,
                    addr=addr,
                    offer_sdp=answer_sdp,
                    previous_invite=None,
                    initial_invite=initial_invite,
                    remote_target_uri=initial_remote_target,
                    accept_answer=delayed_plan.accept_answer,
                    rollback=delayed_plan.rollback,
                )
            self._activate_dialog(call_id, dialog, request)
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
        elif sent and int(result.status) >= 300:
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
        reliable = self._reliable_provisionals.get(call_id)
        if (
            reliable is not None
            and reliable.answer_sdp
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
            invite = self._parse_invite(pending.request, pending.addr)
            dialog = _ActiveDialog(
                request=pending.request,
                addr=pending.addr,
                to_tag=pending.to_tag,
                cseq=_cseq_number(pending.request.header("CSeq")) + 1,
                transport=pending.transport,
                remote_target_uri=(
                    sip.contact_target_uri(pending.request)
                    or _uri_text_from_header(pending.request.header("From"))
                ),
                route_set=sip.record_route_set(pending.request),
                status=int(status),
                reason=str(reason),
                answer_sdp=answer_sdp,
                invite=invite,
                last_request=pending.request,
                last_status=int(status),
                last_reason=str(reason),
                last_response_sdp=answer_sdp,
                local_sdp_session_id=pending.local_sdp_session_id,
                local_sdp_session_version=pending.local_sdp_session_version,
                connected_identity_name=(
                    pending.connected_identity_name
                    or (invite.target if invite is not None else "")
                ),
                connected_identity_user=(
                    pending.connected_identity_user
                    or (invite.routing_target if invite is not None else "")
                ),
                peer_supports_from_change=sip.supports_option(
                    pending.request, "from-change"
                ),
            )
            self._activate_dialog(call_id, dialog, pending.request)
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

    def send_bye(self, call_id: str = "") -> bool:
        if not call_id and len(self.active_dialogs) == 1:
            call_id = next(iter(self.active_dialogs))
        dialog = self.active_dialogs.get(call_id) if call_id else None
        if dialog is None:
            return False
        remote_uri = dialog.remote_uri or _uri_text_from_header(
            dialog.request.header("From")
        )
        remote_target_uri = (
            dialog.remote_target_uri
            or _uri_text_from_header(dialog.request.header("Contact"))
            or remote_uri
        )
        local_uri = _uri_text_from_header(dialog.request.header("To"))
        if not remote_target_uri or not remote_uri or not local_uri:
            return False
        remote_tag = sip.extract_tag(dialog.request.header("From"))
        try:
            routing = sip.dialog_request_routing(
                remote_target_uri,
                dialog.route_set,
            )
        except (TypeError, ValueError, sip.SipError) as err:
            _LOGGER.warning("SIP BYE routing rejected call_id=%s: %s", call_id, err)
            return False
        ids = sip.SipDialogIds(
            call_id=call_id,
            local_tag=dialog.to_tag,
            remote_tag=remote_tag,
            cseq=dialog.local_cseq,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=routing.request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ids,
            method="BYE",
            contact_uri=local_uri,
            transport=dialog.transport,
        )
        headers.extend(("Route", value) for value in routing.route_headers)
        raw = sip.build_request("BYE", routing.request_uri, headers, b"")
        target_addr = dialog.addr
        if dialog.transport == "UDP":
            try:
                target = sip.parse_sip_uri(routing.next_hop_uri)
                target_addr = (target.host, int(target.port or 5060))
            except (TypeError, ValueError, sip.SipError):
                pass
        if not self._send(raw, target_addr):
            _LOGGER.warning("SIP TX BYE dropped call_id=%s", call_id)
            return False
        dialog.local_cseq += 1
        self._cancel_invite_2xx(dialog)
        self._cancel_connected_identity(dialog)
        self._cancel_session_timer(dialog)
        self._cancel_refer(dialog)
        delayed = dialog.delayed_offer
        dialog.delayed_offer = None
        if delayed is not None and not delayed.settled:
            task = asyncio.create_task(
                self._rollback_delayed_offer(delayed),
                name=f"voip-sip-delayed-offer-rollback-{call_id}",
            )
            self._maintenance_tasks.add(task)
            task.add_done_callback(self._maintenance_tasks.discard)
        self._remember_terminated_invite(dialog)
        self.active_dialogs.pop(call_id, None)
        _LOGGER.info("SIP TX BYE call_id=%s to %s:%s", call_id, target_addr[0], target_addr[1])
        sip.mark_sip_event(self, "BYE")
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
    ) -> SipInitialInvite | None:
        """Parse routing identity without inventing an unnegotiated media leg."""

        try:
            request_uri = sip.parse_sip_uri(request.uri)
            from_uri = _uri_from_header(request.header("From"))
            profile = str(peer_profile or "").strip().casefold()
            if not profile and supports_dahua_pcm(request.header("User-Agent")):
                profile = "dahua"
            caller = sip.name_addr_display_name(request.header("From"))
            target = sip.name_addr_display_name(request.header("To"))
            if not caller:
                caller = _identity_header(
                    request.header("X-Voip-Stack-Caller-Name")
                )
            if not target:
                target = _identity_header(request.header("X-Voip-Stack-Dest-Name"))
            if not caller:
                caller = from_uri.user if from_uri is not None else ""
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
            return SipInitialInvite(
                source_host=str(addr[0]),
                source_port=int(addr[1]),
                request_uri=request_uri,
                caller_uri=from_uri,
                target=target,
                caller=caller,
                call_id=request.header("Call-ID"),
                cseq=request.header("CSeq"),
                signaling_transport=self.signaling_transport,
                received_via_trunk=self.trusted_trunk,
                peer_profile=profile,
                caller_route=caller_route,
                target_route=target_route,
            )
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
    ) -> SipInvite | None:
        try:
            initial = self._parse_initial_invite(
                request,
                addr,
                peer_profile=peer_profile,
            )
            if initial is None:
                return None
            peer_profile = initial.peer_profile
            selected = (
                sdp.negotiate_answer_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    local_offer_sdp=local_offer_sdp,
                    allow_dahua_pcm=peer_profile == "dahua",
                )
                if local_offer_sdp
                else sdp.negotiate_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    allow_dahua_pcm=peer_profile == "dahua",
                )
            )
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
                        accepted_encodings=(
                            "H264",
                            "VP8",
                            "JPEG",
                            "H263",
                            "H263P",
                            "H265",
                        )
                        if self.enable_video_transcoding
                        else ("H264", "VP8", "JPEG"),
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
                peer_profile=peer_profile,
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
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        on_media_update: MediaUpdateHandler | None = None,
        on_refer: ReferHandler | None = None,
        on_request: RequestHandler | None = None,
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
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.on_media_update = on_media_update
        self.on_refer = on_refer
        self.on_request = on_request
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
                    on_terminated=self.on_terminated,
                    on_register=self.on_register,
                    on_info=self.on_info,
                    on_media_update=self.on_media_update,
                    on_refer=self.on_refer,
                    on_request=self.on_request,
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

    async def stop(self) -> None:
        if self.transport is not None:
            endpoint = self.endpoint
            self.transport.close()
            self.transport = None
            if endpoint is not None:
                await endpoint.wait_closed()
        self.endpoint = None


class SipTcpServer:
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
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        on_media_update: MediaUpdateHandler | None = None,
        on_refer: ReferHandler | None = None,
        on_request: RequestHandler | None = None,
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
        max_connections: int = 128,
        max_connections_per_host: int = 16,
        initial_message_timeout: float = 15.0,
        frame_timeout: float = 10.0,
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
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.on_media_update = on_media_update
        self.on_refer = on_refer
        self.on_request = on_request
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        self.max_connections = max(1, int(max_connections))
        self.max_connections_per_host = max(1, int(max_connections_per_host))
        self.initial_message_timeout = max(0.1, float(initial_message_timeout))
        self.frame_timeout = max(0.1, float(frame_timeout))
        self.server: asyncio.AbstractServer | None = None
        self.endpoint: SipUdpEndpoint | None = None
        self.endpoints: set[SipUdpEndpoint] = set()
        self._writers: dict[tuple[str, int], asyncio.StreamWriter] = {}
        self._tcp_writers: dict[tuple[str, int], SipTcpWriter] = {}
        self._dialog_queues: dict[tuple[tuple[str, int], str], asyncio.Queue[bytes]] = {}
        self._client_tasks: set[asyncio.Task] = set()
        self._connections_by_host: dict[str, int] = {}

    async def start(self) -> bool:
        if self.server is not None:
            return True
        try:
            self.server = await asyncio.start_server(self._handle_client, self.host, self.port)
        except OSError as err:
            _LOGGER.error("Failed to bind SIP TCP %s:%s: %s", self.host, self.port, err)
            return False
        # A SIP dialog belongs to the listening user agent, not to one TCP
        # connection.  RFC 3261 explicitly allows subsequent in-dialog
        # requests to arrive over a different connection.  Keep one logical
        # endpoint for the whole TCP listener and select the current writer by
        # the source address of each request.
        def _send(data: bytes, addr: tuple[str, int]) -> bool:
            tx = self._tcp_writers.get((str(addr[0]), int(addr[1])))
            return tx is not None and tx.send_nowait(data)

        self.endpoint = SipUdpEndpoint(
            local_ip=self.local_ip,
            local_sip_port=self.port,
            local_rtp_port=self.local_rtp_port,
            supported_formats=self.supported_formats,
            supported_send_formats=self.supported_send_formats,
            supported_recv_formats=self.supported_recv_formats,
            on_invite=self.on_invite,
            on_offerless_invite=self.on_offerless_invite,
            on_terminated=self.on_terminated,
            on_register=self.on_register,
            on_info=self.on_info,
            on_media_update=self.on_media_update,
            on_refer=self.on_refer,
            on_request=self.on_request,
            send_override=_send,
            signaling_transport="TCP",
            enable_video=self.enable_video,
            enable_video_transcoding=self.enable_video_transcoding,
            prefer_browser_video_send=self.prefer_browser_video_send,
        )
        self.endpoints.add(self.endpoint)
        _LOGGER.info("SIP TCP listener ready on %s:%s", self.host, self.port)
        return True

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        addr = (str(peer[0]), int(peer[1]))
        active_for_host = self._connections_by_host.get(addr[0], 0)
        if (
            len(self._client_tasks) >= self.max_connections
            or active_for_host >= self.max_connections_per_host
        ):
            _LOGGER.warning(
                "SIP TCP connection limit reached for %s (global=%s/%s host=%s/%s)",
                addr[0],
                len(self._client_tasks),
                self.max_connections,
                active_for_host,
                self.max_connections_per_host,
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        client_task = asyncio.current_task()
        if client_task is not None:
            self._client_tasks.add(client_task)
        self._connections_by_host[addr[0]] = active_for_host + 1
        self._writers[addr] = writer
        tx = SipTcpWriter(writer, label=f"listener {addr[0]}:{addr[1]}")
        self._tcp_writers[addr] = tx

        endpoint = self.endpoint
        if endpoint is None:
            await tx.close()
            writer.close()
            return
        first_message = True
        keepalive_half = False
        try:
            while not reader.at_eof():
                raw = await _read_sip_stream_message(
                    reader,
                    first_byte_timeout=(
                        self.initial_message_timeout if first_message else None
                    ),
                    frame_timeout=self.frame_timeout,
                )
                if raw is None:
                    break
                if raw == b"\r\n":
                    if keepalive_half:
                        tx.send_nowait(b"\r\n")
                    keepalive_half = not keepalive_half
                    continue
                keepalive_half = False
                first_message = False
                try:
                    msg = sip.parse_message(raw)
                except Exception:
                    msg = None
                if msg is not None:
                    queue = self._dialog_queues.get((addr, msg.header("Call-ID")))
                    if queue is not None:
                        if put_drop_oldest(queue, raw):
                            _LOGGER.debug("SIP TCP dialog queue full for %s; dropped oldest message", addr)
                        continue
                endpoint.submit_datagram(raw, addr)
        finally:
            # Closing one TCP connection must not destroy dialogs owned by
            # the listener; the peer may already have opened the replacement
            # connection used for a re-INVITE, ACK or BYE.
            # A reconnect can reuse the same advertised/source address before
            # this connection's cleanup callback runs.  Never let the stale
            # connection remove the replacement writer or its dialog queues.
            if self._writers.get(addr) is writer:
                self._writers.pop(addr, None)
                self._tcp_writers.pop(addr, None)
                for key in [key for key in self._dialog_queues if key[0] == addr]:
                    self._dialog_queues.pop(key, None)
            await tx.close()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if client_task is not None:
                self._client_tasks.discard(client_task)
            remaining = self._connections_by_host.get(addr[0], 1) - 1
            if remaining > 0:
                self._connections_by_host[addr[0]] = remaining
            else:
                self._connections_by_host.pop(addr[0], None)

    def open_reused_dialog(
        self,
        addr: tuple[str, int],
        call_id: str,
    ) -> tuple[TcpDialogSender, asyncio.Queue[bytes]] | None:
        writer = self._writers.get(addr)
        tx = self._tcp_writers.get(addr)
        if writer is None or writer.is_closing() or tx is None:
            return None
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        key = (addr, call_id)
        self._dialog_queues[key] = queue

        def _send(data: bytes) -> bool:
            return tx.send_nowait(data)

        return _send, queue

    def close_reused_dialog(self, addr: tuple[str, int], call_id: str) -> None:
        self._dialog_queues.pop((addr, call_id), None)

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
        for endpoint in tuple(self.endpoints):
            if endpoint.send_final_response(
                call_id,
                status,
                reason,
                answer_sdp=answer_sdp,
                decline_reason=decline_reason,
                connected_identity_name=connected_identity_name,
                connected_identity_user=connected_identity_user,
            ):
                return True
        return False

    def send_bye(self, call_id: str = "") -> bool:
        for endpoint in tuple(self.endpoints):
            if endpoint.send_bye(call_id):
                return True
        return False

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for tx in tuple(self._tcp_writers.values()):
            await tx.close()
        for writer in tuple(self._writers.values()):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        current = asyncio.current_task()
        client_tasks = tuple(task for task in self._client_tasks if task is not current)
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        endpoint = self.endpoint
        self.endpoint = None
        if endpoint is not None:
            disconnected_calls = set(endpoint.pending_invites) | set(endpoint.active_dialogs)
            endpoint.cancel_request_tasks()
            await endpoint.wait_closed()
            if endpoint.on_terminated is not None:
                for call_id in disconnected_calls:
                    with contextlib.suppress(Exception):
                        await endpoint.on_terminated(call_id, "transport_closed")
            endpoint.pending_invites.clear()
            endpoint.completed_invites.clear()
            endpoint.active_dialogs.clear()
            endpoint.completed_byes.clear()
            endpoint.completed_infos.clear()
        self.endpoints.clear()
        self._writers.clear()
        self._tcp_writers.clear()
        self._dialog_queues.clear()
        self._client_tasks.clear()
        self._connections_by_host.clear()
