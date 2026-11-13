"""Parking applications share the normal SIP, media and forwarding owners."""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, replace
import logging
from typing import ClassVar

from .session_cleanup import async_wait_for_cleanup
from .call_deadlines import CallDeadline
from .call_projection import publish_bridge_projection
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage, TerminationIntent
from .local_application import LocalCallApplication, attach_local_application
from .local_media_bridge import LocalMediaBridge
from .parking_lot import ParkingError, ParkingLot
from .parking_config import DEFAULT_SLOTS, parking_settings, parse_parking_address
from .roster import RosterEntry
from .router import RouteAction, RouteDecision
from .runtime_data import require_runtime_data
from .sip_listener import SipInviteResult

_LOGGER = logging.getLogger(__name__)
PARKING_CHANGED = "voip_stack.parking_changed"


def parking_address(hass, target):
    return parse_parking_address(parking_settings(hass), target)


def resolve_parking_route(hass, target):
    address = parking_address(hass, target)
    if address is None:
        return None
    mode, slot = address
    return RouteDecision(RouteAction.PARKING, target=str(target), source="builtin",
        entry=RosterEntry(str(target), name=f"Parking {slot}".strip(),
                          metadata={"parking_mode": mode, "parking_slot": slot}))


def get_parking_lot(hass):
    runtime = require_runtime_data(hass)
    owner = runtime.sip
    settings = parking_settings(hass)
    if owner is None or not owner.active or not settings.get("enabled"):
        raise ParkingError("Configure non-conflicting parking access and slot numbers in VoIP Stack settings")
    lot = owner.component("parking")
    if lot is None:
        def changed():
            hass.bus.async_fire(PARKING_CHANGED, {"occupied": sorted(lot.calls)})
        lot = ParkingLot(tuple(settings.get("slots") or DEFAULT_SLOTS), changed)
        owner.adopt_component("parking", lot, closer=lot.close)
    return lot


async def _prepare_video_handoff(hass, session, video, source, destination, *, source_client=None, wire_id=None,
                                 source_side="left"):
    """Stage held/resumed video with the existing dialog and relay generations."""
    from .bridge_media_updates import prepare_source_video_update, prepare_owned_video_generation
    from .core import sdp
    from .sip_bridge import dialog_video_rtp_peer, invite_video_rtp_peer
    from .sip_client import PreparedDialogMediaUpdate

    registry = call_registry(hass)
    source_owner = registry.resource_for(session.call_id, "relay")
    source_generation = session.generation

    def owns_source():
        return (session.live and registry.is_generation_current(session.call_id, source_generation)
            and registry.resource_for(session.call_id, "relay") is source_owner
            and getattr(source_owner, "video_relay", None) is video)

    def terminate_failed_restore():
        if owns_source():
            registry.request_termination(session.call_id,
                TerminationIntent("parking_video_restore_failed"), generation=source_generation)

    previous_source, previous_destination = video._peers(source_side)
    source_port = getattr(video, f"{source_side}_port")
    destination_side = "right" if source_side == "left" else "left"
    previous_formats = tuple(dict.fromkeys((previous_source.recv_format, previous_source.send_format)))
    _, undo_left = video.stage_peer_reconfiguration(source_side, source)
    try:
        _, undo_right = video.stage_peer_reconfiguration(destination_side, destination)
    except BaseException:
        undo_left()
        raise
    prepared = None
    commit_video = rollback_video = None
    settled = ""

    async def rollback():
        nonlocal settled
        if settled == "rolled_back":
            return
        committed = settled == "committed"
        settled = "rolled_back"
        if committed:
            if owns_source():
                try:
                    restoring = await _prepare_video_handoff(hass, session, video, previous_source, previous_destination,
                        source_client=source_client, wire_id=wire_id, source_side=source_side)
                    await restoring.commit()
                except BaseException:
                    terminate_failed_restore()
                    raise
            return
        if rollback_video is not None:
            await rollback_video()
        else:
            undo_left()
            undo_right()
        if prepared is not None and owns_source():
            if not await prepared.restore(local_video_rtp_port=source_port, video_formats=previous_formats):
                terminate_failed_restore()
                raise ParkingError("The retained video dialog could not be restored")

    try:
        # Validate conversion admission before changing the retained SIP leg.
        directions = video.transcode_directions_for(
            **{source_side: source, destination_side: destination})
        if directions and video._transcode_hass is None:
            from .config import transport_config
            from .const import CONF_VIDEO_TRANSCODING
            if not transport_config(hass).get(CONF_VIDEO_TRANSCODING, False):
                raise ParkingError("The pickup video formats require unavailable conversion")
            video.configure_transcoding(hass, session.call_id)
        prepared = await prepare_source_video_update(hass, wire_id or session.call_id, source_client=source_client,
            local_video_rtp_port=source_port,
            video_formats=tuple(dict.fromkeys((source.recv_format, source.send_format))),
            video_direction=sdp.local_direction_for_remote(source.video_format.direction))
        if prepared is None:
            raise ParkingError("The retained caller rejected the video update")
        if not owns_source():
            raise ParkingError("The retained video owner changed before preparation")
        actual_source = (dialog_video_rtp_peer(prepared.candidate) if source_client is not None
                         else invite_video_rtp_peer(prepared.candidate))
        video.update_staged_peer(source_side, source, actual_source)
        commit_video, rollback_video = await prepare_owned_video_generation(call_registry(hass), session, video,
            **{source_side: actual_source, destination_side: destination})
        if not session.live or video._stop_requested or not prepared.commit():
            raise ParkingError("The parked video call changed before commit")
    except BaseException:
        await async_wait_for_cleanup(asyncio.create_task(rollback(), name="voip-parking-video-rollback"))
        raise

    async def commit():
        if settled:
            raise ParkingError("The video handoff has already settled")
        if not session.live or video._stop_requested:
            raise ParkingError("The retained caller ended before video handoff")
        async def apply():
            nonlocal settled
            await commit_video()
            settled = "committed"
        await async_wait_for_cleanup(asyncio.create_task(apply(), name="voip-parking-video-settlement"))

    return PreparedDialogMediaUpdate(commit, rollback,
        answer_video_format=destination.send_format, answer_video_rtp_port=getattr(video, f"{destination_side}_port"))


