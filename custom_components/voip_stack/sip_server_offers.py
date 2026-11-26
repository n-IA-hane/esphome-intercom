"""SipServerOffers: operations on the single SIP endpoint state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES
from .core import sdp, sip
from .core.sip_dialog import apply_remote_offer_media, notify_media_commit
from .core.sip_transaction import SessionTimerDriver
from .audio_rtcp import remote_audio_rtcp_fields
from .inbound_identity import identity_fields
import logging

from .sip_server_state import (SipInvite, SipInviteResult, _ActiveDialog, _PendingDelayedOffer)
from .peer_media_profile import legacy_pcm_compatibility


_LOGGER = logging.getLogger(__name__)

class SipServerOffers:
    """Use the endpoint-owned flows, locks, transactions and dialog records."""

    async def async_prepare_video_reinvite(
        self,
        call_id: str,
        *,
        local_video_rtp_port: int,
        video_formats: tuple[sdp.RtpVideoFormat, ...],
        video_direction: str = "sendrecv",
        timeout: float = 8.0,
        session_refresh: bool = False,
        extra_headers: tuple[tuple[str, str], ...] = (),
        response_callback=None,
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
            or (not video_formats and not session_refresh)
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
                current_local_sdp, optional_mappings=legacy_pcm_compatibility(previous.peer_profile == "dahua").optional_mappings,
            ))
            if not current_audio_formats:
                return None
            offer = current_local_sdp if session_refresh else sdp.build_video_reoffer(
                current_local_sdp, video_port=int(local_video_rtp_port),
                video_formats=dialog.codec_policy.video_formats(tuple(video_formats)), video_direction=direction)
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
                extra_headers=extra_headers,
                timeout=timeout,
            )
            if response_callback is not None:
                response_callback(response)
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
                compatibility=legacy_pcm_compatibility(previous.peer_profile == "dahua"),
                local_capabilities=HA_AUDIO_CAPABILITIES,
                previous_payloads=dialog.audio_payloads,
                established=sdp.RtpPcmDirection(previous.send_format, previous.recv_format,
                    previous.recv_formats),
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
                recv_formats=audio.recv_formats,
                send_formats=audio.send_formats,
                audio_payloads=audio.payloads,
                dtmf_direction=audio.dtmf_direction,
                remote_rtp_host=str(parsed["connection_ip"]),
                remote_rtp_port=int(parsed["media_port"]),
                **remote_audio_rtcp_fields(response.body),
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
        dialog.install_invite(candidate)
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
                audio_rtcp_min_interval=initial.audio_rtcp_min_interval,
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
                updated_invite = result.media_contract or updated_invite
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
        dialog.install_invite(updated_invite)
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


    async def _send_session_refresh(self, call_id, dialog, method, *, extra_headers=()):
        """Use the listener's existing offer, ACK and media commit owners."""
        if self.active_dialogs.get(call_id) is not dialog or dialog.invite is None:
            return None
        remaining = dialog.session_timer.deadline - asyncio.get_running_loop().time()
        timeout = min(8.0, remaining) if dialog.session_timer.deadline else 8.0
        if timeout <= 0:
            return None
        if method == "UPDATE":
            return await self._send_dialog_request(call_id, dialog, method,
                extra_headers=extra_headers, timeout=timeout)
        previous, response = dialog.invite, None

        def received(message):
            nonlocal response
            response = message

        try:
            local = dialog.local_sdp_body or dialog.answer_sdp
            video = sdp.parse_video_sdp(local)
            candidate = await self.async_prepare_video_reinvite(call_id,
                local_video_rtp_port=int(video["media_port"]) if video else 0,
                video_formats=tuple(sdp.offered_video_formats(local)),
                video_direction=str(video["direction"]) if video else "inactive",
                timeout=timeout, session_refresh=True, extra_headers=extra_headers,
                response_callback=received)
        except (ValueError, sdp.SdpError, sip.SipError):
            return None
        if candidate is None:
            return response if response is not None and int(response.status_code or 0) >= 300 else None
        result = None
        try:
            if self.on_media_update is not None:
                result = await self.on_media_update(previous, candidate, "INVITE")
                if result is None or not 200 <= result.status < 300:
                    self.abort_prepared_reinvite(call_id, previous, candidate)
                    return None
            elif replace(candidate, remote_sdp=previous.remote_sdp) != previous:
                self.abort_prepared_reinvite(call_id, previous, candidate)
                return None

            async def commit_current():
                if self.active_dialogs.get(call_id) is not dialog or dialog.invite is not previous:
                    raise ConnectionAbortedError("SIP refresh dialog changed during preparation")
                if result is not None and result.commit is not None:
                    await result.commit()

            async def publish():
                if result is not None and result.finalize is not None:
                    await result.finalize()
                if not self.commit_prepared_reinvite(call_id, previous, candidate):
                    raise ConnectionAbortedError("SIP refresh dialog changed before commit")
                if response is not None and (target := sip.contact_target_uri(response)):
                    dialog.remote_target_uri = target
                if result is not None:
                    notify_media_commit(result.on_committed, candidate)

            if not await apply_remote_offer_media(commit_current,
                    result.rollback if result is not None else None, finalize=publish):
                self.abort_prepared_reinvite(call_id, previous, candidate)
                return None
            return response
        except asyncio.CancelledError:
            self.abort_prepared_reinvite(call_id, previous, candidate)
            raise
        except Exception:
            self.abort_prepared_reinvite(call_id, previous, candidate)
            return None


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
                    partial(self._send_session_refresh, call_id, dialog),
                    local_role,
                    asyncio.get_running_loop().time,
                    "UPDATE" if any("UPDATE" in {token.strip().upper() for token in value.split(",")}
                        for value in dialog.request.header_values("Allow")) else "INVITE",
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
