"""SIP endpoint request handling for UDP and TCP listeners."""

from __future__ import annotations

from .sip_server_state import (
    ApplicationRequestRouter as ApplicationRequestRouter,
    DelayedOfferAnswer as DelayedOfferAnswer,
    FollowUpSender as FollowUpSender,
    InfoHandler as InfoHandler,
    InviteAuthorizer as InviteAuthorizer,
    InviteHandler as InviteHandler,
    MediaUpdateHandler as MediaUpdateHandler,
    OfferlessInviteHandler as OfferlessInviteHandler,
    ReferHandler as ReferHandler,
    RegisterHandler as RegisterHandler,
    RequestHandler as RequestHandler,
    SendHandler as SendHandler,
    SipInitialInvite as SipInitialInvite,
    SipInvite as SipInvite,
    SipInviteResult as SipInviteResult,
    TcpDialogSender as TcpDialogSender,
    TerminateHandler as TerminateHandler,
    UasDelayedOfferPlan as UasDelayedOfferPlan,
    _ActiveDialog as _ActiveDialog,
    _CompletedRequest as _CompletedRequest,
    _DeferredInviteFinal as _DeferredInviteFinal,
    _DialogResponse as _DialogResponse,
    _MAX_COMPLETED_TRANSACTIONS as _MAX_COMPLETED_TRANSACTIONS,
    _MAX_PENDING_INVITES as _MAX_PENDING_INVITES,
    _MAX_REUSED_UDP_USAGES as _MAX_REUSED_UDP_USAGES,
    _MAX_SIP_INVITE_TASKS as _MAX_SIP_INVITE_TASKS,
    _MAX_SIP_UDP_TASKS as _MAX_SIP_UDP_TASKS,
    _PendingInvite as _PendingInvite,
    _ReliableProvisional as _ReliableProvisional,
    _identity_header as _identity_header,
    _response_contact_uri as _response_contact_uri,
    _response_via_header as _response_via_header,
    _uri_text_from_header as _uri_text_from_header,
)
from .sip_server_invite import SipIncomingInvites
from .sip_server_dialogs import SipServerDialogs
from .sip_server_offers import SipServerOffers

import asyncio
import contextlib
import logging
import secrets
from typing import Any, Awaitable, Callable

from .sip_capture import capture_io, send_sip_datagram
from .sip_tcp_server import SipTcpServer as _OwnedSipTcpServer
from .sip_listener_request import (
    SipFollowUpRequest,
    SipRequestResult,
    accepts_capability_sdp,
    dialog_extension_error,
    is_application_request,
    matches_retransmission as _same_request_transaction,
    request_sequence,
)

from .core.audio_format import AudioFormat, HA_SIP_PCM_FORMATS
from .const import VOIP_STACK_RTP_PORT
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES
from .core import sdp, sip, sip_transfer
from .core.sip_stun import binding_keepalive_response
from .core.codec_policy import CodecPolicy
from .core.sip_transport import SipResponseMailbox, bind_flow_usage
from .core.sip_dialog import (
    SubscriptionDialog,
    DialogKey,
    uas_request_matches_dialog,
)
from .sip_tcp_server import _read_sip_stream_message as _read_sip_stream_message
from .core.sip_transaction import (
    SIP_TIMER_B as _INVITE_2XX_TIMEOUT,
)
from .queue_utils import put_drop_oldest
from .dtmf import build_sip_info_dtmf_body


_LOGGER = logging.getLogger(__name__)