def _video_peer_direction(peer, direction):
    return replace(peer, video_format=replace(peer.video_format, direction=direction),
                   local_video_format=replace(peer.recv_format, direction=direction))


@dataclass(slots=True)
class ParkingCall(LocalCallApplication):
    owner: ClassVar[str] = "parking"
    lot: ParkingLot | None = None
    parked: object | None = None
    settings: dict | None = None
    music_task: asyncio.Task | None = None
    video_source: object | None = None

    async def prepare_video_pickup(self, invite):
        from .core import sdp
        from .sip_bridge import invite_video_rtp_peer

        if self.media.video_relay is None:
            return None
        registry = call_registry(self.hass)
        if (not registry.is_generation_current(self.session.call_id, self.session.generation)
                or registry.resource_for(self.session.call_id, "parking") is not self
                or registry.resource_for(self.session.call_id, "relay") is not self.media):
            raise ParkingError("The parked media owner changed before pickup")
        if self.video_source is None or invite.video_format is None:
            raise ParkingError("The parked video call requires a video pickup")
        source = self.video_source
        destination = invite_video_rtp_peer(invite)
        source_direction = sdp.constrained_video_direction(source.video_format.direction,
            allow_send=sdp.remote_can_send(destination.video_format) and not source.connection_held,
            allow_receive=sdp.remote_can_receive(destination.video_format, connection_held=destination.connection_held))
        destination_direction = sdp.constrained_video_direction(destination.video_format.direction,
            allow_send=sdp.remote_can_send(source.video_format) and not destination.connection_held,
            allow_receive=sdp.remote_can_receive(source.video_format, connection_held=source.connection_held))
        return await _prepare_video_handoff(self.hass, self.session, self.media.video_relay,
            _video_peer_direction(source, sdp.local_direction_for_remote(source_direction)),
            _video_peer_direction(destination, sdp.local_direction_for_remote(destination_direction)),
            source_client=self.source_client)

    def cancel_deadline(self):
        if self.parked is not None:
            self.lot.release(self.parked)

    async def close(self, _reason):
        self.cancel_deadline()
        await self.stop_music()

    async def stop_music(self):
        playing = self.music_task is not None and not self.music_task.done()
        if playing and self.music_task is not asyncio.current_task():
            self.music_task.cancel()
            await asyncio.gather(self.music_task, return_exceptions=True)
        if playing and self.media is not None:
            self.media.discard_output()

    async def _music(self):
        from .announcement_media import async_audio_stream

        try:
            while self.session.live and not self.media.closed.is_set():
                frames = False
                async with aclosing(async_audio_stream(self.hass, self.settings["hold_media"])) as chunks:
                    async def observed():
                        nonlocal frames
                        async for chunk in chunks:
                            frames = frames or bool(chunk)
                            yield chunk
                    await self.media.play_pcm_stream(observed())
                    await self.media.drain_output()
                if not frames:
                    raise ValueError("The hold recording contains no audio")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning("Parking hold audio is unavailable; retaining the call in silence", exc_info=True)

    async def run(self):
        try:
            await self.answer()
            if self.settings.get("hold_media"):
                self.music_task = self.session.create_task(self._music(), name="parking-hold-audio")
            self.lot.arm_return(self.parked, float(self.settings.get("timeout", 180)), self._return)
            publish_bridge_projection(self.hass, self.session, event_type="parked", route_kind="parking",
                                      parking_slot=self.parked.slot)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Parking setup failed call_id=%s", self.session.call_id)
            call_registry(self.hass).request_termination(self.session.call_id,
                TerminationIntent("parking_failed", response_status=503), generation=self.session.generation)

    async def resume(self):
        self.require_current()
        if self.media is None:
            await self.answer()
        if self.settings.get("hold_media") and (self.music_task is None or self.music_task.done()):
            self.music_task = self.session.create_task(self._music(), name="parking-hold-audio")

    async def _return(self, claim):
        registry = call_registry(self.hass)
        destination = self.parked.return_destination
        if not destination:
            await registry.terminate_call_wait(self.session.call_id, reason="parking_timeout")
            return
        target = require_runtime_data(self.hass).endpoints.get(destination)
        if target is not None:
            destination = target.name
        callback = registry.forward_call
        if callback is None:
            raise ParkingError("Call forwarding is unavailable")
        await self.stop_music()
        await callback(call_id=self.session.call_id, destination=destination, on_failure="terminate")
        if not self.session.live:
            return
        claim.commit()
        def expired():
            if self.session.owner == "router" or self.session.state in {"calling", "connecting", "ringing", "remote_ringing"}:
                registry.request_termination(self.session.call_id, TerminationIntent("parking_return_timeout"),
                                             generation=self.session.generation)
        deadline = CallDeadline(registry, self.session, f"parking_return:{self.session.call_id}", expired)
        deadline.arm(self.hass.loop, float(self.settings.get("return_timeout", 30)))


