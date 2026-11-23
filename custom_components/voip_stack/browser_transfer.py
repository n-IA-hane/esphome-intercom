"""Adapt a retained browser to the existing forward and session handoff owners."""

import asyncio

from .endpoint_session import CleanupStage, SessionHandoff, TerminationIntent
from .local_softphone_runtime import PendingLocalSource, start_local_softphone_call
from .local_call_media import LocalAudioContract
from .media_session_updates import commit_audio_session_update, commit_video_session_update
from .runtime_data import require_runtime_data
from .session_cleanup import async_wait_for_cleanup
from .forward_plan import await_forward_result


class RetainedBrowserSource(PendingLocalSource):
    """Keep the established SIP destination intact until its browser commits."""

    def __init__(self, hass, session, client, local_ip, sip_port):
        super().__init__(hass, None, local_ip, sip_port, call_id=session.call_id,
                         retained_endpoint_id=str(session.metadata.get("source_endpoint_id") or ""),
                         established_client=client, established_dialog=client.incoming_refer_dialog or client.dialog)
        self.original_owner = session.owner
        self.original_route = session.route_kind
        self.transaction = client._incoming_refer_task or asyncio.current_task()
        self.retired_leg = None
        self.retained_reservation = None
        self.retained_sockets = {}
        self.retirement_callbacks = []
        self.referrer_wire_id = client.dialog_ids.call_id
        # The REFER usage, not its terminating transferor, now controls this
        # handoff. The retained browser remains the session's source owner.
        self.watcher_retired = self.registry.cancel_task(session.call_id,
            f"client_watcher:{self.referrer_wire_id}") is not None

    async def retire_destination(self):
        self.validate()

    def validate(self):
        session = self.registry.get_session(self.token.call_id, generation=self.token.generation)
        if (session is None or not session.live or self.committed
                or self.registry.sip_client_for(self.referrer_wire_id) is not self.previous_client
                or (self.previous_client.dialog is None
                    and (self.transaction is None or self.transaction.done()))):
            raise RuntimeError("retained browser dialog changed")
        return session

    def commit(self, *, keep_media=False):
        session = self.validate()
        wire_id = self.referrer_wire_id
        self.retired_leg = self.registry.remove_leg(session.call_id, wire_id)
        if self.retired_leg is None:
            raise RuntimeError("referring dialog owner disappeared")
        session.add_resource(f"retired_destination:{wire_id}", self.retired_leg,
                             self.retired_leg.close, stage=CleanupStage.LEG)
        if self.call.callee_endpoint_id:
            self.registry.release_endpoint_claim(session.call_id, self.call.callee_endpoint_id)
        reservation = getattr(self.previous_client, "media_reservation", None)
        if reservation is not None:
            self.retained_reservation = reservation.transfer()
            self.previous_client.media_reservation = None
        self.retained_sockets = {}
        for name in ("video_rtp_socket", "video_rtcp_socket"):
            value = getattr(self.previous_client, name, None)
            if value is not None:
                self.retained_sockets[name] = value
                setattr(self.previous_client, name, None)
        self.previous_client.on_media_update = None
        self.previous_client.on_info_dtmf = None
        self.previous_client.on_info_video = None
        self.retired = True
        super().commit(keep_media=keep_media)

    def preserve_handoff(self, handoff):
        handoff.preserve(self, "retired", "committed", "retired_leg", "retained_reservation", "retained_sockets")
        handoff.preserve(self.previous_client, "media_reservation", "video_rtp_socket", "video_rtcp_socket")
        reservation = getattr(self.previous_client, "media_reservation", None)
        if reservation is not None:
            handoff.preserve(reservation, "released", "_audio_sockets")

    def after_refer(self, callback):
        """The final RFC 3515 NOTIFY still owns the referring dialog."""
        self.registry.cancel_task(self.token.call_id,
                                 f"client_watcher:{self.referrer_wire_id}")
        self.retirement_callbacks.append(callback)
        if len(self.retirement_callbacks) != 1:
            return
        async def retire():
            for operation in self.retirement_callbacks:
                await operation()
        def done(_task=None):
            session = self.registry.get_session(self.token.call_id, generation=self.token.generation)
            if session is not None and session.live:
                session.create_task(retire(),
                    name=f"browser-transfer-retirement-{self.referrer_wire_id}")
        if self.transaction is not None and not self.transaction.done():
            self.transaction.add_done_callback(done)
        else:
            done()

    async def close_referrer(self):
        if self.retired_leg is not None:
            await self.retired_leg.close("forwarded")
            self.registry.release_resource(self.token.call_id,
                f"retired_destination:{self.referrer_wire_id}", value=self.retired_leg)

    def resume(self, *, redial=False, destination="", trunk_id=""):
        if self.committed:
            return
        session = self.registry.get_session(self.token.call_id, generation=self.token.generation)
        if session is None or not session.live:
            return
        if self.previous_client.dialog is None:
            self.registry.request_termination(session.call_id, TerminationIntent("remote_hangup"),
                                              generation=self.token.generation)
            return
        if self.watcher_retired:
            from .bridge_manager import async_watch_owned_sip_dialog
            task = asyncio.create_task(async_watch_owned_sip_dialog(
                self.registry, session.call_id, self.previous_client, session.generation),
                name=f"browser-transfer-resume-{self.referrer_wire_id}")
            self.registry.attach_client_watcher(self.referrer_wire_id, task)
            self.watcher_retired = False
        self.media.clear()
        self.media.update(self.original_media)
        for key, value in self.original_identity.items():
            if value is None:
                session.metadata.pop(key, None)
            else:
                session.metadata[key] = value
        self.registry.take_pending_invite(session.call_id)
        self.registry.transition(session.call_id, owner=self.original_owner, state="in_call",
            caller=self.original_parties[0], callee=self.original_parties[1], route_kind=self.original_route)
        from .call_projection import publish_phone_projection
        publish_phone_projection(self.hass, session, self.call.caller_endpoint_id,
                                 peer_name=session.callee, direction="outgoing", last_sip_event="ROUTE_RESUME")
        self.registry.release_resource(session.call_id, f"browser_source:{session.call_id}", value=self)

    def connect_answered_browser(self, endpoint_id, media_owner_id):
        session = self.validate()
        with SessionHandoff(self.registry, session) as handoff:
            self.preserve_handoff(handoff)
            self.commit()
            self.registry.take_pending_invite(session.call_id)
            self.registry.release_endpoint_claim(session.call_id, endpoint_id)
            self.registry.release_endpoint_claim(session.call_id, self.call.caller_endpoint_id)
            start_local_softphone_call(self.hass, self.call.caller_endpoint_id, endpoint_id,
                call_id=session.call_id, caller_owner_id=self.call.caller_media_owner_id,
                answered_owner_id=media_owner_id, preserve_controller=True,
                request_video=self.caller_video_receive, enable_caller_video_send=self.caller_video_send)
        runtime = require_runtime_data(self.hass)
        for kind in ("audio", "video"):
            owner = runtime.media.owners_for(kind).get(f"{self.call.caller_endpoint_id}|{session.call_id}")
            if owner is not None:
                owner.revoke()
        self.after_refer(self.close_referrer)

    async def forward_to_browser(self, runtime, endpoint, *, timeout):
        session = self.validate()
        future = asyncio.get_running_loop().create_future()
        self.registry.claim_endpoint(session.call_id, endpoint.endpoint_id, role="group_candidate")
        self.registry.set_pending_route(session.call_id, {"future": future, "invite": self.invite,
            "ring_group_endpoint_ids": (endpoint.endpoint_id,), "declined_endpoint_ids": set()})
        runtime.publish_pending_ringing(self.invite, route_kind="transfer", endpoint_id=endpoint.endpoint_id,
            endpoint_device_id=endpoint.device_id, callee=endpoint.name, last_sip_event="REFER")
        try:
            async with asyncio.timeout(timeout):
                decision = await future
            if decision.get("action") != "answer_ha" or decision.get("endpoint_id") != endpoint.endpoint_id:
                raise RuntimeError("browser transfer declined")
            self.registry.take_pending_route(session.call_id)
            self.connect_answered_browser(endpoint.endpoint_id, str(decision.get("media_client_id") or ""))
        finally:
            self.registry.take_pending_route(session.call_id)
            if not self.committed and session.live:
                self.registry.release_endpoint_claim(session.call_id, endpoint.endpoint_id)

    def prepare_bridge_media(self, relay, video_answer=None, *, video_relay=None):
        """Keep the answered browser's existing sockets during its peer handoff."""
        from dataclasses import replace
        from .local_softphone_runtime import LOCAL_RTP_HOST
        from .core.sdp import local_direction_for_remote
        descriptor = dict(self.original_media)
        descriptor.update(rtp_loopback=True, remote_rtp_host=LOCAL_RTP_HOST,
            remote_rtp_port=relay.left_port, send_format=self.invite.recv_format,
            recv_format=self.invite.send_format, endpoint_id=self.call.caller_endpoint_id,
            media_client_id=self.call.caller_media_owner_id, video_direction="inactive")
        if video_answer is None or video_relay is None:
            descriptor.update(local_video_rtp_port=0, invite=replace(self.invite, video_format=None,
                local_video_format=None, video_answer_format=None))
            return descriptor
        port = int(self.original_media.get("local_video_rtp_port")
                   or getattr(self.previous_client, "local_video_rtp_port", 0) or 0)
        if not port:
            raise RuntimeError("The answered browser has no negotiated video socket")
        video_relay.left.port, video_relay.left.rtcp_port = port, port + 1
        for name in ("video_rtp_socket", "video_rtcp_socket"):
            if descriptor.get(name) is None:
                descriptor[name] = getattr(self.previous_client, name, None)
        descriptor.update(invite=replace(self.invite, remote_video_rtp_host=LOCAL_RTP_HOST,
            remote_video_rtp_port=video_relay.left_port, remote_video_rtcp_host=LOCAL_RTP_HOST,
            remote_video_rtcp_port=video_relay.left_port + 1), local_video_rtp_port=port,
            video_direction=local_direction_for_remote(video_answer.direction), camera_send_authorized=self.caller_video_send)
        return descriptor

    def connected(self, destination, route_kind):
        super().connected(destination, route_kind)
        runtime = require_runtime_data(self.hass)
        active = runtime.media.sessions_for("audio").get((self.token.call_id, self.call.caller_endpoint_id))
        descriptor = self.registry.resource_for(self.token.call_id, "softphone_media")
        if descriptor is not None:
            descriptor.update(self.retained_sockets)
            if self.retained_reservation is not None:
                descriptor["rtp_reservation"] = self.retained_reservation
        if active is not None and descriptor is not None and descriptor.get("rtp_loopback"):
            negotiated = LocalAudioContract(self.token.call_id, remote_rtp_host=descriptor["remote_rtp_host"],
                remote_rtp_port=descriptor["remote_rtp_port"], send_format=descriptor["send_format"],
                recv_format=descriptor["recv_format"])
            active.signaling_host = negotiated.remote_rtp_host
            active.send_dtmf_info = None
            commit_audio_session_update(active, negotiated, dtmf_payload_type=None, dtmf_events=())
        video = runtime.media.sessions_for("video").get((self.token.call_id, self.call.caller_endpoint_id))
        if video is not None and descriptor is not None:
            video_invite = descriptor.get("invite")
            if getattr(video_invite, "video_format", None) is not None and descriptor.get("local_video_rtp_port"):
                commit_video_session_update(video, video_invite, local_direction=descriptor["video_direction"])
            else:
                video.removed = True
                video.media_generation += 1
                video.update_event.set()
        self.after_refer(self.close_referrer)


