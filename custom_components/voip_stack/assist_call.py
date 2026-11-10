"""Expose Assist's existing call/media owner to the forwarding dispatcher."""

from dataclasses import dataclass
from typing import ClassVar

from .call_projection import publish_bridge_projection
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage
from .local_application import LocalCallApplication
from .roster import RosterEntry


@dataclass(slots=True)
class AssistCall(LocalCallApplication):
    """No-answer keeps the existing conversation, context and RTP consumer."""

    owner: ClassVar[str] = "assist"
    conversation: object = None

    async def resume(self):
        self.require_current()
        publish_bridge_projection(self.hass, self.session, direction="incoming",
                                  event_type="state_changed", route_kind="assist")

    async def release_media_for_forward(self):
        if self.conversation is not None:
            self.conversation.detached = True
        await LocalCallApplication.release_media_for_forward(self)

    async def close(self, reason):
        if reason != "forwarded":
            return
        if self.conversation is not None:
            self.conversation.detached = True
        if self.media is not None:
            await self.media.stop_consumer()
            self.media.discard_output()


def attach_assist_application(hass, session, invite, media, contact_name, local_ip):
    registry = call_registry(hass)
    existing = registry.resource_for(session.call_id, "assist_application")
    if existing is not None:
        raise RuntimeError("The call already has an Assist application")
    application = AssistCall(hass, session, invite, RosterEntry("Assist", name=contact_name), local_ip,
        media=media, answered=True, conversation=media.conversation,
        local_source=registry.resource_for(session.call_id, "browser_source"))
    registry.own_resource(session.call_id, f"assist_application:{session.call_id}", application,
                          application.close, stage=CleanupStage.MEDIA, generation=session.generation)
    registry.set_pending_invite(session.call_id, invite)
    if registry.resource_for(session.call_id, "preanswered") is None:
        registry.attach_media(session.call_id, {
            "rtp_reservation": media.reservation,
            "local_rtp_port": media.local_rtp_port,
            "final_response_sent": True,
            "audio_rtp_source": media.rtp_source,
        }, provisional=True)
        media.release_reservation_on_stop = False
    return application
