"""Retire one unanswered SIP destination while retaining its real caller."""

import asyncio
from dataclasses import dataclass
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

    def validate(self, registry):
        session = registry.get_session(self.token.call_id, generation=self.token.generation)
        if (session is None or not session.live or self.reservation.released
                or registry.resource_for(self.token.call_id, "relay") is not self.relay
                or self.reservation.ports != (self.relay.left_port, self.relay.right_port)
                or self.relay.left_transport is None or self.relay.right_transport is None
                or self.relay.left_transport.is_closing() or self.relay.right_transport.is_closing()):
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

    def restore_preparation(self):
        """Undo only the caller projection while its original leg still owns SIP."""
        if (not self.retired and self.session.live
                and self.registry.sip_client_for(self.destination_id) is self.client
                and self.client.dialog is None):
            self.registry.transition(self.session.call_id, owner=self.original_owner,
                state=self.original_state, callee=self.original_name, route_kind=self.original_route_kind)

    def resume(self):
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
