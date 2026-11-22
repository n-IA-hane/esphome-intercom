"""Retire one unanswered SIP destination while retaining its real caller."""

import asyncio
from dataclasses import dataclass
from contextlib import contextmanager
from typing import TYPE_CHECKING

from homeassistant.exceptions import ServiceValidationError

from .endpoint_session import CallToken, CleanupStage, SessionHandoff

if TYPE_CHECKING:
    from .media_ports import RtpPortReservation
    from .sip_rtp_bridge import SipRtpRelay
    from .sip_listener import SipInvite


@dataclass(frozen=True, slots=True)
class RetainedSourceMedia:
    """One session-owned caller contract, including provisional bridge media."""

    token: CallToken
    relay: "SipRtpRelay"
    reservation: "RtpPortReservation"
    caller_invite: "SipInvite"
    final_response_sent: bool
    destination_id: str
    source_client: object = None
    video_relay: object = None

    def validate(self, registry):
        session = registry.get_session(self.token.call_id, generation=self.token.generation)
        if (session is None or not session.live or self.reservation.released
                or registry.resource_for(self.token.call_id, "relay") is not self.relay
                or self.reservation.ports != (self.relay.left_port, self.relay.right_port)
                or self.relay.left_transport is None or self.relay.right_transport is None
                or self.relay.left_transport.is_closing() or self.relay.right_transport.is_closing()
                or (self.video_relay is not None and self.relay.video_relay is not self.video_relay)):
            raise RuntimeError("retained caller media owner changed")
        return session

    async def adopt_for_application(self, hass, registry, on_complete):
        """Move the current caller transport through the established parking adapter."""
        from .local_media_bridge import adopt_relay_for_application

        session = self.validate(registry)
        relay = self.relay
        transport = relay.left_transport
        protocol = transport.get_protocol()
        media = None
        retired_name = f"retired_relay:{id(relay):x}"
        try:
            with SessionHandoff(registry, session) as handoff:
                handoff.preserve(relay, "left_transport", "video_relay", "_on_release", "_stop_requested")
                media = adopt_relay_for_application(hass, relay, "left", self.caller_invite,
                    self.reservation, on_complete)
                media.release_reservation_on_stop = False
                session.release_resource(f"relay:{session.call_id}", value=relay)
                session.add_resource(retired_name, relay, lambda _reason: relay.stop(), stage=CleanupStage.MEDIA)
                registry.attach_relay(session.call_id, media)
        except BaseException:
            if media is not None:
                transport.set_protocol(protocol)
                media.transport = None
                media.video_relay = None
                await media.stop()
            raise
        registry.update_media(session.call_id, provisional=True,
            retained_source_media=None, audio_rtp_source=media.rtp_source)
        # Closing the old B-leg cannot close the transport just moved to media.
        await relay.stop()
        registry.release_resource(session.call_id, retired_name, value=relay,
            generation=self.token.generation)
        session.ensure_live(self.token)
        return media


