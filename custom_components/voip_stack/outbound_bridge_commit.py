"""Prepare and adopt outbound bridge media under one call generation."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
import secrets
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .bridge_manager import async_watch_sip_bridge_destination
from .bridge_media_updates import BridgeMediaUpdateBinder, prepare_source_video_update
from .config import debug_mode, media_capture_enabled
from .dtmf_events import attach_dtmf_event_bridge
from .endpoint_session import CleanupStage, TerminationIntent
from .fsm import CallState, TerminalReason
from .inbound_answer import async_commit_runtime_answer
from .media_ports import RtpPortReservation, release_video_media_reservation
from .outbound_attempts import OutboundLeg, async_apply_outbound_video_answer
from .runtime_data import sip_endpoint_manager
from .session_cleanup import async_wait_for_cleanup
from .core.sdp import build_answer_directional
from .core.rtp import AudioRtpSenderState
from .sip_bridge import (
    async_request_sip_bridge_keyframe,
    async_start_sip_bridge_media,
    build_invite_client_relay,
    build_local_client_relay,
    configure_answered_invite_video_relay,
)
from .sip_runtime import send_final_response
from .softphone_termination import async_terminate_sip_bridge_session
from .pending_sip_destination import RetainedSourceMedia

if TYPE_CHECKING:
    from .endpoint_session import EndpointCallSession


@dataclass(frozen=True, slots=True, kw_only=True)
class BridgeCommitPolicy:
    """Immutable differences between bridge commit call sites."""

    route_kind: str
    caller: str
    callee: str
    connected_party: str
    source_role: str = "caller"
    source_state: str = CallState.CONNECTING.value
    bridge_state: str = CallState.CONNECTING.value
    dest_state: str = CallState.IN_CALL.value
    ingress: str = ""
    origin: str = ""
    expected_generation: int | None = None
    local_source: bool = False
    response_already_sent: bool = False
    consume_pending_source: bool = True
    answer_owner: str = "bridge"
    endpoint_id: str = ""
    source_endpoint_id: str = ""
    dest_endpoint_id: str = ""
    media_client_id: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class BridgeCommitData:
    """Resources transferred to the authoritative call session."""

    invite: Any
    winner: OutboundLeg
    source_relay_port: int
    dest_relay_port: int
    local_ip: str
    release_port_pairs: tuple[tuple[int, int], ...]
    detach_reservations: tuple[Any, ...]
    enable_video_transcoding: bool = False
    local_endpoint_id: str = ""
    source_client: Any | None = None
    prepared_media: Any | None = None
    retained_media: RetainedSourceMedia | None = None
    retained_video_update: Any | None = None
    retained_video_answer: Any | None = None


@dataclass(frozen=True, slots=True)
class BridgeCommitResult:
    """Established bridge values used by public projections."""

    relay: Any
    dest_call_id: str
    video_answer: Any | None
    video_failure_reason: str


async def async_commit_outbound_bridge(
    hass: HomeAssistant,
    registry: Any,
    data: BridgeCommitData,
    policy: BridgeCommitPolicy,
    *,
    session: EndpointCallSession | None = None,
) -> BridgeCommitResult | None:
    """Commit one winning leg into a new or already registered session."""

    invite = data.invite
    winner = data.winner
    client = winner.client
    dest_call_id = client.dialog_ids.call_id
    watcher_ready: asyncio.Event | None = None
    watcher: asyncio.Task[None] | None = None

    async def _watch_destination() -> None:
        assert watcher_ready is not None
        await watcher_ready.wait()
        await async_watch_sip_bridge_destination(
            hass,
            client=client,
            source_call_id=invite.call_id,
            terminate_sip_bridge=async_terminate_sip_bridge_session,
        )

    async def _cancel_watcher() -> None:
        if watcher is None:
            return
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    trunk_metadata = {
        key: value
        for key, value in (
            ("inbound_trunk_id", str(getattr(invite, "trunk_id", "") or "")),
            (
                "outbound_trunk_id",
                str(
                    getattr(winner, "trunk_id", "")
                    or getattr(invite, "outbound_trunk_id", "")
                    or ""
                ),
            ),
        )
        if value
    }
    if session is None:
        watcher_ready = asyncio.Event()
        watcher = asyncio.create_task(
            _watch_destination(),
            name=f"voip-bridge-destination-{dest_call_id}",
        )
        session = registry.register_bridge(
            source_call_id=invite.call_id,
            dest_call_id=dest_call_id,
            client=client,
            lifecycle_task=watcher,
            state=policy.bridge_state,
            caller=policy.caller,
            callee=policy.callee,
            route_kind=policy.route_kind,
            ingress=policy.ingress,
            origin=policy.origin,
            source_role=policy.source_role,
            source_state=policy.source_state,
            dest_state=policy.dest_state,
            expected_generation=policy.expected_generation,
            **trunk_metadata,
        )
        if session is None:
            await _cancel_watcher()
            return None
    elif not registry.is_generation_current(invite.call_id, session.generation):
        return None
    elif trunk_metadata and session.live:
        missing = {
            key: value
            for key, value in trunk_metadata.items()
            if not session.metadata.get(key)
        }
        if missing:
            session.update_metadata(**missing)

    generation = session.generation
    if winner.trunk_id and dest_call_id in session.legs:
        session.legs[dest_call_id].trunk_id = winner.trunk_id
    relay = None
    relay_adopted = False
    ports_transferred = False
    failure_reason = TerminalReason.MEDIA_INCOMPATIBLE.value
    release_port_pairs = data.release_port_pairs
    adopted_reservation = None

    def _require_current() -> None:
        if not registry.is_generation_current(invite.call_id, generation):
            raise RuntimeError("bridge call generation is no longer current")

    def _release_ports(_ports: Any) -> None:
        if ports_transferred and adopted_reservation is not None:
            adopted_reservation.release()

    async def _cleanup_failure(barrier: asyncio.Future | None) -> None:
        try:
            await _cancel_watcher()
        finally:
            try:
                if barrier is not None:
                    await async_wait_for_cleanup(barrier)
            finally:
                try:
                    # Once adopted, only the session closes the relay. Before
                    # adoption this function still owns the prepared resource.
                    if relay is not None and not relay_adopted:
                        await relay.stop()
                finally:
                    try:
                        if winner.video_relay is not None:
                            await winner.video_relay.stop()
                            winner.video_relay = None
                    finally:
                        if not ports_transferred:
                            for reservation in data.detach_reservations:
                                reservation.release()
                            if all(item is not winner.ports for item in data.detach_reservations):
                                winner.ports.release()

    try:
        _require_current()
        if policy.dest_state == CallState.RINGING.value and watcher is not None:
            final = await client.wait_for_final()
            _require_current()
            if final != CallState.IN_CALL.value or client.dialog is None:
                raise RuntimeError(final)

        if client.dialog is not None and session.state in {CallState.RINGING.value, CallState.REMOTE_RINGING.value}:
            registry.transition(invite.call_id, state=CallState.CONNECTING.value,
                                expected_generation=generation)
        video_answer = data.retained_video_answer
        if winner.video_relay is not None and client.dialog is not None:
            video_answer = configure_answered_invite_video_relay(
                invite, client.dialog, winner.video_relay,
                hass=hass, enable_transcoding=data.enable_video_transcoding,
            )
            await async_apply_outbound_video_answer(winner, video_answer)
            _require_current()

        failure_reason = TerminalReason.TRANSPORT_UNREACHABLE.value
        if data.retained_media is not None:
            data.retained_media.validate(registry)
            relay = data.retained_media.relay
            if registry.resource_for(invite.call_id, "relay") is not relay or client.dialog is None:
                raise RuntimeError("early bridge media owner changed")
            relay_adopted = True
            from .sip_bridge import dialog_rtp_peer
            async with getattr(relay, "media_reconfiguration_guard", nullcontext)():
                _require_current()
                data.retained_media.validate(registry)
                if client.dialog is None:
                    raise RuntimeError("early bridge dialog closed before media commit")
                if data.retained_video_update is not None:
                    await data.retained_video_update.commit()
                    _require_current()
                relay.prepare_peer_reconfiguration("right", dialog_rtp_peer(client.dialog))()
            relay._on_release = _release_ports
        elif data.prepared_media is not None:
            await data.prepared_media.acquire_source()
            _require_current()
            relay = data.prepared_media.commit()
            if data.prepared_media.owned_source is not None:
                adopted_reservation = data.prepared_media.reservation
                release_port_pairs = (adopted_reservation.ports,)
                for previous in data.detach_reservations:
                    if previous is not adopted_reservation and not previous.released:
                        previous.release()
            relay._on_release = _release_ports
        elif data.source_client is not None:
            from .sip_bridge import dialog_rtp_peer
            from .sip_rtp_bridge import SipRtpRelay
            if data.source_client.dialog is None:
                raise RuntimeError("The retained source dialog ended")
            relay = SipRtpRelay(left=dialog_rtp_peer(data.source_client.dialog), right=dialog_rtp_peer(client.dialog),
                left_port=data.source_relay_port, right_port=data.dest_relay_port,
                debug=debug_mode(hass), debug_capture=media_capture_enabled(hass), on_release=_release_ports)
        elif policy.local_source:
            relay = build_local_client_relay(
                client=client,
                local_host=data.local_ip,
                local_to_relay_format=invite.recv_format,
                relay_to_local_format=invite.send_format,
                source_relay_port=data.source_relay_port,
                dest_relay_port=data.dest_relay_port,
                capture_name=f"{invite.call_id}_{dest_call_id}",
                debug=debug_mode(hass),
                debug_capture=media_capture_enabled(hass),
                on_release=_release_ports,
            )
        else:
            relay = build_invite_client_relay(
                invite=invite,
                client=client,
                source_relay_port=data.source_relay_port,
                dest_relay_port=data.dest_relay_port,
                debug=debug_mode(hass),
                debug_capture=media_capture_enabled(hass),
                on_release=_release_ports,
            )
        reservations = ((data.prepared_media.reservation,)
            if data.prepared_media is not None and data.prepared_media.owned_source is not None
            else (winner.ports, *(item for item in data.detach_reservations if item is not winner.ports)))
        for reservation in reservations:
            transfer_audio = getattr(reservation, "transfer_audio_sockets", None)
            if transfer_audio is not None:
                transfer_audio(relay, ports=(data.source_relay_port, data.dest_relay_port))
        source_media = registry.resource_for(invite.call_id, "preanswered") or {}
        audio_source = source_media.get("audio_rtp_source")
        if isinstance(audio_source, AudioRtpSenderState):
            relay.left.sequence = audio_source.sequence
            relay.left.timestamp = audio_source.timestamp
            relay.left.ssrc = audio_source.ssrc
        if winner.video_relay is not None:
            relay.attach_video_relay(winner.video_relay)
            winner.video_relay = None
        if data.prepared_media is not None and data.prepared_media.media is not None:
            old_media = data.prepared_media.media
            session.add_resource(f"retired_local_media:{id(old_media):x}", old_media,
                lambda _reason: old_media.stop(), stage=CleanupStage.MEDIA)
            registry.release_resource(invite.call_id, f"relay:{invite.call_id}", value=old_media)
        if data.retained_media is None:
            registry.attach_relay(invite.call_id, relay)
        relay_adopted = True
        # Normalize the complete admitted inventory to the two active ports.
        # No await separates session adoption, the inventory and its closer.
        # Later parking can retain this exact pair without losing spare ports.
        if data.prepared_media is not None and data.prepared_media.owned_source is not None:
            adopted_reservation = data.prepared_media.reservation
        else:
            adopted_reservation = RtpPortReservation.merge(hass, release_port_pairs,
                (relay.left_port, relay.right_port))
        for reservation in reservations:
            if reservation is not adopted_reservation:
                reservation.detach()
        ports_transferred = True
        relay._on_release = _release_ports
        if data.prepared_media is not None and data.prepared_media.owned_source is not None:
            data.prepared_media.owned_source.release_to_session(relay)
        if data.prepared_media is not None and data.prepared_media.video_update is not None:
            await data.prepared_media.video_update.commit()
            _require_current()
        publish_connected_identity = BridgeMediaUpdateBinder(hass).attach(
            client, relay, source_call_id=invite.call_id,
            source_client=data.source_client,
            source_video_send_format=invite.send_video_format,
            source_video_receive_format=invite.recv_video_format,
        )
        if data.source_client is not None:
            BridgeMediaUpdateBinder(hass).attach_audio_side(data.source_client, relay,
                source_call_id=invite.call_id, side="left")
        attach_dtmf_event_bridge(
            hass,
            relay,
            call_id=invite.call_id,
            dest_call_id=dest_call_id,
            caller=policy.caller,
            callee=policy.connected_party,
            client=client,
            source_client=data.source_client,
        )
        if policy.local_source:
            registry.attach_media(
                invite.call_id,
                {
                    "rtp_loopback": True,
                    "remote_rtp_host": data.local_ip,
                    "remote_rtp_port": data.source_relay_port,
                    "send_format": invite.recv_format,
                    "recv_format": invite.send_format,
                    "local_ssrc": secrets.randbelow(0xFFFFFFFF) + 1,
                    "endpoint_id": data.local_endpoint_id,
                },
            )
        elif policy.consume_pending_source:
            registry.take_pending_invite(invite.call_id)
            pending_source = registry.take_media(invite.call_id, provisional=True)
            if pending_source is not None and relay.video_relay is not None:
                # The live relay now owns the source video sockets and reservation.
                # Detach those handles from the provisional owner without closing
                # them, then release only media that was not transferred.
                for key in (
                    "video_rtp_reservation",
                    "video_rtp_socket",
                    "video_rtcp_socket",
                ):
                    pending_source.pop(key, None)
            release_video_media_reservation(pending_source)

        request_video_keyframe = await async_start_sip_bridge_media(relay)
        _require_current()
        if (policy.response_already_sent and video_answer is not None and relay.video_relay is not None
                and data.retained_video_update is None):
            video_formats = tuple(dict.fromkeys((
                relay.video_relay.left.recv_format, relay.video_relay.left.send_format,
            )))
            if data.source_client is not None:
                prepared = await prepare_source_video_update(
                    hass, invite.call_id, source_client=data.source_client,
                    local_video_rtp_port=int(relay.video_relay.left_port),
                    video_formats=video_formats, video_direction=video_answer.direction,
                )
                _require_current()
                activated = bool(prepared is not None and prepared.commit())
            else:
                endpoint = sip_endpoint_manager(hass)
                activated = bool(endpoint is not None and await endpoint.async_activate_video_reinvite(
                    invite.call_id, local_video_rtp_port=int(relay.video_relay.left_port),
                    video_formats=video_formats, video_direction=video_answer.direction,
                ))
            if not activated:
                raise RuntimeError("preanswered source rejected video activation")
        _require_current()
        failure_reason = TerminalReason.PROTOCOL_ERROR.value
        answer = ""
        if not policy.response_already_sent and not policy.local_source:
            answer = build_answer_directional(
                data.local_ip,
                data.local_ip,
                data.source_relay_port,
                invite.send_format,
                invite.recv_format,
                recv_formats=invite.recv_formats, send_formats=invite.send_formats,
                audio_rtcp=invite.audio_rtcp_min_interval > 0, remote_sdp=invite.remote_sdp, dtmf_direction=getattr(invite, "dtmf_direction", None),
                video_port=(
                    relay.video_relay.left_port
                    if relay.video_relay is not None
                    else 0
                ),
                video_format=(
                    video_answer.video_format if video_answer is not None else None
                ),
                video_direction=(
                    video_answer.direction
                    if video_answer is not None
                    else "inactive"
                ),
            )
        committed = await async_commit_runtime_answer(
            registry,
            invite.call_id,
            answer,
            send_final_response=send_final_response,
            response_context=hass,
            owner=policy.answer_owner,
            caller=policy.caller,
            callee=policy.callee,
            route_kind=policy.route_kind,
            endpoint_id=policy.endpoint_id,
            source_endpoint_id=policy.source_endpoint_id,
            dest_endpoint_id=policy.dest_endpoint_id,
            media_client_id=policy.media_client_id,
            response_already_sent=policy.response_already_sent or policy.local_source,
            media_contract=invite,
        )
        if not bool(getattr(committed, "committed", committed)):
            raise RuntimeError(str(getattr(committed, "reason", "answer_not_committed")))

        _require_current()
        registry.observe_leg(invite.call_id, dest_call_id, role="callee",
                             state=CallState.IN_CALL.value,
                             endpoint_id=policy.dest_endpoint_id or winner.endpoint_id, generation=generation)
        registry.observe_leg(invite.call_id, invite.call_id, role=policy.source_role,
                             state=CallState.IN_CALL.value,
                             endpoint_id=policy.source_endpoint_id, generation=generation)
        if publish_connected_identity is not None:
            publish_connected_identity()
        if request_video_keyframe:
            assert relay.video_relay is not None
            session.create_task(
                async_request_sip_bridge_keyframe(client),
                name=f"voip-keyframe-{dest_call_id}",
            )

        from .call_projection import publish_media_route

        publish_media_route(hass, invite.call_id, relay.media_route)
        result = BridgeCommitResult(
            relay,
            dest_call_id,
            video_answer,
            winner.video_failure_reason,
        )
        if watcher_ready is not None:
            watcher_ready.set()
        if winner.pickup:
            from .call_projection import publish_bridge_projection
            publish_bridge_projection(hass, session, event_type="picked_up", route_kind="pickup")
        if data.prepared_media is not None and data.prepared_media.media is not None:
            old_media = data.prepared_media.media
            await old_media.stop()
            registry.release_resource(invite.call_id, f"retired_local_media:{id(old_media):x}", value=old_media)
        return result
    except BaseException:
        # Claim in the original caller before shielding cleanup. The session
        # must not wait for an initiator which is itself awaiting this barrier.
        barrier = registry.request_termination(
            invite.call_id, TerminationIntent(failure_reason), generation=generation,
        )
        if session.cleanup_waits_for(asyncio.current_task()):
            # External termination is already draining this owned startup task.
            # Release only unadopted resources here; the parent closes its media.
            barrier = None
        cleanup = asyncio.create_task(
            _cleanup_failure(barrier), name=f"voip-bridge-rollback-{dest_call_id}",
        )
        await async_wait_for_cleanup(cleanup)
        raise
