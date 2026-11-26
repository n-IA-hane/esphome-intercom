"""SipDialogRequests: operations on the SIP client's single owned state."""

from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator
from collections.abc import Callable
from dataclasses import replace
from .core import sip
from .core.sip_auth import DigestChallengeTracker, build_digest_authorization
from .core.sip_dialog import SubscriptionDialog, build_dialog_request
from .dtmf import build_sip_info_dtmf_body
from .core.sip_transaction import SIP_TIMER_F
import logging

from .sip_client_types import (SipDialog)


_LOGGER = logging.getLogger(__name__)

class SipDialogRequests:
    """Use the existing client reader, writer, locks and dialog state."""

    async def _send_in_dialog_request(
        self,
        method: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
        content_type: str = "",
        timeout: float = 8.0,
        usage: SubscriptionDialog | None = None,
        local_uri: str | None = None,
        local_display_name: str | None = None,
        is_current: Callable[[], bool] | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> sip.SipMessage | None:
        """Run one serialized non-INVITE client transaction on the dialog."""

        deadline = asyncio.get_running_loop().time() + timeout
        try:
            async with asyncio.timeout_at(deadline):
                async with self._dialog_writer():
                    dialog = self.dialog
                    if dialog is None and usage is None:
                        return None
                    challenges = DigestChallengeTracker()
                    pending_auth: dict[str, tuple[str, int]] = {}
                    request = None
                    def active():
                        owned = ((usage.key.call_id in self._outside_refer or usage.key.call_id in self._event_dialogs)
                                 if usage is not None else self._dialog_is_current(dialog))
                        return owned and (is_current is None or is_current())

                    while active() and asyncio.get_running_loop().time() < deadline:
                        authorization = {}
                        for header, (challenge, count) in tuple(pending_auth.items()):
                            assert request is not None
                            authorization[header] = build_digest_authorization(
                                challenge_header=challenge,
                                username=self.username,
                                auth_username=self.auth_username,
                                password=self.password,
                                method=method,
                                uri=request.routing.request_uri,
                                nonce_count=count + 1,
                                body=body,
                            )
                            pending_auth[header] = (challenge, count + 1)
                        try:
                            request = build_dialog_request(
                                method,
                                call_id=usage.key.call_id if usage else self.dialog_ids.call_id,
                                local_tag=usage.key.local_tag if usage else self.dialog_ids.local_tag,
                                remote_tag=usage.key.remote_tag if usage else self.dialog_ids.remote_tag,
                                cseq=usage.next_cseq() if usage and usage.key.call_id != self.dialog_ids.call_id else self._next_dialog_cseq(),
                                local_uri=local_uri if local_uri is not None else (usage.local_uri if usage else dialog.local_uri),
                                remote_uri=usage.remote_uri if usage else dialog.remote_uri,
                                remote_target_uri=usage.remote_target_uri if usage else dialog.remote_target_uri,
                                route_set=usage.route_set if usage else dialog.route_set,
                                contact_uri=self._pending_contact_uri or (usage.local_uri if usage else dialog.local_uri),
                                transport=self.signaling_transport,
                                via_sent_by=(self.local_ip, self.local_sip_port),
                                local_display_name=self.local_name if local_display_name is None else local_display_name,
                                remote_display_name=self._dialog_remote_display_name,
                                extra_headers=(*extra_headers, *authorization.items()),
                                content_type=content_type,
                                body=body,
                            )
                            next_host, next_port = self._dialog_next_hop(
                                request.routing.next_hop_uri,
                                sip.parse_sip_uri(usage.remote_target_uri).host if usage else dialog.remote_host,
                                sip.sip_default_port(sip.parse_sip_uri(usage.remote_target_uri)) if usage else dialog.remote_sip_port,
                            )
                        except (TypeError, ValueError, sip.SipError):
                            return None
                        if method == "REFER" and self._refer_subscription is not None:
                            if usage is not None and usage.key.call_id != self.dialog_ids.call_id:
                                self._refer_subscription.bind(request.ids.cseq, first=True)
                            else:
                                key = (request.ids.call_id, request.ids.local_tag, request.ids.remote_tag)
                                if key != self._refer_dialog_key:
                                    self._refer_dialog_key, self._refer_count = key, 0
                                self._refer_subscription.bind(request.ids.cseq, first=self._refer_count == 0)
                                self._refer_count += 1
                        try:
                            response = await self._run_built_dialog_request(
                                request,
                                next_host,
                                next_port,
                                method=method,
                                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                                active=active, dialog_scoped=usage is None,
                                on_sent=on_sent,
                            )
                        except ConnectionAbortedError:
                            raise
                        except (ConnectionError, OSError):
                            return None
                        if usage is not None and response is not None and 200 <= int(response.status_code or 0) < 300:
                            remote_tag = sip.extract_tag(response.header("To"))
                            if usage.key.remote_tag and remote_tag != usage.key.remote_tag:
                                return None
                        if response is None or response.status_code not in {401, 407} or not self.password:
                            return response
                        header = "Proxy-Authorization" if response.status_code == 407 else "Authorization"
                        try:
                            challenge = challenges.claim(
                                header,
                                response.header_values(
                                    "Proxy-Authenticate" if response.status_code == 407 else "WWW-Authenticate"
                                ),
                            )
                        except ValueError:
                            _LOGGER.info("SIP %s authentication rejected", method)
                            return response
                        pending_auth[header] = (challenge, 0)

                return None

        except TimeoutError:
            return None


    async def async_update_connected_identity(
        self,
        name: str,
        uri: str,
        *,
        expected_dialog: SipDialog,
        is_current: Callable[[], bool] | None = None,
    ) -> bool:
        """Publish RFC 4916 identity on this call without changing the account."""

        identity_uri = str(sip.parse_sip_uri(uri))
        identity_name = str(name or "")
        sip.format_name_addr(identity_uri, identity_name)

        def owned() -> bool:
            return (not self._closing and not self._closed
                    and not self.fixed_local_identity
                    and self._dialog_is_current(expected_dialog)
                    and self.dialog.peer_supports_from_change
                    and (is_current is None or is_current()))

        if not owned():
            return False

        def sent() -> None:
            # RFC 4916 sender state follows the sent From, even after rejection.
            self.dialog = replace(self.dialog, local_uri=identity_uri)
            self.local_name = identity_name

        response = await self._send_in_dialog_request(
            "UPDATE",
            local_uri=identity_uri,
            local_display_name=identity_name,
            is_current=owned,
            on_sent=sent,
        )
        if response is None or not 200 <= int(response.status_code or 0) < 300 or not owned():
            return False
        return True


    @contextlib.asynccontextmanager
    async def _dialog_writer(self) -> AsyncIterator[None]:
        """Serialize requests without interrupting the transport reader."""
        async with self._dialog_request_lock:
            yield


    async def request_video_keyframe(self, *, timeout: float = 3.0) -> bool:
        """Request a full intra frame with RFC 5168 media control."""

        response = await self._send_in_dialog_request(
            "INFO",
            body=sip.RFC5168_PICTURE_FAST_UPDATE_BODY,
            content_type="application/media_control+xml",
            timeout=timeout,
        )
        return bool(
            response is not None
            and 200 <= int(response.status_code or 0) < 300
        )


    async def send_dtmf_info(
        self,
        digit: str,
        *,
        duration_ms: int = 160,
        timeout: float = 3.0,
    ) -> bool:
        """Send one DTMF digit on the owned dialog using legacy SIP INFO."""

        try:
            body = build_sip_info_dtmf_body(digit, duration_ms=duration_ms)
        except ValueError:
            return False
        response = await self._send_in_dialog_request(
            "INFO",
            body=body,
            content_type="application/dtmf-relay",
            timeout=timeout,
        )
        return bool(
            response is not None
            and 200 <= int(response.status_code or 0) < 300
        )


    async def _run_bye_transaction(self, dialog: SipDialog, timeout: float) -> str:
        """Own the confirmed-dialog BYE and publish its terminal outcome."""

        if not self._dialog_is_current(dialog) or not self._has_signaling_path():
            return "transport_unreachable"
        self._uas_invite_2xx.cancel()
        outcome = "transport_unreachable"
        try:
            response = await self._send_in_dialog_request("BYE", timeout=timeout)
            if response is None:
                outcome = "timeout"
            elif 200 <= int(response.status_code or 0) < 300 or response.status_code == 481:
                outcome = "remote_hangup"
            else:
                outcome = sip.sip_failure_reason(int(response.status_code or 0))
        except ConnectionAbortedError as err:
            outcome = str(err) or "remote_hangup"
        finally:
            if self._dialog_is_current(dialog):
                self.dialog = None
                self._publish_dialog_terminal(outcome)
        return outcome


    def _start_bye_transaction(self, timeout: float = SIP_TIMER_F) -> asyncio.Task[str] | None:
        """Start exactly one BYE owner for the current confirmed dialog."""

        task = self._dialog_termination_task
        if task is not None:
            return task
        dialog = self.dialog
        if dialog is None or not self._has_signaling_path():
            return None
        task = asyncio.create_task(
            self._run_bye_transaction(dialog, timeout),
            name=f"voip-sip-client-bye-{self.dialog_ids.call_id}",
        )
        self._dialog_termination_task = task
        return task


    async def _terminate_confirmed_dialog(self, timeout: float = 1.5) -> str:
        task = self._start_bye_transaction(SIP_TIMER_F)
        if task is None:
            return "transport_unreachable"
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, timeout))
        except TimeoutError:
            return "bye_pending"


    def bye(self) -> bool:
        """Compatibility entry point that delegates to the single BYE owner."""

        return self._start_bye_transaction() is not None


    def bye_or_cancel(self) -> None:
        if self.dialog is not None:
            self.bye()
        else:
            self.cancel()


    async def terminate(self, timeout: float = 1.5) -> str:
        """Terminate the SIP dialog/transaction and wait for the SIP response.

        A confirmed dialog ends with BYE + 200 OK. An early INVITE transaction
        ends when its final response arrives after CANCEL. The CANCEL client
        transaction is independent, so a final non-2xx INVITE response is
        sufficient to stop waiting once it has been ACKed.
        """
        if self.dialog is not None or self._dialog_termination_task is not None:
            return await self._terminate_confirmed_dialog(timeout)

        invite_task = self._invite_task
        if invite_task is not None and not invite_task.done():
            if not self.request_cancel():
                return "transport_unreachable"
            try:
                return await asyncio.wait_for(asyncio.shield(invite_task), timeout=timeout)
            except asyncio.TimeoutError:
                invite_task.add_done_callback(self._schedule_deferred_close)
                return "cancel_pending"

        sent_cancel = self.cancel()
        if not sent_cancel:
            return "transport_unreachable"
        saw_cancel_ok = False
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                received = await self._read_response(max(0.05, deadline - asyncio.get_running_loop().time()))
            except asyncio.TimeoutError:
                break
            except sip.SipError as err:
                _LOGGER.debug(
                    "Ignoring malformed SIP response while waiting for CANCEL: %s",
                    err,
                )
                continue
            if received is None:
                break
            msg, addr = received
            if not msg.is_response or msg.status_code is None:
                continue
            sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
            _LOGGER.debug("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            try:
                cseq = sip.parse_cseq(msg.header("CSeq"))
                via_values = msg.header_values("Via")
                response_branch = sip.parse_via(via_values[0] if via_values else "").branch
            except (TypeError, ValueError, sip.SipError):
                continue
            cseq_method = cseq.method
            if cseq_method in {"CANCEL", "INVITE"}:
                if cseq.number != self._invite_cseq or response_branch != self.dialog_ids.branch:
                    continue
            else:
                continue
            if cseq_method == "CANCEL" and 200 <= msg.status_code < 300:
                saw_cancel_ok = True
            elif cseq_method == "INVITE" and 200 <= msg.status_code < 300:
                # The final 2xx won the race with CANCEL. RFC 3261 requires us
                # to accept it, ACK it, then end the confirmed dialog with BYE.
                try:
                    prepared = await self._prepare_response_signaling_target(msg, self._pending_request_uri)
                except (OSError, RuntimeError, TimeoutError, sip.SipError):
                    return "transport_unreachable"
                self._commit_dialog_signaling_target(prepared)
                committed = self._commit_200_ok(
                    msg,
                    self._pending_target,
                    self._pending_remote_host or addr[0],
                    self._pending_remote_sip_port,
                    self._pending_request_uri,
                    self._pending_local_uri,
                    self._pending_remote_uri,
                )
                if committed:
                    remaining = max(
                        0.05,
                        deadline - asyncio.get_running_loop().time(),
                    )
                    await self._terminate_confirmed_dialog(timeout=remaining)
                return "cancelled"
            elif cseq_method == "INVITE" and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                self._invite_transaction_active = False
                # RFC 3261 makes the INVITE and CANCEL client transactions
                # independent. A final INVITE response proves that the call
                # attempt is over even if the 200 to CANCEL was lost or was
                # consumed by the transaction owner just before teardown.
                return "cancelled"
        if saw_cancel_ok:
            return "cancelled"
        return "timeout"
