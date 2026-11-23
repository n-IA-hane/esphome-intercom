"""SipLocalOffers: operations on the SIP client's single owned state."""

from __future__ import annotations

import asyncio
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES
from .core.media.payload_map import PayloadConflict, PayloadMap
from .core import sdp, sip
from .core.sip_auth import DigestChallengeTracker, build_digest_authorization
from .core.sip_dialog import PreparedDialogIdentity, build_dialog_request
import logging

from .sip_client_types import (SipDialog, _audio_payload_update, _video_payload_update)


_LOGGER = logging.getLogger(__name__)

class SipLocalOffers:
    """Use the existing client reader, writer, locks and dialog state."""

    async def async_prepare_video_reinvite(
        self,
        *,
        local_video_rtp_port: int,
        video_formats: tuple[sdp.RtpVideoFormat, ...],
        video_direction: str = "sendrecv",
        timeout: float = 8.0,
        offer_template: str | None = None,
        expected_dialog: SipDialog | None = None,
        media_sockets: dict | None = None,
        _restore_media_endpoints: dict | None = None,
        session_refresh: bool = False,
        extra_headers: tuple[tuple[str, str], ...] = (),
        response_callback=None,
    ) -> SipDialog | None:
        """Change video with one serialized in-dialog offer, without publishing it."""

        deadline = asyncio.get_running_loop().time() + timeout
        try:
            await asyncio.wait_for(self._local_offer_lock.acquire(), timeout=timeout)
        except TimeoutError:
            return None
        try:
            current = self.dialog
            offered_video = tuple(video_formats)
            local_video_port = int(local_video_rtp_port)
            removing_video = local_video_port == 0
            if not removing_video:
                offered_video = self.codec_policy.video_formats(offered_video)
            if (
                current is None
                or (expected_dialog is not None and current is not expected_dialog)
                or self._prepared_reinvite is not None
                or self._closing
                or self._closed
                or (not offered_video and not session_refresh)
                or local_video_port < 0
                or (removing_video and current.video_format is None and not session_refresh)
            ):
                return None
            if current._local_media_endpoints is None:
                current._local_media_endpoints = dict(self.media_endpoints)
            mapped_endpoints = (self.media_endpoints if _restore_media_endpoints is None
                                else _restore_media_endpoints)
            if mapped_endpoints and local_video_port > 0:
                previous_endpoint = mapped_endpoints.get("video")
                if previous_endpoint is None or previous_endpoint.local_port != local_video_port:
                    if media_sockets is None or self.discover_media_sockets is None:
                        return None
                    if set(media_sockets) != {"video", "video_rtcp"} or media_sockets["video"].getsockname()[1] != local_video_port:
                        return None
                    try:
                        async with asyncio.timeout(min(3, max(0, deadline - asyncio.get_running_loop().time()))):
                            additional = await self.discover_media_sockets(media_sockets)
                    except (OSError, ValueError):
                        return None
                    mapped_endpoints = {**mapped_endpoints, **additional}
            fallback_target = ""
            imageattr_round = 0
            challenges = DigestChallengeTracker()
            pending_auth = {}
            request = None
            for _attempt in range(4):
                try:
                    session_id = int(
                        current.local_sdp_session_id or self._sdp_session_id
                    )
                    session_version = int(current.local_sdp_session_version) + imageattr_round
                    offer = (offer_template or current.local_sdp_body) if session_refresh else sdp.build_video_reoffer(
                        offer_template or current.local_sdp_body, video_port=local_video_port,
                        video_formats=offered_video, video_direction="inactive" if removing_video else video_direction)
                    offer = self._advertise_media(offer, mapped_endpoints)
                    offer = sdp.rewrite_sdp_origin(
                        offer, session_id, session_version
                    )
                    if sdp.sdp_description_changed(current.local_sdp_body, offer):
                        session_version += 1
                        offer = sdp.rewrite_sdp_origin(
                            offer, session_id, session_version
                        )
                    authorization = {}
                    for header, (challenge, count) in tuple(pending_auth.items()):
                        authorization[header] = build_digest_authorization(
                            challenge_header=challenge, username=self.username,
                            auth_username=self.auth_username, password=self.password,
                            method="INVITE", uri=request.routing.request_uri,
                            nonce_count=count + 1, body=offer.encode())
                        pending_auth[header] = (challenge, count + 1)
                    request = build_dialog_request(
                        "INVITE",
                        call_id=self.dialog_ids.call_id,
                        local_tag=self.dialog_ids.local_tag,
                        remote_tag=self.dialog_ids.remote_tag,
                        cseq=self._next_dialog_cseq(),
                        local_uri=current.local_uri,
                        remote_uri=current.remote_uri,
                        remote_target_uri=(
                            fallback_target or current.remote_target_uri or current.remote_uri
                        ),
                        route_set=current.route_set,
                        contact_uri=self._pending_contact_uri or current.local_uri,
                        transport=self.signaling_transport,
                        via_sent_by=(self.local_ip, self.local_sip_port),
                        local_display_name=self.local_name,
                        remote_display_name=self._dialog_remote_display_name,
                        extra_headers=(*extra_headers, *authorization.items()),
                        content_type="application/sdp",
                        body=offer.encode(),
                    )
                    next_host, next_port = self._dialog_next_hop(
                        request.routing.next_hop_uri,
                        current.remote_host,
                        current.remote_sip_port,
                    )
                except (TypeError, ValueError, sdp.SdpError, sip.SipError):
                    _LOGGER.exception(
                        "SIP local video re-INVITE could not be prepared call_id=%s",
                        self.dialog_ids.call_id,
                    )
                    return None

                try:
                    await asyncio.wait_for(self._dialog_request_lock.acquire(), timeout=max(0, deadline - asyncio.get_running_loop().time()))
                except TimeoutError:
                    return None
                try:
                    if self.dialog is not current:
                        return None
                    _LOGGER.info(
                        "SIP TX re-INVITE call_id=%s offered=[%s]",
                        self.dialog_ids.call_id,
                        ", ".join(sdp.offered_media_descriptions(offer)),
                    )
                    early_candidate: SipDialog | None = None
                    final_addr = (next_host, next_port)

                    async def handle_request(message, addr) -> None:
                        terminal = await self._dispatch_in_dialog_request(
                            message, addr, local_offer_pending=True
                        )
                        if terminal is not None:
                            raise ConnectionAbortedError(terminal)

                    async def handle_provisional(message, addr) -> None:
                        nonlocal early_candidate
                        status = int(message.status_code or 0)
                        self.last_sip_status_code = status
                        self.last_sip_reason = message.reason
                        if not await self._ack_reliable_provisional(
                            message, addr, process_early_media=False
                        ):
                            raise ConnectionAbortedError("provisional_rejected")
                        if message.body:
                            early_candidate = self._dialog_candidate_from_answer(
                                current,
                                offer,
                                message,
                                local_video_rtp_port=local_video_port,
                                offered_video_formats=offered_video,
                                video_direction=(
                                    "inactive" if removing_video else video_direction
                                ),
                                session_version=session_version,
                            )
                            if early_candidate is None:
                                raise ConnectionAbortedError("invalid_early_answer")

                    def capture_final(_message, addr) -> None:
                        nonlocal final_addr
                        final_addr = addr

                    try:
                        message = await self._run_built_dialog_request(
                            request, next_host, next_port, method="INVITE", timeout=max(0, deadline - asyncio.get_running_loop().time()),
                            active=lambda: self.dialog is current,
                            on_provisional=handle_provisional, on_final=capture_final,
                        )
                    except (ConnectionAbortedError, ConnectionError, OSError):
                        return None
                    if response_callback is not None:
                        response_callback(message)
                    if message is None:
                        self._start_bye_request_transaction(
                            current.remote_host,
                            current.remote_sip_port,
                            current.remote_target_uri or current.remote_uri,
                            current.local_uri,
                            current.remote_uri,
                            route_set=current.route_set,
                        )
                        self.dialog = None
                        return None
                    addr = final_addr
                    status = int(message.status_code or 0)
                    self.last_sip_status_code = status
                    self.last_sip_reason = message.reason
                    if 200 <= status < 300:
                        candidate = (
                            self._dialog_candidate_from_answer(
                                current,
                                offer,
                                message,
                                local_video_rtp_port=local_video_port,
                                offered_video_formats=offered_video,
                                video_direction=(
                                    "inactive" if removing_video else video_direction
                                ),
                                session_version=session_version,
                            )
                            if message.body
                            else early_candidate
                        )
                        remote_tag = sip.extract_tag(message.header("To"))
                        ack_target = (
                            candidate.remote_target_uri
                            if candidate is not None
                            else current.remote_target_uri or current.remote_uri
                        )
                        try:
                            async with asyncio.timeout_at(deadline):
                                signaling = await self._prepare_dialog_signaling_target(ack_target, current.route_set)
                        except (OSError, RuntimeError, TimeoutError, sip.SipError):
                            return None
                        if self.dialog is not current:
                            return None
                        self._commit_dialog_signaling_target(signaling)
                        acked = self._send_ack(
                            current.remote_host,
                            current.remote_sip_port,
                            ack_target,
                            current.local_uri,
                            current.remote_uri,
                            route_set=current.route_set,
                            cseq=request.ids.cseq,
                            remote_tag=remote_tag,
                        )
                        if candidate is None or not acked:
                            self._start_bye_request_transaction(
                                current.remote_host,
                                current.remote_sip_port,
                                ack_target,
                                current.local_uri,
                                current.remote_uri,
                                route_set=current.route_set,
                            )
                            self.dialog = None
                            return None
                        if candidate.imageattr_fallback_needed and imageattr_round == 0:
                            imageattr_round += 1
                            offered_video = sdp.remove_image_attributes(offered_video)
                            offer_template = sdp.remove_image_attributes(offer)
                            fallback_target = candidate.remote_target_uri
                            continue
                        self._prepared_reinvite = (
                            current,
                            candidate,
                            offered_video if candidate.video_format is not None else (),
                            (
                                video_direction
                                if candidate.video_format is not None
                                else "inactive"
                            ),
                            mapped_endpoints,
                        )
                        return candidate
                    self._send_invite_error_ack(
                        message,
                        addr[0],
                        addr[1],
                        request_uri=request.routing.request_uri,
                        local_uri=current.local_uri,
                        remote_uri=current.remote_uri,
                        route_set=current.route_set,
                        cseq=request.ids.cseq,
                        branch=request.ids.branch,
                    )
                    if status in {401, 407} and self.password:
                        header = "Proxy-Authorization" if status == 407 else "Authorization"
                        try:
                            challenge = challenges.claim(header, message.header_values(
                                "Proxy-Authenticate" if status == 407 else "WWW-Authenticate"))
                        except ValueError:
                            return None
                        pending_auth[header] = (challenge, 0)
                        continue
                    if status in {408, 481}:
                        self.dialog = None
                    return None
                finally:
                    self._dialog_request_lock.release()
        finally:
            self._local_offer_lock.release()


    def prepared_reinvite_identity(self, candidate: SipDialog) -> PreparedDialogIdentity | None:
        """Expose the exact baseline, including while its owner is closing.

        Commit independently rejects closing owners. Keeping their identity
        available lets callers retire an accepted offer instead of orphaning it.
        """
        prepared = self._prepared_reinvite
        if (prepared is None or prepared[1] is not candidate
                or self.dialog is not prepared[0]):
            return None
        return PreparedDialogIdentity(self, prepared[0], candidate)


    def commit_prepared_reinvite(
        self,
        previous: SipDialog,
        candidate: SipDialog,
    ) -> bool:
        """Publish exactly the staged dialog generation."""

        prepared = self._prepared_reinvite
        if (
            prepared is None
            or prepared[0] is not previous
            or prepared[1] is not candidate
            or self.dialog is not previous
            or self._closing
            or self._closed
        ):
            return False
        self.dialog = candidate
        self.video_formats = prepared[2]
        self.video_format = candidate.video_format
        self.video_direction = prepared[3]
        self.local_video_rtp_port = candidate.local_video_rtp_port
        self.generic_video_relay = candidate.video_format is not None
        self.media_endpoints = prepared[4]
        if candidate.video_format is None:
            self.media_endpoints = {key: value for key, value in self.media_endpoints.items()
                                    if key not in {"video", "video_rtcp"}}
        candidate._local_media_endpoints = dict(self.media_endpoints)
        self._prepared_reinvite = None
        return True


    async def restore_prepared_reinvite(self, previous: SipDialog, candidate: SipDialog) -> SipDialog | None:
        """Compensate a remotely accepted offer while its dialog is still ours."""
        if self.dialog is previous:
            if not self.commit_prepared_reinvite(previous, candidate):
                return None
        elif self.dialog is not candidate:
            return None
        formats = sdp.offered_video_formats(previous.local_sdp_body) or tuple(
            item for item in (previous.video_format, candidate.video_format) if item is not None)
        restored = await self.async_prepare_video_reinvite(
            local_video_rtp_port=previous.local_video_rtp_port if previous.video_format else 0,
            video_formats=tuple(formats),
            video_direction=previous.local_video_direction or "inactive",
            offer_template=previous.local_sdp_body,
            expected_dialog=candidate,
            _restore_media_endpoints=previous._local_media_endpoints,
        )
        if restored is not None and self.commit_prepared_reinvite(candidate, restored):
            return restored
        if self.dialog is candidate:
            await self.terminate(timeout=1.5)
        return None


    def abort_prepared_reinvite(
        self,
        previous: SipDialog,
        candidate: SipDialog,
    ) -> None:
        """Terminate a destination that accepted video after source rollback."""

        prepared = self._prepared_reinvite
        if (
            (prepared is None or prepared[:2] != (previous, candidate))
            and self.dialog is not candidate
        ):
            return
        self._prepared_reinvite = None
        self._start_bye_request_transaction(
            candidate.remote_host,
            candidate.remote_sip_port,
            candidate.remote_target_uri or candidate.remote_uri,
            candidate.local_uri,
            candidate.remote_uri,
            route_set=candidate.route_set,
        )
        self.dialog = None


    def _commit_200_ok(
        self,
        msg: sip.SipMessage,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
        *,
        provisional: bool = False,
    ) -> bool:
        if not provisional:
            self._invite_transaction_active = False
        if not request_uri:
            request_uri = str(sip.SipUri(target or "voip", remote_host, remote_sip_port))
        transport_param = (("transport", self.signaling_transport.lower()),)
        if not local_uri:
            local_uri = str(
                sip.SipUri(
                    self.local_uri_user,
                    self.local_ip,
                    self.local_sip_port,
                    params=transport_param,
                )
            )
        if not remote_uri:
            remote_uri = request_uri
        # The dialog remote URI comes from the accepted To, independently of
        # the Request-URI and the Contact used for subsequent routing.
        try:
            remote_uri = str(sip.parse_sip_uri(msg.header("To")))
        except (TypeError, ValueError, sip.SipError):
            return False
        remote_target_uri = request_uri
        try:
            contact_target = sip.contact_target_uri(msg)
        except (TypeError, ValueError, sip.SipError):
            _LOGGER.info(
                "SIP 200 OK has invalid Contact; retaining original remote target"
            )
        else:
            if contact_target:
                remote_target_uri = contact_target
        response_display = sip.name_addr_display_name(msg.header("To"))
        self._dialog_remote_display_name = response_display
        self.connected_party = next(
            (
                identity
                for identity in (
                    response_display,
                    self._pending_target_display,
                    sip.name_addr_identity(msg.header("To")),
                )
                if identity
            ),
            str(target or "").strip(),
        )
        try:
            route_set = sip.record_route_set(msg, reverse=True)
        except (TypeError, ValueError, sip.SipError):
            route_set = self._preloaded_route_set()
            _LOGGER.info(
                "SIP 200 OK has invalid Record-Route; retaining configured route"
            )
        else:
            route_set = route_set or self._preloaded_route_set()
        remote_tag = sip.extract_tag(msg.header("To"))
        if not remote_tag:
            return False
        if (
            not provisional
            and not msg.body
            and (candidate := self.early_dialogs.get(remote_tag)) is not None
        ):
            candidate.remote_target_uri = remote_target_uri
            candidate.remote_uri = remote_uri
            candidate.route_set = route_set
            self.dialog_ids.remote_tag = remote_tag
            self.dialog = candidate
            if self._initial_delayed_offer:
                self._local_sdp_body = candidate.local_sdp_body
            self.early_dialogs.clear()
            return self._send_ack(
                remote_host,
                int(remote_sip_port),
                remote_target_uri,
                local_uri,
                remote_uri,
                route_set=route_set,
            )
        if self._closing or self._closed:
            # A 2xx terminates the INVITE transaction even when local teardown
            # won the race.  ACK it and immediately end the just-created remote
            # dialog, but never publish that dialog into a closing client.
            if not provisional:
                self._reject_confirmed_dialog(
                    remote_tag=remote_tag,
                    remote_host=remote_host,
                    remote_sip_port=remote_sip_port,
                    remote_target_uri=remote_target_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    route_set=route_set,
                )
            return False
        if self._initial_delayed_offer and (
            not provisional or "100rel" in sip.option_tags(msg, "Require")
        ):
            if (
                not msg.body
                or msg.header("Content-Type").split(";", 1)[0].strip().lower()
                != "application/sdp"
            ):
                if not provisional:
                    self._reject_confirmed_dialog(
                        remote_tag=remote_tag,
                        remote_host=remote_host,
                        remote_sip_port=remote_sip_port,
                        remote_target_uri=remote_target_uri,
                        local_uri=local_uri,
                        remote_uri=remote_uri,
                        route_set=route_set,
                    )
                return False
            seed_format = self.supported_send_formats[0]
            seed = self.early_dialogs.get(remote_tag) or SipDialog(
                target=target,
                remote_host=remote_host,
                remote_sip_port=int(remote_sip_port),
                remote_rtp_host="",
                remote_rtp_port=0,
                local_rtp_port=self.local_rtp_port,
                call_id=self.dialog_ids.call_id,
                remote_tag=remote_tag,
                local_uri=local_uri,
                remote_uri=remote_uri,
                send_format=seed_format,
                recv_format=self.supported_recv_formats[0],
                remote_target_uri=remote_target_uri,
                route_set=route_set,
                local_sdp_session_id=self._sdp_session_id,
            )
            prepared = self._answer_remote_offer(msg, current=seed)
            if prepared is None:
                if not provisional:
                    self._reject_confirmed_dialog(
                        remote_tag=remote_tag,
                        remote_host=remote_host,
                        remote_sip_port=remote_sip_port,
                        remote_target_uri=remote_target_uri,
                        local_uri=local_uri,
                        remote_uri=remote_uri,
                        route_set=route_set,
                    )
                return False
            candidate, answer = prepared
            candidate.peer_supports_from_change = sip.supports_option(
                msg, "from-change"
            )
            if provisional:
                self.early_dialogs[remote_tag] = candidate
                return True
            self.dialog_ids.remote_tag = remote_tag
            self.dialog = candidate
            self.early_dialogs.clear()
            self._local_sdp_body = answer
            return self._send_ack(
                remote_host,
                int(remote_sip_port),
                remote_target_uri,
                local_uri,
                remote_uri,
                route_set=route_set,
                body=answer.encode(),
            )
        negotiation_error: Exception | None = None
        try:
            if (
                msg.body
                and msg.header("Content-Type").split(";", 1)[0].strip().lower()
                != "application/sdp"
            ):
                raise sdp.SdpError("SIP 200 OK body is not application/sdp")
            local_offer_direction = "sendrecv"
            if self._local_sdp_body:
                sdp.validate_sdp_answer(
                    self._local_sdp_body,
                    msg.body,
                    allow_omitted_trailing_media=True,
                    # The local video direction below intersects both peers.
                    allow_video_direction_capability=True,
                )
                local_offer_direction = str(
                    sdp.parse_sdp(self._local_sdp_body)["direction"]
                )
            selected = sdp.negotiate_answer_directional(
                msg.body,
                self.supported_send_formats,
                self.supported_recv_formats,
                local_offer_direction=local_offer_direction,
                local_offer_sdp=self._local_sdp_body or None,
                codec_policy=self.codec_policy,
                allow_dahua_pcm=self.include_dahua_pcm,
                local_capabilities=HA_AUDIO_CAPABILITIES,
            )
            if selected is not None:
                payload_owner = self.early_dialogs.get(remote_tag) or self.dialog
                audio_payloads = _audio_payload_update(payload_owner.audio_payloads if payload_owner else PayloadMap(),
                    send=selected.send_formats or (selected.send,), receive=selected.recv_formats or (selected.recv,))
        except Exception as err:
            selected = None
            negotiation_error = err
        if selected is None:
            try:
                offered = ", ".join(sdp.offered_media_descriptions(msg.body))
            except Exception as err:
                offered = f"unparseable SDP media: {err}"
            _LOGGER.info(
                "SIP 200 OK rejected: no compatible answer media offered=[%s] error=%s",
                offered,
                negotiation_error or "none",
            )
            if not provisional:
                self._reject_confirmed_dialog(
                    remote_tag=remote_tag,
                    remote_host=remote_host,
                    remote_sip_port=remote_sip_port,
                    remote_target_uri=remote_target_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    route_set=route_set,
                )
            return False
        parsed = sdp.parse_sdp(msg.body)
        video_directional, imageattr_fallback = (
            self._video_answer_or_fallback(msg.body, self.video_formats)
            if self.video_formats
            else (None, False)
        )
        video_answer = (
            video_directional.send if video_directional is not None else None
        )
        local_video_answer = (
            video_directional.recv if video_directional is not None else None
        )
        remote_video = sdp.parse_video_sdp(msg.body) if video_answer is not None else None
        answered_dtmf_formats = sdp.offered_dtmf_formats(msg.body)
        dtmf_direction = (
            sdp.negotiate_dtmf_answer_directional(msg.body, self._local_sdp_body, audio=selected)
            if self._local_sdp_body
            else None
        )
        dtmf_recv = (
            dtmf_direction.recv
            if dtmf_direction is not None
            else next(iter(answered_dtmf_formats), None)
        )
        dtmf_send = (
            dtmf_direction.send
            if dtmf_direction is not None
            else dtmf_recv
        )
        try:
            audio_payloads = _audio_payload_update(audio_payloads,
                send=selected.send_formats or (selected.send,), receive=selected.recv_formats or (selected.recv,),
                send_dtmf=dtmf_send.payload_type if dtmf_send else None,
                send_clock=dtmf_send.sample_rate if dtmf_send else 8000,
                receive_dtmf=dtmf_recv.payload_type if dtmf_recv else None,
                receive_clock=dtmf_recv.sample_rate if dtmf_recv else 8000,
                receive_events=dtmf_direction.recv_formats if dtmf_direction is not None else (),
                send_events=dtmf_direction.send_formats if dtmf_direction is not None else ())
            video_payloads = _video_payload_update(payload_owner.video_payloads if payload_owner else PayloadMap(),
                                                   video_answer, local_video_answer)
        except PayloadConflict as error:
            _LOGGER.info("SIP answer payload lifetime conflict: %s", error)
            if not provisional:
                self._reject_confirmed_dialog(remote_tag=remote_tag, remote_host=remote_host,
                    remote_sip_port=remote_sip_port, remote_target_uri=remote_target_uri,
                    local_uri=local_uri, remote_uri=remote_uri, route_set=route_set)
            return False
        session_timer: sip.SipSessionExpires | None = None
        if raw_session_timer := msg.header("Session-Expires"):
            try:
                session_timer = sip.parse_session_expires(raw_session_timer)
                if not session_timer.refresher:
                    raise sip.SipError("Session-Expires response lacks refresher")
            except sip.SipError:
                if not provisional:
                    self._reject_confirmed_dialog(
                        remote_tag=remote_tag,
                        remote_host=remote_host,
                        remote_sip_port=remote_sip_port,
                        remote_target_uri=remote_target_uri,
                        local_uri=local_uri,
                        remote_uri=remote_uri,
                        route_set=route_set,
                    )
                return False
        now = asyncio.get_running_loop().time() if session_timer else 0.0
        if not provisional:
            self.dialog_ids.remote_tag = remote_tag
            if (
                self._dialog_termination_task is not None
                and self._dialog_termination_task.done()
            ):
                self._dialog_termination_task = None
        candidate = SipDialog(
            target=target,
            peer_supports_update=any("UPDATE" in {token.strip().upper() for token in value.split(",")}
                                     for value in msg.header_values("Allow")),
            remote_host=remote_host,
            remote_sip_port=int(remote_sip_port),
            remote_rtp_host=parsed["connection_ip"],
            remote_rtp_port=int(parsed["media_port"]),
            local_rtp_port=self.local_rtp_port,
            call_id=self.dialog_ids.call_id,
            remote_tag=remote_tag,
            local_uri=local_uri,
            remote_uri=remote_uri,
            send_format=selected.send,
            recv_format=selected.recv,
            recv_formats=selected.recv_formats,
            send_formats=selected.send_formats,
            match_received_codec=self.match_received_codec,
            audio_payloads=audio_payloads,
            video_payloads=video_payloads,
            remote_target_uri=remote_target_uri,
            route_set=route_set,
            recv_dtmf_formats=dtmf_direction.recv_formats if dtmf_direction is not None else (),
            send_dtmf_formats=dtmf_direction.send_formats if dtmf_direction is not None else (),
            dtmf_payload_type=(dtmf_recv.payload_type if dtmf_recv else None),
            dtmf_clock_rate=(dtmf_recv.sample_rate if dtmf_recv else 8000),
            dtmf_events=(dtmf_recv.events if dtmf_recv else frozenset()),
            send_dtmf_payload_type=(
                dtmf_send.payload_type if dtmf_send else None
            ),
            send_dtmf_clock_rate=(
                dtmf_send.sample_rate if dtmf_send else None
            ),
            send_dtmf_events=(dtmf_send.events if dtmf_send else None),
            remote_audio_direction=str(parsed["direction"]),
            local_audio_direction=sdp.local_direction_for_offer(
                parsed["direction"],
                remote_connection_held=bool(parsed["connection_held"]),
            ),
            remote_audio_connection_held=bool(parsed["connection_held"]),
            video_format=video_answer,
            local_video_format=local_video_answer,
            imageattr_fallback_needed=imageattr_fallback,
            remote_video_rtp_host=(str(remote_video["connection_ip"]) if remote_video else ""),
            remote_video_rtp_port=(int(remote_video["media_port"]) if remote_video else 0),
            remote_video_rtcp_host=(
                str(remote_video["rtcp_address"] or remote_video["connection_ip"])
                if remote_video
                else ""
            ),
            remote_video_rtcp_port=(
                int(remote_video["rtcp_port"] or int(remote_video["media_port"]) + 1)
                if remote_video
                else 0
            ),
            remote_video_rtcp_mux=False,
            remote_video_payload_types=(
                tuple(int(item) for item in remote_video["payload_order"])
                if remote_video
                else ()
            ),
            remote_video_connection_held=bool(
                remote_video and remote_video["connection_held"]
            ),
            local_video_rtp_port=(self.local_video_rtp_port if video_answer is not None else 0),
            local_video_direction=(
                sdp.constrained_video_direction(
                    video_answer.direction,
                    allow_send=(
                        self.video_direction in {"sendonly", "sendrecv"}
                        and (
                            self.generic_video_relay
                            or sdp.browser_video_send_supported(video_answer)
                        )
                        and not bool(
                            remote_video and remote_video["connection_held"]
                        )
                    ),
                    allow_receive=self.video_direction in {"recvonly", "sendrecv"},
                )
                if video_answer is not None
                else "inactive"
            ),
            local_sdp_session_id=self._sdp_session_id,
            local_sdp_session_version=0,
            local_sdp_body=self._local_sdp_body,
            peer_supports_from_change=sip.supports_option(msg, "from-change"),
            session_timer=sip.SipSessionTimer(),
        )
        candidate.session_timer.configure(session_timer, local_role="uac", now=now)
        _LOGGER.info(
            "SIP %s media selected call_id=%s tx=%s rx=%s answer=[%s]",
            "provisional" if provisional else "200 OK",
            self.dialog_ids.call_id,
            selected.send.wire_token(),
            selected.recv.wire_token(),
            ", ".join(sdp.offered_media_descriptions(msg.body)),
        )
        if provisional:
            self.early_dialogs[remote_tag] = candidate
            return True
        self.dialog = candidate
        self.early_dialogs.clear()
        self._send_ack(
            remote_host,
            int(remote_sip_port),
            remote_target_uri,
            local_uri,
            remote_uri,
            route_set=route_set,
        )
        return True
