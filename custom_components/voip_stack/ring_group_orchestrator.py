"""PBX ring-group call orchestration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from functools import partial
import logging
import secrets
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from .bridge_manager import async_watch_sip_bridge_destination
from .call_scope import pending_routes as _pending_routes
from .config import media_capture_enabled as _media_capture_enabled
from .const import CONF_VIDEO_TRANSCODING, DOMAIN, HA_SOFTPHONE_DEVICE_ID
from .dial_fork import (
    DialDisposition,
    DialForkController,
)
from .dial_plan import RingPolicy
from .dtmf_events import attach_dtmf_event_bridge as _attach_dtmf_event_bridge
from .endpoint_lifecycle import call_registry as _call_registry
from .fsm import (
    CallState,
    TerminalReason,
    sip_failure_response as _sip_failure_response,
)
from .groups import GROUP_TYPE_RING
from .media_ports import (
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    release_media_reservation as _release_media_reservation,
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
)
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
    async_apply_outbound_video_answer,
    async_cleanup_outbound_attempts as _cleanup_outbound_attempts,
    async_close_outbound_leg as _close_outbound_leg,
)
from .pbx_routing import unique_group_members as _unique_group_members
from .phone_endpoint import DEFAULT_ENDPOINT_ID
from .ring_group import (
    publish_browser_candidates_ringing as _publish_browser_candidates_ringing,
    publish_ring_group_origin_state as _publish_ring_group_origin_state,
    settle_browser_candidates as _settle_ring_browser_candidates,
)
from .ring_group_candidates import (
    RingGroupCandidateRuntime,
    RingGroupCandidates,
    async_prepare_ring_group_candidates,
)
from .ring_group_fork import build_ring_group_fork
from .sdp import build_answer_directional, first_offered_dtmf_format
from .session_cleanup import async_cleanup_sip_runtime, async_wait_for_cleanup
from .sip_bridge import (
    build_invite_client_relay,
    build_local_client_relay,
    configure_answered_invite_video_relay,
)
from .sip_runtime import send_final_response as _sip_send_final_response
from .websocket_api import (
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry
    from .sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RingGroupRuntime:
    """Explicit dependencies required by one ring-group call."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    ha_peer_name: Callable[[HomeAssistant], str]
    browser_leg_for_member: Callable[..., BrowserLeg | None]
    logical_endpoint_for_member: Callable[..., Any]
    prepare_outbound_leg: Callable[..., Any]
    attach_client_media_update: Callable[..., None]
    terminate_sip_bridge: Callable[..., Any]


