"""Call-owned local media shared by Automation and mailbox applications."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .browser_playback import BrowserPlayback
from .call_projection import publish_bridge_projection
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage, EndpointCallSession, TerminationIntent
from .inbound_answer import async_commit_runtime_answer
from .local_call_media import ApplicationCallSource, LocalCallMedia, source_dtmf_formats
from .media_ports import RtpPortReservation, take_delayed_offer_ports
from .core.sdp import build_answer_directional
from .sip_runtime import send_final_response

if TYPE_CHECKING:
    from .local_softphone_runtime import PendingLocalSource
    from .roster import RosterEntry
    from .sip_listener import SipInvite


@dataclass(frozen=True, slots=True)
class ApplicationRef:
    kind: str
    identity: str


@dataclass(slots=True)
class LocalCallApplication:
    """Use the session's media resources; application subclasses own policy."""

    owner: ClassVar[str] = "application"
    cleanup_stage: ClassVar[CleanupStage] = CleanupStage.MEDIA
    hass: HomeAssistant
    session: EndpointCallSession
    invite: SipInvite | ApplicationCallSource | None
    contact: RosterEntry
    local_ip: str
    media: LocalCallMedia | None = None
    playback: BrowserPlayback | None = None
    answered: bool = False
    outgoing: bool = False
    local_source: PendingLocalSource | None = None
    source_client: object | None = None
    resource_key: str = ""

    @property
    def ref(self):
        return ApplicationRef(self.owner, str(self.contact.metadata.get("queue_id")
            or self.contact.metadata.get("paging_group_id") or self.contact.id))

    @property
    def resource_name(self):
        kind = "assist_application" if self.owner == "assist" else self.owner
        return self.resource_key or f"{kind}:{self.session.call_id}"

    def require_current(self, generation: int | None = None) -> None:
        registry = call_registry(self.hass)
        if (
            not registry.is_generation_current(
                self.session.call_id, self.session.generation
            )
            or self.session.owner != self.owner
            or (generation is not None and generation != self.session.generation)
            or (self.resource_key and not any(resource.name == self.resource_name and resource.value is self
                                             for resource in self.session.resources))
        ):
            raise ServiceValidationError(
                "This call has ended or changed owner"
            )

    def speech_stream(self, message, entity_id, language=None, options=None):
        from homeassistant.components import tts

        stream = tts.async_create_stream(self.hass, entity_id, language,
                                        {**(options or {}), **self.media._tts_audio_output()})
        stream.async_set_message(message)
        return stream.async_stream_result()

    def handle_dtmf(self, side, digit, transport):
        from .dtmf_events import publish_dtmf_event

        publish_dtmf_event(self.hass, call_id=self.session.call_id, dest_call_id="",
                           caller=self.session.caller, callee=self.session.callee,
                           side=side, digit=digit, transport=transport)

    async def release_media_for_forward(self) -> None:
        """Release the local receiver before the next media owner binds it."""
        if self.media is None:
            return
        media = self.media
        await media.stop()
        call_registry(self.hass).release_resource(
            self.session.call_id, f"relay:{self.session.call_id}", value=media,
            generation=self.session.generation,
        )
        self.media = None

    async def answer(self) -> None:
        self.require_current()
        if self.answered and self.media is not None:
            return
        if self.outgoing:
            raise ServiceValidationError("The outgoing automation call is not ready")
        if self.source_client is not None:
            self.invite = ApplicationCallSource.from_client(self.source_client,
                call_id=self.session.call_id, caller=self.session.caller, target=self.contact.id,
                trunk_id=str(self.session.metadata.get("inbound_trunk_id") or ""))
        registry = call_registry(self.hass)
        preanswered = registry.resource_for(self.session.call_id, "preanswered")
        reservation = (
            (preanswered or {}).get("rtp_reservation")
            or take_delayed_offer_ports(registry, self.session.call_id)
            or RtpPortReservation.allocate(self.hass)
        )
        if self.local_source is not None:
            self.invite = self.local_source.stage_application_media(self.invite, *reservation.ports)
            registry.set_pending_invite(self.session.call_id, self.invite)

        async def complete(reason: str) -> None:
            registry.request_termination(
                self.session.call_id,
                TerminationIntent(reason),
                generation=self.session.generation,
            )

        retained = (preanswered or {}).get("retained_source_media")
        if retained is not None:
            media = await retained.adopt_for_application(self.hass, registry, complete)
        else:
            media = LocalCallMedia(
                self.hass,
                invite=self.invite,
                local_rtp_port=reservation.ports[0],
                reservation=reservation,
                on_complete=complete,
                rtp_source=(preanswered or {}).get("audio_rtp_source"),
            )
        media.on_dtmf = self.handle_dtmf
        media.private_dtmf = getattr(self, "private_dtmf", False)
        try:
            await media.start()
            self.require_current()
            if retained is None:
                registry.attach_relay(self.session.call_id, media)
            self.media = media
            if self.source_client is not None:
                bind_application_source_dialog(self)
            formats = source_dtmf_formats(self.invite)
            dtmf = formats[0] if formats else None
            if dtmf is not None:
                dtmf = replace(dtmf, events=dtmf.events & frozenset(range(16)))
            answer = "" if self.source_client is not None else build_answer_directional(
                self.local_ip,
                self.local_ip,
                reservation.ports[0],
                self.invite.send_format,
                self.invite.recv_format,
                dtmf=dtmf,
                remote_sdp=self.invite.remote_sdp,
            )
            result = await async_commit_runtime_answer(
                registry,
                self.session.call_id,
                answer,
                send_final_response=send_final_response,
                response_context=self.hass,
                owner=self.owner,
                caller=self.invite.caller,
                callee=self.contact.display_name,
                route_kind=self.owner,
                response_already_sent=self.answered or self.local_source is not None,
                **({
                    "endpoint_id": self.local_source.call.caller_endpoint_id,
                    "source_endpoint_id": self.local_source.call.caller_endpoint_id,
                    "media_client_id": self.local_source.call.caller_media_owner_id,
                } if self.local_source is not None else {}),
            )
            if not result.committed:
                raise ServiceValidationError(
                    "The caller ended before the announcement could start"
                )
            self.answered = True
            if preanswered is None:
                registry.attach_media(
                    self.session.call_id,
                    {
                        "rtp_reservation": reservation,
                        "local_rtp_port": reservation.ports[0],
                        "final_response_sent": True,
                        "early_answer_sdp": answer,
                        "audio_rtp_source": media.rtp_source,
                    },
                    provisional=True,
                )
            else:
                registry.update_media(
                    self.session.call_id,
                    provisional=True,
                    final_response_sent=True,
                    early_answer_sdp=answer,
                    audio_rtp_source=media.rtp_source,
                )
            # The shared preanswered resource now owns the reservation through handoff.
            media.release_reservation_on_stop = False
            if self.local_source is not None:
                self.local_source.connected(self.contact.display_name, self.owner)
            publish_bridge_projection(
                self.hass,
                self.session,
                event_type="answered",
                direction="incoming",
                route_kind=self.owner,
            )
        except BaseException:
            await media.stop()
            raise