async def start_parking_application(hass, session, invite, local_ip, *, slot="", parker="", previous=None, media=None, source_client=None, local_source=None, video_source=None):
    lot = get_parking_lot(hass)
    settings = parking_settings(hass)
    parked = lot.reserve(session, slot=slot, parker=parker,
                         return_destination=str(settings.get("return_destination") or parker))
    registry = call_registry(hass)
    current = registry.transition(session.call_id, owner="parking", route_kind="parking",
        state="in_call" if media is not None else session.state,
        expected_generation=session.generation, expected_owner=session.owner, expected_revision=session.revision)
    if current is None:
        lot.release(parked)
        raise ParkingError("The call changed before it could be parked")
    application = attach_local_application(ParkingCall, hass, session, invite,
        RosterEntry("park."+parked.slot, name="Parking "+parked.slot), local_ip,
        previous=previous, local_source=local_source, lot=lot, parked=parked, settings=settings, video_source=video_source,
        **({"source_client": source_client} if source_client is not None else {}))
    if media is not None:
        application.media, application.answered = media, True
        media.on_dtmf = application.handle_dtmf
        if application.source_client is not None:
            from .local_application import bind_application_source_dialog
            application.source_client.audio_rtp_source = media.rtp_source
            bind_application_source_dialog(application)
    if previous is not None:
        previous.cancel_deadline()
        registry.release_resource(session.call_id, f"{previous.owner}:{session.call_id}", value=previous)
    session.create_task(application.run(), name="parking-start")
    return application


async def park_local_browser_call(hass, session, endpoint_id, *, slot=""):
    """Move the remaining local phone into the shared application media path."""
    from .local_softphone_runtime import PendingLocalSource, local_softphone_bridge
    from .local_call_media import LocalCallMedia
    from .media_ports import RtpPortReservation

    runtime = require_runtime_data(hass)
    registry = runtime.sip
    bridge = local_softphone_bridge(hass)
    snapshot = bridge.get_call(session.call_id) if bridge else None
    if snapshot is None or endpoint_id not in {snapshot.caller_endpoint_id, snapshot.callee_endpoint_id}:
        raise ParkingError("The selected phone does not own this local call")
    retained = snapshot.callee_endpoint_id if endpoint_id == snapshot.caller_endpoint_id else snapshot.caller_endpoint_id
    source = PendingLocalSource(hass, snapshot, "127.0.0.1", int(runtime.transport_config["sip_port"]),
        retained_endpoint_id=retained)
    lot = get_parking_lot(hass)
    settings = parking_settings(hass)
    parked = lot.reserve(session, slot=slot, parker=endpoint_id,
        return_destination=str(settings.get("return_destination") or endpoint_id))
    reservation = None
    browser_reservation = None
    media = None
    try:
        reservation = RtpPortReservation.allocate(hass)
        browser_reservation = RtpPortReservation.allocate(hass)
        source.invite = source.stage_application_media(source.invite, reservation.ports[0], browser_reservation.ports[0])
        source.media["rtp_reservation"] = browser_reservation
        async def complete(reason):
            registry.request_termination(session.call_id, TerminationIntent(reason), generation=session.generation)
        media = LocalCallMedia(hass, invite=source.invite, local_rtp_port=reservation.ports[0],
            reservation=reservation, on_complete=complete)
        await media.start()
        current = bridge.get_call(session.call_id)
        # Audio may keep arriving while the application socket is prepared.
        # Queue counters are not a change of call or media ownership.
        if current is None or not session.live or current != replace(
            snapshot, caller_media=current.caller_media, callee_media=current.callee_media
        ):
            raise ParkingError("The local call changed before parking")
    except BaseException:
        source.media.clear()
        source.media.update(source.original_media)
        lot.release(parked)
        if browser_reservation is not None:
            browser_reservation.release()
        if media is not None:
            await media.stop()
        elif reservation is not None:
            reservation.release()
        raise
    registry.own_resource(session.call_id, f"browser_source:{session.call_id}", source, None)
    registry.attach_relay(session.call_id, media)
    registry.attach_media(session.call_id, {"rtp_reservation": reservation,
        "local_rtp_port": reservation.ports[0], "final_response_sent": True,
        "audio_rtp_source": media.rtp_source}, provisional=True)
    media.release_reservation_on_stop = False
    registry.set_pending_invite(session.call_id, source.invite)
    session.caller = source.invite.caller
    application = await start_parking_application(hass, session, source.invite, "127.0.0.1",
        slot=parked.slot, parker=endpoint_id, media=media, local_source=source)
    source.connected(application.contact.display_name, "parking")
    return application