async def run_ring_group_call(
    runtime: RingGroupRuntime,
    invite: SipInvite,
    entry: RosterEntry,
    peers: list[Peer],
    roster_entries: list[RosterEntry],
    *,
    origin_endpoint_id: str = "",
    origin_media_client_id: str = "",
    request_video: bool = False,
    enable_caller_video_send: bool = False,
) -> None:
    hass = runtime.hass
    local_ip = runtime.local_ip
    _ha_peer_name = runtime.ha_peer_name
    _browser_leg_for_member = runtime.browser_leg_for_member
    _logical_endpoint_for_member = runtime.logical_endpoint_for_member
    _prepare_outbound_leg = runtime.prepare_outbound_leg
    _attach_client_media_update = runtime.attach_client_media_update
    _terminate_sip_bridge = runtime.terminate_sip_bridge
    registry = _call_registry(hass)
    origin_endpoint_id = str(origin_endpoint_id or "").strip()
    endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    origin_endpoint = (
        endpoint_registry.get(origin_endpoint_id)
        if endpoint_registry is not None and origin_endpoint_id
        else None
    )
    origin_device_id = str(
        getattr(origin_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
    )
    origin_name = str(
        getattr(origin_endpoint, "name", "") or _ha_peer_name(hass)
    ).strip()
    ha_origin = bool(origin_endpoint_id)
    call_ingress = "trunk" if invite.received_via_trunk else "extension"
    members = _unique_group_members(entry.metadata.get("members"))
    try:
        ring_policy = RingPolicy.from_metadata(entry.metadata)
    except (TypeError, ValueError) as err:
        _LOGGER.error(
            "SIP ring group has invalid policy call_id=%s group=%s: %s",
            invite.call_id,
            entry.display_name,
            err,
        )
        _sip_send_final_response(
            hass,
            invite.call_id,
            500,
            "Server Internal Error",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.PROTOCOL_ERROR.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        return
    candidates = RingGroupCandidates()
    attempts = candidates.attempts
    browser_legs = candidates.browser_legs
    preflight_failures = candidates.preflight_failures
    session = registry.sessions.get(registry.resolve_session_id(invite.call_id))
    if session is None or not registry.is_generation_current(
        invite.call_id,
        session.generation,
    ):
        _LOGGER.info(
            "SIP ring group did not start for a terminated source call_id=%s",
            invite.call_id,
        )
        return
    call_generation = session.generation

    def _call_is_current() -> bool:
        return registry.is_generation_current(
            invite.call_id,
            call_generation,
        )

    source_endpoint_id = str(
        origin_endpoint_id
        or ((session.metadata if session is not None else {}) or {}).get(
            "source_endpoint_id"
        )
        or ""
    ).strip()

    def _settle_browser_candidates(
        state: str,
        reason: str,
        *,
        keep_endpoint_id: str = "",
    ) -> None:
        """Release and publish every browser candidate except the winner."""
        _settle_ring_browser_candidates(
            hass,
            registry,
            browser_legs,
            call_id=invite.call_id,
            caller=invite.caller,
            callee=entry.display_name,
            state=state,
            reason=reason,
            route_kind=GROUP_TYPE_RING,
            keep_endpoint_id=keep_endpoint_id,
        )

    try:
        await async_prepare_ring_group_candidates(
            candidates,
            RingGroupCandidateRuntime(
                registry=registry,
                endpoint_registry=endpoint_registry,
                browser_leg_for_member=_browser_leg_for_member,
                logical_endpoint_for_member=_logical_endpoint_for_member,
                prepare_outbound_leg=_prepare_outbound_leg,
            ),
            invite=invite,
            group_name=entry.display_name,
            members=members,
            peers=peers,
            roster_entries=roster_entries,
            ring_policy=ring_policy,
            source_endpoint_id=source_endpoint_id,
            local_name=invite.caller or _ha_peer_name(hass),
        )
        if not _call_is_current():
            _settle_browser_candidates(
                CallState.CANCELLED.value,
                TerminalReason.CANCELLED.value,
            )
            await _cleanup_outbound_attempts([], attempts)
            return
    except asyncio.CancelledError:
        _settle_browser_candidates(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
        )
        await _cleanup_outbound_attempts([], attempts)
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.CANCELLED.value,
            state=CallState.CANCELLED.value,
        )
        raise
    except Exception as err:
        _LOGGER.exception(
            "SIP ring group candidate preparation failed call_id=%s: %s",
            invite.call_id,
            err,
        )
        _settle_browser_candidates(
            CallState.TRANSPORT_UNREACHABLE.value,
            TerminalReason.PROTOCOL_ERROR.value,
        )
        await _cleanup_outbound_attempts([], attempts)
        _sip_send_final_response(
            hass,
            invite.call_id,
            500,
            "Server Internal Error",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.PROTOCOL_ERROR.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        return
    route_future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_routes(hass)[invite.call_id] = {
        "invite": invite,
        "future": route_future,
        "ring_group_endpoint_ids": tuple(
            leg.endpoint_id for leg in browser_legs
        ),
        "declined_endpoint_ids": set(),
    }
    try:
        _publish_browser_candidates_ringing(
            hass,
            registry,
            browser_legs,
            invite=invite,
            callee=entry.display_name,
            route_kind=GROUP_TYPE_RING,
            origin_endpoint_id=(
                origin_endpoint_id if ha_origin else ""
            ),
            source_endpoint_id=source_endpoint_id,
            origin_media_client_id=origin_media_client_id,
        )
    except asyncio.CancelledError:
        _pending_routes(hass).pop(invite.call_id, None)
        _settle_browser_candidates(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
        )
        await _cleanup_outbound_attempts([], attempts)
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.CANCELLED.value,
            state=CallState.CANCELLED.value,
        )
        raise
    except Exception as err:
        _LOGGER.exception(
            "SIP ring group state publication failed call_id=%s: %s",
            invite.call_id,
            err,
        )
        _pending_routes(hass).pop(invite.call_id, None)
        _settle_browser_candidates(
            CallState.TRANSPORT_UNREACHABLE.value,
            TerminalReason.PROTOCOL_ERROR.value,
        )
        await _cleanup_outbound_attempts([], attempts)
        _sip_send_final_response(
            hass,
            invite.call_id,
            500,
            "Server Internal Error",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.PROTOCOL_ERROR.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        return
    if not attempts and not browser_legs and not preflight_failures:
        _pending_routes(hass).pop(invite.call_id, None)
        _publish_ring_group_origin_state(
            hass,
            enabled=ha_origin,
            state=CallState.TRANSPORT_UNREACHABLE.value,
            endpoint_id=origin_endpoint_id,
            device_id=origin_device_id,
            caller=origin_name,
            callee=entry.display_name,
            peer_name=entry.display_name,
            call_id=invite.call_id,
            reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            origin="remote",
            route_kind=GROUP_TYPE_RING,
            last_sip_event="SIP_RESPONSE",
            sip_status_code=480,
        )
        _sip_send_final_response(
            hass,
            invite.call_id,
            480,
            "Temporarily Unavailable",
            decline_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
        )
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        return

    (
        fork_candidates,
        candidate_payloads,
        browser_decision,
    ) = build_ring_group_fork(
        sip_port=int(runtime.config["sip_port"]),
        route_future=route_future,
        attempts=attempts,
        browser_legs=browser_legs,
        preflight_failures=preflight_failures,
    )

    # DialForkController owns every branch task and its loser cleanup
    # barrier.  Keep this compatibility list empty for later rollback
    # helpers, which may still close the selected branch after media setup
    # fails but must never own the fork tasks themselves.
    tasks: list[asyncio.Task] = []

    async def _cleanup_ring_resources(reason: str) -> None:
        """Tear down every ownership layer after an aborted group call."""
        _pending_routes(hass).pop(invite.call_id, None)
        (
            _source_call_id,
            _dest_call_id,
            relay,
            bridge_client,
            watcher,
            _called_by_dest,
        ) = registry.detach_bridge(invite.call_id)
        if relay is not None or bridge_client is not None:
            current = asyncio.current_task()
            cleanup = asyncio.create_task(
                async_cleanup_sip_runtime(
                    relay=relay,
                    client=bridge_client,
                    watcher=(watcher if watcher is not current else None),
                    terminate_client=True,
                    relay_first=True,
                ),
                name=f"voip-ring-group-bridge-cleanup-{invite.call_id}",
            )
            await async_wait_for_cleanup(cleanup)
        remaining_attempts = [
            attempt
            for attempt in attempts
            if attempt.client is not bridge_client
        ]
        await _cleanup_outbound_attempts(tasks, remaining_attempts)
        active_media = registry.take_media(invite.call_id)
        _release_media_reservation(active_media)

        # HA-to-HA ring groups switch to the transport-neutral local bridge
        # after selection.  If answer publication then fails, terminate
        # that newly-created call as part of the same rollback boundary.
        from .local_softphone_runtime import local_softphone_bridge

        local_bridge = local_softphone_bridge(hass)
        local_call = (
            local_bridge.get_call(invite.call_id)
            if local_bridge is not None
            else None
        )
        if local_call is not None:
            with contextlib.suppress(Exception):
                local_bridge.hangup(
                    invite.call_id,
                    local_call.caller_endpoint_id,
                )
        registry.finish_and_pop(
            invite.call_id,
            reason=reason,
            state=(
                CallState.CANCELLED.value
                if reason == TerminalReason.CANCELLED.value
                else CallState.TRANSPORT_UNREACHABLE.value
            ),
        )

    async def _abort_stale_ring_group() -> bool:
        """Close every fork if the source generation lost ownership."""

        if _call_is_current():
            return False
        _settle_browser_candidates(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
        )
        await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
        return True

    winner: OutboundLeg | BrowserLeg | dict | None = None
    browser_winner = False
    reroute_decision: dict[str, Any] | None = None
    final_result = "timeout"
    try:
        pbx_runtime = hass.data.get(DOMAIN, {}).get("pbx_runtime")
        authoritative_session = (
            pbx_runtime.get_session(
                invite.call_id,
                generation=call_generation,
            )
            if pbx_runtime is not None
            else None
        )
        if authoritative_session is None:
            await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
            return
        fork_result = await DialForkController(
            authoritative_session,
            fork_candidates,
            strategy=ring_policy.strategy,
            tier_strategies=ring_policy.tier_strategies,
            overall_timeout=ring_policy.overall_timeout,
            step_timeout=ring_policy.step_timeout,
        ).run(
            lambda _candidate, _dial_outcome: _call_is_current()
        )
        if fork_result.winner is not None:
            winner = candidate_payloads.get(
                fork_result.winner.candidate_id
            )
            browser_winner = isinstance(winner, BrowserLeg)
        elif fork_result.outcome.disposition is DialDisposition.REROUTE:
            reroute_decision = dict(browser_decision)
        final_result = {
            DialDisposition.BUSY: "busy",
            DialDisposition.DND: "dnd",
            DialDisposition.DECLINED: "declined",
            DialDisposition.TIMEOUT: "timeout",
            DialDisposition.MEDIA_INCOMPATIBLE: "media_incompatible",
            DialDisposition.AUTH_FAILED: "auth_required_unsupported",
            DialDisposition.CANCELLED: "cancelled",
            DialDisposition.SOURCE_CANCELLED: "cancelled",
            DialDisposition.PROTOCOL_ERROR: "protocol_error",
            DialDisposition.UNAVAILABLE: "transport_unreachable",
        }.get(fork_result.outcome.disposition, final_result)
        if await _abort_stale_ring_group():
            return
        winner_endpoint_id = str(
            getattr(winner, "endpoint_id", "") or ""
        )
        for losing_endpoint_id in {
            attempt.endpoint_id
            for attempt in attempts
            if attempt.endpoint_id
            and attempt.endpoint_id != winner_endpoint_id
        }:
            registry.release_endpoint_claim(
                invite.call_id,
                losing_endpoint_id,
            )
        if await _abort_stale_ring_group():
            return
        if reroute_decision is not None:
            route = _pending_routes(hass).pop(invite.call_id, None) or {}
            _settle_browser_candidates(
                CallState.IDLE.value,
                "forwarded",
            )
            handoff = route.get("forward_handoff")
            if handoff is not None and not handoff.done():
                handoff.set_result(dict(reroute_decision))
            return
        if winner is not None:
            candidate_state = CallState.CANCELLED.value
            candidate_reason = TerminalReason.CANCELLED.value
        else:
            (
                _candidate_status,
                _candidate_sip_reason,
                candidate_reason,
                candidate_state,
            ) = _sip_failure_response(final_result)
        _settle_browser_candidates(
            candidate_state,
            candidate_reason,
            keep_endpoint_id=(
                winner.endpoint_id
                if browser_winner and isinstance(winner, BrowserLeg)
                else ""
            ),
        )
        if winner is None:
            _pending_routes(hass).pop(invite.call_id, None)
            status_code, sip_reason, terminal_reason, public_state = (
                _sip_failure_response(final_result)
            )
            _publish_ring_group_origin_state(
                hass,
                enabled=ha_origin,
                state=public_state,
                endpoint_id=origin_endpoint_id,
                device_id=origin_device_id,
                caller=origin_name,
                callee=entry.display_name,
                peer_name=entry.display_name,
                call_id=invite.call_id,
                reason=terminal_reason,
                origin="remote",
                route_kind=GROUP_TYPE_RING,
                last_sip_event="SIP_RESPONSE",
                sip_status_code=status_code,
            )
            _sip_send_final_response(
                hass,
                invite.call_id,
                status_code,
                sip_reason,
                decline_reason=terminal_reason,
            )
            _set_sip_bridge_call_state(
                hass,
                public_state,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.target,
                call_id=invite.call_id,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                sip_status_code=status_code,
                last_sip_event="SIP_RESPONSE",
                route_kind=GROUP_TYPE_RING,
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=terminal_reason,
                state=public_state,
            )
            return
        if browser_winner and isinstance(winner, BrowserLeg):
            if await _abort_stale_ring_group():
                return
            _pending_routes(hass).pop(invite.call_id, None)
            connected_party = winner.name
            winner_media_client_id = str(
                browser_decision.get("media_client_id") or ""
            ).strip()
            if ha_origin:
                from .local_softphone_runtime import (
                    local_softphone_bridge,
                    start_local_softphone_call,
                )

                original_context = registry.ha_context(invite.call_id)
                if await _abort_stale_ring_group():
                    return
                await registry.finish_and_pop_wait(
                    invite.call_id,
                    reason="local_group_selected",
                    state=CallState.IDLE.value,
                )
                snapshot = start_local_softphone_call(
                    hass,
                    origin_endpoint_id,
                    winner.endpoint_id,
                    call_id=invite.call_id,
                    request_video=request_video,
                    enable_caller_video_send=enable_caller_video_send,
                    caller_owner_id=origin_media_client_id,
                    context=original_context,
                )
                bridge = local_softphone_bridge(hass)
                if bridge is None:
                    raise RuntimeError("local softphone bridge is unavailable")
                bridge.answer(
                    snapshot.call_id,
                    winner.endpoint_id,
                    winner_media_client_id,
                    enable_video_send=bool(
                        browser_decision.get("send_video", False)
                    ),
                )
                return
            local_rtp_port = _allocate_sip_rtp_port(hass)
            answer = build_answer_directional(
                local_ip,
                local_ip,
                local_rtp_port,
                invite.send_format,
                invite.recv_format,
                dtmf=first_offered_dtmf_format(invite.remote_sdp),
                remote_sdp=invite.remote_sdp,
            )
            committed = registry.transition(
                invite.call_id,
                state=CallState.IN_CALL.value,
                owner="ha_softphone",
                caller=invite.caller,
                callee=entry.display_name,
                route_kind=GROUP_TYPE_RING,
                expected_generation=call_generation,
                endpoint_id=winner.endpoint_id,
                dest_endpoint_id=winner.endpoint_id,
                media_client_id=winner_media_client_id,
            )
            if committed is None:
                await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
                return
            media = {
                "invite": invite,
                "local_rtp_port": local_rtp_port,
                "endpoint_id": winner.endpoint_id,
                "media_client_id": winner_media_client_id,
            }
            registry.pending_invites.pop(invite.call_id, None)
            registry.attach_media(invite.call_id, media)
            registry.add_leg(
                invite.call_id,
                f"browser:{winner.endpoint_id}",
                role="ha_softphone",
                state=CallState.IN_CALL.value,
            )
            if not _sip_send_final_response(
                hass, invite.call_id, 200, "OK", answer_sdp=answer
            ):
                await _cleanup_ring_resources(
                    TerminalReason.CANCELLED.value
                    if not _call_is_current()
                    else TerminalReason.PROTOCOL_ERROR.value
                )
                return
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                endpoint_id=winner.endpoint_id,
                session_device_id=winner.device_id,
                caller=invite.caller,
                callee=entry.display_name,
                peer_name=invite.caller,
                direction="incoming",
                call_id=invite.call_id,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                audio_mode="full_duplex",
                route_kind=GROUP_TYPE_RING,
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
            )
            # Mirror the same established-call contract used when a SIP
            # endpoint wins: retain the group as dialed target and expose
            # the HA softphone as the party that actually answered.
            _set_sip_bridge_call_state(
                hass,
                CallState.IN_CALL.value,
                caller=invite.caller,
                callee=entry.display_name,
                peer_name=connected_party,
                call_id=invite.call_id,
                dialed_target=entry.display_name,
                connected_party=connected_party,
                answered_by=connected_party,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                route_kind=GROUP_TYPE_RING,
            )
            return
        _pending_routes(hass).pop(invite.call_id, None)
        if not isinstance(winner, OutboundLeg):
            _LOGGER.error(
                "SIP ring group selected an invalid winner for call_id=%s",
                invite.call_id,
            )
            _sip_send_final_response(
                hass,
                invite.call_id,
                500,
                "Server Internal Error",
                decline_reason=TerminalReason.PROTOCOL_ERROR.value,
            )
            _publish_ring_group_origin_state(
                hass,
                enabled=ha_origin,
                state=CallState.TRANSPORT_UNREACHABLE.value,
                endpoint_id=origin_endpoint_id,
                device_id=origin_device_id,
                caller=origin_name,
                callee=entry.display_name,
                peer_name=entry.display_name,
                call_id=invite.call_id,
                reason=TerminalReason.PROTOCOL_ERROR.value,
                origin="self",
                route_kind=GROUP_TYPE_RING,
                last_sip_event="SIP_RESPONSE",
                sip_status_code=500,
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.PROTOCOL_ERROR.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return
        client = winner.client
        source_relay_port, dest_relay_port = winner.ports.ports
        video_answer = None
        if winner.video_relay is not None and client.dialog is not None:
            video_answer = configure_answered_invite_video_relay(
                invite,
                client.dialog,
                winner.video_relay,
                hass=hass,
                enable_transcoding=bool(
                    runtime.config.get(CONF_VIDEO_TRANSCODING, False)
                ),
            )
            await async_apply_outbound_video_answer(winner, video_answer)
            if video_answer is None:
                _LOGGER.info(
                    "SIP ring group video rejected by winning branch "
                    "call_id=%s member=%s",
                    invite.call_id,
                    winner.member,
                )
        bridge_session = registry.register_bridge(
            source_call_id=invite.call_id,
            dest_call_id=client.dialog_ids.call_id,
            client=client,
            lifecycle_task=asyncio.current_task(),
            state=CallState.CONNECTING.value,
            caller=invite.caller,
            callee=invite.target,
            route_kind=GROUP_TYPE_RING,
            ingress=call_ingress,
            origin=call_ingress,
            source_state=CallState.CONNECTING.value,
            dest_state=CallState.IN_CALL.value,
            expected_generation=call_generation,
        )
        if bridge_session is None:
            await _close_outbound_leg(winner, bye_or_cancel=True)
            return
        try:
            if ha_origin:
                relay = build_local_client_relay(
                    client=client,
                    local_host=local_ip,
                    local_to_relay_format=invite.recv_format,
                    relay_to_local_format=invite.send_format,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    capture_name=f"{invite.call_id}_{client.dialog_ids.call_id}",
                    debug_capture=_media_capture_enabled(hass),
                    on_release=lambda ports: _release_sip_rtp_port_pair(
                        hass, ports
                    ),
                )
            else:
                relay = build_invite_client_relay(
                    invite=invite,
                    client=client,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    debug_capture=_media_capture_enabled(hass),
                    on_release=lambda ports: _release_sip_rtp_port_pair(
                        hass, ports
                    ),
                )
            _attach_dtmf_event_bridge(
                hass,
                relay,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                caller=invite.caller,
                callee=str(winner.member or invite.target),
                client=client,
            )
            if winner.video_relay is not None:
                relay.attach_video_relay(winner.video_relay)
            await relay.start()
        except Exception as err:
            _LOGGER.warning("SIP ring group media bridge unavailable: %s", err)
            _sip_send_final_response(
                hass,
                invite.call_id,
                488,
                "Not Acceptable Here",
                decline_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
            )
            _publish_ring_group_origin_state(
                hass,
                enabled=ha_origin,
                state=CallState.MEDIA_INCOMPATIBLE.value,
                endpoint_id=origin_endpoint_id,
                device_id=origin_device_id,
                caller=origin_name,
                callee=entry.display_name,
                peer_name=str(winner.member or entry.display_name),
                call_id=invite.call_id,
                reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                origin="self",
                route_kind=GROUP_TYPE_RING,
                last_sip_event="SIP_RESPONSE",
                sip_status_code=488,
            )
            registry.discard_bridge_session(
                invite.call_id,
                client.dialog_ids.call_id,
                reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                state=CallState.MEDIA_INCOMPATIBLE.value,
            )
            await _close_outbound_leg(winner)
            return
        if not _call_is_current():
            await relay.stop()
            await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
            return
        winner.ports.detach()
        if winner.video_relay is not None:
            # The audio relay now owns and tears down the video relay.
            winner.video_relay = None
        _attach_client_media_update(
            client,
            relay,
            source_call_id=invite.call_id,
        )
        dialed_target = entry.display_name or invite.target
        connected_party = str(winner.member or "").strip() or invite.target
        if ha_origin:
            # The synthetic HA caller has no SIP/RTP socket of its own.
            # Feed the already-running source side of the relay from the
            # authenticated browser websocket via a local UDP endpoint.
            softphone_media = {
                "rtp_loopback": True,
                "remote_rtp_host": local_ip,
                "remote_rtp_port": source_relay_port,
                "send_format": invite.recv_format,
                "recv_format": invite.send_format,
                "local_ssrc": secrets.randbelow(0xFFFFFFFF) + 1,
                "endpoint_id": origin_endpoint_id,
            }
        else:
            softphone_media = None
        committed = registry.transition(
            invite.call_id,
            state=CallState.IN_CALL.value,
            owner="ha_softphone",
            caller=invite.caller,
            callee=dialed_target,
            route_kind=GROUP_TYPE_RING,
            endpoint_id=origin_endpoint_id if ha_origin else "",
            source_endpoint_id=source_endpoint_id,
            dest_endpoint_id=winner.endpoint_id,
            media_client_id=origin_media_client_id,
            expected_generation=call_generation,
        )
        if committed is None:
            await relay.stop()
            await _close_outbound_leg(winner, bye_or_cancel=True)
            return
        registry.attach_relay(invite.call_id, relay)
        if softphone_media is not None:
            registry.attach_media(invite.call_id, softphone_media)
        if not ha_origin:
            answer = build_answer_directional(
                local_ip,
                local_ip,
                source_relay_port,
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
                    video_answer.video_format
                    if video_answer is not None
                    else None
                ),
                video_direction=(
                    video_answer.direction
                    if video_answer is not None
                    else "inactive"
                ),
            )
            if not _sip_send_final_response(
                hass,
                invite.call_id,
                200,
                "OK",
                answer_sdp=answer,
            ):
                await _cleanup_ring_resources(
                    TerminalReason.CANCELLED.value
                    if not _call_is_current()
                    else TerminalReason.PROTOCOL_ERROR.value
                )
                return
        _set_sip_bridge_call_state(
            hass,
            CallState.IN_CALL.value,
            caller=invite.caller,
            callee=dialed_target,
            peer_name=connected_party,
            call_id=invite.call_id,
            dest_call_id=client.dialog_ids.call_id,
            dialed_target=dialed_target,
            connected_party=connected_party,
            answered_by=connected_party,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            route_kind=GROUP_TYPE_RING,
            sip_uri=str(winner.uri),
        )
        if ha_origin:
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                endpoint_id=origin_endpoint_id,
                session_device_id=origin_device_id,
                caller=invite.caller,
                callee=dialed_target,
                peer_name=connected_party,
                direction="outgoing",
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                dialed_target=dialed_target,
                connected_party=connected_party,
                answered_by=connected_party,
                selected_tx_format=invite.recv_format.audio_format.wire_token(),
                selected_rx_format=invite.send_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.recv_format.wire_token(),
                selected_rx_rtp_format=invite.send_format.wire_token(),
                audio_mode="full_duplex",
                route_kind=GROUP_TYPE_RING,
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                sip_uri=str(winner.uri),
            )
        await async_watch_sip_bridge_destination(
            hass,
            client=client,
            source_call_id=invite.call_id,
            terminate_sip_bridge=partial(
                _terminate_sip_bridge,
                endpoint_id=(
                    origin_endpoint_id if ha_origin else DEFAULT_ENDPOINT_ID
                ),
                session_device_id=(
                    origin_device_id if ha_origin else HA_SOFTPHONE_DEVICE_ID
                ),
            ),
        )
    except asyncio.CancelledError:
        _settle_browser_candidates(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
        )
        with contextlib.suppress(Exception):
            _publish_ring_group_origin_state(
                hass,
                enabled=ha_origin,
                state=CallState.CANCELLED.value,
                endpoint_id=origin_endpoint_id,
                device_id=origin_device_id,
                caller=origin_name,
                callee=entry.display_name,
                peer_name=entry.display_name,
                call_id=invite.call_id,
                reason=TerminalReason.CANCELLED.value,
                origin="self",
                route_kind=GROUP_TYPE_RING,
                last_sip_event="CANCEL",
            )
        await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
        raise
    except Exception as err:
        _LOGGER.exception(
            "SIP ring group runtime failed call_id=%s: %s",
            invite.call_id,
            err,
        )
        _settle_browser_candidates(
            CallState.TRANSPORT_UNREACHABLE.value,
            TerminalReason.PROTOCOL_ERROR.value,
        )
        with contextlib.suppress(Exception):
            _publish_ring_group_origin_state(
                hass,
                enabled=ha_origin,
                state=CallState.TRANSPORT_UNREACHABLE.value,
                endpoint_id=origin_endpoint_id,
                device_id=origin_device_id,
                caller=origin_name,
                callee=entry.display_name,
                peer_name=entry.display_name,
                call_id=invite.call_id,
                reason=TerminalReason.PROTOCOL_ERROR.value,
                origin="self",
                route_kind=GROUP_TYPE_RING,
                last_sip_event="SIP_RESPONSE",
                sip_status_code=500,
            )
        _sip_send_final_response(
            hass,
            invite.call_id,
            500,
            "Server Internal Error",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
        await _cleanup_ring_resources(TerminalReason.PROTOCOL_ERROR.value)
