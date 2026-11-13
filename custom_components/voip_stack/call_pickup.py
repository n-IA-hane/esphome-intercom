"""Directed pickup joins an identified phone to an existing ringing call."""

import asyncio
from dataclasses import dataclass, replace

from homeassistant.exceptions import ServiceValidationError

from .endpoint_session import CallToken, CleanupStage
from .call_scope import call_belongs_to_endpoint, has_ringing_destination
from .call_state import CallPrecondition
from .runtime_data import require_runtime_data, sip_endpoint_manager
from .sip_listener import SipInviteResult


@dataclass(slots=True)
class PickupClaim:
    endpoint_id: str
    claimed: bool = False
    token: CallToken | None = None

    def authorizes_browser_answer(self, session, endpoint_id):
        """One session-owned pickup authorizes only its selected phone."""
        return (self.token is not None and session.owns(self.token)
                and self.endpoint_id == endpoint_id)


@dataclass(slots=True)
class RetiredForward:
    endpoint_id: str
    task: object


def ringing_target(hass, target, *, call_id="", generation=None, leg_id=""):
    """Resolve one eligible call, never the phone's latest selected call."""
    runtime = require_runtime_data(hass)
    endpoint = runtime.endpoints.by_device_id(target) or runtime.endpoints.resolve(target)
    candidates = []
    if endpoint is not None:
        for session in tuple(runtime.sip.sessions.values()):
            if call_id and session.call_id != runtime.sip.resolve_session_id(call_id):
                continue
            if generation is not None and session.generation != generation:
                continue
            if (not session.live or session.owner not in {"bridge", "ha_softphone", "local_bridge", "router"}
                    or endpoint.endpoint_id == session.metadata.get("source_endpoint_id")
                    or (endpoint.endpoint_id not in session.endpoint_claims
                        and not any(item.endpoint_id == endpoint.endpoint_id
                                    and item.state in {"ringing", "remote_ringing"} for item in session.legs.values()))
                    or not has_ringing_destination(session, endpoint.endpoint_id)):
                continue
            legs = [item for item in session.legs.values() if item.endpoint_id == endpoint.endpoint_id]
            if leg_id and not any(item.leg_id == leg_id and item.state in {"ringing", "remote_ringing"} for item in legs):
                continue
            if legs and not any(item.state in {"ringing", "remote_ringing"} for item in legs):
                continue
            candidates.append(session)
    if len(candidates) != 1:
        raise ServiceValidationError("Choose one ringing call for pickup" if candidates
                                     else "This phone has no ringing call available for pickup")
    return runtime, endpoint, candidates[0]


async def pickup_browser(call, endpoint, target):
    from homeassistant.core import ServiceCall
    from .softphone_answer import async_answer_browser_call
    from .softphone_commands import async_resolve_browser_call_command
    from .roster import RosterEntry
    from .router import RouteAction, RouteDecision

    runtime, target_endpoint, session = ringing_target(call.hass, target,
        call_id=str(call.data.get("call_id") or ""), generation=call.data.get("expected_generation"),
        leg_id=str(call.data.get("leg_id") or ""))
    if endpoint.endpoint_id in session.endpoint_claims or endpoint.active_call_id:
        raise ServiceValidationError("Choose an idle phone to pick up another phone")
    if runtime.sip.resource_for(session.call_id, "fork_answer") is not None:
        return await _pickup_group_phone(call, endpoint, session)
    if runtime.sip.forward_call is None:
        raise ServiceValidationError("Call forwarding is unavailable")
    if runtime.sip.resource_for(session.call_id, "pickup") is not None:
        raise ServiceValidationError("This call is already being picked up")
    claim = PickupClaim(endpoint.endpoint_id, token=session.token)
    resource_name = f"pickup:{session.call_id}"
    runtime.sip.own_resource(session.call_id, resource_name, claim, lambda _reason: None,
        stage=CleanupStage.OBSERVER, generation=claim.token.generation)
    try:
        task = await runtime.sip.forward_call(
            call_id=session.call_id,
            destination=endpoint.name,
            on_failure="resume",
            precondition=CallPrecondition.capture(session),
            _resolved_decision=RouteDecision(RouteAction.ANSWER_HA, target=endpoint.name,
                entry=RosterEntry(endpoint.name,
                    metadata={"local_ha": True, "endpoint_id": endpoint.endpoint_id})),
        )
        await task
        if (not claim.authorizes_browser_answer(session, endpoint.endpoint_id)
                or runtime.sip.resource_for(session.call_id, "pickup") is not claim
                or session.owner not in {"ha_softphone", "local_bridge"}
                or not call_belongs_to_endpoint(runtime.sip, session.call_id, endpoint.endpoint_id)):
            raise ServiceValidationError("The ringing call changed before pickup")
        claim.claimed = True
        answer = ServiceCall(
            call.hass,
            "voip_stack",
            "answer",
            {
                "device_id": endpoint.device_id,
                "call_id": session.call_id,
                "expected_generation": claim.token.generation,
                "media_client_id": str(call.data.get("media_client_id") or ""),
            },
            context=call.context,
        )
        command = await async_resolve_browser_call_command(
            call.hass, answer, endpoint_id=endpoint.endpoint_id, endpoint=endpoint
        )
        await async_answer_browser_call(call.hass, answer, command)
        from .call_projection import publish_bridge_projection
        publish_bridge_projection(call.hass, session, event_type="picked_up", route_kind="pickup")
        return session
    finally:
        if session.live:
            runtime.sip.release_resource(session.call_id, resource_name, value=claim,
                                        generation=claim.token.generation)


