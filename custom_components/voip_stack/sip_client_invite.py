"""SipInviteTransactions: operations on the SIP client's single owned state."""

from __future__ import annotations

import asyncio
import errno
from .sip_invite_exchange import InviteExchange
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES
from .core import sdp, sip
from .core.sip_transport import SIP_UDP_MAX_REQUEST_BYTES
from .core.sip_auth import DigestChallengeTracker
from .core.sip_dialog import apply_remote_offer_media, build_dialog_request
from .core.sip_transaction import SIP_T1, SIP_T2, SIP_TIMER_B, SIP_TIMER_F, SipClientTransaction, async_run_dialog_request_transaction, matches_response
import logging

from .sip_client_types import (_is_invite_progress_response, _sip_decline_reason)


_LOGGER = logging.getLogger(__name__)

class SipInviteTransactions:
    """Use the existing client reader, writer, locks and dialog state."""

    def _build_pending_invite(self) -> bytes:
        """Build the current initial-dialog INVITE transaction request."""
        return InviteExchange.compose_request(
            request_uri=self._pending_request_uri,
            route_set=self._preloaded_route_set(),
            local_uri=self._pending_local_uri,
            remote_uri=self._pending_remote_uri,
            dialog=self.dialog_ids,
            contact_uri=self._pending_contact_uri,
            body=self._pending_invite_body,
            transport=self.signaling_transport,
            via_sent_by=(self.local_ip, self.local_sip_port),
            local_name=self.local_name,
            target_display_name=self._pending_target_display,
            answer_mode=getattr(self, "_pending_answer_mode", ""),
            max_forwards=self.max_forwards,
            authorization_headers=tuple(
                (header, value)
                for header, (_challenge, value, _count) in self._pending_invite_auth.items()
            ),
        )


    async def _retry_invite_after(
        self,
        message: sip.SipMessage,
        *,
        remote_host: str,
        remote_sip_port: int,
    ) -> bytes | None:
        """Start at most one RFC-shaped replacement INVITE transaction."""

        delay = self._retry_after_delay(message)
        if (
            delay is None
            or self._retry_after_used
            or self._cancel_requested
            or self._closing
            or self._closed
        ):
            return None
        self._retry_after_used = True
        _LOGGER.info(
            "SIP %s Retry-After=%s, retrying INVITE once",
            message.status_code,
            message.header("Retry-After"),
        )
        try:
            await asyncio.wait_for(self._invite_abort_event.wait(), timeout=delay)
        except TimeoutError:
            pass
        if (
            self._invite_abort_event.is_set()
            or self._cancel_requested
            or self._closing
            or self._closed
        ):
            return None
        # This is a new client transaction for the same logical dialog attempt:
        # preserve Call-ID/From tag, advance CSeq and replace the Via branch.
        self.dialog_ids.cseq += 1
        self.dialog_ids.branch = sip.make_branch()
        self._invite_cseq = self.dialog_ids.cseq
        self._received_provisional = False
        self._cancel_sent = False
        self._refresh_pending_invite_authorization()
        raw = self._build_pending_invite()
        sip.mark_sip_event(self, "INVITE")
        await self._send_raw(raw, remote_host, int(remote_sip_port))
        return raw


    async def _ack_reliable_provisional(
        self,
        response: sip.SipMessage,
        addr: tuple[str, int],
        *,
        process_early_media: bool = True,
    ) -> bool:
        """Acknowledge one in-order RFC 3262 provisional response."""

        if not (
            101 <= int(response.status_code or 0) < 200
            and "100rel" in sip.option_tags(response, "Require")
        ):
            return True
        try:
            rseq = int(response.header("RSeq"))
            invite_cseq = sip.parse_cseq(response.header("CSeq"))
            remote_tag = sip.extract_tag(response.header("To"))
            if not 1 <= rseq <= 0xFFFFFFFF or not remote_tag:
                return False
            sequence_key = (remote_tag, invite_cseq.number)
            previous = self._reliable_rseq.get(sequence_key)
            if previous is not None:
                if rseq == previous:
                    return True
                if rseq != previous + 1:
                    # RFC 3262: ignore an out-of-order reliable response.
                    # It is not a failed PRACK transaction or a call failure.
                    return True
            if process_early_media and response.body and not self._commit_200_ok(
                response,
                self._pending_target,
                self._pending_remote_host or addr[0],
                self._pending_remote_sip_port,
                self._pending_request_uri,
                self._pending_local_uri,
                self._pending_remote_uri,
                provisional=True,
            ):
                return False
            current = self.dialog or self.early_dialogs.get(remote_tag)
            prack_body = (
                current.local_sdp_body.encode()
                if process_early_media and self._initial_delayed_offer
                and response.body and current is not None
                else b""
            )
            local_uri = (
                current.local_uri if current is not None else self._pending_local_uri
            )
            remote_uri = (
                current.remote_uri if current is not None else self._pending_remote_uri
            )
            remote_target = sip.contact_target_uri(response) or (
                current.remote_target_uri if current is not None else remote_uri
            )
            route_set = (
                sip.record_route_set(response, reverse=True)
                or self._preloaded_route_set()
            )
            request = build_dialog_request(
                "PRACK",
                call_id=self.dialog_ids.call_id,
                local_tag=self.dialog_ids.local_tag,
                remote_tag=remote_tag,
                cseq=self._next_dialog_cseq(),
                local_uri=local_uri,
                remote_uri=remote_uri,
                remote_target_uri=remote_target,
                route_set=route_set,
                contact_uri=self._pending_contact_uri or local_uri,
                transport=self.signaling_transport,
                via_sent_by=(self.local_ip, self.local_sip_port),
                local_display_name=self.local_name,
                remote_display_name=self._pending_target_display,
                extra_headers=(
                    (
                        "RAck",
                        f"{rseq} {invite_cseq.number} {invite_cseq.method}",
                    ),
                ),
                content_type="application/sdp" if prack_body else "",
                body=prack_body,
            )
            next_host, next_port = self._dialog_next_hop(
                request.routing.next_hop_uri,
                addr[0],
                addr[1],
            )
        except (TypeError, ValueError, sip.SipError):
            return False
        try:
            if self._dialog_dispatcher is not None:
                prack_response = await self._run_built_dialog_request(
                    request, next_host, next_port, method="PRACK", timeout=SIP_TIMER_B,
                    active=lambda: not self._closing and not self._closed,
                )
            else:
                prack_response = await async_run_dialog_request_transaction(
                    method="PRACK",
                    send=lambda: self._send_dialog_request(
                        request.raw, next_host, next_port
                    ),
                    read=lambda timeout: self._read_response(
                        timeout, network_only=True
                    ),
                    matches=lambda message: sip.response_matches_dialog_transaction(
                        message, request.ids, "PRACK"
                    ),
                    active=lambda: not self._closing and not self._closed,
                    transport=self.signaling_transport,
                    timeout=SIP_TIMER_B,
                    on_unmatched=lambda message, source: self._deferred_signaling.append(
                        (message, source)
                    ),
                    on_sent=lambda: sip.mark_sip_event(self, "PRACK"),
                )
        except (ConnectionError, OSError):
            return False
        status = int(prack_response.status_code or 0) if prack_response else 0
        if not 200 <= status < 300:
            return False
        self._reliable_rseq[sequence_key] = rseq
        return True


    async def invite(
        self,
        *,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str = "",
        target_display_name: str = "",
        timeout: float = 8.0,
        delayed_offer: bool = False,
        answer_mode: str = "",
        audio_direction: str = "sendrecv",
    ) -> str:
        """Run one owned INVITE transaction that survives caller-task cancellation."""
        if answer_mode not in {"", "Auto", "Manual"}:
            raise ValueError("Unsupported SIP answer mode")
        if audio_direction not in {"sendrecv", "sendonly", "recvonly", "inactive"}:
            raise ValueError("Unsupported SIP audio direction")
        self.invite_sent_at = None
        if self._closing or self._closed:
            raise RuntimeError("SIP client is already closed")
        if self._invite_task is not None and not self._invite_task.done():
            raise RuntimeError("INVITE transaction already active")
        progress = asyncio.get_running_loop().create_future()
        self._invite_progress = progress
        task = asyncio.create_task(
            self._run_invite(
                target=target,
                remote_host=remote_host,
                remote_sip_port=remote_sip_port,
                request_uri=request_uri,
                target_display_name=target_display_name,
                timeout=timeout,
                delayed_offer=delayed_offer,
                answer_mode=answer_mode,
                audio_direction=audio_direction,
            ),
            name=f"voip-sip-client-invite-{self.dialog_ids.call_id}",
        )
        self._invite_task = task

        def completed(done: asyncio.Task[str]) -> None:
            if self._deferred_owned_requests:
                self._early_request_task = asyncio.create_task(self._flush_owned_requests(), name="voip-sip-initial-control")
            if self._invite_task is done:
                self._invite_transaction = None
            if progress.done():
                return
            if done.cancelled():
                progress.cancel()
                return
            error = done.exception()
            if error is not None:
                progress.set_exception(error)
            else:
                progress.set_result(done.result())

        task.add_done_callback(completed)
        try:
            return await asyncio.shield(progress)
        except asyncio.CancelledError:
            self.request_cancel()
            raise


    async def _run_invite(
        self,
        *,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str = "",
        target_display_name: str = "",
        timeout: float = 8.0,
        delayed_offer: bool = False,
        answer_mode: str = "",
        audio_direction: str = "sendrecv",
    ) -> str:
        self._invite_setup_deadline = asyncio.get_running_loop().time() + max(0.0, float(timeout))
        self._pending_answer_mode = answer_mode
        exchange = InviteExchange.for_target(
            target, remote_host, remote_sip_port, request_uri, self.signaling_transport,
        )
        route = exchange.next_hop(self.outbound_proxy)
        self._tls_server_name = route.host
        self._signaling_nominal = (remote_host, int(remote_sip_port))
        try:
            if self._reused_flow_send is not None and self._reused_udp_send_to is None:
                exchange.replace_endpoints(((exchange.host, exchange.port, self.signaling_transport),))
            else:
                async with asyncio.timeout_at(self._invite_setup_deadline):
                    await exchange.resolve(self.target_resolver, route, self.signaling_transport)
        except (OSError, RuntimeError, sip.SipError) as err:
            return self._transport_failure(err, target, remote_host, remote_sip_port)
        if not await self._select_next_invite_target(exchange):
            return self._transport_failure(
                OSError("every resolved SIP target is unreachable"), target, remote_host, remote_sip_port,
            )

        self._begin_initial_invite(exchange, target_display_name)
        failure = await self._prepare_initial_media(exchange)
        if failure is not None:
            return failure
        try:
            self._prepare_initial_offer(delayed_offer=delayed_offer, audio_direction=audio_direction)
            exchange.request = self._build_pending_invite()
        except BaseException:
            # Offer/serialization failure precedes any INVITE on the wire.
            # Its owner can release signaling without a nonexistent Timer F.
            self._invite_transaction_active = False
            raise
        failure = await self._prepare_invite_request_transport(exchange)
        if failure is not None:
            return failure
        failure = await self._send_initial_invite(exchange)
        if failure is not None:
            return failure
        exchange.log_sent(self._pending_invite_body, self.signaling_transport)
        # The response budget starts at the first INVITE, after setup.
        remaining = max(0.0, self.invite_sent_at + float(timeout) - asyncio.get_running_loop().time())
        self._invite_setup_deadline = None
        return await self._exchange_initial_responses(exchange, remaining)


    async def _connect_invite_target(self, host: str, port: int) -> int:
        await self._select_initial_signaling_target(host, port)
        return self.local_sip_port


    async def _select_next_invite_target(self, exchange: InviteExchange) -> bool:
        return await exchange.select_next(self.signaling_transport, self.dialog_ids.call_id, self._connect_invite_target)


    def _initial_contact_uri(self) -> str:
        return str(sip.SipUri(self.local_uri_user, self.local_ip, self.local_sip_port,
                              params=(("transport", self.signaling_transport.lower()),)))


    def _set_invite_contact(self, exchange: InviteExchange, *, preserve_identity: bool) -> None:
        contact = self._initial_contact_uri()
        if not preserve_identity:
            self._pending_local_uri = self.local_identity_uri or contact
            self._pending_remote_uri = exchange.request_uri
        self._pending_contact_uri = self.local_contact_uri or contact


    def _begin_initial_invite(self, exchange: InviteExchange, target_display_name: str) -> None:
        contact_uri = self._initial_contact_uri()
        local_uri = self.local_identity_uri or contact_uri
        sip.parse_sip_uri(local_uri)
        self._pending_target = exchange.target
        self._pending_target_display = str(target_display_name or exchange.target).strip()
        self._pending_remote_host = exchange.host
        self._pending_remote_sip_port = exchange.port
        self._pending_request_uri = exchange.request_uri
        self._pending_local_uri = local_uri
        self._pending_contact_uri = self.local_contact_uri or contact_uri
        self._pending_remote_uri = exchange.request_uri
        self._pending_invite_auth.clear()
        self._retry_after_used = False
        self._invite_abort_event.clear()
        self._invite_transaction_active = True
        self._cancel_requested = False
        self._cancel_sent = False
        self._cancel_transaction = None
        self._pending_cancel_request = b""
        self.cancel_reason = ""
        self._received_provisional = False
        self._reliable_rseq.clear()
        self._deferred_signaling.clear()


    async def _prepare_initial_media(self, exchange: InviteExchange) -> str | None:
        if self.prepare_media is None:
            return None
        prepare = asyncio.create_task(self.prepare_media(self._resolved_signaling_target[0]))
        aborted = asyncio.create_task(self._invite_abort_event.wait())
        try:
            async with asyncio.timeout(3):
                done, _ = await asyncio.wait((prepare, aborted), return_when=asyncio.FIRST_COMPLETED)
                if aborted in done:
                    self._invite_transaction_active = False
                    return "cancelled"
                await prepare
        except (OSError, ValueError) as err:
            return self._transport_failure(err, exchange.target, exchange.host, exchange.port)
        finally:
            prepare.cancel()
            aborted.cancel()
            await asyncio.gather(prepare, aborted, return_exceptions=True)
        if self._cancel_requested or self._closing:
            self._invite_transaction_active = False
            return "cancelled"
        return None


    def _prepare_initial_offer(self, *, delayed_offer: bool, audio_direction: str) -> None:
        offer = ""
        if not delayed_offer:
            offer = sdp.build_offer_directional(
                self.local_ip,
                self.media_ip,
                self.local_rtp_port,
                self.supported_send_formats,
                self.supported_recv_formats,
                include_common_codecs=self.include_common_codecs,
                include_dahua_pcm=self.include_dahua_pcm,
                audio_rtcp=False,
                send_rtp_formats=self.supported_send_rtp_formats,
                recv_rtp_formats=self.supported_recv_rtp_formats,
                previous_payloads=self.dialog.audio_payloads if self.dialog is not None else None,
                codec_policy=self.codec_policy,
                local_capabilities=HA_AUDIO_CAPABILITIES,
                video_port=self.local_video_rtp_port,
                video_format=self.video_format,
                video_formats=self.video_formats,
                video_direction=self.video_direction,
                audio_direction=audio_direction,
            )
            offer = self._advertise_media(offer)
            offer = sdp.rewrite_sdp_origin(offer, self._sdp_session_id, 0)
        self._initial_delayed_offer = bool(delayed_offer)
        self._local_sdp_body = offer
        self._pending_invite_body = offer.encode()
        self._invite_cseq = self.dialog_ids.cseq


    async def _prepare_invite_request_transport(self, exchange: InviteExchange) -> str | None:
        if not exchange.needs_tcp(self.signaling_transport):
            return None
        if self._reused_flow_send is not None and self._reused_udp_next_hop is not None:
            # Only the registrar-selected RFC 5626 Outbound binding sets this
            # fixed next hop. Ordinary borrowed sockets and trunks still use
            # the complete-request size rule below.
            if len(exchange.request) > SIP_UDP_MAX_REQUEST_BYTES:
                return self._transport_failure(OSError(errno.EMSGSIZE, "SIP request exceeds UDP datagram limit"),
                    exchange.target, exchange.host, exchange.port)
            return None
        borrowed_flow = (self._reused_flow_send, self._reused_flow_responses, self._reused_flow_close, self._reused_udp_send_to)
        udp_port = self.local_sip_port
        udp_endpoints = exchange.endpoints
        udp_tls_name = self._tls_server_name
        self._reused_flow_send = self._reused_flow_responses = self._reused_flow_close = None
        self._reused_udp_send_to = None
        try:
            _LOGGER.debug("SIP request transport upgrade call_id=%s bytes=%s from=UDP to=TCP reason=message_size",
                          self.dialog_ids.call_id, len(exchange.request))
            if self.transport is not None:
                self.transport.close()
                self.transport = None
                self.protocol = None
            self._resolved_signaling_target = None
            self.signaling_transport = "TCP"
            # RFC 3261 section 18.1.1 changes the connection target; the logical
            # Request-URI and an already transmitted From/To remain unchanged.
            route = exchange.next_hop(self.outbound_proxy, transport="TCP")
            self._tls_server_name = route.host
            try:
                await exchange.resolve(self.target_resolver, route, "TCP")
            except (OSError, RuntimeError, sip.SipError) as err:
                return self._transport_failure(err, exchange.target, exchange.host, exchange.port)
            if not await self._select_next_invite_target(exchange):
                if self._cancel_requested or self._closing or self._closed:
                    self._invite_transaction_active = False
                    return "cancelled"
                # Only a definite refusal/reset or protocol-unreachable error
                # permits UDP fallback before this TCP request was sent.
                if not exchange.tcp_refused_before_send():
                    return self._transport_failure(exchange.selection_error("TCP"), exchange.target, exchange.host, exchange.port)
                _LOGGER.info("SIP request transport fallback call_id=%s bytes=%s from=TCP to=UDP "
                             "reason=connection_rejected_before_send", self.dialog_ids.call_id, len(exchange.request))
                self.signaling_transport = "UDP"
                exchange.udp_fallback_used = True
                (self._reused_flow_send, self._reused_flow_responses, self._reused_flow_close,
                 self._reused_udp_send_to) = borrowed_flow
                if borrowed_flow[0] is not None:
                    self.local_sip_port = udp_port
                borrowed_flow = None
                self._tls_server_name = udp_tls_name
                self._resolved_signaling_target = None
                exchange.replace_endpoints(udp_endpoints)
                if not await self._select_next_invite_target(exchange):
                    return self._transport_failure(exchange.selection_error("UDP"), exchange.target, exchange.host, exchange.port)
            self._set_invite_contact(exchange, preserve_identity=self.invite_sent_at is not None)
            self._refresh_pending_invite_authorization(advance_nonce=False)
            exchange.request = self._build_pending_invite()
            _LOGGER.info("SIP initial request is %s bytes; selected %s per RFC 3261 section 18.1.1",
                         len(exchange.request), self.signaling_transport)
            if self._cancel_requested or self._closing or self._closed:
                self._invite_transaction_active = False
                return "cancelled"
            return None
        finally:
            # A restored UDP flow still belongs to the client; release only a
            # borrowed flow that was relinquished by the transport change.
            if borrowed_flow is not None and borrowed_flow[2] is not None:
                borrowed_flow[2]()


    def _refresh_invite_for_next_hop(self, exchange: InviteExchange) -> None:
        self.dialog_ids.branch = sip.make_branch()
        self._set_invite_contact(exchange, preserve_identity=False)
        exchange.request = self._build_pending_invite()


    async def _send_initial_invite(self, exchange: InviteExchange) -> str | None:
        sip.mark_sip_event(self, "INVITE")
        while True:
            if self._cancel_requested or self._closing or self._closed:
                self._invite_transaction_active = False
                return "cancelled"
            try:
                await self._send_raw(exchange.request, exchange.host, exchange.port)
                if self.invite_sent_at is None:
                    self.invite_sent_at = asyncio.get_running_loop().time()
                break
            except (ConnectionError, OSError, RuntimeError) as err:
                if not await self._select_next_invite_target(exchange):
                    self._invite_transaction_active = False
                    return self._transport_failure(
                        err,
                        exchange.target,
                        exchange.host,
                        exchange.port,
                    )
                self._refresh_invite_for_next_hop(exchange)
        return None


    def _new_initial_transaction(
        self, timeout: float,
    ) -> SipClientTransaction[tuple[sip.SipMessage, tuple[str, int]]]:
        transaction = SipClientTransaction(transport=self.signaling_transport, timeout=timeout,
                                           method="INVITE", t1=SIP_T1, t2=SIP_T2)
        self._invite_transaction = transaction
        return transaction


    async def _failover_initial_transaction(
        self, exchange: InviteExchange, timeout: float,
    ) -> SipClientTransaction[tuple[sip.SipMessage, tuple[str, int]]] | None:
        if self._received_provisional:
            return None
        while await self._select_next_invite_target(exchange):
            self._refresh_invite_for_next_hop(exchange)
            try:
                await self._send_raw(exchange.request, exchange.host, exchange.port)
            except (ConnectionError, OSError, RuntimeError):
                continue
            return self._new_initial_transaction(timeout)
        return None


    async def _handle_invite_provisional(
        self, exchange: InviteExchange, transaction: SipClientTransaction,
        msg: sip.SipMessage, addr: tuple[str, int],
    ) -> str | None:
        remote_tag = sip.extract_tag(msg.header("To"))
        previous_early = self.early_dialogs.get(remote_tag)
        if msg.status_code > 100 and remote_tag:
            self.early_dialogs.setdefault(remote_tag, None)
        self._received_provisional = True
        if not await self._ack_reliable_provisional(msg, addr):
            self.request_cancel()
            return "prack_failed"
        if (msg.status_code > 100 and msg.body and msg.header("Content-Type").split(";", 1)[0].strip().lower() == "application/sdp"
                and "100rel" not in sip.option_tags(msg, "Require")):
            self._commit_200_ok(msg, exchange.target, exchange.host, exchange.port,
                exchange.request_uri, self._pending_local_uri, self._pending_remote_uri, provisional=True)
        candidate = self.early_dialogs.get(remote_tag)
        if (candidate is not None and self.on_provisional_media is not None
                and (previous_early is None or not self._same_dialog_media(previous_early, candidate))):
            proposal = await self.on_provisional_media(candidate)
            if proposal is not None and not await apply_remote_offer_media(proposal.commit, proposal.rollback):
                self.request_cancel()
                return "media_update_failed"
        if self._cancel_requested and not self._cancel_sent:
            self._send_cancel()
        if _is_invite_progress_response(msg.status_code):
            if self._cancel_requested:
                return None
            # Proceeding is bounded by the caller/application policy,
            # not an implicit 60-second ringing limit (RFC 3261 17.1.1.2).
            transaction.reset_deadline(None)
            progress = self._invite_progress
            if progress is not None and not progress.done():
                progress.set_result("ringing")
        # A peer or controlled test transport can deliver a burst of
        # provisional responses without blocking. Yield so the caller
        # can observe progress or request cancellation promptly.
        await asyncio.sleep(0)
        return None


    async def _handle_invite_success(self, exchange: InviteExchange, msg: sip.SipMessage) -> str:
        if not self._commit_200_ok(
            msg, exchange.target, exchange.host, exchange.port, exchange.request_uri,
            self._pending_local_uri, self._pending_remote_uri,
        ):
            return (
                "cancelled"
                if self._closing or self._closed
                else "media_incompatible"
            )
        if self._cancel_requested:
            await self._terminate_confirmed_dialog()
            return "cancelled"
        await self._complete_imageattr_fallback()
        return "in_call" if self.dialog is not None else "timeout"


    async def _retry_initial_authentication(
        self, exchange: InviteExchange, msg: sip.SipMessage, addr: tuple[str, int],
        auth_challenges: DigestChallengeTracker,
    ) -> str | None:
        self._send_invite_error_ack(msg, addr[0], addr[1])
        if self._cancel_requested:
            self._invite_transaction_active = False
            return "cancelled"
        try:
            auth_header, challenge, auth_value = auth_challenges.authorize(
                msg,
                username=self.username,
                auth_username=self.auth_username,
                password=self.password,
                method="INVITE",
                uri=exchange.request_uri,
                nonce_count=1,
                body=self._pending_invite_body,
            )
        except Exception as err:
            _LOGGER.info("SIP digest auth failed to build INVITE response: %s", err)
            return sip.sip_failure_reason(msg.status_code)
        self.dialog_ids.cseq += 1
        self.dialog_ids.branch = sip.make_branch()
        self._invite_cseq = self.dialog_ids.cseq
        self._pending_invite_auth[auth_header] = (
            challenge,
            auth_value,
            1,
        )
        exchange.request = self._build_pending_invite()
        transport_error = await self._prepare_invite_request_transport(exchange)
        if transport_error is not None:
            return transport_error
        sip.mark_sip_event(self, "INVITE")
        try:
            await self._send_raw(exchange.request, exchange.host, exchange.port)
        except (ConnectionError, OSError, RuntimeError) as err:
            return self._transport_failure(err, exchange.target, exchange.host, exchange.port)
        return None


    async def _exchange_initial_responses(self, exchange: InviteExchange, timeout: float) -> str:
        transaction = self._new_initial_transaction(timeout)
        auth_challenges = DigestChallengeTracker()

        async def retransmit() -> None:
            raw = self._pending_cancel_request if self._cancel_transaction is not None else exchange.request
            await self._send_raw(raw, exchange.host, exchange.port)
            request_method = "CANCEL" if self._cancel_transaction is not None else "INVITE"
            _LOGGER.debug("SIP UDP retransmit %s #%d %s@%s:%s", request_method,
                          (self._cancel_transaction or transaction).retransmissions + 1,
                          exchange.target, exchange.host, exchange.port)

        while True:
            if self._cancel_requested and self._received_provisional and not self._cancel_sent:
                self._send_cancel()
            try:
                response_timer = self._cancel_transaction or transaction
                received = await response_timer.receive(
                    self._read_response, retransmit,
                    retransmit_enabled=(self._cancel_transaction is not None or not self._received_provisional),
                )
                if received is None:
                    if response_timer is transaction and self._cancel_transaction is not None:
                        continue
                    if self._cancel_requested:
                        self._invite_transaction_active = False
                        self._cancel_transaction = None
                        return "cancelled"
                    transaction = await self._failover_initial_transaction(exchange, timeout)
                    if transaction is None:
                        return "timeout"
                    continue
                msg, addr = received
            except (ConnectionError, OSError, RuntimeError) as err:
                if self._cancel_requested:
                    self._invite_transaction_active = False
                    self._cancel_transaction = None
                    return "cancelled"
                transaction = await self._failover_initial_transaction(exchange, timeout)
                if transaction is None:
                    return self._transport_failure(err, exchange.target, exchange.host, exchange.port)
                continue
            except Exception as err:
                _LOGGER.debug("SIP RX malformed: %s", err)
                continue
            if not msg.is_response:
                continue
            if self._cancel_transaction is not None and matches_response(
                    msg, method="CANCEL", cseq=self._invite_cseq, branch=self.dialog_ids.branch):
                if int(msg.status_code or 0) >= 200:
                    self._cancel_transaction = None
                elif int(msg.status_code or 0) >= 100:
                    self._cancel_transaction.interval = SIP_T2
                    self._cancel_transaction.next_retransmit = asyncio.get_running_loop().time() + SIP_T2
                continue
            if not matches_response(msg, method="INVITE", cseq=self._invite_cseq, branch=self.dialog_ids.branch):
                _LOGGER.debug("SIP response ignored for non-active INVITE transaction")
                continue
            sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code or 0), msg.reason)
            _LOGGER.debug("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            if msg.status_code is not None and 100 <= msg.status_code < 200:
                result = await self._handle_invite_provisional(exchange, transaction, msg, addr)
                if result is not None:
                    return result
                continue
            if msg.status_code and 200 <= msg.status_code < 300:
                return await self._handle_invite_success(exchange, msg)
            if msg.status_code in {401, 407} and self.password:
                error = await self._retry_initial_authentication(exchange, msg, addr, auth_challenges)
                if error is not None:
                    return error
                transaction = self._new_initial_transaction(timeout)
                self._received_provisional = False
                self._reliable_rseq.clear()
                continue
            if msg.status_code and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                try:
                    retry = await self._retry_invite_after(msg, remote_host=exchange.host, remote_sip_port=exchange.port)
                except (ConnectionError, OSError, RuntimeError) as err:
                    return self._transport_failure(err, exchange.target, exchange.host, exchange.port)
                if retry is not None:
                    exchange.request = retry
                    transaction.restart_retransmissions()
                    self._reliable_rseq.clear()
                    continue
                self._invite_transaction_active = False
                if self._cancel_requested or self._closing or self._closed:
                    return "cancelled"
                return _sip_decline_reason(msg) or sip.sip_failure_reason(msg.status_code)


    async def wait_for_final(self, timeout: float | None = None) -> str:
        """Await the final result from the sole INVITE transaction owner."""

        if self._closing or self._closed:
            return "cancelled"
        if self.dialog is not None:
            return "in_call"
        task = self._invite_task
        if task is None:
            raise RuntimeError("no INVITE transaction owner is active")
        transaction = self._invite_transaction
        if transaction is not None and not self._cancel_requested:
            transaction.reset_deadline(timeout)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            self.request_cancel()
            return "timeout"
        except asyncio.CancelledError:
            self.request_cancel()
            raise


    def _send_invite_error_ack(
        self,
        msg: sip.SipMessage,
        host: str,
        port: int,
        *,
        request_uri: str = "",
        local_uri: str = "",
        remote_uri: str = "",
        route_set: tuple[str, ...] = (),
        cseq: int | None = None,
        branch: str = "",
    ) -> None:
        if not self._has_signaling_path():
            return
        request_uri = request_uri or self._pending_request_uri
        local_uri = local_uri or self._pending_local_uri
        remote_uri = remote_uri or self._pending_remote_uri
        if not request_uri or not local_uri or not remote_uri:
            return
        try:
            routing = sip.dialog_request_routing(
                request_uri,
                route_set or self._preloaded_route_set(),
            )
        except (TypeError, ValueError, sip.SipError):
            return
        ack_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=sip.extract_tag(msg.header("To")),
            cseq=self._invite_cseq if cseq is None else int(cseq),
            branch=branch or self.dialog_ids.branch,
        )
        headers = sip.dialog_headers(
            request_uri=routing.request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ack_ids,
            method="ACK",
            contact_uri=self._pending_contact_uri or local_uri,
            transport=self.signaling_transport,
            via_sent_by=(self.local_ip, self.local_sip_port),
            local_display_name=self.local_name,
            remote_display_name=sip.name_addr_display_name(msg.header("To")),
        )
        headers.extend(("Route", value) for value in routing.route_headers)
        raw = sip.build_request("ACK", routing.request_uri, headers, b"")
        if not self._send_dialog_request(raw, host, int(port)):
            _LOGGER.warning("SIP TX ACK final INVITE error dropped: signaling path unavailable")
            return
        sip.mark_sip_event(self, "ACK")
        _LOGGER.debug("SIP TX ACK final INVITE error %s:%s", host, port)


    def request_cancel(self) -> bool:
        """Request cancellation by the coroutine that owns the INVITE transaction."""
        if (
            not self._invite_transaction_active
            or not self._pending_request_uri
        ):
            _LOGGER.info(
                "SIP CANCEL skipped: no active INVITE call_id=%s transport=%s pending_uri=%s",
                self.dialog_ids.call_id,
                self.signaling_transport,
                bool(self._pending_request_uri),
            )
            return False
        self._cancel_requested = True
        self._invite_abort_event.set()
        if self._received_provisional:
            return self._send_cancel()
        return True


    def _send_cancel(self) -> bool:
        """Send CANCEL after the INVITE has entered the proceeding state."""
        if self._cancel_sent:
            return True
        cancel_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag="",
            cseq=self._invite_cseq,
            branch=self.dialog_ids.branch,
        )
        routing = sip.dialog_request_routing(
            self._pending_request_uri,
            self._preloaded_route_set(),
        )
        headers = sip.dialog_headers(
            request_uri=routing.request_uri,
            local_uri=self._pending_local_uri,
            remote_uri=self._pending_remote_uri,
            dialog=cancel_ids,
            method="CANCEL",
            contact_uri=self._pending_contact_uri or self._pending_local_uri,
            transport=self.signaling_transport,
            via_sent_by=(self.local_ip, self.local_sip_port),
            local_display_name=self.local_name,
            remote_display_name=self._pending_target_display,
        )
        headers.extend(("Route", value) for value in routing.route_headers)
        if self.cancel_reason == "answered_elsewhere":
            headers.append(("Reason", 'SIP;cause=200;text="Call completed elsewhere"'))
        raw = sip.build_request("CANCEL", routing.request_uri, headers, b"")
        if not self._send_dialog_request(raw, self._pending_remote_host, self._pending_remote_sip_port):
            _LOGGER.warning("SIP TX CANCEL dropped: signaling path unavailable")
            return False
        self._cancel_sent = True
        self._pending_cancel_request = raw
        self._cancel_transaction = SipClientTransaction(
            transport=self.signaling_transport, timeout=SIP_TIMER_F, t1=SIP_T1, t2=SIP_T2,
        )
        if self._invite_transaction is not None:
            self._invite_transaction.reset_deadline(SIP_TIMER_F)
            self._invite_transaction.wake_reader()
        sip.mark_sip_event(self, "CANCEL")
        _LOGGER.debug("SIP TX CANCEL %s:%s", self._pending_remote_host, self._pending_remote_sip_port)
        return True


    def cancel(self) -> bool:
        """Cancel now, or defer until the INVITE receives a provisional response."""
        sent_or_deferred = self.request_cancel()
        if sent_or_deferred and not self._received_provisional:
            _LOGGER.info("SIP CANCEL deferred until provisional call_id=%s", self.dialog_ids.call_id)
        return sent_or_deferred
