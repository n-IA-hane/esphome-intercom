"""Adapt a retained browser to the existing forward and session handoff owners."""

import asyncio

from .endpoint_session import CleanupStage, SessionHandoff
from .local_softphone_runtime import PendingLocalSource, start_local_softphone_call
from .local_call_media import LocalAudioContract
from .media_session_updates import commit_audio_session_update
from .runtime_data import require_runtime_data
from .session_cleanup import async_wait_for_cleanup


class RetainedBrowserSource(PendingLocalSource):
    """Keep the established SIP destination intact until its browser commits."""

    def __init__(self, hass, session, client, local_ip, sip_port):
        super().__init__(hass, None, local_ip, sip_port, call_id=session.call_id,
                         retained_endpoint_id=str(session.metadata.get("source_endpoint_id") or ""),
                         established_client=client)
        self.original_owner = session.owner
        self.original_route = session.route_kind
        self.transaction = client._incoming_refer_task or asyncio.current_task()
        self.retired_leg = None
        self.retirement_callbacks = []

    async def retire_destination(self):
        self.validate()

    def validate(self):
        session = self.registry.get_session(self.token.call_id, generation=self.token.generation)
        if (session is None or not session.live or self.committed
                or self.registry.sip_client_for(self.previous_client.dialog_ids.call_id) is not self.previous_client
                or self.previous_client.dialog is None):
            raise RuntimeError("retained browser dialog changed")
        return session

    def commit(self, *, keep_media=False):
        session = self.validate()
        wire_id = self.previous_client.dialog_ids.call_id
        self.retired_leg = self.registry.remove_leg(session.call_id, wire_id)
        if self.retired_leg is None:
            raise RuntimeError("referring dialog owner disappeared")
        session.add_resource(f"retired_destination:{wire_id}", self.retired_leg,
                             self.retired_leg.close, stage=CleanupStage.LEG)
        if self.call.callee_endpoint_id:
            self.registry.release_endpoint_claim(session.call_id, self.call.callee_endpoint_id)
        self.previous_client.on_media_update = None
        self.previous_client.on_info_dtmf = None
        self.retired = True
        super().commit(keep_media=keep_media)

    def after_refer(self, callback):
        """The final RFC 3515 NOTIFY still owns the referring dialog."""
        self.registry.cancel_task(self.token.call_id,
                                 f"client_watcher:{self.previous_client.dialog_ids.call_id}")
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
                    name=f"browser-transfer-retirement-{self.previous_client.dialog_ids.call_id}")
        if self.transaction is not None and not self.transaction.done():
            self.transaction.add_done_callback(done)
        else:
            done()

    async def close_referrer(self):
        if self.retired_leg is not None:
            await self.retired_leg.close("forwarded")
            self.registry.release_resource(self.token.call_id,
                f"retired_destination:{self.previous_client.dialog_ids.call_id}", value=self.retired_leg)

    def resume(self, *, redial=False, destination="", trunk_id=""):
        if self.committed:
            return
        session = self.registry.get_session(self.token.call_id, generation=self.token.generation)
        if session is None or not session.live:
            return
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
            handoff.preserve(self, "retired", "committed", "retired_leg")
            self.commit()
            self.registry.take_pending_invite(session.call_id)
            self.registry.release_endpoint_claim(session.call_id, endpoint_id)
            self.registry.release_endpoint_claim(session.call_id, self.call.caller_endpoint_id)
            start_local_softphone_call(self.hass, self.call.caller_endpoint_id, endpoint_id,
                call_id=session.call_id, caller_owner_id=self.call.caller_media_owner_id,
                answered_owner_id=media_owner_id, preserve_controller=True)
        runtime = require_runtime_data(self.hass)
        owner = runtime.media.owners_for("audio").get(f"{self.call.caller_endpoint_id}|{session.call_id}")
        if owner is not None:
            owner.revoke()
        self.after_refer(self.close_referrer)

    async def forward_to_browser(self, runtime, endpoint, *, timeout):
        session = self.validate()
        future = asyncio.get_running_loop().create_future()
        self.registry.claim_endpoint(session.call_id, endpoint.endpoint_id, role="group_candidate")
        self.registry.set_pending_route(session.call_id, {"future": future, "invite": self.invite,
            "ring_group_endpoint_ids": (endpoint.endpoint_id,), "declined_endpoint_ids": set()})
        runtime.publish_pending_ringing(self.invite, route_kind="group", endpoint_id=endpoint.endpoint_id,
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

    def connected(self, destination, route_kind):
        super().connected(destination, route_kind)
        runtime = require_runtime_data(self.hass)
        active = runtime.media.sessions_for("audio").get((self.token.call_id, self.call.caller_endpoint_id))
        descriptor = self.registry.resource_for(self.token.call_id, "softphone_media")
        if active is not None and descriptor is not None and descriptor.get("rtp_loopback"):
            negotiated = LocalAudioContract(self.token.call_id, remote_rtp_host=descriptor["remote_rtp_host"],
                remote_rtp_port=descriptor["remote_rtp_port"], send_format=descriptor["send_format"],
                recv_format=descriptor["recv_format"])
            active.signaling_host = negotiated.remote_rtp_host
            active.send_dtmf_info = None
            commit_audio_session_update(active, negotiated, dtmf_payload_type=None, dtmf_events=())
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
    if manager is None or runtime.sip.forward_call is None or referrer.dialog.video_format is not None:
        return SipTransferResult(False, 488, "local_transfer_unavailable")
    if _external_transfer_denied(runtime, session.call_id, target,
                                referrer.dialog.remote_uri, referrer.dialog.local_uri):
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
        uri = sip.parse_sip_uri(target.uri)
        local = is_local_listener_uri(uri, local_ip=manager.local_ip,
                                     sip_port=int(runtime.transport_config["sip_port"]))
        runtime.sip.set_pending_invite(session.call_id, source.invite)
        async with asyncio.timeout(30):
            await runtime.sip.forward_call(call_id=session.call_id,
                destination=uri.user if local else target.uri, on_failure="resume", expected_state=session.state)
            task = runtime.sip.task_for(session.call_id, "forward")
            if task is not None:
                await asyncio.shield(task)
        return SipTransferResult(source.committed, 200 if source.committed else 480,
                                 "completed" if source.committed else "transfer_failed", retained_call=source.committed)
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
