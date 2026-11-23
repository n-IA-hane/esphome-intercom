"""SipIncomingInvites: operations on the single SIP endpoint state."""

from __future__ import annotations
from .core.media.payload_map import PayloadMap

import asyncio
from dataclasses import replace
import secrets
from .sip_listener_request import SipRequestResult, matches_cancel, matches_retransmission as _same_request_transaction
from .core.codec_capabilities import supports_dahua_pcm
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES
from .core import sdp, sip, sip_transfer
from .core.codec_policy import CodecPolicy
from .core.sip_dialog import apply_remote_offer_media, notify_media_commit
from .core.sip_transaction import SIP_T1 as _SIP_T1, SIP_T2 as _SIP_T2, SIP_TIMER_B as _INVITE_2XX_TIMEOUT, SIP_TIMER_H as _INVITE_NON2XX_TIMEOUT, async_run_server_transaction
from .inbound_identity import identity_fields
import logging

from .sip_server_state import (SipInitialInvite, SipInvite, SipInviteResult, UasDelayedOfferPlan, _ActiveDialog, _CompletedRequest, _DeferredInviteFinal, _PendingDelayedOffer, _PendingInvite, _ReliableProvisional, _cseq_number, _identity_header, _uri_from_header, _uri_text_from_header)


_LOGGER = logging.getLogger(__name__)