async def park_browser_sip_call(hass, call_id, endpoint_id, *, slot=""):
    """Retain the real SIP dialog and RTP socket when its browser parks it."""
    from .bridge_manager import async_watch_owned_sip_dialog
    from .call_transfer import _inbound_server
    from .local_call_media import ApplicationCallSource, LocalCallMedia
    from .media_ports import RtpPortReservation
    from .media_reservation import release_video_media_reservation
    from .websocket_api import _release_ha_softphone_claim

    runtime = require_runtime_data(hass)
    registry = runtime.sip
    session = registry.get_session(registry.resolve_session_id(call_id))
    if (session is None or not session.live or session.state != "in_call"
            or endpoint_id not in session.endpoint_claims):
        raise ParkingError("Select an established call owned by this phone")
    active = runtime.media.sessions_for("audio").get((session.call_id, endpoint_id))
    descriptor = registry.resource_for(session.call_id, "softphone_media") or {}
    client = registry.source_sip_client_for(session.call_id) or registry.sip_client_for(session.call_id)
    video = runtime.media.sessions_for("video").get((session.call_id, endpoint_id))
    if active is None or active.handoff_transport is None:
        raise ParkingError("The phone audio connection is not ready")
    video_owner = runtime.media.owners_for("video").get(f"{endpoint_id}|{session.call_id}")
    if video is not None and video_owner is None:
        raise ParkingError("The phone video connection is not ready")
    server, _ = _inbound_server(runtime, session.call_id)
    source = server.invite_for_call(session.call_id) if server is not None else None
    retained = next((item for item in session.endpoint_claims if item != endpoint_id), "")
    peer = runtime.endpoints.get(retained) if retained else None
    caller = peer.name if peer is not None else session.caller
    if client is not None:
        source = ApplicationCallSource.from_client(client, call_id=session.call_id, caller=caller,
            trunk_id=str(session.metadata.get("inbound_trunk_id") or ""), allow_video=True)
    if source is None:
        raise ParkingError("The remote SIP dialog is no longer available")
    if (client is None and descriptor.get("local_video_rtp_port") == 0
            and video is None and video_owner is None):
        # The UAS invite retains the remote offer. The committed answer can
        # reject its video stream (RFC 3264 section 6); keep that decision
        # in the application's contract without changing the received offer.
        source = replace(source, video_format=None, local_video_format=None, video_answer_format=None,
            remote_video_rtp_port=0, remote_video_rtcp_port=0, remote_video_payload_types=())
    local_ip = str(client.local_ip if client is not None else server.local_ip)
    lot = get_parking_lot(hass)
    settings = parking_settings(hass)
    parked = lot.reserve(session, slot=slot, parker=endpoint_id,
        return_destination=str(settings.get("return_destination") or endpoint_id))
    # Direct outgoing calls keep their reservation on the SIP client rather
    # than an inbound media descriptor. Transfer that ownership with the RTP
    # transport; unpooled audio sockets use the existing adoption primitive.
    client_reservation = getattr(client, "media_reservation", None)
    reservation = descriptor.get("rtp_reservation") or client_reservation
    acquired = reservation is None
    video_relay = video_source = video_update = None
    original_video_port = int(video.local_rtp_port if video is not None else
        client.dialog.local_video_rtp_port if client is not None else descriptor.get("local_video_rtp_port") or 0)
    try:
        if acquired:
            reservation = RtpPortReservation.adopt_bound_source(hass, active.local_rtp_port)
        if source.video_format is not None:
            from .bridge_media_updates import prepare_source_video_update
            from .config import transport_config
            from .const import CONF_VIDEO_TRANSCODING
            from .media_ports import reserve_sip_video_relay_media
            from .sip_bridge import invite_video_rtp_peer, dialog_video_rtp_peer
            from .sip_video_relay import SipVideoRtpRelay

            if not original_video_port:
                raise ParkingError("The existing video contract has no owned RTP port")
            video_source = invite_video_rtp_peer(source)
            if client is None:
                from .core.sdp import local_direction_for_remote
                video_source = _video_peer_direction(video_source,
                    local_direction_for_remote(str(descriptor.get("video_direction") or "inactive")))
            video_ports, sockets = reserve_sip_video_relay_media(hass)
            inactive = _video_peer_direction(video_source, "inactive")
            video_relay = SipVideoRtpRelay(left=inactive, right=replace(inactive, port=0, rtcp_port=0),
                left_port=video_ports.ports[0], right_port=video_ports.ports[1],
                left_socket=sockets[0], left_rtcp_socket=sockets[1], right_socket=sockets[2], right_rtcp_socket=sockets[3],
                on_release=lambda _ports: video_ports.release())
            if transport_config(hass).get(CONF_VIDEO_TRANSCODING, False):
                video_relay.configure_transcoding(hass, session.call_id)
            await video_relay.start()
            video_update = await prepare_source_video_update(hass, session.call_id, source_client=client,
                local_video_rtp_port=video_relay.left_port,
                video_formats=tuple(dict.fromkeys((video_source.recv_format, video_source.send_format))),
                video_direction="inactive")
            if video_update is None:
                raise ParkingError("The retained caller rejected video parking")
            if (not session.live or runtime.media.sessions_for("audio").get((session.call_id, endpoint_id)) is not active
                    or not video_update.commit()):
                raise ParkingError("The browser call changed before video parking")
            video_relay.left = (dialog_video_rtp_peer(video_update.candidate) if client is not None
                                else invite_video_rtp_peer(video_update.candidate))
            if video_owner is not None:
                video_owner.revoke()
                async with asyncio.timeout(5):
                    await video_owner.released.wait()
            if not session.live or runtime.media.sessions_for("audio").get((session.call_id, endpoint_id)) is not active:
                raise ParkingError("The browser call ended during video retirement")
        async def complete(reason):
            registry.request_termination(session.call_id, TerminationIntent(reason), generation=session.generation)
        media = LocalCallMedia(hass, invite=source, local_rtp_port=active.local_rtp_port,
            reservation=reservation, on_complete=complete, rtp_source=active.rtp_source)
        def adopt(transport, host, port, clock):
            media.remote_rtp_host, media.remote_rtp_port = host, port
            media.rtp_source = clock
            media.adopt_transport(transport)
        active.handoff_transport(adopt)
        media.video_relay = video_relay
    except BaseException:
        async def restore_browser():
            try:
                if video_update is not None and session.live:
                    if not await video_update.restore(local_video_rtp_port=original_video_port,
                            video_formats=tuple(dict.fromkeys((video_source.recv_format, video_source.send_format)))):
                        registry.request_termination(session.call_id, TerminationIntent("parking_video_restore_failed"),
                                                     generation=session.generation)
            finally:
                try:
                    if video_relay is not None:
                        await video_relay.stop()
                finally:
                    lot.release(parked)
                    if acquired and reservation is not None:
                        reservation.release()
        await async_wait_for_cleanup(asyncio.create_task(restore_browser(), name="voip-parking-browser-restore"))
        raise
    if client_reservation is not None and reservation is client_reservation:
        client.media_reservation = None
        for name in ("video_rtp_socket", "video_rtcp_socket"):
            sock = getattr(client, name, None)
            setattr(client, name, None)
            if sock is not None:
                sock.close()
    registry.take_media(session.call_id)
    release_video_media_reservation(descriptor)
    media.release_reservation_on_stop = False
    registry.attach_relay(session.call_id, media)
    registry.attach_media(session.call_id, {"rtp_reservation": reservation,
        "local_rtp_port": active.local_rtp_port, "final_response_sent": True,
        "audio_rtp_source": media.rtp_source}, provisional=True)
    registry.set_pending_invite(session.call_id, source)
    _release_ha_softphone_claim(hass, session.call_id, endpoint_id=endpoint_id,
        destination="Parking " + parked.slot)
    session.update_metadata(source_endpoint_id=retained, endpoint_id=retained,
        dest_endpoint_id="", target_endpoint_id="", ring_endpoint_ids=(), media_client_id="")
    session.caller = caller
    if client is not None:
        wire_id = client.dialog_ids.call_id
        old_watcher = registry.cancel_task(session.call_id, f"client_watcher:{wire_id}")
        registry.add_leg(session.call_id, wire_id, role="caller", state="in_call")
        watcher = hass.async_create_task(async_watch_owned_sip_dialog(registry, session.call_id, client, session.generation))
        registry.attach_client_watcher(wire_id, watcher)
    else:
        old_watcher = None
    application = await start_parking_application(hass, session, source, local_ip,
        slot=parked.slot, parker=endpoint_id, media=media, source_client=client, video_source=video_source)
    if old_watcher is not None:
        await asyncio.gather(old_watcher, return_exceptions=True)
    return application


