"""SipServerDialogs: operations on the single SIP endpoint state."""

from __future__ import annotations

from .peer_media_profile import legacy_pcm_compatibility

import asyncio
import errno
from dataclasses import replace
from typing import Callable
from .sip_listener_request import SipRequestResult
from .core import sdp, sip
from .core.sip_auth import DigestChallengeTracker
from .core.sip_transport import SIP_UDP_SAFE_REQUEST_BYTES, SipResponseMailbox
from .core.sip_dialog import DialogKey, apply_remote_offer_media, notify_media_commit, build_dialog_request, uas_request_matches_dialog
from .core.sip_transaction import SIP_TIMER_B as _INVITE_2XX_TIMEOUT, async_run_dialog_request_transaction, matches_invite_error_ack
import logging

from .sip_server_state import (SipInviteResult, _ActiveDialog, _CompletedRequest, _PendingDelayedOffer, _PendingInvite, _SipRequestNotSent, _same_audio_media, _same_dialog_ack, _same_dialog_request, _same_video_media, _uri_from_header, _uri_text_from_header)


_LOGGER = logging.getLogger(__name__)

class SipServerDialogs:
    """Use the endpoint-owned flows, locks, transactions and dialog records."""

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
                await self.async_update_connected_identity(
                    call_id,
                    dialog.connected_identity_name,
                    identity_uri,
                    expected_key=DialogKey.for_uas_dialog(dialog.request, local_tag=dialog.to_tag),
                )
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


    async def async_update_connected_identity(
        self,
        call_id: str,
        name: str,
        uri: str,
        *,
        expected_key: DialogKey | None = None,
        is_current: Callable[[], bool] | None = None,
    ) -> bool:
        """Change one dialog's local identity through its existing UPDATE writer."""

        dialog = self.active_dialogs.get(call_id)
        if dialog is None or not dialog.peer_supports_from_change:
            return False
        key = DialogKey.for_uas_dialog(dialog.request, local_tag=dialog.to_tag)
        if expected_key is not None and key != expected_key:
            return False
        identity_uri = str(sip.parse_sip_uri(uri))
        identity_name = str(name or "")
        sip.format_name_addr(identity_uri, identity_name)

        def owned() -> bool:
            return (self.active_dialogs.get(call_id) is dialog
                    and dialog.termination_task is None
                    and (is_current is None or is_current()))

        # The existing 2xx owner is cancelled by ACK or retired on timeout.
        # Reuse that barrier instead of sending an identity UPDATE before ACK.
        ack_task = dialog.invite_2xx.task
        if ack_task is not None and not ack_task.done():
            try:
                await asyncio.shield(ack_task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
        initial_identity = dialog.connected_identity_task
        if (initial_identity is not None and initial_identity is not asyncio.current_task()
                and not initial_identity.done()):
            await asyncio.shield(initial_identity)
        if not owned() or dialog.invite_2xx.active:
            return False
        if (dialog.connected_identity_sent and dialog.local_uri == identity_uri
                and dialog.local_display_name == identity_name):
            return True

        def sent() -> None:
            # Sender identity persists after sending, independent of the response.
            dialog.local_uri = identity_uri
            dialog.local_display_name = identity_name
            dialog.connected_identity_user = sip.parse_sip_uri(identity_uri).user
            dialog.connected_identity_name = identity_name
            dialog.connected_identity_sent = True

        response = await self._send_dialog_request(
            call_id,
            dialog,
            "UPDATE",
            local_uri=identity_uri,
            local_display_name=identity_name,
            is_current=owned,
            on_sent=sent,
        )
        if response is None or not 200 <= int(response.status_code or 0) < 300 or not owned():
            return False
        return True


    async def _send_dialog_request(
        self,
        call_id: str,
        dialog: _ActiveDialog,
        method: str,
        *,
        local_uri: str = "",
        local_display_name: str | None = None,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
        content_type: str = "",
        timeout: float = _INVITE_2XX_TIMEOUT,
        is_current: Callable[[], bool] | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> sip.SipMessage | None:
        """Send one serialized non-INVITE request on an owned dialog."""

        async with dialog.update_lock:
            def active() -> bool:
                return (self._dialog_request_current(call_id, dialog, method)
                        and (is_current is None or is_current()))

            if not active():
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
            local_uri = local_uri or dialog.local_uri or _uri_text_from_header(dialog.request.header("To"))
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
                    local_display_name=(dialog.local_display_name
                                        if local_display_name is None else local_display_name),
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
                active=active,
                timeout=timeout,
                on_sent=on_sent,
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
                method=method,
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


    def _reject_dialog_request(self, request, addr, *, to_tag=""):
        """Distinguish a stale request in a known dialog from an absent dialog."""
        dialog = self.active_dialogs.get(request.header("Call-ID"))
        stale = False
        if dialog is not None and request.method not in {"ACK", "CANCEL"}:
            try:
                stale = (uas_request_matches_dialog(request, dialog.request, local_tag=dialog.to_tag)
                         and sip.parse_cseq(request.header("CSeq")).number < dialog.cseq)
            except (TypeError, ValueError, sip.SipError):
                pass
        self._send_response(request, addr, 500 if stale else 481,
                            "Server Internal Error" if stale else "Call/Transaction Does Not Exist",
                            to_tag=to_tag or (dialog.to_tag if dialog is not None else ""),
                            extra_headers=(("Retry-After", "1"),) if stale else ())


    async def _handle_info(self, request, addr, request_cseq) -> None:
        call_id = request.header("Call-ID")
        info_key = (call_id, request_cseq.number)
        completed_info = self.completed_infos.get(info_key)
        if completed_info is not None:
            self._replay_completed_request(request, addr, completed_info)
            return
        dialog = self.active_dialogs.get(call_id)
        if dialog is None or not _same_dialog_request(request, dialog, addr):
            self._reject_dialog_request(request, addr)
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


    async def _handle_bye(self, request, addr) -> None:
        call_id = request.header("Call-ID")
        dialog = self.active_dialogs.get(call_id)
        if dialog is None:
            completed = self.completed_byes.get(call_id)
            if completed is None:
                self._reject_dialog_request(request, addr)
            else:
                self._replay_completed_request(request, addr, completed)
            return
        same_dialog = _same_dialog_request(request, dialog, addr)
        if not same_dialog:
            self._reject_dialog_request(request, addr)
            return
        refer_owner = dialog.refer_task
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
            if refer_owner is not None and not refer_owner.done():
                async def settled():
                    try:
                        await asyncio.shield(refer_owner)
                    except asyncio.CancelledError:
                        return
                    except Exception:
                        _LOGGER.debug("Transfer completed with an error before BYE cleanup", exc_info=True)
                    if self.on_terminated is not None:
                        await self.on_terminated(call_id, "remote_hangup")

                task = asyncio.create_task(settled(), name=f"voip-sip-bye-after-refer-{call_id}")
                self._maintenance_tasks.add(task)
                task.add_done_callback(self._maintenance_tasks.discard)
            else:
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
            self._reject_dialog_request(request, addr, to_tag=dialog.to_tag)
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


    async def _answer_dialog_offer(self, request, addr, request_cseq, call_id, dialog, refreshed_remote_target, updated_invite, result) -> None:
        status = int(result.status)
        reason = str(result.reason)
        answer_sdp = str(result.answer_sdp or "")
        updated_invite = result.media_contract or updated_invite
        if 200 <= status < 300 and answer_sdp:
            from .core.media.negotiator import validate_answer_contract
            try:
                if updated_invite is None or updated_invite.call_id != call_id:
                    raise sdp.SdpError("answer has no matching committed media contract")
                sdp.validate_sdp_answer(request.body, answer_sdp)
                validate_answer_contract(sdp.RtpPcmDirection(updated_invite.send_format,
                    updated_invite.recv_format, updated_invite.recv_formats, updated_invite.send_formats),
                    answer_sdp, events=updated_invite.dtmf_direction,
                    compatibility=legacy_pcm_compatibility(updated_invite.peer_profile == "dahua"), remote_sdp=request.body)
            except (ValueError, sdp.SdpError):
                status, reason = 488, "Not Acceptable Here"
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
                dialog.install_invite(updated_invite)
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


    async def async_send_bye(self, call_id: str = "", *, timeout: float = 1.5) -> bool:
        """Release the caller's wait while one owned BYE transaction finishes."""
        resolved = self._dialog_for_bye(call_id)
        if resolved is None:
            return False
        call_id, dialog = resolved
        owner = dialog.termination_task
        if owner is None:
            owner = asyncio.create_task(
                self._finish_local_bye(call_id, dialog),
                name=f"voip-sip-bye-{call_id}",
            )
            dialog.termination_task = owner
            self._maintenance_tasks.add(owner)
            owner.add_done_callback(self._maintenance_tasks.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(owner), timeout=max(0.0, timeout))
        except TimeoutError:
            return True


    async def _finish_local_bye(self, call_id: str, dialog: _ActiveDialog) -> bool:
        """Retain signaling through Timer F without retaining call media."""
        try:
            response = await self._send_dialog_request(
                call_id, dialog, "BYE", timeout=_INVITE_2XX_TIMEOUT,
            )
            if response is None:
                _LOGGER.debug("SIP BYE completed without a final response call_id=%s", call_id)
        except (ConnectionError, OSError):
            _LOGGER.debug("SIP BYE signaling became unavailable call_id=%s", call_id, exc_info=True)
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
