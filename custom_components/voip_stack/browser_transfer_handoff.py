"""Adopt an answered consultation into the retained browser's existing session."""

import asyncio
from dataclasses import dataclass, replace

from .bridge_manager import async_watch_sip_bridge_destination
from .bridge_media_updates import BridgeMediaUpdateBinder
from .call_projection import publish_bridge_projection
from .core.sip_transfer import SipTransferResult
from .endpoint_session import CleanupStage, SessionHandoff, TerminationIntent
from .dtmf_events import attach_dtmf_event_bridge
from .sip_bridge import local_rtp_peer
from .sip_rtp_bridge import SipRtpRelay
from .softphone_termination import async_terminate_sip_bridge_session
from .session_cleanup import async_wait_for_cleanup


@dataclass(slots=True)
class _ConsultationVideo:
    relay: object
    descriptor: dict
    prepared_dialog: object = None
    commit_generation: object = None
    rollback_generation: object = None
    previous_port: int = 0
    previous_formats: tuple = ()
    previous_direction: str = "inactive"
    adopted: bool = False
    rollback_task: object = None
    registry: object = None
    consultation_id: str = ""

    async def commit(self):
        if self.prepared_dialog is not None and not self.prepared_dialog.commit():
            raise RuntimeError("consultation video dialog changed before handoff")
        if self.commit_generation is not None:
            await self.commit_generation()

    async def rollback(self):
        if self.adopted:
            return
        async def cleanup():
            try:
                if self.rollback_generation is not None:
                    await self.rollback_generation()
                if self.prepared_dialog is not None:
                    restored = await self.prepared_dialog.restore(local_video_rtp_port=self.previous_port,
                        video_formats=self.previous_formats, video_direction=self.previous_direction)
                    if not restored:
                        self.registry.request_termination(self.consultation_id,
                            TerminationIntent("video_transfer_restore_failed"))
                        raise RuntimeError("consultation video restoration failed")
            finally:
                if self.relay is not None:
                    await self.relay.stop()
        if self.rollback_task is None:
            self.rollback_task = asyncio.create_task(cleanup(), name=f"voip-transfer-video-rollback-{self.consultation_id}")
        await async_wait_for_cleanup(self.rollback_task)


async def _prepare_consultation_video(hass, runtime, source, client, relay):
    """Prepare an inactive generation with the existing SIP and video owners."""
    from .bridge_media_updates import prepare_source_video_update
    from .const import CONF_VIDEO_TRANSCODING
    from .local_call_media import ApplicationCallSource
    from .core import sip
    from .sip_bridge import async_prepare_invite_pair_video, dialog_video_rtp_peer

    previous = client.dialog
    remote = ApplicationCallSource.from_client(client, call_id=source.token.call_id,
        caller=sip.parse_sip_uri(previous.remote_uri).user, allow_video=True)
    next_video, source_answer, destination_answer = await async_prepare_invite_pair_video(
        hass, source.invite, remote, left_peer=source.video_rtp_peer(),
        enable_transcoding=bool(runtime.transport_config.get(CONF_VIDEO_TRANSCODING, False)))
    if source.invite.video_format is not None and remote.video_format is not None and next_video is None:
        raise ValueError("browser and consultation video have no common contract")
    candidate = _ConsultationVideo(next_video, {}, previous_port=client.local_video_rtp_port,
        previous_formats=tuple(dict.fromkeys(fmt for fmt in (previous.recv_video_format, previous.send_video_format) if fmt is not None)),
        previous_direction=client.video_direction, registry=runtime.sip,
        consultation_id=runtime.sip.resolve_session_id(client.dialog_ids.call_id))
    try:
        candidate.descriptor = source.prepare_bridge_media(relay, source_answer, video_relay=next_video)
        if previous.video_format is not None:
            # This staged handoff owns the candidate media generation. The
            # predecessor binder cannot publish it into the old consultation.
            handler = client.on_media_update
            client.on_media_update = None
            try:
                candidate.prepared_dialog = await prepare_source_video_update(hass, remote.call_id,
                    source_client=client, local_video_rtp_port=next_video.right_port if next_video else 0,
                    video_formats=candidate.previous_formats if next_video else (),
                    video_direction=destination_answer.direction if destination_answer else "inactive")
            finally:
                if client.on_media_update is None:
                    client.on_media_update = handler
            if candidate.prepared_dialog is None:
                raise RuntimeError("consultation rejected the video handoff")
        if next_video is not None:
            active_left = replace(next_video.left)
            active_right = dialog_video_rtp_peer(candidate.prepared_dialog.candidate)
            next_video.left = replace(active_left, video_format=replace(active_left.video_format, direction="inactive"))
            next_video.right = replace(active_right, video_format=replace(active_right.video_format, direction="inactive"))
            await next_video.start()
            next_video.stage_peer_reconfiguration("left", active_left)
            next_video.stage_peer_reconfiguration("right", active_right)
            candidate.commit_generation, candidate.rollback_generation = await next_video.async_prepare_peer_generation(
                left=active_left, right=active_right)
        return candidate
    except BaseException:
        await candidate.rollback()
        raise