async def park_established_sip_call(hass, call_id, parker, *, slot="", referrer=None, refer_transaction=None):
    """Move the retained real SIP leg into parking without another INVITE."""
    import secrets
    from .bridge_manager import async_watch_owned_sip_dialog
    from .call_transfer import _inbound_server
    from .endpoint_session import SipTerminationDisposition
    from .local_call_media import ApplicationCallSource
    from .local_media_bridge import adopt_relay_for_application
    from .media_ports import RtpPortReservation
    from .sip_rtp_bridge import SipRtpRelay

    runtime = require_runtime_data(hass)
    registry = runtime.sip
    original = registry.get_session(registry.resolve_session_id(call_id))
    if original is None or not original.live or original.state != "in_call":
        raise ParkingError("Select an established call to park")
    destination_id = registry.bridge_link_for(original.call_id)
    destination_leg = original.legs.get(destination_id)
    if destination_leg is None:
        raise ParkingError("The selected call has no receiving leg")
    from_callee = (destination_leg.dialog is referrer if referrer is not None else
                   call_id == destination_id or bool(parker and parker == original.metadata.get("dest_endpoint_id")))
    if not from_callee and referrer is None and parker != original.metadata.get("source_endpoint_id"):
        raise ParkingError("The selected phone does not own this call")
    relay = registry.resource_for(original.call_id, "relay")
    if not isinstance(relay, SipRtpRelay):
        raise ParkingError("This call has no supported audio bridge to park")
    side = "left" if from_callee else "right"
    retained_endpoint = str(original.metadata.get("source_endpoint_id" if from_callee else "dest_endpoint_id") or "")
    retained_leg = (next((leg for leg in original.legs.values() if leg.role == "caller" and leg.dialog is not None), None)
                    if from_callee else destination_leg)
    source_client = retained_leg.dialog if retained_leg is not None else None
    wire_id = source_client.dialog_ids.call_id if source_client is not None else original.call_id if from_callee else destination_id
    server, _uri = _inbound_server(runtime, wire_id)
    native_invite = server.invite_for_call(wire_id) if server is not None else None
    if source_client is None and native_invite is None:
        raise ParkingError("The retained caller dialog is no longer available")
    local_ip = str(source_client.local_ip if source_client is not None else server.local_ip)
    goal_id = original.call_id if from_callee else f"ha-park-{secrets.token_hex(12)}" if source_client is not None else wire_id
    caller = original.caller if from_callee else original.callee
    source = (ApplicationCallSource.from_client(source_client, call_id=goal_id, caller=caller,
                                               trunk_id=retained_leg.trunk_id if retained_leg is not None else "",
                                               allow_video=relay.video_relay is not None)
              if source_client is not None else native_invite)
    lot = get_parking_lot(hass)
    settings = parking_settings(hass)
    goal = original if from_callee else registry.upsert(goal_id, state="connecting", owner="router", caller=caller,
        source_endpoint_id=retained_endpoint, event_only=True,
        inbound_trunk_id=retained_leg.trunk_id if retained_leg is not None else "")
    parked = None
    video_update = None
    video_source = replace(getattr(relay.video_relay, side)) if relay.video_relay is not None else None
    try:
        parked = lot.reserve(goal, slot=slot, parker=parker,
                             return_destination=str(settings.get("return_destination") or parker))
        ports = (getattr(relay, f"{side}_port"), getattr(relay, f"{'right' if side == 'left' else 'left'}_port"))
        if not set(ports) <= runtime.rtp_port_pool.get("used", set()):
            raise ParkingError("The call's media reservation changed")
        reservation = RtpPortReservation(hass, ports)
        async def complete(reason):
            registry.request_termination(goal.call_id, TerminationIntent(reason), generation=goal.generation)
        if relay.video_relay is not None:
            video = relay.video_relay
            video_update = await goal.create_task(_prepare_video_handoff(hass, original, video,
                _video_peer_direction(getattr(video, side), "inactive"),
                _video_peer_direction(getattr(video, "right" if side == "left" else "left"), "inactive"),
                source_client=source_client, wire_id=wire_id, source_side=side), name="parking-video-hold")
            await video_update.commit()
            if not goal.live or registry.resource_for(original.call_id, "relay") is not relay:
                raise ParkingError("The call changed while video was held")
        media = adopt_relay_for_application(hass, relay, side, source, reservation, complete)
    except BaseException:
        try:
            if video_update is not None:
                await async_wait_for_cleanup(asyncio.create_task(video_update.rollback(), name="voip-parking-video-restore"))
        finally:
            if goal is not original:
                await registry.terminate_call_wait(goal.call_id, intent=TerminationIntent("parking_failed", sip_disposition=SipTerminationDisposition.NONE))
            elif parked is not None:
                lot.release(parked)
        raise
    # The actual media socket, dialog and endpoint claim move together.
    old_watcher = registry.cancel_task(original.call_id, f"client_watcher:{destination_id}")
    registry.remove_leg(original.call_id, destination_id)
    registry.forget_bridge_link(original.call_id)
    retired_name = f"retired_relay:{id(relay):x}"
    original.release_resource(f"relay:{original.call_id}", value=relay)
    goal.add_resource(retired_name, relay, lambda _reason: relay.stop(), stage=CleanupStage.MEDIA)
    registry.attach_relay(goal.call_id, media)
    registry.attach_media(goal.call_id, {"rtp_reservation": reservation, "local_rtp_port": ports[0],
        "final_response_sent": True, "audio_rtp_source": media.rtp_source}, provisional=True)
    media.release_reservation_on_stop = False
    registry.set_pending_invite(goal.call_id, source)
    goal.update_metadata(event_only=False,
        policy_endpoint_ids=tuple(sorted(set((*original.endpoint_claims, *original.metadata.get("policy_endpoint_ids", ()))))),
        connected_party="Parking "+parked.slot)
    if not from_callee:
        if source_client is not None:
            source_client.on_refer = None
            registry.add_leg(goal.call_id, wire_id, role="caller", state="in_call", endpoint_id=retained_endpoint)
            registry.attach_sip_client(goal.call_id, wire_id, source_client, role="caller", state="in_call", trunk_id=destination_leg.trunk_id)
            watcher = hass.async_create_task(async_watch_owned_sip_dialog(registry, goal.call_id, source_client, goal.generation))
            registry.attach_client_watcher(wire_id, watcher)
        else:
            registry.add_leg(goal.call_id, goal.call_id, role="caller", state="in_call", endpoint_id=retained_endpoint)
        if retained_endpoint:
            registry.transfer_endpoint_claim(original.call_id, goal.call_id, retained_endpoint)
        goal.claim_answer(goal.token, lambda: True)
    elif parker:
        registry.release_endpoint_claim(goal.call_id, parker)
    retiring_name = f"retired_parker:{destination_id}"
    if from_callee:
        goal.add_resource(retiring_name, destination_leg, destination_leg.close, stage=CleanupStage.LEG)
    application = await start_parking_application(hass, goal, source, local_ip,
        slot=parked.slot, parker=parker, media=media, source_client=source_client, video_source=video_source)
    async def finish():
        if from_callee:
            await destination_leg.close("parked")
            if goal.live:
                registry.release_resource(goal.call_id, retiring_name, value=destination_leg)
        else:
            await registry.terminate_call_wait(original.call_id, reason="forwarded")
    def after_refer(_task=None):
        if goal.live:
            goal.create_task(finish(), name=f"parking-retire-{destination_id}")
        elif not from_callee and original.live:
            registry.request_termination(original.call_id, TerminationIntent("forwarded"))
    transaction = refer_transaction or (getattr(referrer, "_incoming_refer_task", None) if referrer is not None else None)
    if transaction is not None and not transaction.done():
        transaction.add_done_callback(after_refer)
    else:
        after_refer()
    await relay.stop()
    if goal.live:
        registry.release_resource(goal.call_id, retired_name, value=relay)
    if old_watcher is not None:
        await asyncio.gather(old_watcher, return_exceptions=True)
    return application