def attach_local_application(application_type, hass, session, invite, contact, local_ip, *,
                             previous=None, local_source=None, **settings):
    """Attach one application capability after its session grants ownership."""
    session.ensure_live()
    if session.owner != application_type.owner:
        raise RuntimeError("Local application ownership has not been acquired")
    registry = call_registry(hass)
    preanswered = registry.resource_for(session.call_id, "preanswered")
    source = local_source or (previous.local_source if previous is not None else None)
    if previous is not None and "source_client" not in settings:
        settings["source_client"] = previous.source_client
    application = application_type(
        hass, session, invite, contact, local_ip,
        media=previous.media if previous is not None else None,
        answered=previous.answered if previous is not None else bool((preanswered or {}).get("final_response_sent")),
        outgoing=previous.outgoing if previous is not None else False,
        playback=previous.playback if previous is not None else registry.resource_for(session.call_id, "browser_playback"),
        local_source=source, **settings,
    )
    registry.own_resource(session.call_id, application.resource_name, application, application.close,
                          stage=application.cleanup_stage, generation=session.generation)
    if application.media is not None:
        application.media.on_dtmf = application.handle_dtmf
        application.media.private_dtmf = getattr(application, "private_dtmf", False)
    if application.source_client is not None:
        bind_application_source_dialog(application)
    if source is not None:
        if application.playback is None:
            application.playback = BrowserPlayback()
            registry.own_resource(session.call_id, f"browser_playback:{session.call_id}", application.playback,
                                  application.playback.close, stage=CleanupStage.MEDIA, generation=session.generation)
        if not source.committed:
            if not source.paused:
                source.pause()
            source.commit(keep_media=True)
    elif previous is None:
        session.update_metadata(endpoint_id=str(session.metadata.get("source_endpoint_id") or ""),
                                dest_endpoint_id="", target_endpoint_id="", ring_endpoint_ids=())
    return application