class SipUdpEndpoint(SipIncomingInvites, SipServerDialogs, SipServerOffers, asyncio.DatagramProtocol):
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
        codec_policy=None,
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
        self.codec_policy = codec_policy or CodecPolicy()
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
        for dialog in self.active_dialogs.values():
            if dialog.authenticated_flow is flow:
                dialog.authenticated_flow = None
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

    def submit_datagram(self, data: bytes, addr, *, parsed=None) -> bool:
        """Schedule one message while preserving capacity for dialog control."""

        if self.signaling_transport == "UDP" and data[:1] in (b"\0", b"\1"):
            response = binding_keepalive_response(data, addr)
            if response is None or self.transport is None:
                self.dropped_datagrams += 1
                return False
            send_sip_datagram(self.transport, response, addr)
            return True
        try:
            message = parsed if parsed is not None else sip.parse_message(data)
        except (ValueError, sip.SipError):
            self.dropped_datagrams += 1
            oversized = sip.oversized_request_envelope(data)
            if oversized is not None:
                request, status = oversized
                self._send_response(request, addr, status,
                                    "Request Entity Too Large" if status == 413 else "Message Too Large")
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
        task = asyncio.create_task(self._handle_datagram_guarded(data, addr, parsed=message))
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


    async def _terminate_dialog(self, call_id: str, reason: str) -> None:
        """Signal and retire a confirmed dialog, then notify its owner."""

        await self.async_send_bye(
            call_id,
            timeout=min(1.5, _INVITE_2XX_TIMEOUT),
        )
        if self.on_terminated is not None:
            await self.on_terminated(call_id, reason)


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


    async def _handle_datagram_guarded(self, data: bytes, addr, *, parsed=None) -> None:
        try:
            await self._handle_datagram(data, addr, parsed=parsed)
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
        raw = self._build_response(request, addr, status, reason, body=body,
            to_tag=to_tag, decline_reason=decline_reason, extra_headers=extra_headers)
        return self._send_response_bytes(raw, addr, status, reason)

    def _build_response(
        self, request: sip.SipMessage, addr, status: int, reason: str, *,
        body: bytes = b"", to_tag: str = "", decline_reason: str = "",
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> bytes:
        return sip.build_uas_response(
            request,
            status,
            reason,
            contact_uri=self._contact_for_request(request, transport=self.signaling_transport),
            to_tag=to_tag,
            top_via=_response_via_header(request, addr),
            body=body,
            extra_headers=extra_headers,
            decline_reason=_identity_header(decline_reason),
        )

    def _send_response_bytes(self, raw: bytes, addr, status: int, reason: str) -> bool:
        if not self._send(raw, addr):
            _LOGGER.warning("SIP TX %s %s dropped for %s:%s", status, reason, addr[0], addr[1])
            return False
        _LOGGER.debug("SIP TX %s %s to %s:%s", status, reason, addr[0], addr[1])
        sip.mark_sip_event(self, "SIP_RESPONSE", int(status), reason)
        return True

    async def _handle_datagram(self, data: bytes, addr, *, parsed=None) -> None:
        try:
            request = parsed if parsed is not None else sip.parse_message(data)
        except Exception as err:
            _LOGGER.debug("SIP RX malformed from %s:%s: %s", addr[0], addr[1], err)
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

        _LOGGER.debug("SIP RX %s %s from %s:%s", request.method, request.uri, addr[0], addr[1])
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
                    self._reject_dialog_request(request, addr)
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
        _LOGGER.debug("SIP RX response ignored from %s:%s", addr[0], addr[1])

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
            include_common_codecs=True, codec_policy=self.codec_policy,
            local_capabilities=HA_AUDIO_CAPABILITIES) if accepts_capability_sdp(request) else ""
        body = sdp.rewrite_sdp_origin(body, secrets.randbits(63) or 1, 0).encode() if body else b""
        capability_headers = (("Allow", ", ".join(sorted(sip.SUPPORTED_METHODS))),
                              ("Accept", "application/sdp"),
                              ("Supported", ", ".join(sorted(sip.SUPPORTED_OPTION_TAGS))))
        self._send_response(request, addr, 200, "OK", body=body,
            extra_headers=capability_headers)

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
                _LOGGER.debug("SIP TX %s %s to %s:%s", result.status, result.reason, addr[0], addr[1])
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
            **({"request_flow": self.transaction_flow(addr) if self.transaction_flow is not None else None}
               if self.signaling_transport == "TCP" and request.method == "REFER" else {}),
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
            if completed.response:
                self._send_response_bytes(completed.response, addr, completed.status, completed.reason)
            else:
                self._send_response(request, addr, completed.status, completed.reason)
        else:
            self._reject_dialog_request(request, addr)


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


    def _dialog_for_bye(self, call_id: str) -> tuple[str, _ActiveDialog] | None:
        if not call_id and len(self.active_dialogs) == 1:
            call_id = next(iter(self.active_dialogs))
        dialog = self.active_dialogs.get(call_id) if call_id else None
        if dialog is None:
            return None
        return call_id, dialog


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

    def authenticated_refer_principal(self, call_id: str, flow: object) -> str:
        """Read the INVITE principal only on its still-owned authenticated TCP flow."""
        dialog = self.active_dialogs.get(call_id)
        if (dialog is None or dialog.transport != "TCP" or flow is None
                or dialog.authenticated_flow is not flow or self.transaction_flow is None
                or self.transaction_flow(dialog.addr) is not flow or flow.is_closing()
                or dialog.invite is None):
            return ""
        return dialog.invite.authenticated_username

    def refer_operation_for(self, call_id: str):
        """Observe the accepted event usage which still owns a transfer decision."""
        dialog = self.active_dialogs.get(call_id) or self._event_dialogs.get(call_id)
        task = dialog.refer_task if dialog is not None else None
        return task if task is not None and not task.done() else None

    def observe_refer_operation(self, call_id: str, task) -> bool:
        """Delay dialog termination for an accepted REFER owned by the event layer."""
        dialog = self.active_dialogs.get(call_id)
        if dialog is None or self.refer_operation_for(call_id) is not None:
            return False
        dialog.refer_task = task

        def settled(completed):
            if dialog.refer_task is completed:
                dialog.refer_task = None
            self._release_event_dialog(call_id, dialog)

        task.add_done_callback(settled)
        return True

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
        codec_policy=None,
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
        self.codec_policy = codec_policy
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
                    codec_policy=self.codec_policy,
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
        media_contract=None,
    ) -> bool:
        return self.endpoint is not None and self.endpoint.send_final_response(
            call_id,
            status,
            reason,
            answer_sdp=answer_sdp,
            decline_reason=decline_reason,
            connected_identity_name=connected_identity_name,
            connected_identity_user=connected_identity_user,
            media_contract=media_contract,
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
        codec_policy=None,
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
            codec_policy=codec_policy,
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