async def route_parking_call(runtime, invite, decision, registry, source_endpoint):
    from .inbound_routing.local import _claim_source

    if decision.entry.metadata["parking_mode"] == "directed_pickup":
        from .call_pickup import prepare_sip_pickup
        return await prepare_sip_pickup(runtime.hass, invite, decision.entry.metadata["parking_slot"], runtime.local_ip)
    if decision.entry.metadata["parking_mode"] == "pickup":
        return await prepare_parking_pickup(runtime.hass, invite, decision.entry.metadata["parking_slot"], runtime.local_ip)
    if busy := await _claim_source(hass=runtime.hass, registry=registry, invite=invite,
                                  source_endpoint=source_endpoint, state="ringing", route_kind="parking"):
        return busy
    session = registry.upsert(invite.call_id, state="ringing", owner="router",
                              caller=invite.caller, callee=decision.target, route_kind="parking")
    registry.set_pending_invite(invite.call_id, invite)
    try:
        await start_parking_application(runtime.hass, session, invite, runtime.local_ip,
                                       slot=decision.entry.metadata["parking_slot"])
    except ParkingError:
        registry.request_termination(session.call_id, TerminationIntent("parking_unavailable", response_status=486))
        return SipInviteResult(486, "Busy Here")
    return SipInviteResult(180, "Ringing", defer_final=True)