class SipIncomingInvites:
    """Use the endpoint-owned flows, locks, transactions and dialog records."""

    def _stage_answer_contract(self, pending, answer_sdp, media_contract=None):
        """Accept legacy unchanged answers; changed media requires its explicit DTO."""
        if not answer_sdp or not pending.request.body:
            return True
        chosen = media_contract or pending.media_contract or pending.identity
        if not isinstance(chosen, SipInvite) or chosen.call_id != pending.request.header("Call-ID"):
            return False
        from .core.media.negotiator import validate_answer_contract
        try:
            sdp.validate_sdp_answer(pending.request.body, answer_sdp)
            if media_contract is None:
                from .sip_server_state import answered_media_contract
                video = answered_media_contract(chosen, answer_sdp)
                if (video.video_format, video.local_video_format, video.video_answer_format) != (
                        chosen.video_format, chosen.local_video_format, chosen.video_answer_format):
                    raise sdp.SdpError("changed video requires an explicit media contract")
            validate_answer_contract(sdp.RtpPcmDirection(chosen.send_format, chosen.recv_format,
                chosen.recv_formats, chosen.send_formats), answer_sdp,
                events=chosen.dtmf_direction, dahua=chosen.peer_profile == "dahua")
        except (ValueError, sdp.SdpError) as error:
            _LOGGER.error("SIP answer media contract rejected call_id=%s: %s", chosen.call_id, error)
            return False
        pending.media_contract = chosen
        return True

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


    def _activate_dialog(
        self,
        call_id: str,
        pending: _PendingInvite,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> _ActiveDialog:
        """Build and activate one UAS dialog from its authoritative INVITE."""

        invite = pending.media_contract or (pending.identity if isinstance(pending.identity, SipInvite) else None)
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
            authenticated_flow=(self.transaction_flow(addr)
                if pending.transport == "TCP" and signaling_invite.authenticated_username
                and self.transaction_flow is not None else None),
            codec_policy=pending.codec_policy,
            remote_uri=_uri_text_from_header(request.header("From")),
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
        response = self._build_response(
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
        if not self._send_response_bytes(response, addr, 200, "OK"):
            if prack_result is not None and prack_result.rollback is not None:
                await prack_result.rollback()
            return
        self._remember_completed(
            self.completed_pracks,
            prack_key,
            _CompletedRequest(request, addr, 200, "OK", response),
        )
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
            pending = self.pending_invites.get(call_id)
            if pending is not None:
                pending.media_contract = updated
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


    async def _handle_cancel(self, request, addr) -> None:
        call_id = request.header("Call-ID")
        pending = self.pending_invites.get(call_id)
        completed = self.completed_invites.get(call_id)
        invite_transaction = pending or (completed if completed is not None and completed.cancelled else None)
        # Match the transaction and originating host, but do not pin the
        # source port: NATs and SIP proxies may legitimately rewrite it.
        if (invite_transaction is None or invite_transaction.addr[0] != addr[0]
                or not matches_cancel(request, invite_transaction.request)):
            self._reject_dialog_request(request, addr)
            return
        if pending is None:
            self._send_response(request, addr, 200, "OK", to_tag=invite_transaction.to_tag)
            return
        pending.cancelled = True
        self._cancel_pending_expiry(pending)
        pending.status = 487
        pending.reason = "Request Terminated"
        pending.decline_reason = "cancelled"
        self.pending_invites.pop(call_id, None)
        self._remember_completed(self.completed_invites, call_id, pending)
        self._send_response(request, addr, 200, "OK", to_tag=pending.to_tag)
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


    async def _handle_initial_invite(self, request, addr, call_id) -> None:
        try:
            hops = sip.max_forwards(request)
        except sip.SipError:
            self._send_response(request, addr, 400, "Bad Request")
            return
        if hops == 0:
            self._send_response(request, addr, 483, "Too Many Hops")
            return
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
                status = 482 if sip.is_merged_request(request, existing_pending.request) else 400
                self._send_response(request, addr, status, "Loop Detected" if status == 482 else "Bad Request",
                                    to_tag=existing_pending.to_tag)
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
                status = 482 if sip.is_merged_request(request, completed_invite.request) else 400
                self._send_response(request, addr, status, "Loop Detected" if status == 482 else "Bad Request",
                                    to_tag=completed_invite.to_tag)
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
            if result.answer_sdp and not self._stage_answer_contract(pending, result.answer_sdp, result.media_contract):
                result = SipInviteResult(488, "Not Acceptable Here", rollback=result.rollback,
                    decline_reason="media_contract_mismatch")
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
        media_contract: SipInvite | None = None,
    ) -> bool:
        pending = self.pending_invites.get(call_id)
        if pending is not None and not self._stage_answer_contract(pending, answer_sdp, media_contract):
            return False
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
            identity = _uri_text_from_header(request.header("From"))
            if not caller and identity.lower().startswith("tel:"):
                caller = identity[4:].split(";", 1)[0]
            target = sip.name_addr_display_name(request.header("To"))
            if not target:
                target = request_uri.user
            caller_route = (
                from_uri.user
                if from_uri is not None and from_uri.user
                else caller
            )
            target_route = (
                request_uri.user
                or target
            )
            initial = SipInitialInvite(
                max_forwards=sip.max_forwards(request) if request.header("Max-Forwards") else None,
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
            local_audio_capabilities = HA_AUDIO_CAPABILITIES
            current_dialog = self.active_dialogs.get(request.header("Call-ID"))
            current = current_dialog.invite if current_dialog is not None else None
            established = (sdp.RtpPcmDirection(
                current.send_format, current.recv_format, recv_formats=current.recv_formats,
                send_formats=current.send_formats,
            ) if current is not None else None)
            selected = (
                sdp.negotiate_answer_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    local_offer_sdp=local_offer_sdp,
                    established=established,
                    previous_payloads=current_dialog.audio_payloads if current_dialog is not None else PayloadMap(),
                    codec_policy=codec_policy,
                    allow_dahua_pcm=peer_profile == "dahua",
                    local_capabilities=local_audio_capabilities,
                )
                if local_offer_sdp
                else sdp.negotiate_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    allow_dahua_pcm=peer_profile == "dahua",
                    codec_policy=codec_policy,
                    local_capabilities=local_audio_capabilities,
                    established=established,
                    previous_payloads=current_dialog.audio_payloads if current_dialog is not None else PayloadMap(),
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
                send_formats=selected.send_formats,
                max_forwards=initial.max_forwards,
                audio_payloads=selected.payloads,
                dtmf_direction=selected.dtmf_direction,
                local_recv_capabilities=tuple(self.supported_recv_formats),
                local_send_capabilities=tuple(self.supported_send_formats),
                local_audio_capabilities=local_audio_capabilities,
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