@dataclass(slots=True)
class PendingSipDestination:
    hass: object
    registry: object
    session: object
    invite: object
    destination_id: str
    client: object
    endpoint_id: str
    original_name: str
    original_owner: str = ""
    original_state: str = ""
    original_route_kind: str = ""
    retired: bool = False
    cleanup_task: object = None
    retained_media: RetainedSourceMedia | None = None

    @classmethod
    def capture(cls, hass, registry, session):
        prepared = registry.resource_for(session.call_id, "native_destination") if session is not None else None
        if prepared is not None:
            prepared.validate()
            return prepared
        if (
            session is None
            or not session.live
            or session.owner != "bridge"
            or session.state not in {"connecting", "ringing", "remote_ringing"}
        ):
            return None
        invite = session.artifacts.caller_invite
        identity = registry.bridge_link_for(session.call_id)
        client = registry.sip_client_for(identity) if identity else None
        if invite is None or client is None:
            return None
        if client.dialog is not None:
            raise ServiceValidationError("The original SIP phone already answered")
        if invite.initial_response_sent and invite.video_format is not None:
            raise ServiceValidationError(
                "This already-answered video source cannot change its ringing destination"
            )
        return cls(
            hass,
            registry,
            session,
            invite,
            identity,
            client,
            str(
                session.metadata.get("dest_endpoint_id")
                or session.metadata.get("target_endpoint_id")
                or ""
            ),
            session.callee,
            original_owner=session.owner, original_state=session.state, original_route_kind=session.route_kind,
        )

    def validate(self):
        if (
            not self.session.live
            or self.registry.sip_client_for(self.destination_id) is not self.client
            or self.client.dialog is not None
        ):
            raise ServiceValidationError(
                "The ringing SIP destination changed or answered"
            )

    async def retire(self):
        self.validate()
        registry, session = self.registry, self.session
        pending_media = [resource for resource in session.resources
                         if resource.name.startswith("pending_bridge_media:")]
        relay = registry.resource_for(session.call_id, "relay")
        # Cut over reversible inventory before cancelling or closing any owner.
        with SessionHandoff(registry, session) as handoff:
            handoff.preserve(self, "retained_media", "retired")
            for resource in pending_media:
                if self.invite.initial_response_sent or relay is not None:
                    handoff.preserve(resource.value, "released", "_audio_sockets")
                    reservation = resource.value.transfer()
                    retained = (RetainedSourceMedia(session.token, relay, reservation, self.invite,
                        self.invite.initial_response_sent, self.destination_id) if relay is not None else None)
                    registry.attach_media(session.call_id, {
                        "rtp_reservation": reservation,
                        "local_rtp_port": reservation.ports[0],
                        "final_response_sent": self.invite.initial_response_sent,
                        "retained_source_media": retained,
                    }, provisional=True)
                    self.retained_media = retained
            session.update_metadata(
                policy_endpoint_ids=tuple(
                    sorted(
                        set(
                            (
                                *session.endpoint_claims,
                                *session.metadata.get("policy_endpoint_ids", ()),
                            )
                        )
                    )
                )
            )
            leg = registry.remove_leg(session.call_id, self.destination_id)
            registry.forget_bridge_link(session.call_id)
            retired_name = f"retired_destination:{self.destination_id}"
            if leg is not None:
                session.add_resource(retired_name, leg, leg.close, stage=CleanupStage.LEG)
            self.retired = True
            session.update_metadata(
                dest_endpoint_id="",
                target_endpoint_id="",
                endpoint_id=str(session.metadata.get("source_endpoint_id") or ""),
            )
            if leg is not None:
                leg.endpoint_id = self.endpoint_id
        watcher = registry.cancel_task(
            session.call_id, f"client_watcher:{self.destination_id}"
        )

        async def drain():
            try:
                if watcher is not None:
                    await asyncio.gather(watcher, return_exceptions=True)
                if leg is not None:
                    await leg.close("forwarded")
            finally:
                if session.live:
                    if leg is not None:
                        registry.release_resource(
                            session.call_id, retired_name, value=leg
                        )
                    if self.endpoint_id:
                        registry.release_endpoint_claim(
                            session.call_id, self.endpoint_id
                        )

        self.cleanup_task = session.create_task(
            drain(), name=f"retired-sip-destination-{self.destination_id}"
        )
        # The old transaction keeps its own leg/endpoint ownership. Its network
        # timeout must not delay ringing the replacement or answering voicemail.
        for resource in pending_media:
            await resource.close("forwarded")
            if session.live:
                registry.release_resource(session.call_id, resource.name, value=resource.value)

    async def restore_preparation(self):
        """Undo only the caller projection while its original leg still owns SIP."""
        if (not self.retired and self.session.live
                and self.registry.sip_client_for(self.destination_id) is self.client
                and self.client.dialog is None):
            self.registry.transition(self.session.call_id, owner=self.original_owner,
                state=self.original_state, callee=self.original_name, route_kind=self.original_route_kind)

    async def resume(self):
        if not self.retired or not self.session.live:
            return
        from .runtime_data import require_runtime_data

        endpoint = require_runtime_data(self.hass).endpoints.get(self.endpoint_id)
        destination = endpoint.name if endpoint is not None else self.original_name
        previous = self.registry.task_for(self.session.call_id, "forward")

        async def redial():
            if self.cleanup_task is not None:
                await asyncio.gather(self.cleanup_task, return_exceptions=True)
            if previous is not None and previous is not asyncio.current_task():
                await asyncio.gather(previous, return_exceptions=True)
            if self.session.live:
                await self.registry.forward_call(
                    call_id=self.session.call_id,
                    destination=destination,
                    on_failure="terminate",
                )

        self.session.create_task(redial(), name="sip-forward-resume")