async def prepare_parking_pickup(hass, invite, slot, local_ip):
    from .core.sdp import build_answer_directional, first_offered_dtmf_format, local_direction_for_remote
    from .dtmf_events import attach_dtmf_event_bridge
    from .endpoint_registry import EndpointBusyError
    from .runtime_data import sip_endpoint_manager

    caller = invite.identified_source_endpoint_id
    if not caller:
        return SipInviteResult(403, "Forbidden")
    lot = get_parking_lot(hass)
    try:
        claim = lot.claim(slot, invite.call_id)
    except ParkingError:
        return SipInviteResult(480, "Temporarily Unavailable")
    registry = call_registry(hass)
    session = claim.parked.session
    application = registry.resource_for(session.call_id, "parking")
    if (application is None or application.media is None or not application.answered
            or caller in session.endpoint_claims or registry.get_session(invite.call_id) is not None):
        claim.rollback()
        return SipInviteResult(486, "Busy Here")
    prepared = None
    acquired = False
    committed = False
    attached = False
    prepared_name = f"parking_pickup:{invite.call_id}"
    async def rollback():
        if committed:
            return
        if prepared is not None:
            await prepared.rollback()
            registry.release_resource(session.call_id, prepared_name, value=prepared)
        if attached:
            registry.remove_leg(session.call_id, invite.call_id)
        if acquired:
            registry.release_endpoint_claim(session.call_id, caller)
        claim.rollback()
    try:
        registry.claim_endpoint(session.call_id, caller, role="source", adopt_transport=True)
        acquired = True
        async def prepare_media():
            video_update = (await application.prepare_video_pickup(invite)
                            if getattr(application.media, "video_relay", None) is not None else None)
            media_invite = invite
            if application.media.invite.video_format is None and application.media.video_relay is None:
                media_invite = replace(invite, video_format=None, local_video_format=None, video_answer_format=None,
                    remote_video_rtp_port=0, remote_video_rtcp_port=0, remote_video_payload_types=())
            return await LocalMediaBridge.prepare(application.media, media_invite,
                **({"video_update": video_update} if video_update is not None else {}))
        prepared = await session.create_task(prepare_media(), name="parking-pickup-media")
        session.add_resource(prepared_name, prepared, lambda _reason: prepared.rollback(), stage=CleanupStage.MEDIA)
        if not claim.current:
            raise ParkingError("The parked call ended")
        answer = build_answer_directional(local_ip, local_ip, prepared.relay.right_port,
            invite.send_format, invite.recv_format, dtmf=first_offered_dtmf_format(invite.remote_sdp), remote_sdp=invite.remote_sdp,
            **({"video_port": prepared.video_update.answer_video_rtp_port,
                "video_format": prepared.video_update.answer_video_format,
                "video_direction": local_direction_for_remote(prepared.video_update.answer_video_format.direction)}
               if prepared.video_update is not None else {}))
    except (ParkingError, EndpointBusyError, ValueError, RuntimeError, OSError):
        await rollback()
        return SipInviteResult(480, "Temporarily Unavailable")
    except BaseException:
        await rollback()
        raise

    async def commit():
        nonlocal committed, attached
        if not claim.current or registry.resource_for(session.call_id, "parking") is not application:
            raise ParkingError("Parking ownership changed before answer")
        prepared.validate()
        manager = sip_endpoint_manager(hass)
        registry.attach_inbound_dialog(session.call_id, invite.call_id, manager,
            endpoint_id=caller, expected_generation=session.generation)
        attached = True
        old_media = application.media
        relay = prepared.commit()
        retired_name = f"retired_local_media:{id(old_media):x}"
        session.add_resource(retired_name, old_media, lambda _reason: old_media.stop(), stage=CleanupStage.MEDIA)
        registry.release_resource(session.call_id, f"relay:{session.call_id}", value=old_media)
        registry.attach_relay(session.call_id, relay)
        if prepared.video_update is not None:
            await async_wait_for_cleanup(asyncio.create_task(prepared.video_update.commit(), name="voip-parking-video-commit"))
        old_media.reservation.detach()
        from .media_ports import RtpPortReservation
        reservation = RtpPortReservation(hass, (relay.left_port, relay.right_port))
        relay._on_release = lambda _ports: reservation.release()
        registry.take_media(session.call_id, provisional=True)
        registry.set_bridge_link(session.call_id, invite.call_id)
        session.update_metadata(dest_endpoint_id=caller, target_endpoint_id=caller, connected_party=invite.caller)
        registry.transition(session.call_id, owner="bridge", state="in_call", callee=invite.caller, route_kind="parking_pickup")
        attach_dtmf_event_bridge(hass, relay, call_id=session.call_id, dest_call_id=invite.call_id,
                                caller=session.caller, callee=invite.caller, source_client=application.source_client)
        if application.source_client is not None:
            from .bridge_media_updates import BridgeMediaUpdateBinder
            BridgeMediaUpdateBinder(hass).attach_audio_side(application.source_client, relay,
                source_call_id=session.call_id, side="left")
        claim.commit()
        committed = True
        registry.release_resource(session.call_id, prepared_name, value=prepared)
        registry.release_resource(session.call_id, f"parking:{session.call_id}", value=application)
        publish_bridge_projection(hass, session, event_type="answered", route_kind="parking_pickup")
        await application.close("picked_up")
        application.media = None
        await old_media.stop()
        registry.release_resource(session.call_id, retired_name, value=old_media)
    return SipInviteResult(200, "OK", answer_sdp=answer, commit=commit, rollback=rollback)