async def transfer_browser_target(hass, runtime, session, referrer, target):
    """Use normal forwarding for a REFER whose opposite participant is a browser."""
    from .core import sip
    from .core.sip_transfer import SipTransferResult
    from .endpoint_routing import is_local_listener_uri
    from .runtime_data import sip_endpoint_manager
    from .call_replaces import prepare_replacement_grant, ReplacementRejected
    from .call_transfer import _external_transfer_denied

    manager = sip_endpoint_manager(hass)
    if manager is None or runtime.sip.forward_call is None:
        return SipTransferResult(False, 488, "local_transfer_unavailable")
    uri = sip.parse_sip_uri(target.uri)
    local = is_local_listener_uri(uri, local_ip=manager.local_ip,
                                 sip_port=int(runtime.transport_config["sip_port"]))
    dialog = referrer.incoming_refer_dialog or referrer.dialog
    if dialog is None:
        return SipTransferResult(False, 481, "call_not_found")
    if (target.replaces is not None or not local) and _external_transfer_denied(runtime, session.call_id, target,
                                dialog.remote_uri, dialog.local_uri):
        return SipTransferResult(False, 403, "external_calls_disabled")
    previous = runtime.sip.resource_for(session.call_id, "browser_source")
    if previous is not None and not previous.committed:
        return SipTransferResult(False, 491, "transfer_in_progress")
    source = RetainedBrowserSource(hass, session, referrer, manager.local_ip,
                                   int(runtime.transport_config["sip_port"]))
    if previous is not None:
        runtime.sip.release_resource(session.call_id, f"browser_source:{session.call_id}", value=previous)
    runtime.sip.own_resource(session.call_id, f"browser_source:{session.call_id}", source, None)
    task, grant = None, None
    try:
        if target.replaces is not None:
            try:
                grant = prepare_replacement_grant(runtime, session.call_id, target, referrer)
            except ReplacementRejected as error:
                return SipTransferResult(False, error.status, "replacement_rejected")
            if grant is None:
                return SipTransferResult(False, 481, "replacement_not_found")
            from .browser_transfer_handoff import adopt_consultation
            return await adopt_consultation(hass, runtime, source, grant)
        runtime.sip.set_pending_invite(session.call_id, source.invite)
        task = await runtime.sip.forward_call(call_id=session.call_id,
            destination=uri.user if local else target.uri, on_failure="resume", expected_state=session.state)
        if task is None:
            task = runtime.sip.task_for(session.call_id, "forward")
        outcome = await await_forward_result(task) if task is not None else None
        success = outcome is not None and outcome.committed_for(session)
        return SipTransferResult(success, 200 if success else 480,
                                 "completed" if success else "transfer_failed", retained_call=success and session.live)
    except BaseException:
        if task is not None and not task.done() and not source.committed:
            task.cancel()
            await async_wait_for_cleanup(asyncio.gather(task, return_exceptions=True))
        raise
    finally:
        if grant is not None:
            grant.release()
        if not source.committed:
            source.resume()