@contextmanager
def _reversed_transfer_media(relay, reverse):
    """Compensate an orientation change if its session handoff cannot commit."""
    if reverse:
        relay.reverse_held_sides()
    try:
        yield
    except BaseException:
        if reverse:
            relay.reverse_held_sides()
        raise


@dataclass(slots=True)
class EstablishedSipDestination(PendingSipDestination):
    """Reversible retirement of a REFER transferor in the existing relay owner."""

    original_session: object = None
    retained_client: object = None
    referrer: object = None
    transaction: object = None
    reversed_sides: bool = False
    previous_release: object = None
    previous_callbacks: tuple = ()
    restored: bool = False
    settled: bool = False
    runtime: object = None
    original_metadata: object = None

    @classmethod
    async def prepare(cls, hass, runtime, original, referrer, wire_id):
        from .local_call_media import ApplicationCallSource
        from .media_ports import RtpPortReservation
        from .sip_rtp_bridge import SipRtpRelay
        from .call_transfer import _inbound_server
        from .endpoint_session import TerminationIntent
        from .bridge_manager import async_watch_owned_sip_dialog
        import secrets

        registry = runtime.sip
        destination_id = registry.bridge_link_for(original.call_id)
        destination = original.legs.get(destination_id)
        relay = registry.resource_for(original.call_id, "relay")
        if (original.owner != "bridge" or original.state not in {"in_call", "held"}
                or destination is None or destination.dialog is None
                or not isinstance(relay, SipRtpRelay)):
            return None
        from_callee = destination.dialog is referrer if referrer is not None else wire_id == destination_id
        retained_client = registry.source_sip_client_for(original.call_id) if from_callee else destination.dialog
        for leg in tuple(original.legs.values()):
            if leg.dialog is referrer and referrer is not None and referrer.dialog is None:
                continue
            if leg.dialog is not None and not await leg.dialog.async_wait_for_media_idle():
                raise RuntimeError("transfer media negotiation is still pending")
        if (not original.live or registry.resource_for(original.call_id, "relay") is not relay
                or registry.bridge_link_for(original.call_id) != destination_id):
            raise RuntimeError("transfer dialog changed during admission")
        retained_endpoint = str(original.metadata.get("source_endpoint_id" if from_callee else "dest_endpoint_id") or "")
        goal_id = original.call_id if from_callee else f"ha-transfer-{secrets.token_hex(12)}"
        caller = original.caller if from_callee else original.callee
        if retained_client is not None:
            invite = ApplicationCallSource.from_client(retained_client, call_id=goal_id, caller=caller,
                trunk_id=(str(original.metadata.get("inbound_trunk_id") or "") if from_callee else destination.trunk_id),
                allow_video=True)
        else:
            server, _ = _inbound_server(runtime, original.call_id)
            invite = server.invite_for_call(original.call_id) if server is not None else None
        if invite is None:
            return None
        ports = (relay.left_port, relay.right_port) if from_callee else (relay.right_port, relay.left_port)
        if not set(ports) <= runtime.rtp_port_pool.get("used", set()):
            raise RuntimeError("transfer source reservation changed")
        policies = tuple(sorted(set((*original.endpoint_claims,
            *(original.metadata.get("policy_endpoint_ids") or ()), retained_endpoint)) - {""}))
        goal = original if from_callee else registry.upsert(goal_id, state="in_call", owner="bridge",
            caller=caller, source_endpoint_id=retained_endpoint, event_only=True,
            inbound_trunk_id=destination.trunk_id)
        operation = cls(hass, registry, goal, invite, destination_id, destination.dialog,
            str(original.metadata.get("dest_endpoint_id" if from_callee else "source_endpoint_id") or ""),
            original.callee, original.owner, original.state, original.route_kind,
            original_session=original, retained_client=retained_client, referrer=referrer, runtime=runtime,
            original_metadata=dict(original.metadata),
            transaction=getattr(referrer, "_incoming_refer_task", None) if referrer is not None else asyncio.current_task(),
            reversed_sides=not from_callee, previous_release=relay._on_release,
            previous_callbacks=tuple(getattr(destination.dialog, name, None)
                for name in ("on_refer", "on_media_update", "on_info_dtmf")))
        try:
            async with relay.media_reconfiguration_guard():
                with _reversed_transfer_media(relay, not from_callee), SessionHandoff(registry, original, goal) as handoff:
                    handoff.preserve(relay, "_on_release")
                    reservation = RtpPortReservation(hass, ports)
                    relay._on_release = None
                    registry.remove_leg(original.call_id, destination_id)
                    registry.forget_bridge_link(original.call_id)
                    if goal is not original:
                        registry.release_resource(original.call_id, f"relay:{original.call_id}", value=relay)
                        registry.attach_relay(goal.call_id, relay)
                        registry.add_leg(goal.call_id, destination_id, role="caller", state="in_call", endpoint_id=retained_endpoint)
                        registry.attach_sip_client(goal.call_id, destination_id, retained_client, role="caller", state="in_call",
                                                   trunk_id=destination.trunk_id)
                        if retained_endpoint:
                            registry.transfer_endpoint_claim(original.call_id, goal.call_id, retained_endpoint)
                        goal.claim_answer(goal.token, lambda: True)
                    else:
                        goal.add_resource(f"retired_destination:{destination_id}", destination, destination.close,
                                          stage=CleanupStage.LEG)
                    operation.retained_media = RetainedSourceMedia(goal.token, relay, reservation, invite,
                        True, destination_id, retained_client, relay.video_relay)
                    registry.attach_media(goal.call_id, {"rtp_reservation": reservation,
                        "local_rtp_port": ports[0], "final_response_sent": True,
                        "retained_source_media": operation.retained_media}, provisional=True)
                    registry.set_pending_invite(goal.call_id, invite)
                    registry.remember_invite_identity(goal.call_id, invite)
                    goal.artifacts.caller_invite = invite
                    registry.own_resource(goal.call_id, f"native_destination:{goal.call_id}", operation, None)
                    goal.update_metadata(event_only=False, dest_endpoint_id="", target_endpoint_id="",
                                         policy_endpoint_ids=policies,
                        registration_only_caller=original.metadata.get("registration_only_caller", ""))
                    operation.retired = True
                registry.cancel_task(original.call_id, f"client_watcher:{destination_id}")
            if goal is not original and retained_client is not None:
                task = asyncio.create_task(async_watch_owned_sip_dialog(registry, goal.call_id,
                    retained_client, goal.generation), name=f"voip-transfer-source-{destination_id}")
                registry.attach_client_watcher(destination_id, task)
                async def refer(target):
                    from .call_transfer import async_transfer_target
                    return await async_transfer_target(runtime, goal.call_id, target,
                        source_client=retained_client, hass=hass)
                retained_client.on_refer = refer
            return operation
        except BaseException:
            if goal is not original and goal.live:
                registry.request_termination(goal.call_id, TerminationIntent("transfer_failed"), generation=goal.generation)
            raise

    def validate(self):
        if not self.session.live or self.retained_media is None:
            raise RuntimeError("retained transfer source ended")
        self.retained_media.validate(self.registry)

    async def retire(self):
        self.validate()

    async def resume(self):
        await self.restore(self.runtime)

    async def restore_preparation(self):
        await self.restore(self.runtime)

    async def restore(self, runtime):
        """Restore the previous conversation only while its transferor survives."""
        from .bridge_manager import async_watch_sip_bridge_destination
        from .call_transfer import _inbound_server
        from .endpoint_session import SipTerminationDisposition, TerminationIntent
        from .softphone_termination import async_terminate_sip_bridge_session
        original, goal = self.original_session, self.session
        if self.settled or not original.live or not goal.live:
            return False
        if self.referrer is not None:
            alive = self.referrer.dialog is not None
        else:
            server, uri = _inbound_server(runtime, original.call_id)
            alive = server is not None and bool(uri)
        if not alive:
            return False
        relay = self.retained_media.relay
        async with relay.media_reconfiguration_guard():
            with _reversed_transfer_media(relay, self.reversed_sides), SessionHandoff(self.registry, original, goal) as handoff:
                handoff.preserve(relay, "_on_release")
                handoff.preserve(self.retained_media.reservation, "released", "_audio_sockets")
                self.registry.take_media(goal.call_id, provisional=True)
                self.retained_media.reservation.detach()
                relay._on_release = self.previous_release
                if self.reversed_sides:
                    self.registry.remove_leg(goal.call_id, self.destination_id)
                    self.registry.release_resource(goal.call_id, f"relay:{goal.call_id}", value=relay)
                    self.registry.attach_relay(original.call_id, relay)
                    retained_endpoint = str(goal.metadata.get("source_endpoint_id") or "")
                    if retained_endpoint:
                        self.registry.transfer_endpoint_claim(goal.call_id, original.call_id, retained_endpoint)
                else:
                    self.registry.release_resource(original.call_id,
                        f"retired_destination:{self.destination_id}")
                self.registry.add_leg(original.call_id, self.destination_id, role="callee", state="in_call",
                    endpoint_id=str(original.metadata.get("dest_endpoint_id") or self.endpoint_id))
                self.registry.attach_sip_client(original.call_id, self.destination_id, self.client,
                    role="callee", state="in_call")
                self.registry.set_bridge_link(original.call_id, self.destination_id)
                for name, value in zip(("on_refer", "on_media_update", "on_info_dtmf"), self.previous_callbacks):
                    setattr(self.client, name, value)
                original.metadata.update(self.original_metadata)
                self.registry.transition(original.call_id, owner=self.original_owner, state=self.original_state,
                    callee=self.original_name, route_kind=self.original_route_kind)
                self.registry.take_pending_invite(goal.call_id)
                self.registry.release_resource(goal.call_id, f"native_destination:{goal.call_id}", value=self)
            self.registry.cancel_task(goal.call_id, f"client_watcher:{self.destination_id}")
        task = asyncio.create_task(async_watch_sip_bridge_destination(self.hass, client=self.client,
            source_call_id=original.call_id, terminate_sip_bridge=async_terminate_sip_bridge_session),
            name=f"voip-transfer-restore-{self.destination_id}")
        self.registry.attach_client_watcher(self.destination_id, task)
        if goal is not original:
            goal.update_metadata(event_only=True)
            def close_rolled_back(_task=None):
                self.registry.request_termination(goal.call_id,
                    TerminationIntent("transfer_rolled_back", sip_disposition=SipTerminationDisposition.NONE),
                    generation=goal.generation)
            forward = self.registry.task_for(goal.call_id, "forward")
            if forward is not None and not forward.done():
                forward.add_done_callback(close_rolled_back)
            else:
                close_rolled_back()
        self.restored = self.settled = True
        return True

    def finish(self, *, success):
        """Retire only the transferor after its final REFER notification."""
        if self.settled:
            return
        self.settled = True
        original, goal = self.original_session, self.session
        async def retire():
            from .endpoint_session import TerminationIntent
            if goal is original:
                resource = next((item for item in goal.resources if item.name == f"retired_destination:{self.destination_id}"), None)
                if resource is not None:
                    await resource.value.close("forwarded" if success else "transfer_failed")
                    self.registry.release_resource(goal.call_id, resource.name, value=resource.value)
                if self.endpoint_id and goal.live:
                    self.registry.release_endpoint_claim(goal.call_id, self.endpoint_id)
            elif original.live:
                self.registry.request_termination(original.call_id, TerminationIntent("forwarded"), generation=original.generation)
            if not success and goal.live:
                self.registry.request_termination(goal.call_id, TerminationIntent("transfer_failed"), generation=goal.generation)
            self.registry.release_resource(goal.call_id, f"native_destination:{goal.call_id}", value=self)
        def done(_transaction=None):
            if goal.live:
                goal.create_task(retire(), name=f"voip-transfer-retirement-{self.destination_id}")
            else:
                original.create_task(retire(), name=f"voip-transfer-retirement-{self.destination_id}") if original.live else None
        if self.transaction is not None and not self.transaction.done():
            self.transaction.add_done_callback(done)
        else:
            done()