async def prepare_sip_pickup(hass, incoming, target, local_ip, *, call_id="", generation=None, leg_id=""):
    from .core.sdp import build_answer_directional, first_offered_dtmf_format
    from .session_cleanup import async_wait_for_cleanup
    from .core.rtp import AudioRtpSenderState
    from .call_projection import publish_bridge_projection
    from .dtmf_events import attach_dtmf_event_bridge
    from .inbound_answer import async_commit_runtime_answer
    from .local_softphone_runtime import PendingLocalSource, local_softphone_bridge
    from .media_ports import RtpPortReservation
    from .pending_sip_destination import PendingSipDestination
    from .sip_bridge import invite_rtp_peer, local_rtp_peer
    from .local_media_bridge import LocalMediaBridge, application_rtp_peer
    from .sip_rtp_bridge import SipRtpRelay, _RelayProtocol
    from .sip_runtime import send_final_response
    from .websocket_api import _release_ha_softphone_claim

    picker = incoming.identified_source_endpoint_id
    if not picker:
        return SipInviteResult(403, "Forbidden")
    try:
        runtime, target_endpoint, session = ringing_target(hass, target, call_id=call_id, generation=generation, leg_id=leg_id)
    except ServiceValidationError:
        return SipInviteResult(480, "Temporarily Unavailable")
    registry = runtime.sip
    if (
        picker in session.endpoint_claims
        or registry.resource_for(session.call_id, "pickup") is not None
    ):
        return SipInviteResult(486, "Busy Here")
    source = (session.artifacts.pending_invite or session.artifacts.caller_invite
              or (session.artifacts.pending_route or {}).get("invite"))
    try:
        native = PendingSipDestination.capture(hass, registry, session)
    except ServiceValidationError:
        return SipInviteResult(480, "Temporarily Unavailable")
    local_source = registry.resource_for(session.call_id, "browser_source")
    application = registry.resource_for(
        session.call_id, "automation"
    ) or registry.resource_for(session.call_id, "parking")
    if native is not None:
        source = native.invite
    if session.owner == "local_bridge":
        bridge = local_softphone_bridge(hass)
        local_source = PendingLocalSource(
            hass,
            bridge.get_call(session.call_id),
            local_ip,
            int(runtime.transport_config["sip_port"]),
            video_peer_format=incoming.send_video_format,
        )
        source = local_source.invite
    elif (
        source is None
        and session.owner == "ha_softphone"
        and session.state == "remote_ringing"
    ):
        local_source = PendingLocalSource(
            hass,
            None,
            local_ip,
            int(runtime.transport_config["sip_port"]),
            call_id=session.call_id,
        )
        source = local_source.invite
    if source is None:
        return SipInviteResult(488, "Not Acceptable Here")
    fork_gate = registry.resource_for(session.call_id, "fork_answer")
    fork_answer = None
    claim = PickupClaim(picker, token=session.token)
    resource_name = f"pickup:{session.call_id}"
    reservation = None
    relay = None
    media_handoff = None
    source_video_answer = picker_video_answer = None
    local_media_descriptor = None
    local_media_adopted = False
    acquired = False
    committed = False
    rollback_task = None
    changed = False
    expected_owner = session.owner
    expected_state = session.state
    preanswered = registry.resource_for(session.call_id, "preanswered") or {}
    from .local_call_media import ApplicationCallSource

    source_answered = bool(
        isinstance(source, ApplicationCallSource)
        or preanswered.get("final_response_sent")
        or source.initial_response_sent
    )
    if source_answered and source.video_format is not None:
        return SipInviteResult(488, "Not Acceptable Here")
    source_ports = (
        preanswered.get("rtp_reservation")
        if source_answered and local_source is None
        else None
    )
    if source_answered and local_source is None and source_ports is None:
        source_ports = next(
            (
                resource.value
                for resource in session.resources
                if resource.name.startswith("pending_bridge_media:")
            ),
            None,
        )
        if source_ports is None:
            return SipInviteResult(488, "Not Acceptable Here")
    previous_forward = (
        registry.task_for(session.call_id, "forward")
        if session.owner == "router" and fork_gate is None
        else None
    )
    previous_destination = registry.bridge_link_for(session.call_id)

    def validate():
        if (
            not session.live
            or session.owner != expected_owner
            or session.state != expected_state
            or registry.resource_for(session.call_id, "pickup") is not claim
        ):
            raise ServiceValidationError("The call was answered or its route changed")
        if fork_answer is not None and (
            fork_gate.closed or fork_gate.current is not fork_answer
            or (fork_answer.committed.done() and not fork_answer.accepted)
        ):
            raise ServiceValidationError("The group withdrew this pickup attempt")
        if native is not None:
            native.validate()
        previous_client = (
            registry.sip_client_for(previous_destination)
            if previous_destination
            else None
        )
        if previous_client is not None and previous_client.dialog is not None:
            raise ServiceValidationError("The original phone already answered")
        if (
            local_source is not None
            and local_source.outgoing
            and not local_source.retired
            and local_source.previous_client.dialog is not None
        ):
            raise ServiceValidationError("The original phone already answered")

    async def cleanup():
        if fork_answer is not None:
            fork_answer.finish(False)
        try:
            if relay is not None:
                await relay.stop()
        finally:
            if local_media_descriptor is not None and not local_media_adopted:
                from .media_reservation import release_video_media_reservation
                release_video_media_reservation(local_media_descriptor)
            if reservation is not None:
                reservation.release()
            if session.live:
                if local_source is not None:
                    local_source.preserve_answered_local_claims()
                registry.release_resource(session.call_id, resource_name, value=claim)
                if acquired:
                    registry.release_endpoint_claim(session.call_id, picker)
                if changed:
                    await registry.terminate_call_wait(session.call_id, reason="pickup_failed")

    async def rollback():
        nonlocal rollback_task
        if committed:
            return
        if rollback_task is None:
            rollback_task = asyncio.create_task(cleanup(), name=f"voip-pickup-rollback-{incoming.call_id}")
        await async_wait_for_cleanup(rollback_task)

    registry.own_resource(
        session.call_id,
        resource_name,
        claim,
        lambda _reason: rollback(),
        stage=CleanupStage.MEDIA,
        generation=session.generation,
    )
    try:
        registry.claim_endpoint(
            session.call_id, picker, role="destination", adopt_transport=True
        )
        acquired = True
        reservation = RtpPortReservation.allocate(hass)
        left = (
            local_rtp_peer("127.0.0.1", source.recv_format, source.send_format)
            if local_source is not None
            else application_rtp_peer(source)
        )
        right = invite_rtp_peer(incoming)
        directions = (
            left.can_send,
            left.can_receive,
            right.can_send,
            right.can_receive,
        )
        relay = SipRtpRelay(
            left=replace(left, can_send=False, can_receive=False),
            right=replace(right, can_send=False, can_receive=False),
            left_port=source_ports.ports[0]
            if source_ports is not None
            else reservation.ports[0],
            right_port=reservation.ports[1],
        )
        from .sip_bridge import async_prepare_invite_pair_video
        from .const import CONF_VIDEO_TRANSCODING
        relay.video_relay, source_video_answer, picker_video_answer = await async_prepare_invite_pair_video(
            hass, source, incoming, enable_transcoding=bool(runtime.transport_config.get(CONF_VIDEO_TRANSCODING, False)),
            left_peer=local_source.video_rtp_peer() if local_source is not None else None)
        if local_source is not None:
            local_media_descriptor = local_source.prepare_bridge_media(relay, source_video_answer)
        if source_ports is not None:
            sock = relay._rtp_socket(relay.right_port)
            try:
                relay.right_transport, _ = await hass.loop.create_datagram_endpoint(
                    lambda: _RelayProtocol(relay, "right", paused=True), sock=sock
                )
            except BaseException:
                sock.close()
                raise
        else:
            await relay.start()
        if (
            source_ports is not None
            and application is not None
            and application.media is not None
        ):
            media = application.media
            media_handoff = LocalMediaBridge(
                media, media.invite, relay, media.transport
            )
            media_handoff.validate()
        validate()
        answer = build_answer_directional(
            local_ip,
            local_ip,
            relay.right_port,
            incoming.send_format,
            incoming.recv_format,
            dtmf=first_offered_dtmf_format(incoming.remote_sdp),
            remote_sdp=incoming.remote_sdp,
            video_port=relay.video_relay.right_port if picker_video_answer is not None else 0,
            video_format=picker_video_answer.video_format if picker_video_answer is not None else None,
            video_direction=picker_video_answer.direction if picker_video_answer is not None else None,
        )
        if fork_gate is not None:
            fork_answer = fork_gate.submit(picker)
            if not await asyncio.shield(fork_answer.selected):
                raise ServiceValidationError("Another group member answered before pickup")
            validate()
    except asyncio.CancelledError:
        await rollback()
        if not session.live:
            return SipInviteResult(480, "Temporarily Unavailable")
        raise
    except (ServiceValidationError, ValueError, RuntimeError, OSError) as error:
        await rollback()
        return SipInviteResult(
            503 if isinstance(error, OSError) else 480, "Temporarily Unavailable"
        )
    except BaseException:
        await rollback()
        raise

    async def commit():
        nonlocal committed, changed, local_media_adopted
        validate()
        claim.claimed = True
        registry.transition(
            session.call_id,
            owner="router",
            state="connecting",
            expected_generation=session.generation,
        )
        changed = True
        manager = sip_endpoint_manager(hass)
        registry.attach_inbound_dialog(
            session.call_id,
            incoming.call_id,
            manager,
            endpoint_id=picker,
            expected_generation=session.generation,
        )
        session.update_metadata(dest_endpoint_id=picker, target_endpoint_id=picker)
        if previous_forward is not None:
            registry.cancel_task(session.call_id, "forward")
            retired = RetiredForward(target_endpoint.endpoint_id, previous_forward)
            name = f"retired_destination:forward:{incoming.call_id}"

            async def drain(_reason):
                await asyncio.gather(previous_forward, return_exceptions=True)

            resource = session.add_resource(
                name, retired, drain, stage=CleanupStage.LEG
            )

            async def finish_old_route():
                await resource.close("picked_up")
                if session.live:
                    registry.release_resource(session.call_id, name, value=retired)

            session.create_task(finish_old_route(), name="pickup-retired-forward")
        if native is not None:
            await native.retire()
        elif local_source is not None:
            await local_source.retire_destination()
            local_source.pause()
            local_source.commit()
        else:
            if fork_gate is None and target_endpoint.supports_ha_client:
                _release_ha_softphone_claim(
                    hass,
                    session.call_id,
                    endpoint_id=target_endpoint.endpoint_id,
                    destination=incoming.caller,
                )
        if media_handoff is not None:
            media_handoff.commit()
        if application is not None:
            await application.release_media_for_forward()
        if not session.live:
            raise ServiceValidationError("The caller ended during pickup")
        # Retiring the previous destination may reassign this reservation.
        # Take and detach from the same current owner, retaining its queued RTP.
        current_source = source_ports
        if current_source is not None:
            current_source = (
                registry.resource_for(session.call_id, "preanswered") or {}
            ).get("rtp_reservation") or current_source
        if relay.left_transport is None:
            sock = current_source.take_audio_socket(relay.left_port) if current_source is not None else None
            if sock is None:
                sock = relay._rtp_socket(relay.left_port)
            try:
                relay.left_transport, _ = await hass.loop.create_datagram_endpoint(
                    lambda: _RelayProtocol(relay, "left", paused=True), sock=sock
                )
            except BaseException:
                sock.close()
                raise
        audio_source = preanswered.get("audio_rtp_source")
        if isinstance(audio_source, AudioRtpSenderState):
            relay.left.sequence, relay.left.timestamp, relay.left.ssrc = (
                audio_source.sequence,
                audio_source.timestamp,
                audio_source.ssrc,
            )
        if source_ports is not None:
            merged = RtpPortReservation.merge(
                hass,
                (current_source.ports, reservation.ports),
                (relay.left_port, relay.right_port),
            )
            current_source.detach()
            reservation.detach()
            relay._on_release = lambda _ports: merged.release()
        else:
            relay._on_release = lambda _ports: reservation.release()
        registry.attach_relay(session.call_id, relay)
        registry.take_media(session.call_id, provisional=True)
        if local_source is not None:
            registry.own_resource(
                session.call_id, f"browser_source:{session.call_id}", local_source, None
            )
            registry.attach_media(session.call_id, local_media_descriptor)
            local_media_adopted = True
        source_answer = (
            build_answer_directional(
                local_ip,
                local_ip,
                relay.left_port,
                source.send_format,
                source.recv_format,
                dtmf=first_offered_dtmf_format(source.remote_sdp),
                remote_sdp=source.remote_sdp,
                video_port=relay.video_relay.left_port if source_video_answer is not None else 0,
                video_format=source_video_answer.video_format if source_video_answer is not None else None,
                video_direction=source_video_answer.direction if source_video_answer is not None else None,
            )
            if local_source is None and not source_answered
            else ""
        )
        result = await async_commit_runtime_answer(
            registry,
            session.call_id,
            source_answer,
            send_final_response=send_final_response,
            response_context=hass,
            owner="bridge",
            caller=session.caller,
            callee=incoming.caller,
            route_kind="pickup",
            source_endpoint_id=str(session.metadata.get("source_endpoint_id") or ""),
            dest_endpoint_id=picker,
            response_already_sent=source_answered or local_source is not None,
        )
        if not result.committed:
            raise ServiceValidationError(
                "The caller ended before pickup could complete"
            )
        registry.set_bridge_link(session.call_id, incoming.call_id)
        session.update_metadata(
            target_endpoint_id=picker, connected_party=incoming.caller
        )
        (
            relay.left.can_send,
            relay.left.can_receive,
            relay.right.can_send,
            relay.right.can_receive,
        ) = directions
        relay.left_transport.resume_reading()
        relay.right_transport.resume_reading()
        attach_dtmf_event_bridge(
            hass,
            relay,
            call_id=session.call_id,
            dest_call_id=incoming.call_id,
            caller=session.caller,
            callee=incoming.caller,
            source_client=application.source_client
            if application is not None
            else None,
        )
        if application is not None and application.source_client is not None:
            from .bridge_media_updates import BridgeMediaUpdateBinder

            BridgeMediaUpdateBinder(hass).attach_audio_side(
                application.source_client,
                relay,
                source_call_id=session.call_id,
                side="left",
            )
        committed = True
        if fork_answer is not None:
            fork_answer.finish(True)
        registry.release_resource(session.call_id, resource_name, value=claim)
        if local_source is not None:
            local_source.connected(incoming.caller, "pickup")
        if application is not None:
            await application.close("picked_up")
            registry.release_resource(
                session.call_id,
                f"{application.owner}:{session.call_id}",
                value=application,
            )
        publish_bridge_projection(hass, session, event_type="answered", route_kind="pickup")
        publish_bridge_projection(hass, session, event_type="picked_up", route_kind="pickup")

    return SipInviteResult(
        200,
        "OK",
        answer_sdp=answer,
        commit=commit,
        rollback=rollback,
        connected_identity_name=session.caller,
        connected_identity_user=source.routing_caller,
    )


