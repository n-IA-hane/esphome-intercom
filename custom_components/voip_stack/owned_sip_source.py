"""Acquire a card's real SIP source for the existing forwarding/media owners."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from .core import rtp
from .endpoint_session import CleanupStage, TerminationIntent
from .local_call_media import ApplicationCallSource, LocalCallMedia
from .media_ports import RtpPortReservation
from .runtime_data import require_runtime_data


@dataclass(slots=True)
class _HeldVideo:
    relay: object
    source: object
    update: object
    original_port: int


@dataclass(slots=True)
class OwnedSipSource:
    hass: object
    session: object
    endpoint_id: str
    invite: object
    client: object
    server: object
    descriptor: object
    active: object
    dialog: object
    reservation: object
    local_port: int
    sender: object
    generation: int
    adopted: bool = False
    successor: object = None
    previous_watcher: object = None
    peer_endpoint_id: str = ""
    held_video: _HeldVideo | None = None

    @classmethod
    def capture(cls, hass, session, endpoint_id=""):
        """Capture references only; keep the WS, descriptor and claims untouched."""
        if session is None or not session.live:
            return None
        runtime = require_runtime_data(hass)
        registry = runtime.sip
        if endpoint_id:
            endpoint = runtime.endpoints.get(endpoint_id)
            if endpoint is None or not endpoint.supports_ha_client:
                return None
        existing = registry.resource_for(session.call_id, "owned_sip_source")
        if isinstance(existing, cls) and not existing.adopted:
            if endpoint_id and endpoint_id != existing.endpoint_id:
                raise RuntimeError("The card no longer owns this SIP call")
            existing.validate()
            return existing
        if session.owner != "ha_softphone" or session.state != "in_call":
            return None
        if not endpoint_id:
            candidates = [key for key in session.endpoint_claims
                if (endpoint := runtime.endpoints.get(key)) is not None and endpoint.supports_ha_client]
            if not candidates:
                return None
            if len(candidates) > 1:
                raise RuntimeError("The retained SIP call has no unique card owner")
            endpoint_id = candidates[0]
        if endpoint_id not in session.endpoint_claims:
            raise RuntimeError("The card no longer owns this SIP call")
        from .call_transfer import _inbound_server

        client = registry.source_sip_client_for(session.call_id) or registry.sip_client_for(session.call_id)
        server, _ = _inbound_server(runtime, session.call_id)
        descriptor = registry.resource_for(session.call_id, "softphone_media")
        active = runtime.media.sessions_for("audio").get((session.call_id, endpoint_id))
        ws_owner = runtime.media.owners_for("audio").get(f"{endpoint_id}|{session.call_id}")
        if active is None and ws_owner is not None and not ws_owner.released.is_set():
            raise RuntimeError("The card's RTP owner is still acquiring its socket")
        if active is not None and active.handoff_transport is None:
            raise RuntimeError("The card's RTP owner is not ready for handoff")
        dialog = client.dialog if client is not None else None
        retained_id = next((key for key in session.endpoint_claims if key != endpoint_id), "")
        retained_endpoint = runtime.endpoints.get(retained_id) if retained_id else None
        caller = (retained_endpoint.name if retained_endpoint is not None else session.callee
                  if session.metadata.get("source_endpoint_id") == endpoint_id else session.caller)
        source = (ApplicationCallSource.from_client(client, call_id=session.call_id,
            caller=caller, trunk_id=str(session.metadata.get("inbound_trunk_id") or ""), allow_video=True)
            if dialog is not None else server.invite_for_call(session.call_id) if server is not None else None)
        if source is None:
            raise RuntimeError("The retained SIP dialog ended before transfer")
        if client is None:
            dialog = source
        if client is None and (descriptor or {}).get("local_video_rtp_port") == 0:
            source = replace(source, video_format=None, local_video_format=None, video_answer_format=None,
                remote_video_rtp_port=0, remote_video_rtcp_port=0, remote_video_payload_types=())
        reservation = (descriptor or {}).get("rtp_reservation") or getattr(client, "media_reservation", None)
        port = int(active.local_rtp_port if active is not None else dialog.local_rtp_port if client is not None
                   else (descriptor or {}).get("local_rtp_port") or 0)
        if not port:
            raise RuntimeError("The retained SIP call has no owned RTP port")
        sender = (active.rtp_source if active is not None else getattr(client, "audio_rtp_source", None)
                  or (descriptor or {}).get("audio_rtp_source"))
        result = cls(hass, session, endpoint_id, source, client, server, descriptor, active, dialog,
                     reservation, port, sender or rtp.AudioRtpSenderState.create(), session.generation)
        result.peer_endpoint_id = retained_id
        registry.own_resource(session.call_id, f"owned_sip_source:{session.call_id}", result,
            result.close, stage=CleanupStage.MEDIA, generation=session.generation)
        return result

    def validate(self):
        runtime = require_runtime_data(self.hass)
        registry = runtime.sip
        if (self.adopted or not registry.is_generation_current(self.session.call_id, self.generation)
                or self.endpoint_id not in self.session.endpoint_claims
                or registry.resource_for(self.session.call_id, "softphone_media") is not self.descriptor
                or runtime.media.sessions_for("audio").get((self.session.call_id, self.endpoint_id)) is not self.active
                or (self.client is not None and self.client.dialog is not self.dialog)
                or (self.client is None and self.server.invite_for_call(self.session.call_id) is not self.dialog)):
            raise RuntimeError("The retained SIP source changed during destination preparation")
        if self.active is not None:
            self.active.validate_handoff()
        else:
            owner = runtime.media.owners_for("audio").get(f"{self.endpoint_id}|{self.session.call_id}")
            if owner is not None and not owner.released.is_set():
                raise RuntimeError("A card media connection started while preparing the transfer")

    def _commit_resources(self, staged, destination_port, *, preserve_staged=False):
        registry = require_runtime_data(self.hass).sip
        if self.reservation is None:
            self.reservation = (RtpPortReservation.adopt_bound_source(self.hass, self.local_port)
                if preserve_staged else RtpPortReservation.adopt_bound_source(self.hass, self.local_port,
                    reservation=staged, retained_port=destination_port))
        elif staged is not self.reservation and not preserve_staged:
            previous = self.reservation
            self.reservation = RtpPortReservation.merge(self.hass,
                (previous.ports, staged.ports), (self.local_port, destination_port))
            previous.detach()
            staged.detach()
        if self.client is not None and getattr(self.client, "media_reservation", None) is not None:
            self.client.media_reservation = None
        registry.take_media(self.session.call_id)
        registry.set_pending_invite(self.session.call_id, self.invite)
        registry.attach_media(self.session.call_id, {"rtp_reservation": self.reservation,
            "local_rtp_port": self.local_port, "final_response_sent": True,
            "audio_rtp_source": self.sender}, provisional=True)
        from .browser_phone_runtime import _release_ha_softphone_claim

        _release_ha_softphone_claim(self.hass, self.session.call_id,
            endpoint_id=self.endpoint_id, destination=self.session.callee)
        registry.release_endpoint_claim(self.session.call_id, self.endpoint_id)
        self.session.update_metadata(source_endpoint_id=self.peer_endpoint_id,
            endpoint_id=self.peer_endpoint_id, media_client_id="", source_media_client_id="")
        self.session.caller = self.invite.caller
        if self.client is not None:
            from .bridge_manager import async_watch_owned_sip_dialog

            wire_id = self.client.dialog_ids.call_id
            self.previous_watcher = registry.cancel_task(self.session.call_id, f"client_watcher:{wire_id}")
            registry.add_leg(self.session.call_id, wire_id, role="caller", state="in_call")
            watcher = self.hass.async_create_task(async_watch_owned_sip_dialog(
                registry, self.session.call_id, self.client, self.generation))
            registry.attach_client_watcher(wire_id, watcher)

    async def prepare_video_hold(self):
        """Reuse parking's held video child without creating a parking operation."""
        if self.held_video is not None or self.invite.video_format is None:
            return self.held_video
        from .bridge_media_updates import prepare_source_video_update
        from .config import transport_config
        from .const import CONF_VIDEO_TRANSCODING
        from .core.sdp import local_direction_for_remote
        from .media_ports import reserve_sip_video_relay_media
        from .parking import _video_peer_direction
        from .sip_bridge import invite_video_rtp_peer, dialog_video_rtp_peer
        from .sip_video_relay import SipVideoRtpRelay

        self.validate()
        runtime = require_runtime_data(self.hass)
        video = runtime.media.sessions_for("video").get((self.session.call_id, self.endpoint_id))
        original_port = int(video.local_rtp_port if video is not None else
            self.client.dialog.local_video_rtp_port if self.client is not None else
            (self.descriptor or {}).get("local_video_rtp_port") or 0)
        if not original_port:
            raise RuntimeError("The existing video contract has no owned RTP port")
        source = invite_video_rtp_peer(self.invite)
        if self.client is None:
            source = _video_peer_direction(source,
                local_direction_for_remote(str((self.descriptor or {}).get("video_direction") or "inactive")))
        ports, sockets = reserve_sip_video_relay_media(self.hass)
        inactive = _video_peer_direction(source, "inactive")
        relay = SipVideoRtpRelay(left=inactive, right=replace(inactive, port=0, rtcp_port=0),
            left_port=ports.ports[0], right_port=ports.ports[1], left_socket=sockets[0],
            left_rtcp_socket=sockets[1], right_socket=sockets[2], right_rtcp_socket=sockets[3],
            on_release=lambda _ports: ports.release())
        if transport_config(self.hass).get(CONF_VIDEO_TRANSCODING, False):
            relay.configure_transcoding(self.hass, self.session.call_id)
        update = None
        try:
            await relay.start()
            update = await prepare_source_video_update(self.hass, self.session.call_id,
                source_client=self.client, local_video_rtp_port=relay.left_port,
                video_formats=tuple(dict.fromkeys((source.recv_format, source.send_format))), video_direction="inactive")
            self.validate()
            if update is None or not update.commit():
                raise RuntimeError("The retained SIP source rejected video hold")
            self.held_video = _HeldVideo(relay, source, update, original_port)
            self.dialog = self.client.dialog if self.client is not None else self.server.invite_for_call(self.session.call_id)
            self.invite = (ApplicationCallSource.from_client(self.client, call_id=self.session.call_id,
                caller=self.invite.caller, trunk_id=str(self.session.metadata.get("inbound_trunk_id") or ""),
                allow_video=True) if self.client is not None else self.dialog)
            relay.left = dialog_video_rtp_peer(update.candidate) if self.client is not None else invite_video_rtp_peer(update.candidate)
            return self.held_video
        except BaseException:
            try:
                if update is not None:
                    restored = await update.restore(local_video_rtp_port=original_port,
                        video_formats=tuple(dict.fromkeys((source.recv_format, source.send_format))))
                    if not restored:
                        runtime.sip.request_termination(self.session.call_id,
                            TerminationIntent("source_video_restore_failed"), generation=self.generation)
            finally:
                await relay.stop()
            raise

    async def rollback_video(self):
        hold, self.held_video = self.held_video, None
        if hold is None or self.adopted:
            return
        try:
            restored = await hold.update.restore(local_video_rtp_port=hold.original_port,
                video_formats=tuple(dict.fromkeys((hold.source.recv_format, hold.source.send_format))))
            if not restored:
                require_runtime_data(self.hass).sip.request_termination(self.session.call_id,
                    TerminationIntent("source_video_restore_failed"), generation=self.generation)
            else:
                self.dialog = (self.client.dialog if self.client is not None
                    else self.server.invite_for_call(self.session.call_id))
                self.invite = (ApplicationCallSource.from_client(self.client, call_id=self.session.call_id,
                    caller=self.invite.caller, trunk_id=str(self.session.metadata.get("inbound_trunk_id") or ""),
                    allow_video=True) if self.client is not None else self.dialog)
        finally:
            await hold.relay.stop()

    async def _retire_card_video(self):
        from .media_reservation import release_video_media_reservation

        runtime = require_runtime_data(self.hass)
        owner = runtime.media.owners_for("video").get(f"{self.endpoint_id}|{self.session.call_id}")
        if owner is not None:
            owner.revoke()
            async with asyncio.timeout(5):
                await owner.released.wait()
        release_video_media_reservation(self.descriptor)
        if self.client is not None:
            for name in ("video_rtp_socket", "video_rtcp_socket"):
                sock = getattr(self.client, name, None)
                if sock is not None:
                    setattr(self.client, name, None)
                    sock.close()

    async def _adopt(self, successor, staged, destination_port, install, *, preserve_staged=False):
        # This is the sole adapter to the WS owner's existing identity lock.
        # It prevents a late card connection from acquiring the same UDP fd
        # while the no-WS source is prepared and committed.
        from .websocket_owner import _media_identity_guard

        runtime = require_runtime_data(self.hass)
        sock = None
        state = self.active.audio_state if self.active is not None else None
        try:
            async with _media_identity_guard(runtime.media, f"{self.endpoint_id}|{self.session.call_id}"):
                self.validate()
                self.successor = successor
                original = None
                if self.active is None:
                    original = getattr(self.client, "audio_rtp_socket", None) if self.client is not None else None
                    if original is None and self.reservation is not None:
                        original = self.reservation.audio_socket(self.local_port)
                    sock = original.dup() if original is not None else None
                    await install(None, None, sock)
                    sock = None
                else:
                    self.active.handoff_transport(install)
                # Installation succeeded. From here only the successor closes
                # media; before here the old descriptor and claims are intact.
                self.adopted = True
                if original is not None:
                    if self.client is not None and getattr(self.client, "audio_rtp_socket", None) is original:
                        self.client.audio_rtp_socket = None
                    if self.reservation is not None and self.reservation.audio_socket(self.local_port) is original:
                        self.reservation.take_audio_socket(self.local_port)
                    original.close()
                self._commit_resources(staged, destination_port, preserve_staged=preserve_staged)
            if self.previous_watcher is not None:
                await asyncio.gather(self.previous_watcher, return_exceptions=True)
            await self._retire_card_video()
        except BaseException:
            if sock is not None and sock.fileno() >= 0:
                sock.close()
            await successor.stop()
            if state is not None and state.transferred:
                self.adopted = True
                if state.transport is not None and not state.transport.is_closing():
                    state.transport.close()
            if self.adopted:
                runtime.sip.request_termination(self.session.call_id,
                    TerminationIntent("source_handoff_failed"), generation=self.generation)
            raise

    async def adopt_application(self, on_complete, *, video_relay=None, staged=None):
        """Acquire only after the real application has accepted the destination."""
        hold = await self.prepare_video_hold() if video_relay is None else None
        staged = staged or self.reservation or RtpPortReservation.allocate(self.hass)
        media = LocalCallMedia(self.hass, invite=self.invite, local_rtp_port=self.local_port,
            reservation=staged, on_complete=on_complete, rtp_source=self.sender)
        media.video_relay = video_relay or (hold.relay if hold is not None else None)
        def install(transport, state, sock=None):
            if state is not None:
                media.adopt_audio_state(state)
                media.adopt_transport(transport)
                for data, addr in state.pending_packets:
                    media.handle_rtp(data, addr)
                return None
            media._rtp_socket = sock
            return media.start()
        try:
            await self._adopt(media, staged, staged.ports[1], install)
        except BaseException:
            await self.rollback_video()
            raise
        media.reservation = self.reservation
        media.release_reservation_on_stop = False
        require_runtime_data(self.hass).sip.attach_relay(self.session.call_id, media)
        self.release_to_session(media)
        return media

    async def adopt_relay(self, relay, staged, *, preserve_staged=False):
        """Commit a staged relay without rebinding an attached WS transport."""
        from .sip_rtp_bridge import _RelayProtocol

        def install(transport, state, sock=None):
            if state is None:
                async def bind():
                    relay.left_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                        lambda: _RelayProtocol(relay, "left"), **({"sock": sock} if sock is not None
                            else {"local_addr": ("0.0.0.0", self.local_port)}))
                return bind()
            protocol = _RelayProtocol(relay, "left")
            protocol.connection_made(transport)
            transport.set_protocol(protocol)
            relay.left_transport = transport
            pending = relay.adopt_audio_state("left", state)
            if pending:
                relay._receive_deferred_pcm("left", relay.left_decoder, pending)
            for data, addr in state.pending_packets:
                relay.handle_packet("left", data, addr)
        await self._adopt(relay, staged, relay.right_port, install, preserve_staged=preserve_staged)
        if self.held_video is not None:
            relay.attach_video_relay(self.held_video.relay)
        if self.active is None:
            relay.left.sequence, relay.left.timestamp, relay.left.ssrc = (
                self.sender.sequence, self.sender.timestamp, self.sender.ssrc)
            relay.left.clock_rate = self.sender.clock_rate
        return relay

    def release_to_session(self, successor):
        """Retire this closer only after the session owns the exact successor."""
        registry = require_runtime_data(self.hass).sip
        if (not self.adopted or self.successor is not successor
                or registry.resource_for(self.session.call_id, "relay") is not successor):
            raise RuntimeError("The SIP source successor has not been adopted by its session")
        released = registry.release_resource(self.session.call_id,
            f"owned_sip_source:{self.session.call_id}", value=self, generation=self.generation)
        self.successor = None
        if not released:
            raise RuntimeError("The SIP source capability changed before ownership release")

    async def close(self, _reason):
        if self.adopted:
            if self.successor is not None:
                await self.successor.stop()
            return
        elif self.held_video is not None:
            if self.session.live:
                await self.rollback_video()
            else:
                await self.held_video.relay.stop()