async def prepare_held_application_video_update(media, source_peer):
    """Stage a parked child's authenticated source while keeping video inactive."""
    from .core.sdp import video_formats_renegotiation_compatible
    from .sip_client import PreparedDialogMediaUpdate

    video = media.video_relay
    if (video is None or source_peer is None or source_peer.port <= 0
            or any(peer.video_format.direction != "inactive" for peer in (video.left, video.right))):
        raise ValueError("Application video must retain its existing held child")
    # Unlike an active-path check, a held child must retain the codec budget
    # even though neither direction is currently forwarding video.
    if not all(video_formats_renegotiation_compatible(old, new) for old, new in (
        (video.left.send_format, source_peer.send_format),
        (video.left.recv_format, source_peer.recv_format),
    )):
        raise ValueError("The held video update exceeds its negotiated contract")
    held_source = replace(source_peer,
        video_format=replace(source_peer.video_format, direction="inactive"),
        local_video_format=replace(source_peer.recv_format, direction="inactive"))
    undo_stages = []
    try:
        for side, peer in (("left", held_source), ("right", video.right)):
            _, undo = video.stage_peer_reconfiguration(side, peer)
            undo_stages.append(undo)
        commit_video, rollback_video = await video.async_prepare_peer_generation(
            left=held_source, right=video.right,
        )
    except BaseException:
        for undo in reversed(undo_stages):
            undo()
        raise

    async def commit():
        if media.video_relay is not video or media.closed.is_set():
            raise RuntimeError("The held video child changed before commit")
        await commit_video()

    return PreparedDialogMediaUpdate(commit, rollback_video,
        answer_video_format=held_source.recv_format, answer_video_rtp_port=video.left_port)


def bind_application_source_dialog(application):
    """Keep a retained outgoing SIP dialog attached to its current application."""
    client = application.source_client
    session = application.session
    registry = call_registry(application.hass)
    media = application.media
    if media is None:
        return
    async def prepare(_previous, updated, _method):
        if (not session.live or registry.resource_for(session.call_id, "relay") is not media
                or client.dialog is not _previous):
            return None
        video = getattr(media, "video_relay", None)
        if video is not None and updated.video_format is None:
            return None
        source = ApplicationCallSource.from_peer_dialog(updated, call_id=session.call_id,
            caller=session.caller, target=session.callee, trunk_id=str(session.metadata.get("inbound_trunk_id") or ""),
            allow_video=video is not None)
        previous_media_invite = media.invite
        commit_media = media.prepare_media_update(source)
        video_update = source_peer = None
        if video is not None:
            from .sip_bridge import dialog_video_rtp_peer
            try:
                source_peer = dialog_video_rtp_peer(updated)
                video_update = await prepare_held_application_video_update(media, source_peer)
            except (RuntimeError, TypeError, ValueError):
                return None
        def require_current():
            if (not registry.is_generation_current(session.call_id, session.generation)
                    or registry.resource_for(session.call_id, "relay") is not media
                    or application.media is not media or application.source_client is not client
                    or client.on_media_update is not prepare or client.dialog is not _previous
                    or getattr(media, "video_relay", None) is not video
                    or media.invite is not previous_media_invite):
                raise RuntimeError("The retained caller changed media owner")

        async def commit():
            require_current()
            if video_update is not None:
                await video_update.commit()
                require_current()
            commit_media()
            if video_update is not None:
                if hasattr(application, "video_source"):
                    application.video_source = source_peer
                application.invite = source
            if registry.artifact_for(session.call_id, "pending_invite") is not None:
                registry.set_pending_invite(session.call_id, source)
        if video_update is not None:
            from .sip_client import PreparedDialogMediaUpdate
            return PreparedDialogMediaUpdate(commit, video_update.rollback,
                answer_video_format=video_update.answer_video_format,
                answer_video_rtp_port=video_update.answer_video_rtp_port)
        return commit
    client.on_media_update = prepare
    def on_info(digit):
        if registry.resource_for(session.call_id, "relay") is media and media.on_dtmf is not None:
            media.on_dtmf("left", digit, "sip_info")
    client.on_info_dtmf = on_info
