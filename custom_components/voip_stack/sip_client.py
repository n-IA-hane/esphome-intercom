"""Outbound SIP dialogs, transactions and RTP media negotiation."""

from __future__ import annotations

from .sip_client_types import (
    DialogMediaCommit as DialogMediaCommit,
    DialogMediaUpdateHandler as DialogMediaUpdateHandler,
    PreparedDialogMediaUpdate as PreparedDialogMediaUpdate,
    ReferHandler as ReferHandler,
    SipDialog as SipDialog,
    SipTransferResult as SipTransferResult,
    _DelayedRemoteOffer as _DelayedRemoteOffer,
    _InDialogResponse as _InDialogResponse,
    _MAX_INVITE_RETRY_AFTER as _MAX_INVITE_RETRY_AFTER,
    _MIN_INVITE_RETRY_AFTER as _MIN_INVITE_RETRY_AFTER,
    _audio_payload_update as _audio_payload_update,
    _video_payload_update as _video_payload_update,
)
from .sip_client_invite import SipInviteTransactions
from .sip_client_offers import SipLocalOffers
from .sip_client_requests import SipDialogRequests

import asyncio
import contextlib
from contextvars import ContextVar
from dataclasses import replace
import ipaddress
import logging
import re
import secrets
import socket
import ssl
from typing import Any, Awaitable, Callable

from .sip_capture import send_sip_datagram

from .core.audio_format import AudioFormat, HA_SIP_PCM_FORMATS
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES
from .core.codec_capabilities import supports_dahua_pcm
from .core.media.payload_map import PayloadConflict
from .core.rtp_payload import (
    RtpPayloadDecoder as RtpPayloadDecoder,
    RtpPayloadEncoder as RtpPayloadEncoder,
    pcm_to_rtp_payload as pcm_to_rtp_payload,
    rtp_payload_to_pcm as rtp_payload_to_pcm,
)
from .session_cleanup import async_wait_for_cleanup
from .core import sdp, sip, sip_transfer
from .core.sip_transport import (
    default_tls_context,
    SipMessageDispatcher,
)
from .core.sip_auth import (
    build_digest_authorization,
)
from .core.sip_resolution import SipServerResolver
from .core.sip_dialog import (
    DialogKey,
    SubscriptionDialog,
    apply_remote_offer_media,
    notify_media_commit,
    build_dialog_request,
)
from .sip_tcp_io import (
    SipTcpWriter,
    read_sip_stream_message as _read_sip_stream_message,
)
from .sip_udp_io import SipDatagramQueueProtocol
from .core.sip_transaction import (
    SIP_T1,
    SIP_T2,
    SIP_TIMER_B,
    SIP_TIMER_F,
    SipClientTransaction,
    SipInvite2xxTransaction,
    SessionTimerDriver,
    async_run_dialog_request_transaction,
    matches_response,
    same_request_transaction,
)
from .peer_media_profile import legacy_pcm_compatibility

_LOGGER = logging.getLogger(__name__)