async def pickup_phone_call(call, endpoint, slot):
    """Recover a slot through ordinary routing, answering browser legs locally."""
    from homeassistant.core import ServiceCall
    from .softphone_answer import async_answer_browser_call
    from .softphone_commands import async_resolve_browser_call_command
    from .websocket_api import _release_ha_softphone_claim

    hass = call.hass
    registry = call_registry(hass)
    lot = get_parking_lot(hass)
    claim = lot.claim(slot, f"browser:{call.context.id}:{endpoint.endpoint_id}",
        browser_endpoint_id=endpoint.endpoint_id if endpoint.kind.is_softphone else "")
    session = claim.parked.session
    application = registry.resource_for(session.call_id, "parking")
    if application is None or endpoint.endpoint_id in session.endpoint_claims:
        claim.rollback()
        raise ParkingError("This phone cannot pick up its own parked call")
    try:
        if registry.forward_call is None:
            raise ParkingError("Call forwarding is unavailable")
        resolved = {}
        if endpoint.kind.is_softphone:
            resolved["_resolved_decision"] = RouteDecision(RouteAction.ANSWER_HA,
                target=endpoint.name, entry=RosterEntry(endpoint.name,
                    metadata={"local_ha": True, "endpoint_id": endpoint.endpoint_id}))
        task = await registry.forward_call(call_id=session.call_id, destination=endpoint.name,
            on_failure="resume", **resolved)
        if task is not None:
            try:
                async with asyncio.timeout(float(call.data.get("answer_timeout", 30))):
                    await asyncio.shield(task)
            except BaseException:
                if not task.done():
                    task.cancel()
                await async_wait_for_cleanup(asyncio.gather(task, return_exceptions=True))
                raise
        if not endpoint.kind.is_softphone:
            if not session.live or session.state != "in_call" or session.metadata.get("dest_endpoint_id") != endpoint.endpoint_id:
                raise ParkingError("The selected phone did not answer the pickup")
            if claim.current:
                claim.commit()
            publish_bridge_projection(hass, session, event_type="picked_up", route_kind="parking_pickup")
            return session
        if not claim.current or session.owner == "parking":
            raise ParkingError("The selected phone could not accept the parked call")
        answer = ServiceCall(hass, "voip_stack", "answer", {
            "device_id": endpoint.device_id, "call_id": session.call_id,
            "expected_generation": session.generation,
            "media_client_id": str(call.data.get("media_client_id") or ""),
            **{key: call.data[key] for key in ("send_video", "accept_video") if key in call.data},
        }, context=call.context)
        command = await async_resolve_browser_call_command(hass, answer,
            endpoint_id=endpoint.endpoint_id, endpoint=endpoint)
        await async_answer_browser_call(hass, answer, command)
        if not claim.current:
            raise ParkingError("The parked caller ended before pickup completed")
        claim.commit()
        registry.release_resource(session.call_id, f"parking:{session.call_id}", value=application)
        await application.close("picked_up")
        publish_bridge_projection(hass, session, event_type="picked_up", route_kind="parking_pickup")
        return session
    except BaseException:
        claim.rollback()
        retained_video = (application.media is not None and application.media.video_relay is not None
            and registry.resource_for(session.call_id, "relay") is application.media)
        if session.live and session.owner == "ha_softphone" and (session.state == "ringing" or retained_video):
            _release_ha_softphone_claim(hass, session.call_id, destination=application.contact.display_name,
                                       endpoint_id=endpoint.endpoint_id)
            registry.transition(session.call_id, owner="parking", state="in_call", route_kind="parking",
                                callee=application.contact.display_name)
            session.update_metadata(endpoint_id=str(session.metadata.get("source_endpoint_id") or ""),
                                    dest_endpoint_id="", target_endpoint_id="", ring_endpoint_ids=())
            await application.resume()
        raise


async def pickup_browser_call(call, endpoint, slot):
    return await pickup_phone_call(call, endpoint, slot)


async def async_park_call(call):
    """Park the selected phone's current call through its existing media owner."""
    from homeassistant.exceptions import ServiceValidationError
    runtime = require_runtime_data(call.hass)
    phone = await runtime.phones.resolve_source(call)
    endpoint = runtime.endpoints.get(phone.endpoint_id)
    identity = str(call.data.get("call_id") or (endpoint.active_call_id if endpoint else "") or "")
    session = runtime.sip.get_session(runtime.sip.resolve_session_id(identity))
    from .call_scope import call_belongs_to_endpoint
    if session is None or not call_belongs_to_endpoint(runtime.sip, identity, phone.endpoint_id):
        raise ServiceValidationError("The selected phone does not own this call")
    expected = call.data.get("expected_generation")
    if expected is not None and expected != session.generation:
        raise ServiceValidationError("The selected call generation is no longer current")
    slot = str(call.data.get("slot") or "")
    try:
        if session.owner == "local_bridge":
            application = await park_local_browser_call(call.hass, session, phone.endpoint_id, slot=slot)
        elif phone.kind.is_softphone:
            application = await park_browser_sip_call(call.hass, session.call_id, phone.endpoint_id, slot=slot)
        else:
            application = await park_established_sip_call(call.hass, session.call_id, phone.endpoint_id, slot=slot)
    except (ValueError, RuntimeError) as err:
        raise ServiceValidationError(str(err)) from err
    return {"call_id": application.session.call_id, "slot": application.parked.slot,
            "generation": application.session.generation}
