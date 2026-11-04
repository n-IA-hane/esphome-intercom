"""Retire one unanswered SIP destination while retaining its real caller."""

import asyncio
from dataclasses import dataclass

from homeassistant.exceptions import ServiceValidationError

from .endpoint_session import CleanupStage


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
    retired: bool = False
    cleanup_task: object = None

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
        watcher = registry.cancel_task(
            session.call_id, f"client_watcher:{self.destination_id}"
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
        for resource in tuple(session.resources):
            if resource.name.startswith("pending_bridge_media:"):
                if self.invite.initial_response_sent:
                    reservation = resource.value.transfer()
                    ports = reservation.ports
                    registry.attach_media(
                        session.call_id,
                        {
                            "rtp_reservation": reservation,
                            "local_rtp_port": ports[0],
                            "final_response_sent": True,
                        },
                        provisional=True,
                    )
                await resource.close("forwarded")
                if session.live:
                    registry.release_resource(
                        session.call_id, resource.name, value=resource.value
                    )

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