async def adopt_consultation(hass, runtime, source, grant):
    calls, consultation = runtime.sip, grant.consultation
    original = source.validate()
    target = str(consultation.metadata.get("dest_endpoint_id") or consultation.metadata.get("endpoint_id") or "")
    endpoint = runtime.endpoints.get(target)
    if (not grant.current() or not consultation.live or consultation.state not in {"in_call", "held"}
            or original is consultation or endpoint is None or target not in consultation.endpoint_claims
            or grant.endpoint_id != source.call.caller_endpoint_id):
        return SipTransferResult(False, 481, "replacement_not_found")
    descriptor = calls.resource_for(consultation.call_id, "softphone_media")
    if endpoint.supports_ha_client and descriptor is not None and descriptor.get("endpoint_id") == target:
        with SessionHandoff(calls, original, consultation):
            calls.transfer_endpoint_claim(consultation.call_id, original.call_id, target)
            source.connect_answered_browser(target, str(descriptor.get("media_client_id") or ""))
        owner = runtime.media.owners_for("audio").get(f"{target}|{consultation.call_id}")
        if owner is not None:
            owner.revoke()
    else:
        destination_id = calls.bridge_link_for(consultation.call_id)
        client = calls.sip_client_for(destination_id)
        relay = calls.resource_for(consultation.call_id, "relay")
        if client is None or client.dialog is None or not isinstance(relay, SipRtpRelay):
            return SipTransferResult(False, 488, "local_transfer_unavailable")
        video = await _prepare_consultation_video(hass, runtime, source, client, relay)
        previous_video = relay.video_relay
        ready = asyncio.Event()
        async def watch():
            await ready.wait()
            await async_watch_sip_bridge_destination(hass, client=client, source_call_id=original.call_id,
                terminate_sip_bridge=async_terminate_sip_bridge_session)
        watcher = asyncio.create_task(watch(), name=f"voip-bridge-destination-{destination_id}")
        try:
            async with relay.media_reconfiguration_guard():
                source.validate()
                if not grant.current() or not consultation.live:
                    raise RuntimeError("consultation changed before transfer")
                peer = local_rtp_peer(source.invite.source_host, source.invite.recv_format, source.invite.send_format)
                reconfigure = relay.prepare_peer_reconfiguration("left", peer)
                with SessionHandoff(calls, original, consultation) as handoff:
                    source.preserve_handoff(handoff)
                    handoff.preserve(video, "adopted")
                    handoff.preserve(client, "on_refer", "on_media_update", "on_connected_identity", "on_info_dtmf", "on_info_video")
                    handoff.preserve(relay, "on_dtmf", "on_info_dtmf", "on_dtmf_fallback", "on_media_route_changed", "video_relay")
                    source.commit()
                    relay.video_relay = video.relay
                    if previous_video is not None:
                        original.add_resource(f"retired_transfer_video:{id(previous_video):x}", previous_video,
                            lambda _reason: previous_video.stop(), stage=CleanupStage.MEDIA)
                    calls.remove_leg(consultation.call_id, destination_id)
                    calls.forget_bridge_link(consultation.call_id)
                    calls.release_resource(consultation.call_id, f"relay:{consultation.call_id}", value=relay)
                    client.on_refer = None
                    calls.register_bridge(source_call_id=original.call_id, dest_call_id=destination_id,
                        client=client, lifecycle_task=watcher, state="in_call", caller=original.caller,
                        callee=consultation.callee, route_kind=consultation.route_kind,
                        expected_generation=original.generation)
                    calls.attach_relay(original.call_id, relay)
                    calls.transfer_endpoint_claim(consultation.call_id, original.call_id, target)
                    original.update_metadata(dest_endpoint_id=target, target_endpoint_id=target,
                        connected_party=consultation.callee,
                        outbound_trunk_id=consultation.metadata.get("outbound_trunk_id", ""),
                        policy_endpoint_ids=tuple(sorted(set((*original.endpoint_claims,
                            *consultation.endpoint_claims, *(original.metadata.get("policy_endpoint_ids") or ()),
                            *(consultation.metadata.get("policy_endpoint_ids") or ()))))))
                    BridgeMediaUpdateBinder(hass).attach(client, relay, source_call_id=original.call_id,
                        source_video_send_format=source.invite.send_video_format,
                        source_video_receive_format=source.invite.recv_video_format)
                    attach_dtmf_event_bridge(hass, relay, call_id=original.call_id, dest_call_id=destination_id,
                        caller=original.caller, callee=consultation.callee, client=client)
                    calls.attach_media(original.call_id, video.descriptor)
                    reconfigure()
                    video.adopted = True
            try:
                await video.commit()
            except BaseException:
                calls.request_termination(original.call_id, TerminationIntent("video_handoff_failed"),
                                          generation=original.generation)
                raise
            if previous_video is not None:
                await previous_video.stop()
                calls.release_resource(original.call_id, f"retired_transfer_video:{id(previous_video):x}", value=previous_video)
        except BaseException:
            if video.adopted and original.live:
                calls.request_termination(original.call_id, TerminationIntent("media_handoff_failed"), generation=original.generation)
            await video.rollback()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            raise
        calls.cancel_task(consultation.call_id, f"client_watcher:{destination_id}")
        ready.set()
        source.connected(consultation.callee, consultation.route_kind)
        publish_bridge_projection(hass, original, event_type="transferred", last_sip_event="REFER")
    async def retire():
        await source.close_referrer()
        barrier = calls.request_termination(consultation.call_id, TerminationIntent("forwarded"),
                                            generation=consultation.generation)
        if barrier is not None:
            await barrier
    source.after_refer(retire)
    return SipTransferResult(True, 200, "completed", retained_call=True)
