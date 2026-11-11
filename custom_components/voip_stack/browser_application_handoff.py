"""Prepare an ordinary browser B-leg without retiring application media."""

import asyncio
from dataclasses import replace

from .browser_playback import BrowserPlayback
from .call_projection import publish_phone_projection
from .core.sdp import RtpPcmFormat
from .endpoint_session import CleanupStage
from .local_media_bridge import LocalMediaBridge
from .media_ports import RtpPortReservation
from .runtime_data import require_runtime_data
from .session_cleanup import async_wait_for_cleanup


async def forward_application_to_browser(runtime, registry, session, invite, endpoint, application, *, timeout=30, accepted_decision=None):
    """Commit only after this selected browser answers and confirms playback."""
    call_id, token = session.call_id, session.token
    metadata_keys = ("endpoint_id", "dest_endpoint_id", "target_endpoint_id", "session_device_id", "media_client_id", "ring_endpoint_ids")
    previous_metadata = {key: session.metadata.get(key) for key in metadata_keys}
    media, prepared, reservation = application.media, None, None
    playback = BrowserPlayback()
    playback_key = f"browser_playback:{call_id}"
    previous_playback = registry.resource_for(call_id, "browser_playback")
    media_key = f"softphone_media_endpoint:{endpoint.endpoint_id}:{call_id}"
    descriptor = None
    committed = False
    owner_key = f"{endpoint.endpoint_id}|{call_id}"
    future = asyncio.get_running_loop().create_future()
    if media is None or media.closed.is_set():
        raise RuntimeError("The source application media has ended")
    registry.claim_endpoint(call_id, endpoint.endpoint_id, role="group_candidate")
    session.update_metadata(ring_endpoint_ids=(endpoint.endpoint_id,))
    registry.set_pending_route(call_id, {"future": future, "invite": invite,
        "ring_group_endpoint_ids": (endpoint.endpoint_id,), "declined_endpoint_ids": set()})
    if accepted_decision is None:
        runtime.publish_pending_ringing(invite, route_kind="group", endpoint_id=endpoint.endpoint_id,
            endpoint_device_id=endpoint.device_id, callee=endpoint.name, last_sip_event="ROUTE_FORWARD")
    else:
        future.set_result(accepted_decision)
    try:
        async with asyncio.timeout(timeout):
            decision = await future
            registry.take_pending_route(call_id)
            if decision.get("action") != "answer_ha" or decision.get("endpoint_id") != endpoint.endpoint_id:
                raise RuntimeError("The browser did not answer")
            session.ensure_live(token)
            if registry.resource_for(call_id, "relay") is not media:
                raise RuntimeError("The source media owner changed")
            reservation = RtpPortReservation.allocate(runtime.hass)
            fmt = RtpPcmFormat(96, "L16", 16000, 1, 20)
            browser_invite = replace(invite, send_format=fmt, recv_format=fmt,
                local_send_capabilities=(fmt.audio_format,), local_recv_capabilities=(fmt.audio_format,),
                remote_rtp_host=runtime.local_ip, remote_rtp_port=reservation.ports[0],
                local_audio_direction="sendrecv", remote_audio_connection_held=False,
                video_format=None, local_video_format=None)
            prepared = await LocalMediaBridge.prepare(media, browser_invite, right_port=reservation.ports[1])
            descriptor = {"invite": replace(browser_invite, remote_rtp_port=prepared.relay.right_port),
                "local_rtp_port": reservation.ports[0], "local_video_rtp_port": 0,
                "rtp_reservation": reservation, "endpoint_id": endpoint.endpoint_id,
                "media_client_id": str(decision.get("media_client_id") or "")}
            registry.own_resource(call_id, media_key, descriptor, lambda _reason: reservation.release(),
                                  stage=CleanupStage.RESERVATION, generation=session.generation)
            if previous_playback is not None:
                session.release_resource(playback_key, value=previous_playback)
            registry.own_resource(call_id, playback_key, playback, playback.close, stage=CleanupStage.MEDIA,
                                  generation=session.generation)
            session.update_metadata(endpoint_id=endpoint.endpoint_id, dest_endpoint_id=endpoint.endpoint_id,
                target_endpoint_id=endpoint.endpoint_id, session_device_id=endpoint.device_id,
                media_client_id=descriptor["media_client_id"])
            registry.observe_leg(call_id, f"browser:{endpoint.endpoint_id}", role="destination",
                                 endpoint_id=endpoint.endpoint_id, state="in_call", generation=session.generation)
            publish_phone_projection(runtime.hass, session, endpoint.endpoint_id,
                direction="incoming", peer_name=session.caller, route_kind="answer_ha",
                audio_direction="sendrecv", last_sip_event="BROWSER_MEDIA_PREPARED")
            await playback.wait_ready(fail_on_detach=True)
            session.ensure_live(token)
            owner = require_runtime_data(runtime.hass).media.owners_for("audio").get(owner_key)
            if owner is None or owner.released.is_set():
                raise RuntimeError("The selected browser media connection ended")
            prepared.validate()
            async def close_browser(_reason):
                owner.revoke()
                await owner.released.wait()
            relay = prepared.commit()
            committed = True
            registry.own_resource(call_id, f"browser_transfer_owner:{endpoint.endpoint_id}:{call_id}", owner,
                                  close_browser, stage=CleanupStage.OBSERVER, generation=session.generation)
            # The same bound source transport is now the relay's left leg.
            registry.release_resource(call_id, f"relay:{call_id}", value=media)
            registry.attach_relay(call_id, relay)
            await relay.start()
            session.ensure_live(token)
            registry.transition(call_id, owner="ha_softphone", state="in_call", callee=endpoint.name,
                route_kind="ha_softphone", expected_generation=token.generation)
            registry.take_pending_invite(call_id)
            await application.release_media_for_forward()
            await application.close("forwarded")
            registry.release_resource(call_id, application.resource_name, value=application)
    except BaseException:
        if not committed:
            async def rollback():
                registry.take_pending_route(call_id)
                owner = require_runtime_data(runtime.hass).media.owners_for("audio").get(owner_key)
                if owner is not None:
                    owner.revoke()
                    await owner.released.wait()
                if prepared is not None:
                    await prepared.rollback()
                if reservation is not None:
                    reservation.release()
                if session.live:
                    session.release_resource(media_key, value=descriptor)
                    session.release_resource(playback_key, value=playback)
                    if previous_playback is not None:
                        session.add_resource(playback_key, previous_playback, previous_playback.close, stage=CleanupStage.MEDIA)
                    for key, value in previous_metadata.items():
                        if value is None:
                            session.metadata.pop(key, None)
                        else:
                            session.metadata[key] = value
                    registry.release_endpoint_claim(call_id, endpoint.endpoint_id)
                    registry.observe_leg(call_id, f"browser:{endpoint.endpoint_id}", role="destination",
                        endpoint_id=endpoint.endpoint_id, state="cancelled", generation=session.generation)
                    publish_phone_projection(runtime.hass, session, endpoint.endpoint_id,
                        direction="incoming", peer_name=session.caller, reason="transfer_failed", event_type="state_changed")
            await async_wait_for_cleanup(asyncio.create_task(rollback(), name="voip-browser-transfer-rollback"))
        else:
            await registry.terminate_call_wait(call_id, reason="media_handoff_failed")
        raise
