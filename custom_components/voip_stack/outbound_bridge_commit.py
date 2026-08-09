"""Atomic ownership handoff for one answered outbound SIP leg."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
from typing import Any

from homeassistant.core import HomeAssistant

from .bridge_manager import async_watch_sip_bridge_destination
from .config import media_capture_enabled
from .dtmf_events import attach_dtmf_event_bridge
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationIntent
from .fsm import CallState, TerminalReason
from .inbound_answer import async_commit_runtime_answer
from .media_ports import release_sip_rtp_port_pair, release_video_media_reservation
from .outbound_attempts import OutboundLeg, async_apply_outbound_video_answer
from .core.sdp import build_answer_directional, first_offered_dtmf_format
from .sip_bridge import (
    build_invite_client_relay,
    build_local_client_relay,
    configure_answered_invite_video_relay,
)
from .runtime_data import sip_endpoint_runtime
from .sip_runtime import send_final_response
from .softphone_termination import async_terminate_sip_bridge_session


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
) -> BridgeCommitResult | None:
    """Commit one winning leg, transfer its resources and watch its dialog."""

    invite = data.invite
    winner = data.winner
    client = winner.client
    dest_call_id = client.dialog_ids.call_id
    watcher_ready = asyncio.Event()

    async def _watch_destination() -> None:
        await watcher_ready.wait()
        await async_watch_sip_bridge_destination(
            hass,
            client=client,
            source_call_id=invite.call_id,
            terminate_sip_bridge=async_terminate_sip_bridge_session,
        )

    watcher = asyncio.create_task(
        _watch_destination(),
        name=f"voip-bridge-destination-{dest_call_id}",
    )

    async def _cancel_watcher() -> None:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    async def _fail_commit(reason: str) -> None:
        await _cancel_watcher()
        await EndpointTerminationHandler(hass).terminate(
            invite.call_id,
            TerminationIntent(reason),
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
    )
    if session is None:
        await _cancel_watcher()
        return None
    if policy.dest_state == CallState.RINGING.value:
        final = await client.wait_for_final()
        if final != CallState.IN_CALL.value or client.dialog is None:
            await _cancel_watcher()
            raise RuntimeError(final)

    video_answer = None
    try:
        if winner.video_relay is not None and client.dialog is not None:
            video_answer = configure_answered_invite_video_relay(
                invite,
                client.dialog,
                winner.video_relay,
                hass=hass,
                enable_transcoding=data.enable_video_transcoding,
            )
            await async_apply_outbound_video_answer(winner, video_answer)
    except BaseException:
        await _fail_commit(TerminalReason.MEDIA_INCOMPATIBLE.value)
        raise

    def _release_ports(_ports: Any) -> None:
        for ports in data.release_port_pairs:
            release_sip_rtp_port_pair(hass, ports)

    relay = None
    try:
        if policy.local_source:
            relay = build_local_client_relay(
                client=client,
                local_host=data.local_ip,
                local_to_relay_format=invite.recv_format,
                relay_to_local_format=invite.send_format,
                source_relay_port=data.source_relay_port,
                dest_relay_port=data.dest_relay_port,
                capture_name=f"{invite.call_id}_{dest_call_id}",
                debug_capture=media_capture_enabled(hass),
                on_release=_release_ports,
            )
        else:
            relay = build_invite_client_relay(
                invite=invite,
                client=client,
                source_relay_port=data.source_relay_port,
                dest_relay_port=data.dest_relay_port,
                debug_capture=media_capture_enabled(hass),
                on_release=_release_ports,
            )
        attach_dtmf_event_bridge(
            hass,
            relay,
            call_id=invite.call_id,
            dest_call_id=dest_call_id,
            caller=policy.caller,
            callee=policy.connected_party,
            client=client,
        )
        if winner.video_relay is not None:
            relay.attach_video_relay(winner.video_relay)
        await relay.start()
    except BaseException:
        await _cancel_watcher()
        registry.forget_bridge_link(invite.call_id)
        await registry.close_leg(
            invite.call_id,
            dest_call_id,
            reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
        )
        if relay is not None:
            await relay.stop()
        elif winner.video_relay is not None:
            await winner.video_relay.stop()
            winner.video_relay = None
        winner.ports.release()
        raise

    for reservation in data.detach_reservations:
        reservation.detach()
    winner.video_relay = None
    sip_endpoint_runtime(hass).attach_client_media_update(
        client, relay, source_call_id=invite.call_id
    )
    registry.attach_relay(invite.call_id, relay)

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
        release_video_media_reservation(
            registry.take_media(invite.call_id, provisional=True)
        )

    try:
        answer = ""
        if not policy.response_already_sent and not policy.local_source:
            answer = build_answer_directional(
                data.local_ip,
                data.local_ip,
                data.source_relay_port,
                invite.send_format,
                invite.recv_format,
                dtmf=first_offered_dtmf_format(invite.remote_sdp),
                remote_sdp=invite.remote_sdp,
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
        )
    except BaseException:
        await _fail_commit(TerminalReason.PROTOCOL_ERROR.value)
        raise
    if not bool(getattr(committed, "committed", committed)):
        await _fail_commit(TerminalReason.PROTOCOL_ERROR.value)
        raise RuntimeError(str(getattr(committed, "reason", "answer_not_committed")))

    result = BridgeCommitResult(
        relay,
        dest_call_id,
        video_answer,
        winner.video_failure_reason,
    )
    watcher_ready.set()
    return result