async def async_pickup_call(call):
    """Recover a selected call through the same routing and answer primitives."""
    from .call_projection import publish_bridge_projection
    from .parking import get_parking_lot, pickup_phone_call

    runtime = require_runtime_data(call.hass)
    from .phone_control import PhoneOperation
    phone = await runtime.phones.resolve_source(call,
        PhoneOperation.ORIGINATE if call.data.get("media_client_id") else None)
    endpoint = runtime.endpoints.require(phone.endpoint_id)
    if phone.kind.is_softphone:
        from .phone_endpoint import ha_client_endpoint
        endpoint = ha_client_endpoint(endpoint)
    slot = str(call.data.get("slot") or "")
    target = str(call.data.get("target_device_id") or "")
    if bool(slot) == bool(target):
        raise ServiceValidationError("Choose a ringing phone or a parking slot")
    if slot:
        parked = get_parking_lot(call.hass).calls.get(slot)
        if parked is None:
            raise ServiceValidationError("No call is parked in that slot")
        session = parked.session
        if call.data.get("call_id") and call.data["call_id"] != session.call_id:
            raise ServiceValidationError("The parked call changed before pickup")
        if (
            call.data.get("expected_generation", session.generation)
            != session.generation
        ):
            raise ServiceValidationError(
                "The parked call generation changed before pickup"
            )
        await pickup_phone_call(call, endpoint, slot)
    else:
        _runtime, _target, session = ringing_target(
            call.hass,
            target,
            call_id=str(call.data.get("call_id") or ""),
            generation=call.data.get("expected_generation"),
            leg_id=str(call.data.get("leg_id") or ""),
        )
        if runtime.sip.resource_for(session.call_id, "fork_answer") is not None:
            session = await _pickup_group_phone(call, endpoint, session)
        elif endpoint.kind.is_softphone:
            session = await pickup_browser(call, endpoint, target)
        else:
            if (
                endpoint.active_call_id
                or endpoint.availability.value != "available"
                or endpoint.dnd
            ):
                raise ServiceValidationError("Choose an available phone for pickup")
            if runtime.sip.forward_call is None:
                raise ServiceValidationError("Call routing is unavailable")
            await runtime.sip.forward_call(
                call_id=session.call_id,
                destination=endpoint.name,
                on_failure="resume",
                precondition=CallPrecondition.capture(session),
            )
            task = runtime.sip.task_for(session.call_id, "forward")
            if task is not None:
                try:
                    async with asyncio.timeout(
                        float(call.data.get("answer_timeout", 30))
                    ):
                        await asyncio.shield(task)
                except BaseException:
                    from .session_cleanup import async_wait_for_cleanup

                    if not task.done():
                        task.cancel()
                    await async_wait_for_cleanup(
                        asyncio.gather(task, return_exceptions=True)
                    )
                    raise
            if (
                not session.live
                or session.metadata.get("dest_endpoint_id") != endpoint.endpoint_id
                or session.state != "in_call"
            ):
                raise ServiceValidationError(
                    "The selected phone did not answer the pickup"
                )
            publish_bridge_projection(
                call.hass, session, event_type="picked_up", route_kind="pickup"
            )
    return {
        "schema_version": 1,
        "call_id": session.call_id,
        "generation": session.generation,
        "endpoint_id": endpoint.endpoint_id,
        "state": session.state,
    }