class SipCallClient(SipInviteTransactions, SipLocalOffers, SipDialogRequests):
    """One outbound SIP dialog.

    This is intentionally small and standards-shaped. It can call an ESP or HA
    SIP URI and expose the negotiated RTP parameters to a relay/session owner.
    """

    def __init__(
        self,
        *,
        local_ip: str,
        local_name: str,
        max_forwards: int = 70,
        local_uri_user: str = "",
        local_identity_uri: str = "",
        fixed_local_identity: bool = False,
        local_sip_port: int,
        local_rtp_port: int,
        supported_formats: list[AudioFormat] | None = None,
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        supported_send_rtp_formats: tuple[sdp.RtpPcmFormat, ...] | None = None,
        supported_recv_rtp_formats: tuple[sdp.RtpPcmFormat, ...] | None = None,
        signaling_transport: str = "UDP",
        auth_username: str = "",
        username: str = "",
        password: str = "",
        outbound_proxy: str = "",
        include_common_codecs: bool = False,
        codec_policy=None,
        peer_user_agent: str = "",
        include_dahua_pcm: bool | None = None,
        match_received_codec: bool = False,
        local_video_rtp_port: int = 0,
        video_format: sdp.RtpVideoFormat | None = None,
        video_formats: tuple[sdp.RtpVideoFormat, ...] | list[sdp.RtpVideoFormat] | None = None,
        video_direction: str = "sendrecv",
        generic_video_relay: bool = False,
        allow_video_transcoding: bool = False,
        media_reservation=None,
        video_rtp_socket: socket.socket | None = None,
        video_rtcp_socket: socket.socket | None = None,
        target_resolver: SipServerResolver | None = None,
        tls_context: ssl.SSLContext | None = None,
        call_id: str | None = None,
    ) -> None:
        if call_id is not None and (not call_id or any(ord(char) < 33 or ord(char) == 127 for char in call_id)):
            raise ValueError("Call-ID must be a nonempty SIP header value without whitespace")
        self.local_ip = local_ip
        self.media_ip = local_ip
        self.media_endpoints = {}
        self.prepare_media: Callable[[str], Awaitable[None]] | None = None
        self.media_socket_provider = None
        self.discover_media_sockets = None
        self.local_name = local_name
        self.local_uri_user = str(local_uri_user or username or local_name).strip()
        self.local_identity_uri = str(local_identity_uri or "").strip()
        self.fixed_local_identity = bool(fixed_local_identity)
        self.local_contact_uri = ""
        self.resolve_local_target: Callable[[sip.SipUri], sip.SipUri | None] | None = None
        self.local_sip_port = int(local_sip_port)
        self.local_rtp_port = int(local_rtp_port)
        # ``None`` means that the caller did not constrain the profile.  An
        # empty list is materially different: directional capability
        # negotiation ran and found no usable format.  Never turn that result
        # back into the broad HA default offer.
        base_formats = (
            list(HA_SIP_PCM_FORMATS)
            if supported_formats is None
            else list(supported_formats)
        )
        self.supported_send_formats = (
            list(base_formats)
            if supported_send_formats is None
            else list(supported_send_formats)
        )
        self.supported_recv_formats = (
            list(base_formats)
            if supported_recv_formats is None
            else list(supported_recv_formats)
        )
        self.supported_send_rtp_formats = (
            tuple(supported_send_rtp_formats)
            if supported_send_rtp_formats is not None
            else None
        )
        self.supported_recv_rtp_formats = (
            tuple(supported_recv_rtp_formats)
            if supported_recv_rtp_formats is not None
            else None
        )
        self.signaling_transport = (signaling_transport or "UDP").upper()
        self.auth_username = auth_username
        self.username = username or local_name
        self.password = password
        self.outbound_proxy = outbound_proxy
        from .core.codec_policy import CodecPolicy
        self.codec_policy = codec_policy or CodecPolicy()
        self.include_common_codecs = bool(include_common_codecs)
        self.max_forwards = int(max_forwards)
        if not 0 <= self.max_forwards <= 255:
            raise ValueError("SIP Max-Forwards is outside its valid range")
        self.match_received_codec = bool(match_received_codec)
        self.peer_user_agent = str(peer_user_agent or "").strip()
        self.include_dahua_pcm = (
            supports_dahua_pcm(self.peer_user_agent)
            if include_dahua_pcm is None else bool(include_dahua_pcm)
        )
        self.local_video_rtp_port = int(local_video_rtp_port or 0)
        requested_video = self.codec_policy.video_formats(tuple(video_formats or (() if video_format is None else (video_format,))))
        self.video_formats = requested_video if self.local_video_rtp_port > 0 else ()
        self.video_format = self.video_formats[0] if self.video_formats else None
        self.video_direction = str(video_direction or "sendrecv")
        self.generic_video_relay = bool(generic_video_relay)
        self.allow_video_transcoding = bool(allow_video_transcoding)
        self.media_reservation = media_reservation
        self.audio_rtp_socket: socket.socket | None = None
        self.video_rtp_socket = video_rtp_socket
        self.video_rtcp_socket = video_rtcp_socket
        self.target_resolver = target_resolver or SipServerResolver()
        self.tls_context = tls_context
        self._tls_server_name = ""
        # RTP source identity belongs to this SIP call and survives a
        # dashboard media-owner handoff. The WebSocket views initialize the
        # codec-specific state lazily to avoid coupling SIP signaling to them.
        self.audio_rtp_source = None
        self.video_rtp_source = None
        self._sdp_session_id = secrets.randbits(63) or 1
        self._local_sdp_body = ""
        self.transport: asyncio.DatagramTransport | None = None
        self.protocol: SipDatagramQueueProtocol | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._tcp_writer: SipTcpWriter | None = None
        self._reused_flow_send: Callable[[bytes], bool | None] | None = None
        self._reused_udp_next_hop: tuple[str, int] | None = None
        self._reused_udp_send_to: Callable[[bytes, tuple[str, int]], bool | None] | None = None
        self._reused_flow_responses: asyncio.Queue[bytes | tuple[bytes, tuple[str, int]]] | None = None
        self._reused_flow_close: Callable[[], None] | None = None
        self.bind_signaling_usage: Callable[[str], Callable[[], None]] | None = None
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=128)
        self._deferred_signaling: list[tuple[sip.SipMessage, tuple[str, int]]] = []
        self._reliable_rseq: dict[tuple[str, int], int] = {}
        self.dialog_ids = sip.SipDialogIds(call_id=call_id if call_id is not None else sip.make_call_id("ha"),
                                         local_tag=sip.make_tag())
        self._dialog_changed = asyncio.Event()
        self.dialog = None
        self.early_dialogs: dict[str, SipDialog | None] = {}
        self._terminated_invite_branches: set[str] = set()
        self.on_info_dtmf: Callable[[str, int], None] | None = None
        self.on_info_video: Callable[[], Awaitable[None]] | None = None
        self.on_media_update: DialogMediaUpdateHandler | None = None
        self.on_provisional_media: Callable[[SipDialog], Awaitable[PreparedDialogMediaUpdate | None]] | None = None
        self.on_connected_identity: Callable[[str, str], None] | None = None
        self.on_refer: ReferHandler | None = None
        self._invite_cseq = self.dialog_ids.cseq
        self._pending_target = ""
        self._pending_target_display = ""
        self._dialog_remote_display_name = ""
        self._pending_remote_host = ""
        self._pending_remote_sip_port = 5060
        self._pending_request_uri = ""
        self._pending_local_uri = ""
        self._pending_contact_uri = ""
        self._pending_remote_uri = ""
        self._signaling_nominal: tuple[str, int] | None = None
        self._invite_setup_deadline: float | None = None
        self._resolved_signaling_target: tuple[str, int] | None = None
        self._udp_family_host = ""
        self._pending_invite_body = b""
        self._pending_invite_auth: dict[str, tuple[str, str, int]] = {}
        self._initial_delayed_offer = False
        self._retry_after_used = False
        self._invite_abort_event = asyncio.Event()
        self._invite_transaction_active = False
        self._cancel_requested = False
        self._cancel_sent = False
        self._cancel_transaction = None
        self._pending_cancel_request = b""
        self.cancel_reason = ""
        self._received_provisional = False
        self._invite_task: asyncio.Task[str] | None = None
        self._invite_progress: asyncio.Future[str] | None = None
        self._invite_transaction: SipClientTransaction[
            tuple[sip.SipMessage, tuple[str, int]]
        ] | None = None
        self._start_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._deferred_close_task: asyncio.Task[None] | None = None
        self._dialog_termination_task: asyncio.Task[str] | None = None
        self._exceptional_bye_tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._closed = False
        self._local_dialog_cseq = self._invite_cseq
        self._remote_cseq = 0
        self._in_dialog_responses: list[_InDialogResponse] = []
        self._refer_subscription: sip_transfer.ReferSubscription | None = None
        self._refer_dialog_key = None
        self._refer_count = 0
        self._refer_notifiers = {}
        self._incoming_refer_count = 0
        self._outside_refer = {}
        self._event_dialogs = {}
        self.retain_signaling = None
        self._event_close_task = None
        self._event_finalize_task = None
        self._event_close_initiator = None
        self._deferred_owned_requests = []
        self._early_request_task = None
        self.observe_event_owner = None
        self.release_event_owner = None
        self.archive_responses = None
        self._reply_path = ContextVar(f"sip_reply_{id(self)}", default=None)
        self._incoming_refer_task: asyncio.Task[None] | None = None
        self._incoming_refer_dialog: SipDialog | None = None
        self._dialog_request_lock = asyncio.Lock()
        self._dialog_dispatcher = None
        self._dialog_result = None
        self._dialog_monitor_task = None
        self._local_offer_lock = asyncio.Lock()
        self._remote_offer_request = None
        self._prepared_reinvite: tuple[
            SipDialog,
            SipDialog,
            tuple[sdp.RtpVideoFormat, ...],
            str,
            dict,
        ] | None = None
        self._uas_invite_2xx = SipInvite2xxTransaction()
        self._uas_invite_ack_timeout = asyncio.Event()
        self._uas_delayed_offer: _DelayedRemoteOffer | None = None
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""
        self.connected_party = ""

    @property
    def dialog(self) -> SipDialog | None:
        return self._dialog

    @dialog.setter
    def dialog(self, value: SipDialog | None) -> None:
        self._dialog = value
        self._dialog_changed.set()

    async def start(self) -> None:
        if self.signaling_transport in {"TCP", "TLS"}:
            return
        async with self._start_lock:
            if self._reused_flow_send is not None:
                return
            if self.transport is not None:
                return
            if self._closing or self._closed:
                raise RuntimeError("SIP client is already closed")
            loop = asyncio.get_running_loop()
            protocol = SipDatagramQueueProtocol(self.queue)
            family = socket.AF_INET6 if ":" in self._udp_family_host else socket.AF_INET
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("::" if family == socket.AF_INET6 else "0.0.0.0", 0),
                family=family,
            )
            if self._closing or self._closed:
                transport.close()
                raise RuntimeError("SIP client closed while starting")
            self.protocol = protocol
            self.transport = transport  # type: ignore[assignment]
            sockname = transport.get_extra_info("sockname")
            if sockname and len(sockname) >= 2 and int(sockname[1]) > 0:
                self.local_sip_port = int(sockname[1])

    async def close(self) -> None:
        if asyncio.current_task() is self._incoming_refer_task:
            self._event_close_initiator = asyncio.current_task()
        self._closing = True
        self._invite_abort_event.set()
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(),
                name=f"voip-sip-client-close-{self.dialog_ids.call_id}",
            )
        try:
            await asyncio.wait_for(asyncio.shield(self._close_task), timeout=1.5)
        except TimeoutError:
            # The same cleanup task retains only signaling until Timer F.
            self._close_task.add_done_callback(self._observe_close)

    @staticmethod
    def _observe_close(task):
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.warning("Deferred SIP cleanup failed: %s", type(error).__name__,
                            exc_info=(type(error), error, error.__traceback__))

    async def _close(self) -> None:
        self._uas_invite_2xx.cancel()
        self._uas_delayed_offer = None
        if self._invite_transaction_active:
            with contextlib.suppress(Exception):
                self.cancel()
        # Detach and release media before the first cancellation point.  SIP
        # TCP shutdown can wait on a congested writer, but it must never hold
        # video sockets or an allocator reservation hostage.
        reservation = self.media_reservation
        self.media_reservation = None
        self.prepare_media = None
        self.media_socket_provider = None
        self.discover_media_sockets = None
        self.media_endpoints.clear()
        audio_socket = self.audio_rtp_socket
        self.audio_rtp_socket = None
        if audio_socket is not None:
            audio_socket.close()
        video_socket = self.video_rtp_socket
        self.video_rtp_socket = None
        if video_socket is not None:
            video_socket.close()
        video_rtcp_socket = self.video_rtcp_socket
        self.video_rtcp_socket = None
        if video_rtcp_socket is not None:
            video_rtcp_socket.close()
        if reservation is not None and hasattr(reservation, "release"):
            reservation.release()

        # ``invite()`` and ``wait_for_final()`` shield the SIP transaction
        # from a cancelled UI/service waiter.  Closing therefore first gives
        # the current owner a bounded opportunity to complete the standard
        # CANCEL/487 or ACK/BYE exchange while signaling is still available.
        # Only an unresponsive transaction is force-cancelled afterwards.
        current_task = asyncio.current_task()
        owned_signaling_task = next(
            (
                task
                for task in (self._dialog_termination_task, self._invite_task)
                if task is not None
                and task is not current_task
                and not task.done()
            ),
            None,
        )
        if owned_signaling_task is None and (
            self.dialog is not None or self._invite_transaction_active
        ):
            owned_signaling_task = asyncio.create_task(
                self.terminate(timeout=SIP_TIMER_F),
                name=f"voip-sip-client-terminate-{self.dialog_ids.call_id}",
            )
        if owned_signaling_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(owned_signaling_task),
                    timeout=SIP_TIMER_F,
                )
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "SIP signaling teardown reached its bounded fallback call_id=%s",
                    self.dialog_ids.call_id,
                )
            except asyncio.CancelledError:
                _LOGGER.debug(
                    "SIP signaling owner was already cancelled call_id=%s",
                    self.dialog_ids.call_id,
                )
            except Exception:
                _LOGGER.debug(
                    "SIP signaling teardown failed call_id=%s",
                    self.dialog_ids.call_id,
                    exc_info=True,
                )

        lingering_tasks = tuple(
            dict.fromkeys(
                task
                for task in (
                    owned_signaling_task,
                    self._dialog_termination_task,
                    self._invite_task,
                    self._dialog_monitor_task,
                    self._early_request_task,
                    *self._exceptional_bye_tasks,
                )
                if task is not None
                and task is not current_task
                and not task.done()
            )
        )
        for task in lingering_tasks:
            task.cancel()
        if lingering_tasks:
            await asyncio.gather(*lingering_tasks, return_exceptions=True)
        if self._dialog_result is not None and not self._dialog_result.done():
            self._dialog_result.set_result("closed")
        self._dialog_monitor_task = None
        self.dialog = None
        pending = self._event_waiters()
        if pending:
            if self.retain_signaling is not None:
                self._event_close_task = self.retain_signaling(self, self._drain_event_signaling())
                if self._event_close_task is None:
                    await self.close_event_signaling()
                return
            if self._event_close_initiator in pending:
                self._event_close_task = asyncio.create_task(self._drain_event_signaling(), name="voip-event-signaling-lease")
                return
            await self._drain_event_signaling()
            return
        await self._close_signaling()

    def _event_waiters(self):
        pending = [item.result for item in self._outside_refer.values() if not item.result.done()]
        if self._incoming_refer_task is not None and not self._incoming_refer_task.done():
            pending.append(self._incoming_refer_task)
        return pending

    async def _drain_event_signaling(self):
        try:
            async with asyncio.timeout(60):
                pending = self._event_waiters()
                if pending:
                    await asyncio.gather(*(asyncio.shield(item) for item in pending), return_exceptions=True)
        finally:
            await self.close_event_signaling()

    async def close_event_signaling(self):
        """Finish a retained signaling lease through one cancellation-safe barrier."""
        if self._event_finalize_task is None:
            async def finish():
                for subscription in self._outside_refer.values():
                    subscription.close()
                incoming = self._incoming_refer_task
                if (incoming is not None and incoming is not asyncio.current_task()
                        and incoming is not self._event_close_initiator and not incoming.done()):
                    incoming.cancel()
                    await asyncio.gather(incoming, return_exceptions=True)
                await self._close_signaling()
            self._event_finalize_task = asyncio.create_task(finish(), name="voip-event-signaling-close")
        await async_wait_for_cleanup(self._event_finalize_task)

    async def _close_signaling(self):
        if self.archive_responses is not None:
            self.archive_responses(tuple(self._in_dialog_responses))
        if self.release_event_owner is not None:
            self.release_event_owner(self)
        if self._dialog_dispatcher is not None:
            await self._dialog_dispatcher.close()
            self._dialog_dispatcher = None
        for notifier in tuple(self._refer_notifiers.values()):
            await notifier.close()
        self._refer_notifiers.clear()
        self._outside_refer.clear()
        self._event_dialogs.clear()
        self._invite_transaction_active = False
        self._invite_transaction = None
        self._invite_progress = None
        self._dialog_termination_task = None
        self._exceptional_bye_tasks.clear()
        self.dialog = None
        self.early_dialogs.clear()
        self._terminated_invite_branches.clear()
        self._reliable_rseq.clear()
        self._deferred_signaling.clear()
        if self._refer_subscription is not None:
            self._refer_subscription.close()
        self._refer_subscription = None
        self._incoming_refer_task = None
        self._incoming_refer_dialog = None

        if self.transport is not None:
            self.transport.close()
            self.transport = None

        tcp_writer = self._tcp_writer
        self._tcp_writer = None
        writer = self.writer
        self.writer = None
        self.reader = None
        if self._reused_flow_close is not None:
            self._reused_flow_close()
            self._reused_flow_close = None
        self._reused_flow_send = None
        self._reused_flow_responses = None
        self._reused_udp_next_hop = None
        self._reused_udp_send_to = None
        try:
            try:
                if tcp_writer is not None:
                    await tcp_writer.close()
            finally:
                # StreamWriter.close() is synchronous and must still run if
                # the queued writer task is cancelled while draining.
                if writer is not None:
                    writer.close()
            if writer is not None:
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        finally:
            self._closed = True

    def set_codec_policy(self, policy) -> None:
        """Set the leg policy before starting SIP, preserving offered profiles."""
        if self._invite_transaction_active or self.dialog is not None:
            raise RuntimeError("Codec policy cannot change during a call")
        self.codec_policy = policy
        self.video_formats = policy.video_formats(self.video_formats)
        self.video_format = self.video_formats[0] if self.video_formats else None

    def use_reused_tcp_connection(
        self,
        *,
        send: Callable[[bytes], bool | None],
        responses: asyncio.Queue[bytes | tuple[bytes, tuple[str, int]]],
        close: Callable[[], None],
    ) -> None:
        if self._closing or self._closed:
            close()
            raise RuntimeError("SIP client is already closed")
        self._reused_flow_send = send
        self._reused_flow_responses = responses
        self._reused_flow_close = close

    def use_reused_signaling_flow(
        self,
        *,
        send: Callable[[bytes], bool | None],
        responses: asyncio.Queue[bytes | tuple[bytes, tuple[str, int]]],
        close: Callable[[], None],
        udp_next_hop: tuple[str, int] | None = None,
        udp_send_to: Callable[[bytes, tuple[str, int]], bool | None] | None = None,
    ) -> None:
        """Use one externally owned SIP flow for this dialog."""
        self.use_reused_tcp_connection(send=send, responses=responses, close=close)
        self._reused_udp_next_hop = udp_next_hop
        self._reused_udp_send_to = udp_send_to

    async def _connect_tcp(self, remote_host: str, remote_sip_port: int) -> None:
        async with self._start_lock:
            if self._closing or self._closed:
                raise RuntimeError("SIP client is already closed")
            if self._reused_flow_send is not None:
                return
            if self.writer is not None and not self.writer.is_closing():
                return
            if self._tcp_writer is not None:
                await self._tcp_writer.close()
                self._tcp_writer = None
            if self.writer is not None:
                self.writer.close()
                with contextlib.suppress(Exception):
                    await self.writer.wait_closed()
                self.writer = None
                self.reader = None
            if self._closing or self._closed:
                raise RuntimeError("SIP client closed while connecting")
            host, port = self._signaling_target(remote_host, int(remote_sip_port))
            tls = self.signaling_transport == "TLS"
            if tls:
                if self.tls_context is None:
                    self.tls_context = await default_tls_context()
                budget = min(8.0, max(0.001, self._invite_setup_deadline - asyncio.get_running_loop().time())) if self._invite_setup_deadline is not None else 8.0
                async with asyncio.timeout(budget):
                    reader, writer = await asyncio.open_connection(
                        host, port, ssl=self.tls_context, server_hostname=self._tls_server_name,
                    )
            else:
                budget = min(8.0, max(0.001, self._invite_setup_deadline - asyncio.get_running_loop().time())) if self._invite_setup_deadline is not None else 8.0
                async with asyncio.timeout(budget):
                    reader, writer = await asyncio.open_connection(host, port)
            if self._closing or self._closed:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                raise RuntimeError("SIP client closed while connecting")
            self.reader = reader
            self.writer = writer
            self._tcp_writer = SipTcpWriter(writer, label=f"client {host}:{port}")
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sockname = sock.getsockname()
                if sockname and len(sockname) >= 2 and int(sockname[1]) > 0:
                    self.local_sip_port = int(sockname[1])

    async def _select_initial_signaling_target(
        self,
        host: str,
        port: int,
    ) -> None:
        """Own one pre-dialog network target and discard the prior attempt."""
        if self._reused_flow_send is not None:
            self._resolved_signaling_target = (host, int(port))
            return
        selected = (host, int(port))
        replacing = (
            self._resolved_signaling_target is not None
            and self._resolved_signaling_target != selected
        )
        if replacing and self.transport is not None:
            self.transport.close()
            self.transport = None
            self.protocol = None
        if replacing:
            tcp_writer = self._tcp_writer
            self._tcp_writer = None
            writer = self.writer
            self.writer = None
            self.reader = None
            if tcp_writer is not None:
                await tcp_writer.close()
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            while not self.queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()
        self._resolved_signaling_target = selected
        self._udp_family_host = host
        if self.signaling_transport in {"TCP", "TLS"}:
            assert self._signaling_nominal is not None
            await self._connect_tcp(*self._signaling_nominal)
        else:
            await self.start()

    def _signaling_target(self, remote_host: str, remote_sip_port: int) -> tuple[str, int]:
        if self._reused_flow_send is not None and self._reused_udp_next_hop is not None:
            return self._reused_udp_next_hop
        if self._resolved_signaling_target is not None and (
            self.outbound_proxy
            or self._signaling_nominal == (remote_host, int(remote_sip_port))
        ):
            return self._resolved_signaling_target
        proxy = str(self.outbound_proxy or "").strip()
        if not proxy:
            return remote_host, int(remote_sip_port)
        if proxy.lower().startswith(("sip:", "sips:")):
            proxy = proxy.split(":", 1)[1]
        proxy = proxy.split(";", 1)[0].strip()
        if "@" in proxy:
            proxy = proxy.rsplit("@", 1)[1]
        if ":" in proxy and proxy.count(":") == 1:
            host, port = proxy.rsplit(":", 1)
            try:
                return host.strip(), int(port)
            except ValueError:
                return host.strip(), int(remote_sip_port)
        return proxy, int(remote_sip_port)

    async def _prepare_dialog_signaling_target(self, remote_target: str, route_set: tuple[str, ...]):
        """Resolve a borrowed UDP next hop without changing the winning dialog."""
        if self._reused_udp_send_to is None or self.outbound_proxy:
            return None
        routing = sip.dialog_request_routing(remote_target, route_set)
        uri = sip.parse_sip_uri(routing.next_hop_uri)
        nominal = (uri.host, sip.sip_default_port(uri))
        try:
            ipaddress.ip_address(uri.host)
        except ValueError:
            if self._signaling_nominal == nominal and self._resolved_signaling_target is not None:
                return nominal, self._resolved_signaling_target
            async with asyncio.timeout(SIP_TIMER_F):
                targets = await self.target_resolver.resolve(uri, transport=self.signaling_transport)
            endpoints = (endpoint for target in targets for endpoint in target.endpoints())
            current_host = (self._resolved_signaling_target or (self.local_ip, 0))[0]
            family = ipaddress.ip_address(current_host).version
            selected = next(((host, port) for host, port, protocol in endpoints
                             if protocol == self.signaling_transport and ipaddress.ip_address(host).version == family), None)
            if selected is None:
                raise OSError("SIP dialog target has no address for its owned UDP socket")
            return nominal, selected
        return nominal, nominal

    def _commit_dialog_signaling_target(self, prepared):
        if prepared is not None:
            self._signaling_nominal, self._resolved_signaling_target = prepared

    async def _prepare_response_signaling_target(self, response, fallback_target):
        """Retain the accepted-response routing fallbacks before async lookup."""
        try:
            target = sip.contact_target_uri(response) or fallback_target
        except (TypeError, ValueError, sip.SipError):
            target = fallback_target
        try:
            routes = sip.record_route_set(response, reverse=True) or self._preloaded_route_set()
        except (TypeError, ValueError, sip.SipError):
            routes = self._preloaded_route_set()
        return await self._prepare_dialog_signaling_target(target, routes)

    def _preloaded_route_set(self) -> tuple[str, ...]:
        """Return the configured outbound proxy as one loose SIP route."""

        proxy = str(self.outbound_proxy or "").strip()
        if not proxy:
            return ()
        uri = sip.parse_sip_uri(
            proxy
            if proxy.lower().startswith(("sip:", "sips:"))
            else f"sip:{proxy}"
        )
        if not any(key.lower() == "lr" for key, _value in uri.params):
            uri = sip.SipUri(
                uri.user,
                uri.host,
                uri.port,
                params=(*uri.params, ("lr", None)),
                scheme=uri.scheme,
            )
        return (f"<{uri}>",)

    async def _send_raw(self, raw: bytes, remote_host: str, remote_sip_port: int) -> None:
        if self._reused_flow_send is not None:
            sent = (self._reused_udp_send_to(raw, self._signaling_target(remote_host, int(remote_sip_port)))
                    if self._reused_udp_send_to is not None else self._reused_flow_send(raw))
            if sent is False:
                raise ConnectionError("reused SIP signaling flow is not writable")
            return
        if self.signaling_transport in {"TCP", "TLS"}:
            await self._connect_tcp(remote_host, remote_sip_port)
            if self._tcp_writer is None:
                raise ConnectionError("SIP TCP writer is not available")
            if not await self._tcp_writer.send(raw):
                raise ConnectionError("SIP TCP connection is not writable")
            return
        if self.transport is None:
            raise ConnectionError("SIP UDP transport is not available")
        host, port = self._signaling_target(remote_host, int(remote_sip_port))
        send_sip_datagram(self.transport, raw, (host, port))

    def _has_signaling_path(self) -> bool:
        if self._reused_flow_send is not None:
            return True
        if self.signaling_transport in {"TCP", "TLS"}:
            return self.writer is not None
        return self.transport is not None

    def matches_local_invite_source(self, invite, *, listener_hosts, listener_port: int) -> bool:
        """Correlate an initial local INVITE with this owned signaling flow."""
        if (self._closing or self._closed or not self._invite_transaction_active
                or self._reused_flow_send is not None
                or invite.call_id != self.dialog_ids.call_id
                or invite.signaling_transport.upper() != self.signaling_transport):
            return False
        try:
            sequence = sip.parse_cseq(invite.cseq)
            if (sequence.method != "INVITE" or sequence.number != self._invite_cseq
                    or invite.request_uri != sip.parse_sip_uri(self._pending_request_uri)):
                return False
            hosts = {ipaddress.ip_address(host) for host in listener_hosts}
            source_host = ipaddress.ip_address(invite.source_host)
            target_host, target_port = self._signaling_target(
                self._pending_remote_host, self._pending_remote_sip_port)
            destination = (ipaddress.ip_address(target_host), int(target_port))
            if (source_host not in hosts or destination[0] not in hosts
                    or destination[1] != int(listener_port)
                    or ipaddress.ip_address(invite.request_uri.host) not in hosts
                    or sip.sip_default_port(invite.request_uri) != int(listener_port)):
                return False
            if self.signaling_transport in {"TCP", "TLS"}:
                flow = self.writer
                if flow is None or flow.is_closing():
                    return False
                peer = flow.get_extra_info("peername")
                if (not peer or (ipaddress.ip_address(peer[0]), int(peer[1])) != destination):
                    return False
            else:
                flow = self.transport
                if flow is None or flow.is_closing():
                    return False
            bound = flow.get_extra_info("sockname")
            return bool(bound and int(bound[1]) == int(invite.source_port)
                and (ipaddress.ip_address(bound[0]) == source_host
                    or (self.signaling_transport == "UDP" and ipaddress.ip_address(bound[0]).is_unspecified)))
        except (ValueError, TypeError, sip.SipError):
            return False

    def _send_dialog_request(self, raw: bytes, host: str, port: int, *, network_target=None) -> bool:
        try:
            if self._reused_flow_send is not None:
                return (self._reused_udp_send_to(raw, network_target or self._signaling_target(host, int(port)))
                        if self._reused_udp_send_to is not None else self._reused_flow_send(raw)) is not False
            if self.signaling_transport in {"TCP", "TLS"}:
                if self.writer is None:
                    return False
                if self._tcp_writer is not None:
                    return self._tcp_writer.send_nowait(raw)
                return False
            if self.transport is not None:
                host, port = self._signaling_target(host, int(port))
                send_sip_datagram(self.transport, raw, (host, port))
                return True
        except (ConnectionError, OSError, RuntimeError) as err:
            _LOGGER.debug("SIP dialog send failed for %s:%s: %s", host, port, err)
        return False

    def _dialog_next_hop(self, request_uri: str, fallback_host: str, fallback_port: int) -> tuple[str, int]:
        """Resolve an in-dialog target while preserving an explicit proxy."""
        if self.outbound_proxy:
            return fallback_host, int(fallback_port)
        try:
            target = sip.parse_sip_uri(request_uri)
            if self.resolve_local_target is not None:
                target = self.resolve_local_target(target) or target
        except (TypeError, ValueError, sip.SipError):
            return fallback_host, int(fallback_port)
        return target.host, int(target.port or 5060)

    def _next_dialog_cseq(self) -> int:
        self._local_dialog_cseq = max(
            self._local_dialog_cseq,
            self._invite_cseq,
        ) + 1
        return self._local_dialog_cseq

    def _ack_retransmitted_invite_2xx(self, message: sip.SipMessage) -> bool:
        dialog = self.dialog
        if (
            dialog is None
            or message.status_code is None
            or not 200 <= message.status_code < 300
            or not matches_response(
                message,
                method="INVITE",
                cseq=self._invite_cseq,
                branch=self.dialog_ids.branch,
            )
        ):
            return False
        remote_tag = sip.extract_tag(message.header("To"))
        if not remote_tag:
            return False
        if remote_tag != self.dialog_ids.remote_tag:
            return self._settle_losing_invite_branch(message)
        self._send_ack(
            dialog.remote_host,
            dialog.remote_sip_port,
            dialog.remote_target_uri or dialog.remote_uri,
            dialog.local_uri,
            dialog.remote_uri,
            route_set=dialog.route_set,
            remote_tag=remote_tag,
        )
        return True

    def _settle_losing_invite_branch(self, response: sip.SipMessage) -> bool:
        """ACK one forked 2xx and terminate that non-winning dialog once."""

        remote_tag = sip.extract_tag(response.header("To"))
        if not remote_tag:
            return False
        try:
            remote_target = (
                sip.contact_target_uri(response) or self._pending_remote_uri
            )
            route_set = (
                sip.record_route_set(response, reverse=True)
                or self._preloaded_route_set()
            )
        except (TypeError, ValueError, sip.SipError):
            return False
        host, port = self._dialog_next_hop(
            remote_target,
            self._pending_remote_host,
            self._pending_remote_sip_port,
        )
        # The established reader is synchronous. Let the existing exceptional
        # cleanup task resolve this losing branch, without replacing the winner.
        if self._reused_udp_send_to is not None and not self.outbound_proxy:
            routing = sip.dialog_request_routing(remote_target, route_set)
            try:
                ipaddress.ip_address(sip.parse_sip_uri(routing.next_hop_uri).host)
            except ValueError:
                duplicate = remote_tag in self._terminated_invite_branches
                self._terminated_invite_branches.add(remote_tag)
                return self._start_bye_request_transaction(host, port, remote_target,
                    self._pending_local_uri, self._pending_remote_uri, route_set=route_set,
                    remote_tag=remote_tag, ack_before_bye=True, ack_only=duplicate)
        acknowledged = self._send_ack(
            host,
            port,
            remote_target,
            self._pending_local_uri,
            self._pending_remote_uri,
            route_set=route_set,
            remote_tag=remote_tag,
        )
        if remote_tag not in self._terminated_invite_branches:
            self._terminated_invite_branches.add(remote_tag)
            self._start_bye_request_transaction(
                host,
                port,
                remote_target,
                self._pending_local_uri,
                self._pending_remote_uri,
                route_set=route_set,
                remote_tag=remote_tag,
            )
        return acknowledged

    def _send_response_to_request(
        self,
        request: sip.SipMessage,
        host: str,
        port: int,
        status: int,
        reason: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
        to_tag: str = "",
    ) -> bool:
        raw = sip.build_uas_response(
            request,
            status,
            reason,
            contact_uri=(
                self._pending_contact_uri
                or getattr(self.dialog, "local_uri", "")
                or self._pending_local_uri
            ),
            body=body,
            extra_headers=extra_headers, to_tag=to_tag,
        )
        reply = self._reply_path.get()
        if not (reply(raw) if reply is not None else self._send_dialog_request(raw, host, int(port))):
            _LOGGER.warning("SIP TX %s %s dropped: signaling path unavailable", status, reason)
            return False
        sip.mark_sip_event(self, "SIP_RESPONSE", int(status), reason)
        _LOGGER.debug("SIP TX %s %s to %s:%s", status, reason, host, port)
        return True

    def _arm_uas_invite_2xx(
        self,
        request: sip.SipMessage,
        host: str,
        port: int,
        status: int,
        reason: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> None:
        """Retransmit an INVITE 2xx until its end-to-end ACK arrives.

        RFC 3261 makes this UAS-core responsibility independent of the
        underlying transport, so the timer intentionally also runs over TCP.
        """
        if not 200 <= status < 300:
            return
        self._uas_invite_ack_timeout.clear()
        async def ack_timeout() -> None:
            _LOGGER.warning(
                "SIP remote re-INVITE ACK timed out call_id=%s cseq=%s; terminating dialog",
                self.dialog_ids.call_id,
                self._uas_invite_2xx.cseq,
            )
            dialog = self.dialog
            self._uas_invite_2xx.cancel()
            self._uas_delayed_offer = None
            if dialog is not None:
                self._start_bye_request_transaction(
                    dialog.remote_host,
                    dialog.remote_sip_port,
                    dialog.remote_target_uri or dialog.remote_uri,
                    dialog.local_uri,
                    dialog.remote_uri,
                    route_set=dialog.route_set,
                )
            self.dialog = None
            self._uas_invite_ack_timeout.set()

        self._uas_invite_2xx.start(
            request,
            transport=self.signaling_transport,
            send=lambda: self._send_response_to_request(
                request,
                host,
                port,
                status,
                reason,
                extra_headers=extra_headers,
                body=body,
            ),
            on_timeout=ack_timeout,
            timeout=SIP_TIMER_B,
            t1=SIP_T1,
            t2=SIP_T2,
            task_name=f"voip-sip-client-2xx-{self.dialog_ids.call_id}",
        )

    def _acknowledges_uas_invite_2xx(self, request: sip.SipMessage, host: str) -> bool:
        acknowledged = self._uas_invite_2xx.acknowledge(
            request,
            lambda ack: self._request_matches_dialog(ack, host, "ACK"),
        )
        if acknowledged:
            self._uas_invite_ack_timeout.clear()
        return acknowledged

    async def _commit_delayed_offer_ack(
        self, request: sip.SipMessage
    ) -> str | None:
        delayed = self._uas_delayed_offer
        self._uas_delayed_offer = None
        if delayed is None:
            return None
        content_type = request.header("Content-Type").split(";", 1)[0].lower()
        prepared = (
            self._answer_remote_offer(
                request,
                local_offer_sdp=delayed.offer_sdp,
            )
            if request.body and content_type == "application/sdp"
            else None
        )
        if prepared is None or self.dialog is None:
            await self._terminate_confirmed_dialog()
            return "media_incompatible"
        current = self.dialog
        updated, _offer = prepared
        if delayed.remote_target_uri:
            updated = replace(
                updated,
                remote_target_uri=delayed.remote_target_uri,
            )
        unchanged = self._same_dialog_media(current, updated)
        commit = rollback = on_committed = None
        advertised_endpoints = dict(self.media_endpoints)
        incompatible_update = False

        async def discard_prepared():
            nonlocal rollback
            undo, rollback = rollback, None
            try:
                if undo is not None:
                    await async_wait_for_cleanup(asyncio.create_task(undo(), name="voip-delayed-media-rollback"))
            finally:
                if self.dialog is current:
                    await self._terminate_confirmed_dialog()
        if self.on_media_update is not None:
            try:
                prepared_update = await self.on_media_update(current, updated, "INVITE")
                if isinstance(prepared_update, PreparedDialogMediaUpdate):
                    commit, rollback = prepared_update.commit, prepared_update.rollback
                    on_committed = prepared_update.on_committed
                    proposed_port = prepared_update.answer_video_rtp_port
                    incompatible_update = proposed_port is not None and int(proposed_port) != updated.local_video_rtp_port
                    if prepared_update.answer_media_endpoints is not None:
                        active = {"audio", "audio_rtcp"}
                        if updated.video_format is not None and updated.local_video_rtp_port:
                            active.update(("video", "video_rtcp"))
                        incompatible_update |= any(
                            prepared_update.answer_media_endpoints.get(kind) != advertised_endpoints.get(kind)
                            for kind in active)
                else:
                    commit = prepared_update
            except asyncio.CancelledError:
                await discard_prepared()
                raise
            except Exception:
                await discard_prepared()
                _LOGGER.exception(
                    "SIP delayed offer preparation failed call_id=%s",
                    self.dialog_ids.call_id,
                )
                return "media_update_failed"
        if incompatible_update or (commit is None and not unchanged):
            await discard_prepared()
            return "media_incompatible"
        applied = False
        try:
            applied = await apply_remote_offer_media(commit, rollback)
        finally:
            if not applied and self.dialog is current:
                await self._terminate_confirmed_dialog()
        if not applied:
            _LOGGER.error(
                "SIP delayed offer commit failed call_id=%s", self.dialog_ids.call_id
            )
            return "media_update_failed"
        requested_timer = sip.negotiate_uas_session_timer(delayed.request)
        updated.session_timer.configure(
            requested_timer, local_role="uas", now=asyncio.get_running_loop().time(),
        )
        self.dialog = updated
        notify_media_commit(on_committed, updated)
        await self._complete_imageattr_fallback()
        return None if self.dialog is not None else "media_update_failed"

    @staticmethod
    def _video_answer_or_fallback(answer, offered, **kwargs):
        """Preserve usable media while requesting the RFC 6236 fallback."""

        try:
            return sdp.negotiate_video_answer_directional(answer, offered, **kwargs), False
        except sdp.ImageAttributeFallbackRequired:
            formats = (offered,) if isinstance(offered, sdp.RtpVideoFormat) else tuple(offered)
            legacy = sdp.remove_image_attributes(formats)
            return sdp.negotiate_video_answer_directional(
                sdp.remove_image_attributes(answer), legacy, **kwargs,
            ), True

    async def _complete_imageattr_fallback(self) -> None:
        """Use the existing serialized offer owner for one optional fallback."""

        current = self.dialog
        if current is None or not current.imageattr_fallback_needed:
            return
        # Clear the intent before awaiting signaling. A new incompatible
        # attribute on an unsolicited answer to the legacy offer is ignored
        # by the SDP negotiator, so this cannot become an offer loop.
        current.imageattr_fallback_needed = False
        formats = sdp.remove_image_attributes(sdp.offered_video_formats(current.local_sdp_body))
        if not formats or current.local_video_rtp_port == 0:
            return
        candidate = await self.async_prepare_video_reinvite(
            local_video_rtp_port=current.local_video_rtp_port,
            video_formats=formats,
            video_direction=current.local_video_direction,
            offer_template=sdp.remove_image_attributes(current.local_sdp_body),
            expected_dialog=current,
        )
        if candidate is None:
            return
        commit = rollback = on_committed = None
        committed = False
        try:
            if self.on_media_update is not None:
                update = await self.on_media_update(current, candidate, "INVITE")
                if isinstance(update, PreparedDialogMediaUpdate):
                    commit, rollback = update.commit, update.rollback
                    on_committed = update.on_committed
                else:
                    commit = update
                if commit is None and not self._same_dialog_media(current, candidate):
                    return

            async def commit_current():
                if self.dialog is not current:
                    raise ConnectionAbortedError("SIP dialog changed during media preparation")
                if commit is not None:
                    await commit()

            async def publish():
                nonlocal committed
                if not self.commit_prepared_reinvite(current, candidate):
                    raise ConnectionAbortedError("SIP dialog changed during media commit")
                committed = True
                notify_media_commit(on_committed, candidate)

            async def rollback_unpublished():
                # Cancellation can arrive while the common finalize barrier
                # is completing. Publication is the irreversible commit point.
                if not committed and rollback is not None:
                    await rollback()

            await apply_remote_offer_media(commit_current, rollback_unpublished, finalize=publish)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "SIP image-size fallback media preparation failed call_id=%s",
                self.dialog_ids.call_id,
            )
        finally:
            # The peer already accepted this generation. The common media
            # compensation restores the previous contract before releasing
            # its reservation, and refuses to overwrite a newer dialog.
            if not committed:
                prepared = self._prepared_reinvite
                if prepared is not None and prepared[0] is current and prepared[1] is candidate:
                    await self.restore_prepared_reinvite(current, candidate)
                    # A BYE or a newer generation forbids compensation. The
                    # old reservation still belongs to this completed owner.
                    if self._prepared_reinvite is prepared and self.dialog is not current and self.dialog is not candidate:
                        self._prepared_reinvite = None

    def _request_matches_dialog(self, request: sip.SipMessage, _host: str, method: str) -> bool:
        """Match an in-dialog request using the RFC 3261 dialog identifiers.

        A dialog is identified by its Call-ID and local/remote tags, not by the
        source IP address.  The request has already been selected by Call-ID in
        ``_read_response``.  Requiring the source to match the original target
        breaks valid dialogs traversing a proxy or SBC whose sequential
        requests can be emitted by a different signaling node.
        """
        dialog = self.dialog
        if dialog is None:
            return False
        try:
            cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            return False
        return (
            request.header("Call-ID") == self.dialog_ids.call_id
            and
            cseq.method == method.upper()
            and cseq.number > 0
            and sip.extract_tag(request.header("From")) == self.dialog_ids.remote_tag
            and sip.extract_tag(request.header("To")) == self.dialog_ids.local_tag
        )

    _same_in_dialog_transaction = staticmethod(same_request_transaction)

    def completed_responses(self) -> tuple[_InDialogResponse, ...]:
        """Expose immutable transaction replies for the runtime replay cache."""
        return tuple(self._in_dialog_responses)

    def _find_in_dialog_response(self, request: sip.SipMessage) -> _InDialogResponse | None:
        for cached in reversed(self._in_dialog_responses):
            if self._same_in_dialog_transaction(request, cached.request):
                return cached
        return None

    def _remember_in_dialog_response(
        self,
        request: sip.SipMessage,
        status: int,
        reason: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
        to_tag: str = "",
    ) -> None:
        self._in_dialog_responses = [
            cached
            for cached in self._in_dialog_responses
            if not self._same_in_dialog_transaction(request, cached.request)
        ]
        self._in_dialog_responses.append(
            _InDialogResponse(request, int(status), str(reason), extra_headers, body, to_tag,
                                  self._pending_contact_uri or getattr(self.dialog, "local_uri", ""))
        )
        del self._in_dialog_responses[:-16]

    @staticmethod
    def _same_dialog_media(previous: SipDialog, updated: SipDialog) -> bool:
        return bool(
            previous.send_format.wire_token() == updated.send_format.wire_token()
            and previous.recv_format.wire_token() == updated.recv_format.wire_token()
            and (previous.recv_formats or (previous.recv_format,))
            == (updated.recv_formats or (updated.recv_format,))
            and (previous.send_formats or (previous.send_format,))
            == (updated.send_formats or (updated.send_format,))
            and previous.remote_rtp_host == updated.remote_rtp_host
            and previous.remote_rtp_port == updated.remote_rtp_port
            and previous.remote_audio_direction == updated.remote_audio_direction
            and previous.remote_audio_connection_held
            == updated.remote_audio_connection_held
            and previous.video_format == updated.video_format
            and previous.local_video_format == updated.local_video_format
            and previous.remote_video_rtp_host == updated.remote_video_rtp_host
            and previous.remote_video_rtp_port == updated.remote_video_rtp_port
            and previous.remote_video_rtcp_host == updated.remote_video_rtcp_host
            and previous.remote_video_rtcp_port == updated.remote_video_rtcp_port
            and previous.remote_video_rtcp_mux == updated.remote_video_rtcp_mux
            and previous.remote_video_connection_held
            == updated.remote_video_connection_held
        )

    def _answer_remote_offer(
        self,
        request: sip.SipMessage,
        *,
        local_offer_sdp: str = "",
        current: SipDialog | None = None,
        advertise_media: bool = True,
    ) -> tuple[SipDialog, str] | None:
        """Build the media replacement for a remote offer or delayed answer."""

        current = current or self.dialog
        if current is None:
            return None
        local_video_port = current.local_video_rtp_port if local_offer_sdp else self.local_video_rtp_port
        try:
            selected = (
                sdp.negotiate_answer_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    local_offer_sdp=local_offer_sdp,
                    codec_policy=self.codec_policy,
                    compatibility=legacy_pcm_compatibility(self.include_dahua_pcm),
                    local_capabilities=HA_AUDIO_CAPABILITIES,
                )
                if local_offer_sdp
                else sdp.negotiate_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    compatibility=legacy_pcm_compatibility(self.include_dahua_pcm),
                    codec_policy=self.codec_policy,
                    local_capabilities=HA_AUDIO_CAPABILITIES,
                    # A delayed initial offer has only a signaling seed. RTP
                    # identity exists after the first remote media endpoint.
                    established=(sdp.RtpPcmDirection(current.send_format, current.recv_format)
                                 if current.remote_rtp_port else None),
                )
            )
            if selected is None:
                return None
            receive_formats = selected.recv_formats
            send_formats = selected.send_formats
            audio_payloads = _audio_payload_update(current.audio_payloads,
                send=selected.send_formats or (selected.send,),
                receive=receive_formats)
            parsed = sdp.parse_sdp(request.body)
            accepted_video = tuple(
                dict.fromkeys(
                    (
                        *(fmt.encoding for fmt in self.video_formats),
                        *(("H264", "VP8", "JPEG") if self.allow_video_transcoding else ()),
                    )
                )
            )
            if self.codec_policy.video is not None:
                accepted_video = tuple(codec for codec in accepted_video if codec in self.codec_policy.video)
            imageattr_fallback = False
            video_directional = (
                (
                    self._video_answer_or_fallback(
                        request.body,
                        tuple(sdp.offered_video_formats(local_offer_sdp)),
                    )
                    if local_offer_sdp
                    else sdp.negotiate_video_offer_directional(
                        request.body,
                        local_formats=self.video_formats,
                        accepted_encodings=accepted_video,
                        prefer_browser_send=self.video_direction
                        in {"sendonly", "sendrecv"},
                        allow_passthrough_fallback=self.allow_video_transcoding,
                    )
                )
                if accepted_video and local_video_port
                else None
            )
            if local_offer_sdp and video_directional is not None:
                video_directional, imageattr_fallback = video_directional
            video = video_directional.send if video_directional is not None else None
            local_video = (
                video_directional.recv if video_directional is not None else None
            )
            video_answer = (
                video_directional.answer_format
                if video_directional is not None
                else None
            )
            try:
                video_payloads = _video_payload_update(current.video_payloads, video, local_video)
            except PayloadConflict:
                if local_offer_sdp:
                    return None  # The existing offer owner handles a remotely committed answer.
                video = local_video = video_answer = None
                video_payloads = _video_payload_update(current.video_payloads, None, None)
            remote_video = sdp.parse_video_sdp(request.body) if video is not None else None
            remote_video_target = sdp.RemoteMediaTarget.from_section(
                remote_video,
                rtcp_mux=False,
            )
            local_video_direction = (
                sdp.constrained_video_direction(
                    video.direction,
                    allow_send=(
                        self.video_direction in {"sendonly", "sendrecv"}
                        and (self.generic_video_relay or sdp.browser_video_send_supported(video))
                        and not bool(
                            remote_video and remote_video["connection_held"]
                        )
                    ),
                    allow_receive=self.video_direction in {"recvonly", "sendrecv"},
                )
                if video is not None
                else "inactive"
            )
            dtmf_direction = selected.dtmf_direction
            dtmf_recv = dtmf_direction.recv if dtmf_direction is not None else None
            dtmf_send = dtmf_direction.send if dtmf_direction is not None else None
            answer = local_offer_sdp or sdp.build_answer_directional(
                self.local_ip,
                self.media_ip,
                self.local_rtp_port,
                selected.send,
                selected.recv,
                recv_formats=receive_formats, send_formats=send_formats,
                dtmf_direction=dtmf_direction,
                audio_rtcp=False,
                remote_sdp=request.body,
                video_port=(local_video_port if video is not None else 0),
                video_format=video_answer,
                video_direction=local_video_direction,
            )
            session_id = int(current.local_sdp_session_id or self._sdp_session_id)
            session_version = int(current.local_sdp_session_version)
            if advertise_media:
                answer = self._advertise_media(answer)
            answer = sdp.rewrite_sdp_origin(answer, session_id, session_version)
            if advertise_media and not local_offer_sdp and sdp.sdp_description_changed(
                current.local_sdp_body, answer
            ):
                session_version += 1
                answer = sdp.rewrite_sdp_origin(
                    answer, session_id, session_version
                )
            contact = request.header("Contact")
            remote_target = current.remote_target_uri
            if contact:
                remote_target = str(sip.parse_sip_uri(contact))
            updated = replace(
                current,
                remote_rtp_host=str(parsed["connection_ip"]),
                remote_rtp_port=int(parsed["media_port"]),
                send_format=selected.send,
                recv_format=selected.recv,
                recv_formats=receive_formats,
                send_formats=send_formats,
                match_received_codec=self.match_received_codec,
                audio_payloads=audio_payloads,
                video_payloads=video_payloads,
                remote_target_uri=remote_target,
                recv_dtmf_formats=dtmf_direction.recv_formats if dtmf_direction is not None else (),
                send_dtmf_formats=dtmf_direction.send_formats if dtmf_direction is not None else (),
                dtmf_payload_type=dtmf_recv.payload_type if dtmf_recv else None,
                dtmf_clock_rate=dtmf_recv.sample_rate if dtmf_recv else 8000,
                dtmf_events=dtmf_recv.events if dtmf_recv else frozenset(),
                send_dtmf_payload_type=dtmf_send.payload_type if dtmf_send else None,
                send_dtmf_clock_rate=dtmf_send.sample_rate if dtmf_send else None,
                send_dtmf_events=dtmf_send.events if dtmf_send else frozenset(),
                remote_audio_direction=str(parsed["direction"]),
                local_audio_direction=sdp.local_direction_for_offer(
                    parsed["direction"],
                    remote_connection_held=bool(parsed["connection_held"]),
                ),
                remote_audio_connection_held=bool(parsed["connection_held"]),
                video_format=video,
                local_video_format=local_video,
                imageattr_fallback_needed=imageattr_fallback,
                local_video_rtp_port=(local_video_port if video is not None else 0),
                local_video_direction=local_video_direction,
                local_sdp_session_id=session_id,
                local_sdp_session_version=session_version,
                local_sdp_body=answer,
                **remote_video_target.as_remote_video_fields(),
            )
        except (TypeError, ValueError, sdp.SdpError, sip.SipError):
            return None
        return updated, answer

    def _transport_failure(self, err: BaseException, target: str, remote_host: str, remote_sip_port: int) -> str:
        self._invite_transaction_active = False
        sip.mark_sip_event(self, "TRANSPORT_ERROR", 0, str(err))
        _LOGGER.info(
            "SIP transport unreachable target=%s host=%s:%s transport=%s error=%s",
            target,
            remote_host,
            remote_sip_port,
            self.signaling_transport,
            err,
        )
        return "transport_unreachable"

    @staticmethod
    def _retry_after_delay(message: sip.SipMessage) -> float | None:
        """Return one bounded Retry-After delay accepted for an INVITE retry."""

        if message.status_code not in {500, 503}:
            return None
        match = re.match(r"\s*(\d+)", message.header("Retry-After"))
        if match is None:
            return None
        return min(
            max(float(match.group(1)), _MIN_INVITE_RETRY_AFTER),
            _MAX_INVITE_RETRY_AFTER,
        )


    def _refresh_pending_invite_authorization(self, *, advance_nonce: bool = True) -> None:
        """Regenerate qop state when an authenticated INVITE gets a new CSeq."""

        for header, (challenge, _value, count) in tuple(
            self._pending_invite_auth.items()
        ):
            count += int(advance_nonce)
            self._pending_invite_auth[header] = (
                challenge,
                build_digest_authorization(
                    challenge_header=challenge,
                    username=self.username,
                    auth_username=self.auth_username,
                    password=self.password,
                    method="INVITE",
                    uri=self._pending_request_uri,
                    nonce_count=count,
                    body=self._pending_invite_body,
                ),
                count,
            )


    async def _read_response(
        self,
        timeout: float | None,
        *,
        network_only: bool = False,
    ) -> tuple[sip.SipMessage, tuple[str, int]] | None:
        if self._deferred_signaling and not network_only:
            return self._deferred_signaling.pop(0)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, float(timeout))
        while True:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            try:
                if self._reused_flow_responses is not None:
                    raw = await asyncio.wait_for(
                        self._reused_flow_responses.get(), timeout=remaining
                    )
                    if isinstance(raw, tuple):
                        raw, addr = raw
                    else:
                        addr = (self._pending_remote_host, self._pending_remote_sip_port)
                elif self.signaling_transport in {"TCP", "TLS"}:
                    if self.reader is None:
                        return None
                    raw = await asyncio.wait_for(
                        _read_sip_stream_message(self.reader, writer=self.writer), timeout=remaining
                    )
                    if raw is None:
                        return None
                    addr = (self._pending_remote_host, self._pending_remote_sip_port)
                else:
                    raw, addr = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

            if raw is None:
                return None
            if raw == b"\r\n":
                continue
            try:
                message = sip.parse_message(raw)
            except sip.SipError:
                _LOGGER.debug("Malformed SIP message ignored")
                continue
            response_call_id = message.header("Call-ID")
            if (response_call_id != self.dialog_ids.call_id and response_call_id not in self._outside_refer
                    and response_call_id not in self._event_dialogs
                    and self._find_in_dialog_response(message) is None
                    and not (message.is_request and message.method in {"REFER", "NOTIFY"})):
                _LOGGER.debug(
                    "SIP message ignored for stale call_id=%s current=%s",
                    response_call_id or "(empty)",
                    self.dialog_ids.call_id,
                )
                continue
            return message, addr


    def _advertise_media(self, body: str, endpoints=None) -> str:
        from .core.media_address import apply_media_endpoints
        return apply_media_endpoints(body, self.media_endpoints if endpoints is None else endpoints)


    async def _handle_dialog_media_request(self, request, host, port):
        if not self._request_matches_dialog(request, host, request.method):
            self._send_response_to_request(request, host, port, 481, "Call/Transaction Does Not Exist")
            return None
        cached = self._find_in_dialog_response(request)
        if cached is not None:
            self._send_response_to_request(request, host, port, cached.status, cached.reason,
                                           extra_headers=cached.extra_headers, body=cached.body)
            return None
        if self._local_offer_lock.locked() or self._prepared_reinvite is not None:
            if self._remote_offer_request is not None:
                if self._same_in_dialog_transaction(request, self._remote_offer_request):
                    self._send_response_to_request(request, host, port, 100, "Trying")
                    return None
                headers = (("Retry-After", str(secrets.randbelow(11))),)
                self._send_response_to_request(request, host, port, 500, "Server Internal Error", extra_headers=headers)
                self._remember_in_dialog_response(request, 500, "Server Internal Error", extra_headers=headers)
            else:
                self._send_response_to_request(request, host, port, 491, "Request Pending")
            return None
        async with self._local_offer_lock:
            self._remote_offer_request = request
            try:
                return await self._prepare_dialog_media_request(request, host, port)
            finally:
                self._remote_offer_request = None

    async def _prepare_dialog_media_request(
        self,
        request: sip.SipMessage,
        host: str,
        port: int,
    ) -> str | None:
        """Answer one remote re-INVITE/UPDATE and preserve the old session on failure."""

        expected_dialog = self.dialog
        method = str(request.method or "").upper()
        if not self._request_matches_dialog(request, host, method):
            self._send_response_to_request(
                request, host, port, 481, "Call/Transaction Does Not Exist"
            )
            return None
        cached = self._find_in_dialog_response(request)
        if cached is not None:
            self._send_response_to_request(
                request,
                host,
                port,
                cached.status,
                cached.reason,
                extra_headers=cached.extra_headers,
                body=cached.body,
            )
            return None
        try:
            request_cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return None
        try:
            requested_timer = sip.negotiate_uas_session_timer(request)
        except sip.SipSessionIntervalTooSmall as err:
            self._send_response_to_request(
                request,
                host,
                port,
                422,
                "Session Interval Too Small",
                extra_headers=(("Min-SE", str(err.minimum)),),
            )
            return None
        except sip.SipError:
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return None
        if request_cseq.number <= self._remote_cseq:
            self._send_response_to_request(
                request,
                host,
                port,
                500,
                "Server Internal Error",
                extra_headers=(("Retry-After", "1"),),
            )
            return None

        try:
            refreshed_remote_target = sip.contact_target_uri(request)
        except sip.SipError:
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return None

        status = 0
        reason = ""
        body = b""
        updated: SipDialog | None = None
        commit: DialogMediaCommit | None = None
        rollback: DialogMediaCommit | None = None
        on_committed = None
        prepared_endpoints = None

        async def discard_prepared():
            nonlocal rollback
            undo, rollback = rollback, None
            if undo is not None:
                await async_wait_for_cleanup(asyncio.create_task(undo(), name="voip-remote-media-rollback"))
        if not request.body:
            if method == "UPDATE":
                status = 200
                reason = "OK"
            elif self.dialog is None or not self.dialog.local_sdp_body:
                status = 488
                reason = "Not Acceptable Here"
            else:
                self._send_response_to_request(request, host, port, 100, "Trying")
                next_version = self.dialog.local_sdp_session_version + 1
                offer = sdp.rewrite_sdp_origin(
                    self.dialog.local_sdp_body,
                    self.dialog.local_sdp_session_id,
                    next_version,
                )
                self._uas_delayed_offer = _DelayedRemoteOffer(
                    request=request,
                    offer_sdp=offer,
                    remote_target_uri=refreshed_remote_target,
                    local_sdp_session_version=next_version,
                )
                status = 200
                reason = "OK"
                body = offer.encode()
        elif request.header("Content-Type").split(";", 1)[0].strip().lower() != "application/sdp":
            status = 415
            reason = "Unsupported Media Type"
        else:
            prepared = self._answer_remote_offer(request, advertise_media=False)
            if prepared is None or self.dialog is None:
                status = 488
                reason = "Not Acceptable Here"
            else:
                updated, answer = prepared
                if refreshed_remote_target:
                    updated = replace(
                        updated,
                        remote_target_uri=refreshed_remote_target,
                    )
                unchanged = self._same_dialog_media(self.dialog, updated)
                if self.on_media_update is not None:
                    if method == "INVITE":
                        self._send_response_to_request(request, host, port, 100, "Trying")
                    try:
                        prepared_update = await self.on_media_update(
                            self.dialog, updated, method
                        )
                        if isinstance(prepared_update, PreparedDialogMediaUpdate):
                            commit = prepared_update.commit
                            rollback = prepared_update.rollback
                            on_committed = prepared_update.on_committed
                            prepared_endpoints = (dict(prepared_update.answer_media_endpoints)
                                if prepared_update.answer_media_endpoints is not None else None)
                            answer_video_format = (
                                prepared_update.answer_video_format
                            )
                            if prepared_update.answer_video_rtp_port is not None:
                                updated = replace(
                                    updated,
                                    local_video_rtp_port=int(
                                        prepared_update.answer_video_rtp_port
                                    ),
                                )
                            if answer_video_format is not None:
                                video_pair = sdp.video_offer_answer_directional(
                                    updated.video_format,
                                    answer_video_format,
                                )
                                if video_pair is None:
                                    commit = None
                                else:
                                    answer = sdp.build_answer_directional(
                                        self.local_ip,
                                        self.media_ip,
                                        updated.local_rtp_port,
                                        updated.send_format,
                                        updated.recv_format,
                                        recv_formats=updated.recv_formats, send_formats=updated.send_formats,
                                        audio_rtcp=False,
                                        dtmf_direction=updated.dtmf_direction,
                                        remote_sdp=request.body,
                                        video_port=updated.local_video_rtp_port,
                                        video_format=answer_video_format,
                                        audio_direction=updated.local_audio_direction,
                                        video_direction=updated.local_video_direction,
                                    )
                                    updated = replace(
                                        updated,
                                        video_format=video_pair.send,
                                        local_video_format=video_pair.recv,
                                        local_sdp_body=answer,
                                    )
                            elif prepared_update.answer_video_rtp_port is not None:
                                answer = sdp.build_answer_directional(
                                    self.local_ip,
                                    self.media_ip,
                                    updated.local_rtp_port,
                                    updated.send_format,
                                    updated.recv_format,
                                    recv_formats=updated.recv_formats, send_formats=updated.send_formats,
                                    audio_rtcp=False,
                                    dtmf_direction=updated.dtmf_direction,
                                    remote_sdp=request.body,
                                    video_port=updated.local_video_rtp_port,
                                    video_format=updated.video_format,
                                    audio_direction=updated.local_audio_direction,
                                    video_direction=updated.local_video_direction,
                                )
                                updated = replace(updated, local_sdp_body=answer)
                        else:
                            commit = prepared_update
                    except asyncio.CancelledError:
                        await discard_prepared()
                        raise
                    except Exception:
                        _LOGGER.exception(
                            "SIP remote media update preparation failed call_id=%s method=%s",
                            self.dialog_ids.call_id,
                            method,
                        )
                        status = 500
                        reason = "Server Internal Error"
                    else:
                        if commit is None and not unchanged:
                            status = 488
                            reason = "Not Acceptable Here"
                        else:
                            status = 200
                            reason = "OK"
                elif unchanged:
                    status = 200
                    reason = "OK"
                else:
                    status = 488
                    reason = "Not Acceptable Here"

        if 200 <= status < 300 and updated is not None:
            try:
                answer = self._advertise_media(answer, prepared_endpoints)
                session_id = int(expected_dialog.local_sdp_session_id or self._sdp_session_id)
                session_version = int(expected_dialog.local_sdp_session_version)
                answer = sdp.rewrite_sdp_origin(answer, session_id, session_version)
                if sdp.sdp_description_changed(expected_dialog.local_sdp_body, answer):
                    session_version += 1
                    answer = sdp.rewrite_sdp_origin(answer, session_id, session_version)
                body = answer.encode("utf-8")
                updated = replace(updated, local_sdp_body=answer,
                    local_sdp_session_id=session_id, local_sdp_session_version=session_version)
            except asyncio.CancelledError:
                await discard_prepared()
                raise
            except Exception:
                _LOGGER.exception("SIP remote media answer serialization failed call_id=%s method=%s",
                    self.dialog_ids.call_id, method)
                status, reason = 500, "Server Internal Error"

        if self.dialog is not expected_dialog:
            await discard_prepared()
            self._send_response_to_request(request, host, port, 481, "Call/Transaction Does Not Exist")
            return "remote_hangup" if self.dialog is None else None
        if not 200 <= status < 300:
            await discard_prepared()
        if status == 415:
            extra_headers = (("Accept", "application/sdp"),)
        elif status == 488 and self.on_media_update is None:
            extra_headers = ((
                "Warning",
                f'399 {self.local_ip} "Session renegotiation is not supported by the active media owner"',
            ),)
        else:
            extra_headers = ()
        try:
            sent = self._send_response_to_request(
                request,
                host,
                port,
                status,
                reason,
                extra_headers=extra_headers,
                body=body if 200 <= status < 300 else b"",
            )
        except BaseException:
            await discard_prepared()
            raise
        if not sent:
            self._uas_delayed_offer = None
            await discard_prepared()
            return "transport_unreachable"
        if self._uas_delayed_offer is not None and self.dialog is not None:
            self.dialog.local_sdp_session_version = (
                self._uas_delayed_offer.local_sdp_session_version
            )
            self.dialog.local_sdp_body = self._uas_delayed_offer.offer_sdp
        self._remember_in_dialog_response(
            request,
            status,
            reason,
            extra_headers=extra_headers,
            body=body if 200 <= status < 300 else b"",
        )
        self._remote_cseq = max(self._remote_cseq, request_cseq.number)
        if method == "INVITE" and 200 <= status < 300:
            # Arm before the async media commit: an ACK is allowed to arrive
            # immediately after the 2xx is emitted.
            self._arm_uas_invite_2xx(
                request,
                host,
                port,
                status,
                reason,
                extra_headers=extra_headers,
                body=body,
            )
        if 200 <= status < 300 and updated is not None:
            if not await apply_remote_offer_media(commit, rollback):
                _LOGGER.error(
                    "SIP remote media update commit failed call_id=%s method=%s",
                    self.dialog_ids.call_id,
                    method,
                )
                self._uas_invite_2xx.cancel()
                await self._terminate_confirmed_dialog()
                return "media_update_failed"
            if self._uas_invite_ack_timeout.is_set() or self.dialog is None:
                return "ack_timeout"
            if prepared_endpoints is not None:
                self.media_endpoints = dict(prepared_endpoints)
                updated._local_media_endpoints = dict(self.media_endpoints)
            self.dialog = updated
            notify_media_commit(on_committed, updated)
        elif (
            200 <= status < 300
            and refreshed_remote_target
            and self.dialog is not None
            and self._uas_delayed_offer is None
        ):
            self.dialog = replace(
                self.dialog,
                remote_target_uri=refreshed_remote_target,
            )
        if (
            200 <= status < 300
            and self.dialog is not None
            and self._uas_delayed_offer is None
        ):
            self.dialog.session_timer.configure(
                requested_timer,
                local_role="uas",
                now=asyncio.get_running_loop().time(),
            )
            self._dialog_changed.set()
        if (
            method == "UPDATE"
            and 200 <= status < 300
            and self.dialog is not None
            and self.dialog.peer_supports_from_change
        ):
            try:
                connected_uri = str(sip.parse_sip_uri(request.header("From")))
            except (TypeError, ValueError, sip.SipError):
                connected_uri = ""
            if connected_uri and connected_uri != self.dialog.remote_uri:
                self.dialog = replace(self.dialog, remote_uri=connected_uri)
            connected_name = sip.name_addr_display_name(request.header("From"))
            connected_party = connected_name or sip.name_addr_identity(
                request.header("From")
            )
            if connected_party:
                self._dialog_remote_display_name = connected_name
                self.connected_party = connected_party
                callback = self.on_connected_identity
                if callback is not None:
                    try:
                        callback(connected_party, connected_uri)
                    except Exception as err:  # noqa: BLE001 - observers cannot break the dialog.
                        _LOGGER.warning(
                            "SIP connected identity callback failed: %s",
                            err,
                        )
        return None


    async def accept_owned_request(self, request, addr, reply):
        """Dispatch a known dialog arriving on another owned listening flow."""
        call_id = request.header("Call-ID")
        if (call_id == self.dialog_ids.call_id and self._invite_transaction_active and self.dialog is None
                and sip.extract_tag(request.header("To")) == self.dialog_ids.local_tag):
            if any(self._same_in_dialog_transaction(request, item[0]) for item in self._deferred_owned_requests):
                return True
            if len(self._deferred_owned_requests) >= 8:
                reply(sip.build_uas_response(request, 503, "Service Unavailable"))
            else:
                self._deferred_owned_requests.append((request, addr, reply))
            return True
        known = self.knows_control_request(request, addr)
        if request.method == "REFER" and not sip.extract_tag(request.header("To")):
            try:
                target = sip_transfer.request_target_dialog(request)
                known = (target is not None and self.dialog is not None and
                         (target.call_id, target.local_tag, target.remote_tag) ==
                         (self.dialog_ids.call_id, self.dialog_ids.local_tag, self.dialog_ids.remote_tag))
            except sip.SipError:
                return False
        elif call_id == self.dialog_ids.call_id and self.dialog is not None:
            known = self._request_matches_dialog(request, addr[0], request.method)
        if not known and self._find_in_dialog_response(request) is None:
            return False
        self._ensure_dialog_dispatcher()
        token = self._reply_path.set(reply)
        try:
            terminal = await self._dispatch_in_dialog_request(request, addr,
                local_offer_pending=self._local_offer_lock.locked() and self._remote_offer_request is None)
            if terminal is not None:
                self._publish_dialog_terminal(terminal)
            return True
        finally:
            self._reply_path.reset(token)

    async def _flush_owned_requests(self):
        pending = self._deferred_owned_requests
        self._deferred_owned_requests = []
        for request, addr, reply in pending:
            if not await self.accept_owned_request(request, addr, reply):
                reply(sip.build_uas_response(request, 481, "Call/Transaction Does Not Exist"))

    def _publish_dialog_terminal(self, terminal):
        if self._dialog_result is None:
            return
        if not self._dialog_result.done():
            self._dialog_result.set_result(terminal)
        for mailbox in tuple(self._dialog_dispatcher.mailboxes):
            if mailbox.dialog_scoped:
                mailbox.deliver(None)

    def _observe_events(self):
        if self.observe_event_owner is not None:
            self.observe_event_owner(self)

    def knows_control_request(self, request, addr):
        if request.method not in {"ACK", "BYE", "CANCEL", "UPDATE", "NOTIFY", "PRACK", "SUBSCRIBE"}:
            return False
        notifier = self._refer_notifiers.get(request.header("Call-ID"))
        if request.method == "SUBSCRIBE" and notifier is not None:
            return DialogKey.from_uas_request(request) == notifier.dialog.key
        subscription = self._outside_refer.get(request.header("Call-ID"))
        if request.method == "NOTIFY" and subscription is not None:
            key = subscription.dialog.key
            return (sip.extract_tag(request.header("To")) == key.local_tag
                    and bool(sip.extract_tag(request.header("From")))
                    and (not key.remote_tag or sip.extract_tag(request.header("From")) == key.remote_tag))
        return self._request_matches_dialog(request, addr[0], request.method)

    def _ensure_dialog_dispatcher(self):
        if self._dialog_dispatcher is None:
            self._dialog_result = asyncio.get_running_loop().create_future()

            async def request(message, addr):
                terminal = await self._dispatch_in_dialog_request(
                    message, addr, local_offer_pending=self._local_offer_lock.locked() and self._remote_offer_request is None,
                )
                if terminal is not None:
                    self._publish_dialog_terminal(terminal)

            def failed(error):
                _LOGGER.warning("SIP dispatcher failed call_id=%s: %s", self.dialog_ids.call_id, error)
                if not self._dialog_result.done():
                    self._dialog_result.set_result("transport_unreachable")
                if self._refer_subscription is not None:
                    self._refer_subscription.close()

            self._dialog_dispatcher = SipMessageDispatcher(
                self._read_response, request,
                lambda message, _addr: self._ack_retransmitted_invite_2xx(message),
                failed,
                lambda message, addr: self._send_response_to_request(
                    message, *addr, 503, "Service Unavailable"),
                self.knows_control_request,
            )
            self._dialog_dispatcher.start()
        return self._dialog_dispatcher

    async def _run_built_dialog_request(
        self, request, host: str, port: int, *, method: str, timeout: float,
        active: Callable[[], bool], on_provisional=None, on_final=None, dialog_scoped=True,
        on_sent: Callable[[], None] | None = None,
        network_target=None,
    ) -> sip.SipMessage | None:
        """Await this transaction's mailbox; only the dispatcher reads SIP."""
        if network_target is None:
            prepared = await self._prepare_dialog_signaling_target(request.routing.next_hop_uri, ())
            if prepared is not None:
                if not active():
                    raise ConnectionAbortedError("SIP dialog changed during target resolution")
                network_target = prepared[1]
                if dialog_scoped:
                    self._commit_dialog_signaling_target(prepared)
        dispatcher = self._ensure_dialog_dispatcher()
        def matches(message):
            return sip.response_matches_dialog_transaction(message, request.ids, method, creating_dialog=not dialog_scoped)
        mailbox = dispatcher.register(matches)
        mailbox.dialog_scoped = dialog_scoped
        def sent():
            sip.mark_sip_event(self, method)
            if on_sent is not None:
                on_sent()
        try:
            response = await async_run_dialog_request_transaction(
                send=lambda: (self._send_dialog_request(request.raw, host, port, network_target=network_target)
                              if network_target is not None else self._send_dialog_request(request.raw, host, port)),
                read=mailbox.receive, matches=matches, active=active,
                transport=self.signaling_transport, timeout=timeout, method=method,
                on_provisional=on_provisional, on_final=on_final,
                on_sent=sent,
            )
            if response is None and dialog_scoped and self._dialog_result.done():
                raise ConnectionAbortedError(self._dialog_result.result())
            return response
        finally:
            dispatcher.unregister(mailbox)


    def _dialog_is_current(self, expected: SipDialog) -> bool:
        current = self.dialog
        return bool(
            current is not None
            and (
                current is expected
                or (
                    current.call_id == expected.call_id
                    and current.remote_tag == expected.remote_tag
                )
            )
        )

    async def refer(
        self, target: sip_transfer.SipReferTarget, *, timeout: float = 30.0,
    ) -> SipTransferResult:
        """Bound serialization, authentication and subscription by one deadline."""
        if self.dialog is None or self._refer_subscription is not None:
            return SipTransferResult(False, 0, "unavailable")
        self._ensure_dialog_dispatcher()
        subscription = sip_transfer.ReferSubscription()
        self._refer_subscription = subscription
        peer = self.dialog
        target_uri = sip.parse_sip_uri(peer.remote_target_uri or peer.remote_uri)
        modern = any(key.casefold() == "gr" for key, _ in target_uri.params)
        usage = self._event_dialog(peer)
        if modern:
            usage = SubscriptionDialog(
                key=DialogKey(sip.make_call_id("refer"), sip.make_tag(), ""),
                local_uri=peer.local_uri, remote_uri=str(target_uri), remote_target_uri=str(target_uri),
                remote_cseq=0,
            )
        subscription.dialog = usage
        self._outside_refer[usage.key.call_id] = subscription
        self._observe_events()
        release_usage = None
        try:
            if modern and self.bind_signaling_usage is not None:
                release_usage = self.bind_signaling_usage(usage.key.call_id)
            async with asyncio.timeout(float(timeout)):
                response = await self._send_in_dialog_request(
                    "REFER", extra_headers=(
                        ("Refer-To", target.as_header()),
                        ("Referred-By", target.referred_by or sip.format_name_addr(self.dialog.local_uri)),
                        *target.referral_body_headers,
                        *((("Target-Dialog", str(sip_transfer.SipTargetDialog(
                            self.dialog_ids.call_id, self.dialog_ids.remote_tag, self.dialog_ids.local_tag))),
                           ("Require", "tdialog")) if modern else ()),
                    ), body=target.referral_body, content_type=target.referral_content_type,
                    timeout=min(float(timeout), 8.0), usage=usage,
                )
                status = int(response.status_code or 0) if response is not None else 0
                if not 200 <= status < 300:
                    return SipTransferResult(False, status, "rejected")
                return await asyncio.shield(subscription.result)
        except TimeoutError:
            return SipTransferResult(False, 0, "timeout")
        except ConnectionAbortedError:
            return SipTransferResult(False, 0, "terminated")
        finally:
            if release_usage is not None:
                release_usage()
            subscription.close()
            if usage is not None:
                self._outside_refer.pop(usage.key.call_id, None)
            if self._refer_subscription is subscription:
                self._refer_subscription = None
            self._release_event_dialog(usage.key.call_id)

    def _event_dialog(self, dialog):
        state = self._event_dialogs.get(self.dialog_ids.call_id)
        if state is None:
            state = SubscriptionDialog(key=DialogKey(self.dialog_ids.call_id, self.dialog_ids.local_tag, self.dialog_ids.remote_tag),
                local_uri=dialog.local_uri, remote_uri=dialog.remote_uri,
                remote_target_uri=dialog.remote_target_uri or dialog.remote_uri,
                route_set=dialog.route_set, remote_cseq=self._remote_cseq)
            self._event_dialogs[state.key.call_id] = state
            self._observe_events()
        return state

    def _release_event_dialog(self, call_id):
        if call_id in self._outside_refer:
            return
        if call_id == self.dialog_ids.call_id and self._incoming_refer_task is not None and not self._incoming_refer_task.done():
            return
        self._event_dialogs.pop(call_id, None)
        if not self._event_dialogs and not self._outside_refer and self.release_event_owner is not None:
            self.release_event_owner(self)

    async def wait_for_dialog_termination(self, timeout: float | None = None) -> str:
        """Observe dialog lifetime without owning or competing for SIP reads."""
        if self.dialog is None:
            return "not_in_call"
        self._ensure_dialog_dispatcher()
        if self._dialog_monitor_task is None:
            self._dialog_monitor_task = asyncio.create_task(
                self._monitor_dialog(), name=f"voip-sip-lifetime-{self.dialog_ids.call_id}",
            )
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(self._dialog_monitor_task)
        except TimeoutError:
            return "timeout"

    async def _send_session_refresh(self, method, *, extra_headers=()):
        """Refresh through the existing request or serialized offer owner."""
        current = self.dialog
        if current is None:
            return None
        remaining = current.session_timer.deadline - asyncio.get_running_loop().time()
        timeout = min(8.0, remaining) if current.session_timer.deadline else 8.0
        if timeout <= 0:
            return None
        if method == "UPDATE":
            return await self._send_in_dialog_request(method, extra_headers=extra_headers, timeout=timeout)
        response = None

        def received(message):
            nonlocal response
            response = message

        try:
            candidate = await self.async_prepare_video_reinvite(
                local_video_rtp_port=current.local_video_rtp_port,
                video_formats=tuple(sdp.offered_video_formats(current.local_sdp_body)),
                video_direction=current.local_video_direction,
                timeout=timeout, expected_dialog=current, session_refresh=True,
                extra_headers=extra_headers, response_callback=received)
        except (ValueError, sdp.SdpError, sip.SipError):
            return None
        if candidate is None:
            return response if response is not None and int(response.status_code or 0) >= 300 else None
        commit = rollback = on_committed = None
        try:
            if self.on_media_update is not None:
                update = await self.on_media_update(current, candidate, "INVITE")
                if isinstance(update, PreparedDialogMediaUpdate):
                    commit, rollback, on_committed = update.commit, update.rollback, update.on_committed
                else:
                    commit = update
            if commit is None and not self._same_dialog_media(current, candidate):
                self.abort_prepared_reinvite(current, candidate)
                return None

            async def publish():
                if not self.commit_prepared_reinvite(current, candidate):
                    raise ConnectionAbortedError("SIP refresh dialog changed before commit")
                notify_media_commit(on_committed, candidate)

            async def commit_current():
                if self.dialog is not current:
                    raise ConnectionAbortedError("SIP refresh dialog changed during preparation")
                if commit is not None:
                    await commit()

            if not await apply_remote_offer_media(commit_current, rollback, finalize=publish):
                self.abort_prepared_reinvite(current, candidate)
                return None
            return response
        except asyncio.CancelledError:
            self.abort_prepared_reinvite(current, candidate)
            raise
        except Exception:
            self.abort_prepared_reinvite(current, candidate)
            return None

    async def _monitor_dialog(self) -> str:
        ack_timeout = asyncio.create_task(self._uas_invite_ack_timeout.wait())
        changed = None
        try:
            while self.dialog is not None:
                # Clear before reading the contract: a subsequent publication
                # must wake this wait, including when the timer was disabled.
                self._dialog_changed.clear()
                timer = self.dialog.session_timer
                driver = SessionTimerDriver(timer, self._send_session_refresh, "uac", asyncio.get_running_loop().time,
                    "UPDATE" if getattr(self.dialog, "peer_supports_update", False) else "INVITE")
                timeout = max(0.0, driver.deadline - asyncio.get_running_loop().time()) if driver.deadline else None
                changed = asyncio.create_task(self._dialog_changed.wait())
                try:
                    done, _ = await asyncio.wait((self._dialog_result, ack_timeout, changed), timeout=timeout,
                                                 return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
                if self._dialog_result in done:
                    return self._dialog_result.result()
                if ack_timeout in done:
                    return "ack_timeout"
                if self.dialog is None:
                    return "remote_hangup"
                if self._dialog_changed.is_set():
                    continue
                driver = SessionTimerDriver(self.dialog.session_timer, self._send_session_refresh,
                    "uac", asyncio.get_running_loop().time,
                    "UPDATE" if getattr(self.dialog, "peer_supports_update", False) else "INVITE")
                try:
                    result = await driver.advance()
                except ConnectionAbortedError as error:
                    return str(error) or "remote_hangup"
                if result in {"session_timer_expired", "session_timer_failed"}:
                    await self._terminate_confirmed_dialog()
                    return result
            return "remote_hangup"
        finally:
            ack_timeout.cancel()
            await asyncio.gather(ack_timeout, return_exceptions=True)

    async def _dispatch_in_dialog_request(
        self,
        msg: sip.SipMessage,
        addr: tuple[str, int],
        *,
        local_offer_pending: bool = False,
    ) -> str | None:
        """Dispatch one sequential request without creating a second reader."""

        if msg.method in {"BYE", "REFER", "NOTIFY", "INFO", "OPTIONS", "SUBSCRIBE"}:
            cached = self._find_in_dialog_response(msg)
            if cached is not None:
                self._send_response_to_request(msg, *addr, cached.status, cached.reason,
                    extra_headers=cached.extra_headers, body=cached.body, to_tag=cached.to_tag)
                return None
        if msg.method == "SUBSCRIBE":
            notifier = self._refer_notifiers.get(msg.header("Call-ID"))
            if notifier is None:
                self._send_response_to_request(msg, *addr, 481, "Subscription Does Not Exist")
                return None
            if notifier.dialog.key.call_id == self.dialog_ids.call_id:
                notifier.dialog.remote_cseq = max(notifier.dialog.remote_cseq, self._remote_cseq)
            status, reason, headers = notifier.refresh(msg)
            self._send_response_to_request(msg, *addr, status, reason, extra_headers=headers,
                                           to_tag=notifier.dialog.key.local_tag)
            self._remember_in_dialog_response(msg, status, reason, extra_headers=headers,
                                               to_tag=notifier.dialog.key.local_tag)
            if status == 200 and notifier.dialog.key.call_id == self.dialog_ids.call_id:
                self._remote_cseq = notifier.dialog.remote_cseq
            return None
        if msg.method == "BYE":
            _LOGGER.debug("SIP RX BYE from %s:%s", addr[0], addr[1])
            if not self._request_matches_dialog(msg, addr[0], "BYE"):
                self._send_response_to_request(
                    msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist"
                )
                return None
            try:
                bye_cseq = sip.parse_cseq(msg.header("CSeq"))
            except (TypeError, ValueError, sip.SipError):
                self._send_response_to_request(
                    msg, addr[0], addr[1], 400, "Bad Request"
                )
                return None
            if bye_cseq.number <= self._remote_cseq:
                self._send_response_to_request(
                    msg,
                    addr[0],
                    addr[1],
                    500,
                    "Server Internal Error",
                    extra_headers=(("Retry-After", "1"),),
                )
                return None
            self._uas_invite_2xx.cancel()
            self._uas_delayed_offer = None
            self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
            self._remember_in_dialog_response(msg, 200, "OK")
            self._remote_cseq = bye_cseq.number
            self.dialog = None
            return "remote_hangup"
        if msg.method == "CANCEL":
            self._send_response_to_request(
                msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist"
            )
            return None
        if msg.method == "ACK":
            if self._acknowledges_uas_invite_2xx(msg, addr[0]):
                _LOGGER.info(
                    "SIP RX ACK remote re-INVITE call_id=%s",
                    self.dialog_ids.call_id,
                )
                return await self._commit_delayed_offer_ack(msg)
            return None
        if msg.method in {"INVITE", "UPDATE"}:
            if local_offer_pending:
                if not self._request_matches_dialog(msg, addr[0], msg.method):
                    self._send_response_to_request(
                        msg,
                        addr[0],
                        addr[1],
                        481,
                        "Call/Transaction Does Not Exist",
                    )
                    return None
                self._send_response_to_request(
                    msg, addr[0], addr[1], 491, "Request Pending"
                )
                return None
            return await self._handle_dialog_media_request(msg, addr[0], addr[1])
        if msg.method == "REFER":
            self._handle_refer_request(msg, addr)
            return None
        if msg.method == "NOTIFY":
            return self._handle_refer_notify(msg, addr)
        if msg.method in {"INFO", "OPTIONS"}:
            if not self._request_matches_dialog(msg, addr[0], msg.method):
                self._send_response_to_request(
                    msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist"
                )
                return None
            cached = self._find_in_dialog_response(msg)
            if cached is not None:
                self._send_response_to_request(
                    msg,
                    addr[0],
                    addr[1],
                    cached.status,
                    cached.reason,
                    extra_headers=cached.extra_headers,
                    body=cached.body,
                )
                return None
            try:
                request_cseq = sip.parse_cseq(msg.header("CSeq"))
            except (TypeError, ValueError, sip.SipError):
                self._send_response_to_request(
                    msg, addr[0], addr[1], 400, "Bad Request"
                )
                return None
            if request_cseq.number <= self._remote_cseq:
                self._send_response_to_request(
                    msg,
                    addr[0],
                    addr[1],
                    500,
                    "Server Internal Error",
                    extra_headers=(("Retry-After", "1"),),
                )
                return None
            if msg.method == "INFO":
                from .dtmf import parse_sip_info_digit, parse_sip_info_duration

                if sip.is_picture_fast_update(msg.header("Content-Type"), msg.body) and self.on_info_video is not None:
                    await self.on_info_video()
                digit = parse_sip_info_digit(msg.header("Content-Type"), msg.body)
                if digit and self.on_info_dtmf is not None:
                    try:
                        self.on_info_dtmf(digit, parse_sip_info_duration(msg.body))
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning("SIP INFO DTMF callback failed: %s", err)
            self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
            self._remember_in_dialog_response(msg, 200, "OK")
            self._remote_cseq = request_cseq.number
            return None
        self._send_response_to_request(
            msg, addr[0], addr[1], 405, "Method Not Allowed"
        )
        return None

    def _handle_refer_request(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> None:
        """Accept one in-dialog REFER and hand it to the session owner."""

        host, port = addr
        if request.header("Call-ID") != self.dialog_ids.call_id:
            return self._handle_external_refer(request, addr)
        if not self._request_matches_dialog(request, host, "REFER"):
            self._send_response_to_request(
                request, host, port, 481, "Call/Transaction Does Not Exist"
            )
            return
        cached = self._find_in_dialog_response(request)
        if cached is not None:
            self._send_response_to_request(
                request,
                host,
                port,
                cached.status,
                cached.reason,
                extra_headers=cached.extra_headers,
                body=cached.body,
            )
            return
        try:
            request_cseq = sip.parse_cseq(request.header("CSeq"))
            target = sip_transfer.request_refer_to(request)
        except (TypeError, ValueError, sip.SipError):
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return
        if request_cseq.number <= self._remote_cseq:
            self._send_response_to_request(
                request,
                host,
                port,
                500,
                "Server Internal Error",
                extra_headers=(("Retry-After", "1"),),
            )
            return
        if self.on_refer is None or (
            self._incoming_refer_task is not None
            and not self._incoming_refer_task.done()
        ):
            self._send_response_to_request(request, host, port, 603, "Decline")
            self._remember_in_dialog_response(request, 603, "Decline")
            return
        self._send_response_to_request(request, host, port, 200, "OK")
        self._remember_in_dialog_response(request, 200, "OK")
        self._remote_cseq = request_cseq.number
        work = self._run_incoming_refer(target, request_cseq.number, self._event_dialog(self.dialog))
        self._incoming_refer_dialog = replace(self.dialog)
        try:
            task = asyncio.create_task(work, name=f"voip-sip-refer-{self.dialog_ids.call_id}")
        except BaseException:
            self._incoming_refer_dialog = None
            work.close()
            raise
        self._incoming_refer_task = task
        task.add_done_callback(self._incoming_refer_done)

    def _handle_external_refer(self, request, addr):
        cached = self._find_in_dialog_response(request)
        if cached is not None:
            self._send_response_to_request(request, *addr, cached.status, cached.reason, to_tag=cached.to_tag)
            return
        try:
            target_dialog = sip_transfer.request_target_dialog(request)
            target = sip_transfer.request_refer_to(request)
            sequence = sip.parse_cseq(request.header("CSeq"))
            if sequence.method != "REFER" or sip.extract_tag(request.header("To")):
                raise sip.SipError("invalid outside-dialog REFER")
            if (self.dialog is None or target_dialog is None
                    or (target_dialog.call_id, target_dialog.local_tag, target_dialog.remote_tag)
                    != (self.dialog_ids.call_id, self.dialog_ids.local_tag, self.dialog_ids.remote_tag)):
                self._send_response_to_request(request, *addr, 481, "Call/Transaction Does Not Exist")
                return
            tag = sip.make_tag()
            usage = SubscriptionDialog.from_request(request, tag)
            if not usage.key.remote_tag or not usage.remote_target_uri:
                raise sip.SipError("REFER requires From tag and Contact")
        except (TypeError, ValueError, sip.SipError):
            self._send_response_to_request(request, *addr, 400, "Bad Request")
            return
        if self.on_refer is None or (self._incoming_refer_task is not None and not self._incoming_refer_task.done()):
            self._send_response_to_request(request, *addr, 603, "Decline")
            return
        self._send_response_to_request(request, *addr, 200, "OK", to_tag=tag)
        self._remember_in_dialog_response(request, 200, "OK", to_tag=tag)
        self._event_dialogs[usage.key.call_id] = usage
        self._observe_events()
        work = self._run_incoming_refer(target, sequence.number, usage)
        self._incoming_refer_dialog = replace(self.dialog)
        try:
            task = asyncio.create_task(work, name=f"voip-sip-refer-{usage.key.call_id}")
        except BaseException:
            self._incoming_refer_dialog = None
            work.close()
            raise
        self._incoming_refer_task = task
        task.add_done_callback(lambda completed: self._incoming_refer_done(completed, usage.key.call_id))

    @property
    def incoming_refer_dialog(self) -> SipDialog | None:
        """Borrow the accepted media snapshot while its REFER usage is pending."""
        task = self._incoming_refer_task
        return self._incoming_refer_dialog if task is not None and not task.done() else None

    def observe_refer_operation(self, task) -> bool:
        """Keep this dialog's lifetime tied to an accepted external event usage."""
        if self.dialog is None or self.incoming_refer_dialog is not None:
            return False
        self._incoming_refer_dialog = replace(self.dialog)
        self._incoming_refer_task = task
        task.add_done_callback(self._incoming_refer_done)
        return True

    def _incoming_refer_done(self, task: asyncio.Task[None], usage_id: str = "") -> None:
        if self._incoming_refer_task is task:
            self._incoming_refer_task = None
            self._incoming_refer_dialog = None
        self._release_event_dialog(usage_id or self.dialog_ids.call_id)
        if task.cancelled():
            notifier = self._refer_notifiers.get(usage_id or self.dialog_ids.call_id)
            if notifier is not None:
                notifier.cancel()
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning(
                "SIP incoming REFER failed call_id=%s error=%s",
                self.dialog_ids.call_id,
                error,
            )

    def _run_incoming_refer(
        self,
        target: sip_transfer.SipReferTarget,
        refer_cseq: int,
        usage: SubscriptionDialog | None = None,
    ) -> Awaitable[None]:
        """Report progress and the final transfer outcome with NOTIFY."""

        usage = usage or self._event_dialog(self.dialog)
        async def send(headers, body):
            return await self._send_in_dialog_request("NOTIFY", extra_headers=headers,
                body=body, content_type="message/sipfrag", usage=usage)

        async def action():
            return await self.on_refer(target) if self.on_refer else 603

        self._incoming_refer_count += 1
        def observation_ended():
            if self._refer_notifiers.get(usage.key.call_id) is notifier:
                self._refer_notifiers.pop(usage.key.call_id, None)
        notifier = sip_transfer.ReferNotifier(usage, refer_cseq, send,
            allow_missing_id=usage.key.call_id != self.dialog_ids.call_id or self._incoming_refer_count == 1,
            on_end=observation_ended)
        self._refer_notifiers[usage.key.call_id] = notifier
        async def run():
            try:
                await sip_transfer.run_refer_action(action, notifier)
            finally:
                await notifier.close()
                self._refer_notifiers.pop(usage.key.call_id, None)

        return run()

    def _handle_refer_notify(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> None:
        """Acknowledge one RFC 3515 transfer progress notification."""

        host, port = addr
        cached = self._find_in_dialog_response(request)
        if cached is not None:
            self._send_response_to_request(
                request,
                host,
                port,
                cached.status,
                cached.reason,
                extra_headers=cached.extra_headers,
                body=cached.body,
            )
            return None
        subscription = self._outside_refer.get(request.header("Call-ID"))
        if subscription is not None:
            if request.header("Call-ID") == self.dialog_ids.call_id:
                subscription.dialog.remote_cseq = max(subscription.dialog.remote_cseq, self._remote_cseq)
            status, reason = subscription.receive_dialog_notify(request)
            self._send_response_to_request(request, host, port, status, reason)
            if status == 200:
                self._remember_in_dialog_response(request, status, reason)
                if request.header("Call-ID") == self.dialog_ids.call_id:
                    self._remote_cseq = subscription.dialog.remote_cseq
            return None
        self._send_response_to_request(request, host, port, 481, "Subscription Does Not Exist")
        return None

    def _dialog_candidate_from_answer(
        self,
        current: SipDialog,
        offer: str,
        answer: sip.SipMessage,
        *,
        local_video_rtp_port: int,
        offered_video_formats: tuple[sdp.RtpVideoFormat, ...],
        video_direction: str,
        session_version: int,
    ) -> SipDialog | None:
        """Validate one local re-offer answer without publishing it."""

        try:
            if (
                not answer.body
                or answer.header("Content-Type").split(";", 1)[0].strip().lower()
                != "application/sdp"
            ):
                return None
            sdp.validate_sdp_answer(
                offer,
                answer.body,
                # Some deployed SIP UAs retain their allocated RTP port while
                # answering an m=video 0 removal with a=inactive. The stream
                # is still unambiguously disabled, so normalize that legacy
                # answer at this B2BUA boundary while keeping general SDP
                # answer validation strict.
                allow_inactive_rejected_media_port=local_video_rtp_port == 0,
                # local_video_direction below remains bounded by our offer.
                allow_video_direction_capability=True,
            )
            audio = sdp.negotiate_answer_directional(
                answer.body,
                self.supported_send_formats,
                self.supported_recv_formats,
                local_offer_direction=current.local_audio_direction,
                local_offer_sdp=offer,
                codec_policy=self.codec_policy,
                compatibility=legacy_pcm_compatibility(self.include_dahua_pcm),
                local_capabilities=HA_AUDIO_CAPABILITIES,
                established=sdp.RtpPcmDirection(current.send_format, current.recv_format,
                    current.recv_formats, current.send_formats),
            )
            if audio is None:
                return None
            audio_payloads = _audio_payload_update(current.audio_payloads,
                send=audio.send_formats or (audio.send,), receive=audio.recv_formats or (audio.recv,))
            parsed = sdp.parse_sdp(answer.body)
            video_pair, imageattr_fallback = (
                self._video_answer_or_fallback(
                    answer.body,
                    offered_video_formats,
                    allow_h264_level_capability=True,
                )
                if offered_video_formats and local_video_rtp_port > 0
                else (None, False)
            )
            video_send = video_pair.send if video_pair is not None else None
            video_recv = video_pair.recv if video_pair is not None else None
            remote_video = (
                sdp.parse_video_sdp(answer.body)
                if video_send is not None
                else None
            )
            video_target = sdp.RemoteMediaTarget.from_section(
                remote_video,
                rtcp_mux=False,
            )
            dtmf_pair = audio.dtmf_direction
            dtmf_recv = dtmf_pair.recv if dtmf_pair is not None else None
            dtmf_send = dtmf_pair.send if dtmf_pair is not None else None
            remote_target = sip.contact_target_uri(answer) or current.remote_target_uri
            if not remote_target:
                return None
            local_video_direction = (
                sdp.constrained_video_direction(
                    video_send.direction,
                    allow_send=(
                        video_direction in {"sendonly", "sendrecv"}
                        and not bool(
                            remote_video and remote_video["connection_held"]
                        )
                    ),
                    allow_receive=video_direction in {"recvonly", "sendrecv"},
                )
                if video_send is not None
                else "inactive"
            )
            return replace(
                current,
                remote_rtp_host=str(parsed["connection_ip"]),
                remote_rtp_port=int(parsed["media_port"]),
                send_format=audio.send,
                recv_format=audio.recv,
                recv_formats=audio.recv_formats,
                send_formats=audio.send_formats,
                match_received_codec=self.match_received_codec,
                audio_payloads=audio_payloads,
                video_payloads=current.video_payloads,
                remote_target_uri=remote_target,
                recv_dtmf_formats=dtmf_pair.recv_formats if dtmf_pair is not None else (),
                send_dtmf_formats=dtmf_pair.send_formats if dtmf_pair is not None else (),
                dtmf_payload_type=(
                    dtmf_recv.payload_type if dtmf_recv is not None else None
                ),
                dtmf_clock_rate=(
                    dtmf_recv.sample_rate if dtmf_recv is not None else 8000
                ),
                dtmf_events=(
                    dtmf_recv.events if dtmf_recv is not None else frozenset()
                ),
                send_dtmf_payload_type=(
                    dtmf_send.payload_type if dtmf_send is not None else None
                ),
                send_dtmf_clock_rate=(
                    dtmf_send.sample_rate if dtmf_send is not None else None
                ),
                send_dtmf_events=(
                    dtmf_send.events if dtmf_send is not None else None
                ),
                remote_audio_direction=str(parsed["direction"]),
                local_audio_direction=sdp.local_direction_for_offer(
                    parsed["direction"],
                    remote_connection_held=bool(parsed["connection_held"]),
                ),
                remote_audio_connection_held=bool(parsed["connection_held"]),
                video_format=video_send,
                local_video_format=video_recv,
                imageattr_fallback_needed=imageattr_fallback,
                local_video_rtp_port=(
                    local_video_rtp_port if video_send is not None else 0
                ),
                local_video_direction=local_video_direction,
                local_sdp_session_version=session_version,
                local_sdp_body=offer,
                **video_target.as_remote_video_fields(),
            )
        except (TypeError, ValueError, sdp.SdpError, sip.SipError):
            return None


    async def async_wait_for_media_idle(self, *, timeout: float = 8.0) -> bool:
        """Wait for an executing media update without overtaking its commit."""
        try:
            async with asyncio.timeout(timeout):
                async with self._local_offer_lock:
                    return bool(self.dialog is not None and self._prepared_reinvite is None
                                and not self._closing and not self._closed)
        except TimeoutError:
            return False


    def _reject_confirmed_dialog(
        self,
        *,
        remote_tag: str,
        remote_host: str,
        remote_sip_port: int,
        remote_target_uri: str,
        local_uri: str,
        remote_uri: str,
        route_set: tuple[str, ...],
    ) -> None:
        """ACK a successful INVITE response, then terminate its unusable dialog."""

        self.dialog_ids.remote_tag = remote_tag
        self._send_ack(
            remote_host,
            int(remote_sip_port),
            remote_target_uri,
            local_uri,
            remote_uri,
            route_set=route_set,
        )
        self._start_bye_request_transaction(
            remote_host,
            int(remote_sip_port),
            remote_target_uri,
            local_uri,
            remote_uri,
            route_set=route_set,
        )

    def _send_ack(
        self,
        host: str,
        port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
        *,
        route_set: tuple[str, ...] = (),
        cseq: int | None = None,
        remote_tag: str = "",
        body: bytes = b"",
        network_target=None,
    ) -> bool:
        if not self._has_signaling_path():
            return False
        try:
            request = build_dialog_request(
                "ACK",
                call_id=self.dialog_ids.call_id,
                local_tag=self.dialog_ids.local_tag,
                remote_tag=remote_tag or self.dialog_ids.remote_tag,
                cseq=self._invite_cseq if cseq is None else int(cseq),
                local_uri=local_uri,
                remote_uri=remote_uri,
                remote_target_uri=request_uri,
                route_set=route_set,
                contact_uri=self._pending_contact_uri or local_uri,
                transport=self.signaling_transport,
                via_sent_by=(self.local_ip, self.local_sip_port),
                local_display_name=self.local_name,
                remote_display_name=self._dialog_remote_display_name,
                content_type="application/sdp" if body else "",
                body=body,
            )
        except (TypeError, ValueError, sip.SipError) as err:
            _LOGGER.warning("SIP ACK routing rejected: %s", err)
            return False
        next_host, next_port = self._dialog_next_hop(
            request.routing.next_hop_uri,
            host,
            int(port),
        )
        sent = (self._send_dialog_request(request.raw, next_host, next_port, network_target=network_target)
                if network_target is not None else self._send_dialog_request(request.raw, next_host, next_port))
        if not sent:
            _LOGGER.warning("SIP TX ACK dropped: signaling path unavailable")
            return False
        sip.mark_sip_event(self, "ACK")
        _LOGGER.debug("SIP TX ACK %s:%s", next_host, next_port)
        return True


    def _start_bye_request_transaction(
        self,
        host: str,
        port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
        *,
        route_set: tuple[str, ...] = (),
        remote_tag: str = "",
        timeout: float = 1.5,
        ack_before_bye: bool = False,
        ack_only: bool = False,
    ) -> bool:
        if not self._has_signaling_path() or not request_uri or not local_uri or not remote_uri:
            return False
        try:
            request = build_dialog_request(
                "ACK" if ack_only else "BYE",
                call_id=self.dialog_ids.call_id,
                local_tag=self.dialog_ids.local_tag,
                remote_tag=remote_tag or self.dialog_ids.remote_tag,
                cseq=self._invite_cseq if ack_only else self._next_dialog_cseq(),
                local_uri=local_uri,
                remote_uri=remote_uri,
                remote_target_uri=request_uri,
                route_set=route_set,
                contact_uri=self._pending_contact_uri or local_uri,
                transport=self.signaling_transport,
                via_sent_by=(self.local_ip, self.local_sip_port),
                local_display_name=self.local_name,
                remote_display_name=self._dialog_remote_display_name,
            )
        except (TypeError, ValueError, sip.SipError) as err:
            _LOGGER.warning("SIP BYE routing rejected: %s", err)
            return False
        next_host, next_port = self._dialog_next_hop(
            request.routing.next_hop_uri,
            host,
            int(port),
        )
        async def run() -> None:
            try:
                prepared = await self._prepare_dialog_signaling_target(request_uri, route_set)
            except (OSError, RuntimeError, TimeoutError, sip.SipError):
                if ack_before_bye and not ack_only:
                    self._terminated_invite_branches.discard(remote_tag)
                raise
            network_target = prepared[1] if prepared is not None else None
            if ack_before_bye:
                self._send_ack(host, port, request_uri, local_uri, remote_uri,
                    route_set=route_set, remote_tag=remote_tag, network_target=network_target)
            if ack_only:
                return
            await self._run_built_dialog_request(
                request,
                next_host,
                next_port,
                method="BYE",
                timeout=timeout,
                active=lambda: self._has_signaling_path() and not self._closed,
                network_target=network_target,
            )

        task = asyncio.create_task(
            run(),
            name=f"voip-sip-client-exceptional-bye-{self.dialog_ids.call_id}",
        )
        self._exceptional_bye_tasks.add(task)
        task.add_done_callback(self._exceptional_bye_done)
        return True

    def _exceptional_bye_done(self, task: asyncio.Task[None]) -> None:
        self._exceptional_bye_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning(
                "Exceptional SIP BYE failed call_id=%s error=%s",
                self.dialog_ids.call_id,
                error,
            )


    def _schedule_deferred_close(self, _invite_task: asyncio.Task[str]) -> None:
        """Own and observe cleanup deferred until an INVITE task completes."""

        task = self._deferred_close_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self.close(),
            name=f"voip-sip-client-deferred-close-{self.dialog_ids.call_id}",
        )
        self._deferred_close_task = task

        def completed(done: asyncio.Task[None]) -> None:
            if self._deferred_close_task is done:
                self._deferred_close_task = None
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                _LOGGER.warning(
                    "Deferred SIP client close failed call_id=%s error=%s",
                    self.dialog_ids.call_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(completed)


    def snapshot(self) -> dict[str, Any]:
        dialog = self.dialog
        return {
            "call_id": self.dialog_ids.call_id,
            "local_uri": dialog.local_uri if dialog is not None else self._pending_local_uri,
            "remote_uri": dialog.remote_uri if dialog is not None else self._pending_remote_uri,
            "remote_target_uri": (
                (dialog.remote_target_uri or dialog.remote_uri)
                if dialog is not None
                else self._pending_request_uri
            ),
            "route_set": list(dialog.route_set) if dialog is not None else [],
            "remote_host": dialog.remote_host if dialog is not None else self._pending_remote_host,
            "remote_sip_port": dialog.remote_sip_port if dialog is not None else self._pending_remote_sip_port,
            "remote_rtp_host": dialog.remote_rtp_host if dialog is not None else "",
            "remote_rtp_port": dialog.remote_rtp_port if dialog is not None else 0,
            "local_rtp_port": dialog.local_rtp_port if dialog is not None else self.local_rtp_port,
            "selected_tx_format": dialog.send_format.audio_format.wire_token() if dialog is not None else "",
            "selected_rx_format": dialog.recv_format.audio_format.wire_token() if dialog is not None else "",
            "selected_tx_rtp_format": dialog.send_format.wire_token() if dialog is not None else "",
            "selected_rx_rtp_format": dialog.recv_format.wire_token() if dialog is not None else "",
            "dialog_active": dialog is not None,
            "pending_invite": bool(self._invite_transaction_active and dialog is None),
            "pending_remote_invite_ack": self._uas_invite_2xx.cseq,
            "remote_invite_2xx_retransmissions": self._uas_invite_2xx.retransmissions,
            "sip_transport": self.signaling_transport.lower(),
            "last_sip_event": self.last_sip_event,
            "last_sip_status_code": self.last_sip_status_code,
            "last_sip_reason": self.last_sip_reason,
        }