async def _pickup_group_phone(call, endpoint, session):
    """Add an operator to the existing fork, retaining its original branches."""
    from homeassistant.core import ServiceCall, callback
    from .call_projection import observe_phone_leg_projection, publish_phone_projection
    from .route_decisions import ensure_ring_group_phone_not_declined
    from .websocket_api import CALL_EVENT
    from .outbound_attempts import BrowserLeg, async_close_outbound_leg
    from .softphone_answer import async_answer_browser_call
    from .softphone_commands import async_resolve_browser_call_command
    from .session_cleanup import async_wait_for_cleanup

    runtime = require_runtime_data(call.hass)
    registry = runtime.sip
    gate = registry.resource_for(session.call_id, "fork_answer")
    if gate is None or gate.closed or endpoint.active_call_id or endpoint.dnd:
        raise ServiceValidationError(
            "Choose an idle phone while the group is still ringing"
        )
    token = session.token
    outcome = call.hass.loop.create_future()

    @callback
    def changed(_event=None):
        if outcome.done():
            return
        if (
            registry.get_session(token.call_id, generation=token.generation)
            is not session
            or not session.live
        ):
            outcome.set_result(False)
        elif session.state == "in_call":
            outcome.set_result(
                session.metadata.get("dest_endpoint_id") == endpoint.endpoint_id
            )

    unsubscribe = call.hass.bus.async_listen(CALL_EVENT, changed)
    attempt = ticket = dial_task = browser_leg = None
    selected = False
    route = previous_candidate = projected_leg = previous_leg_state = None
    route_member_added = session_member_added = claim_added = False
    controller_before = {}
    missing = object()
    try:
        async with asyncio.timeout(float(call.data.get("answer_timeout", 30))):
            if endpoint.kind.is_softphone:
                route = session.artifacts.pending_route
                if route is None or route["future"].done():
                    raise ServiceValidationError(
                        "The ringing group already selected an answer"
                    )
                ensure_ring_group_phone_not_declined(route, endpoint.endpoint_id)
                previous_candidate = next(
                    (leg for leg in gate.browser_legs if leg.endpoint_id == endpoint.endpoint_id), None
                )
                leg_id = f"browser:{endpoint.endpoint_id}"
                prior_leg = session.legs.get(leg_id)
                if prior_leg is not None:
                    previous_leg_state = (prior_leg.state, prior_leg.phase, prior_leg.role, prior_leg.endpoint_id)
                browser_leg = BrowserLeg(
                    endpoint.name,
                    endpoint.endpoint_id,
                    endpoint.name,
                    endpoint.device_id,
                    pickup=True,
                )
                claim_added = endpoint.endpoint_id not in session.endpoint_claims
                registry.claim_endpoint(
                    session.call_id, endpoint.endpoint_id, role="group_candidate"
                )
                if previous_candidate is None:
                    gate.browser_legs.append(browser_leg)
                else:
                    gate.browser_legs[gate.browser_legs.index(previous_candidate)] = browser_leg
                route_member_added = endpoint.endpoint_id not in route.get("ring_group_endpoint_ids", ())
                if route_member_added:
                    route["ring_group_endpoint_ids"] = (*route.get("ring_group_endpoint_ids", ()), endpoint.endpoint_id)
                session_member_added = endpoint.endpoint_id not in session.metadata.get("ring_endpoint_ids", ())
                if session_member_added:
                    session.update_metadata(ring_endpoint_ids=(
                        *session.metadata.get("ring_endpoint_ids", ()), endpoint.endpoint_id
                    ))
                try:
                    observe_phone_leg_projection(
                        call.hass,
                        registry,
                        session,
                        endpoint.endpoint_id,
                        "ringing",
                        leg_id=f"browser:{endpoint.endpoint_id}",
                        peer_name=session.caller,
                        direction="incoming",
                    )
                finally:
                    projected_leg = session.legs.get(leg_id)
                answer = ServiceCall(
                    call.hass,
                    "voip_stack",
                    "answer",
                    {
                        "device_id": endpoint.device_id,
                        "call_id": session.call_id,
                        "media_client_id": str(call.data.get("media_client_id") or ""),
                        "send_video": bool(call.data.get("send_video", False)),
                    },
                    context=call.context,
                )
                command = await async_resolve_browser_call_command(
                    call.hass,
                    answer,
                    endpoint_id=endpoint.endpoint_id,
                    endpoint=endpoint,
                )
                if (not session.owns(token) or gate.closed or route["future"].done()
                        or session.artifacts.pending_route is not route):
                    raise ServiceValidationError("The ringing group already selected an answer")
                ensure_ring_group_phone_not_declined(route, endpoint.endpoint_id)
                controller_before = {key: session.metadata.get(key, missing)
                                     for key in ("ha_context", "controller_user_id")}
                await async_answer_browser_call(call.hass, answer, command)
                selected = True
            else:
                from .peer_snapshot import async_build_peer_snapshot
                from .endpoint_routing import roster_from_peers
                from .phonebook_runtime import registered_roster_entries
                from .core.sip import sip_default_port

                dialer = registry.component("endpoint_dialer")
                peers = await async_build_peer_snapshot(call.hass)
                entries = roster_from_peers(
                    call.hass, peers, registered_roster_entries(call.hass)
                )
                if gate.closed or not session.owns(token) or dialer is None:
                    raise ServiceValidationError(
                        "The ringing group ended before pickup"
                    )
                attempt = dialer.prepare_outbound_leg(
                    member=endpoint.name,
                    peers=peers,
                    roster_entries=entries,
                    local_name=session.caller,
                    local_rtp_port_index=1,
                    invite=gate.invite,
                    source_call_id=session.call_id,
                    endpoint_id_override=endpoint.endpoint_id,
                )
                if attempt is None:
                    raise ServiceValidationError(
                        "The pickup phone has no reachable SIP destination"
                    )
                registry.claim_endpoint(
                    session.call_id, endpoint.endpoint_id, role="group_candidate"
                )
                attempt.pickup = True
                gate.attempts.append(attempt)

                async def dial():
                    if gate.on_leg_state is not None:
                        gate.on_leg_state(attempt, "calling")
                    status = await attempt.client.invite(
                        target=attempt.uri.user or endpoint.name,
                        request_uri=str(attempt.uri),
                        remote_host=attempt.uri.host,
                        remote_sip_port=sip_default_port(attempt.uri),
                        timeout=float(call.data.get("answer_timeout", 30)),
                    )
                    if status == "ringing":
                        if gate.on_leg_state is not None:
                            gate.on_leg_state(attempt, "remote_ringing")
                        status = await attempt.client.wait_for_final(
                            timeout=float(call.data.get("answer_timeout", 30))
                        )
                    return status

                dial_task = session.create_task(
                    dial(), name=f"pickup-callback-{endpoint.endpoint_id}"
                )
                done, _ = await asyncio.wait(
                    (dial_task, outcome), return_when=asyncio.FIRST_COMPLETED
                )
                if outcome in done or await dial_task != "in_call":
                    raise ServiceValidationError(
                        "The group changed or the pickup phone did not answer"
                    )
                ticket = gate.submit(endpoint.endpoint_id)
                ticket.payload = attempt
                ticket.finish(True)
                selected = await asyncio.shield(ticket.selected)
                if not selected:
                    raise ServiceValidationError(
                        "Another phone answered the group first"
                    )
            changed()
            if not await asyncio.shield(outcome):
                raise ServiceValidationError(
                    "Another phone answered or the caller ended"
                )
            selected = True
            return session
    finally:
        unsubscribe()
        if not outcome.done():
            outcome.cancel()
        if dial_task is not None and not dial_task.done():
            dial_task.cancel()
            await async_wait_for_cleanup(
                asyncio.gather(dial_task, return_exceptions=True)
            )
        selected = selected or (ticket is not None and ticket.accepted)
        if not selected:
            if ticket is not None:
                ticket.finish(False)
            if attempt is not None:
                await async_close_outbound_leg(attempt, bye_or_cancel=True)
                if attempt in gate.attempts:
                    gate.attempts.remove(attempt)
                if session.live:
                    registry.remove_leg(
                        session.call_id, attempt.client.dialog_ids.call_id
                    )
            current = registry.get_session(token.call_id, generation=token.generation) is session and session.owns(token)
            owned_browser = next((i for i, leg in enumerate(gate.browser_legs) if leg is browser_leg), None)
            if browser_leg is not None and owned_browser is not None:
                if previous_candidate is None:
                    gate.browser_legs.pop(owned_browser)
                else:
                    gate.browser_legs[owned_browser] = previous_candidate
                if current:
                    leg_id = f"browser:{endpoint.endpoint_id}"
                    restore_projection = (projected_leg is not None
                        and session.legs.get(leg_id) is projected_leg
                        and projected_leg.state == "ringing")
                    with registry.state_operation():
                        if route_member_added and session.artifacts.pending_route is route:
                            route["ring_group_endpoint_ids"] = tuple(
                                item for item in route["ring_group_endpoint_ids"] if item != endpoint.endpoint_id
                            )
                        if session_member_added:
                            session.update_metadata(ring_endpoint_ids=tuple(
                                item for item in session.metadata.get("ring_endpoint_ids", ()) if item != endpoint.endpoint_id
                            ))
                        if claim_added:
                            registry.release_endpoint_claim(session.call_id, endpoint.endpoint_id)
                        if session.state != "in_call":
                            for key, expected in (("ha_context", call.context), ("controller_user_id", call.context.user_id)):
                                if controller_before.get(key) is missing and session.metadata.get(key) == expected:
                                    session.metadata.pop(key, None)
                        leg_id = f"browser:{endpoint.endpoint_id}"
                        if restore_projection:
                            registry.restore_leg_state(
                                session.call_id, projected_leg, previous_leg_state,
                                generation=token.generation,
                            )
                    if restore_projection:
                        publish_phone_projection(call.hass, session, endpoint.endpoint_id, leg_id=leg_id)
                        if previous_leg_state is None:
                            registry.remove_leg(session.call_id, leg_id)

            elif browser_leg is None and current and attempt is not None:
                registry.release_endpoint_claim(session.call_id, endpoint.endpoint_id)
