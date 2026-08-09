"""Forwarding orchestration for an existing HA-owned SIP call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
import logging
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant

from .core.audio_format import HA_TRUNK_AUDIO_FORMATS
from .config import trunk_config as _get_trunk_config
from .call_projection import publish_bridge_projection
from .const import (
    CONF_SIP_VIDEO,
    CONF_VIDEO_TRANSCODING,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
)
from .dial_fork import DialDisposition, DialForkController, terminal_reason
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .runtime_data import (
    call_runtime_artifacts,
    browser_phone,
    endpoint_directory,
    preferred_browser_phone,
    sip_endpoint_runtime,
)
from .endpoint_routing import (
    EndpointRouteResolver,
    peer_audio_formats as _peer_audio_formats,
    peer_for_target as _peer_for_target,
    peer_video_codec as _peer_video_codec,
    roster_entry_formats as _roster_entry_formats,
    roster_from_peers as _roster_from_peers,
    sip_target_audio_profile as _sip_target_audio_profile,
)
from .group_candidates import (
    GroupCandidateRuntime,
    GroupCandidates,
    async_prepare_group_candidates,
)
from .fsm import (
    CallState,
    TerminalReason,
)
from .groups import GROUP_TYPE_RING
from .inbound_answer import (
    AnswerCommitResult,
    async_commit_runtime_answer,
)
from .media_ports import (
    RtpPortReservation,
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
    reserve_sip_video_relay_media,
)
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
)
from .outbound_bridge_commit import (
    BridgeCommitData,
    BridgeCommitPolicy,
    async_commit_outbound_bridge,
)
from .pbx_routing import unique_group_members as _unique_group_members
from .phone_endpoint import EndpointAvailability, EndpointKind
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .ring_group import (
    settle_browser_candidates as _settle_ring_browser_candidates,
)
from .ring_group_fork import build_ring_group_fork
from .router import RouteAction
from .route_abort import (
    RouteAbortContext,
    RouteAbortIntent,
    async_abort_route,
)
from .core.sdp import build_answer_directional
from .session_cleanup import async_cleanup_sip_runtime
from .service_errors import service_error as _service_error
from .core.sip import parse_sip_uri
from .sip_bridge import (
    build_pending_invite_video_relay,
    video_bridge_offer_formats,
)
from .sip_client import SIP_TIMER_B, SipCallClient
from .sip_runtime import (
    enable_reused_tcp_connection as _enable_reused_sip_tcp_connection,
    send_final_response as _sip_send_final_response,
    uri_transport as _sip_uri_transport,
)
from .peer_snapshot import async_build_peer_snapshot as _async_build_peer_snapshot
from .websocket_api import (
    _ha_peer_name,
    _release_ha_softphone_claim,
)

_LOGGER = logging.getLogger(__name__)


def _source_dialog_is_answered(early_media: dict | None) -> bool:
    return early_media is not None and bool(
        early_media.get("final_response_sent", True)
    )


@dataclass(frozen=True, slots=True)
class ForwardRuntime:
    """Explicit endpoint-owned operations used by call forwarding."""

    hass: HomeAssistant
    config: dict
    local_ip: str
    route_resolver: EndpointRouteResolver
    attach_client_media_update: Callable[..., None]
    browser_leg_for_member: Callable[..., Any]
    defer_invite_to_softphone: Callable[..., None]
    prepare_outbound_leg: Callable[..., Any]
    publish_pending_ringing: Callable[..., None]
    sip_uri_for_member: Callable[..., Any]
    start_local_assist_bridge: Callable[..., Awaitable[Any]]


async def async_forward_existing_call(
    runtime: ForwardRuntime,
    *,
    call_id: str,
    destination: str,
    on_failure: str = "resume",
    expected_state: str = "",
    expected_sequence: int = 0,
    initial_selection: bool = False,
) -> None:
    """Route or move one HA-owned pending/ringing call to a target.

    ``initial_selection`` is used only by the bounded ``route_requested``
    decision point. Unlike a later forward, it must not exclude browser
    phones from a ring group merely because the pre-answered trunk dialog
    is temporarily anchored on the default HA phone.
    """

    hass = runtime.hass
    cfg = runtime.config
    local_ip = runtime.local_ip
    call_id = str(call_id or "").strip()
    destination = str(destination or "").strip()
    on_failure = str(on_failure or "resume").strip().lower()
    if not call_id or not destination:
        raise _service_error(
            "call_id and destination are required",
            "call_id_destination_required",
        )
    if on_failure not in {"resume", "terminate", "busy"}:
        raise _service_error(
            "on_failure must be resume, terminate, or busy",
            "forward_failure_policy_invalid",
        )

    registry = _call_registry(hass)
    expected_state = str(expected_state or "").strip().lower()

    def _validate_snapshot():
        current = registry.event_context(call_id)
        if expected_state and (current is None or current.state != expected_state):
            actual = current.state if current is not None else "ended"
            raise _service_error(
                f"call_id {call_id} is {actual}, expected {expected_state}",
                "call_state_mismatch",
                call_id=call_id,
                actual=actual,
                expected=expected_state,
            )
        if expected_sequence and (
            current is None or current.sequence != int(expected_sequence)
        ):
            actual = current.sequence if current is not None else 0
            raise _service_error(
                f"call_id {call_id} sequence is {actual}, expected {expected_sequence}",
                "call_sequence_mismatch",
                call_id=call_id,
                actual=actual,
                expected=expected_sequence,
            )
        return current

    context = _validate_snapshot()
    if context is not None and len(context.route_history) >= 8:
        raise _service_error(
            f"call_id {call_id} exceeded 8 routing hops",
            "route_hop_limit_exceeded",
            call_id=call_id,
            limit=8,
        )

    invite = registry.artifact_for(call_id, "pending_invite")
    if invite is None:
        raise _service_error(
            f"call_id {call_id} is not a forwardable pending or ringing HA-owned call",
            "call_not_forwardable",
            call_id=call_id,
        )
    artifacts = call_runtime_artifacts(hass)
    call_artifacts = artifacts.artifacts_for(call_id)
    if call_artifacts is None:
        raise _service_error(
            f"call_id {call_id} is no longer active",
            "call_inactive",
            call_id=call_id,
        )
    current_forward = artifacts.task_for(call_id, "forward")
    if current_forward is not None and not current_forward.done():
        current_context = registry.event_context(call_id)
        if (
            current_context is None
            or current_context.state != CallState.REMOTE_RINGING.value
        ):
            raise _service_error(
                f"call_id {call_id} is already being forwarded",
                "call_forwarding",
                call_id=call_id,
            )
        current_forward.cancel()
        await asyncio.gather(current_forward, return_exceptions=True)
    if call_artifacts.forward_claim:
        raise _service_error(
            f"call_id {call_id} is already being forwarded",
            "call_forwarding",
            call_id=call_id,
        )
    call_artifacts.forward_claim = True
    target_browser_endpoint = None
    try:
        peers = await _async_build_peer_snapshot(hass)
        if registry.artifact_for(call_id, "pending_invite") is not invite:
            raise _service_error(
                f"call_id {call_id} changed while the route was being resolved",
                "call_changed_during_operation",
                call_id=call_id,
            )
        context = _validate_snapshot()
        roster_entries = _roster_from_peers(
            hass,
            peers,
            _registered_roster_entries(hass),
        )
        decision = runtime.route_resolver.route(destination, roster_entries)
        if decision.action is RouteAction.ANSWER_HA:
            target_browser_endpoint = runtime.route_resolver.logical_endpoint(
                decision.target or destination,
                peers,
                roster_entries,
            )
            if (
                target_browser_endpoint is None
                or target_browser_endpoint.kind is not EndpointKind.BROWSER
            ):
                raise _service_error(
                    f"destination {destination} is not a configured Home Assistant phone",
                    "destination_not_ha_phone",
                    destination=destination,
                )
        if decision.action is RouteAction.REJECT:
            raise _service_error(
                f"destination {destination} is not a forwardable SIP dial-plan target",
                "destination_not_forwardable",
                destination=destination,
            )
        if (
            decision.action is RouteAction.GROUP
            and str(
                (
                    (decision.entry.metadata if decision.entry is not None else {})
                    or {}
                ).get("group_type")
                or ""
            )
            != GROUP_TYPE_RING
        ):
            raise _service_error(
                "forwarding an already-ringing call is currently limited to ring groups",
                "forward_ring_group_only",
            )

        last_route = (
            context.route_history[-1] if context and context.route_history else {}
        )
        if not (
            last_route.get("action") in {"forward", "bridge"}
            and last_route.get("destination") == destination
        ):
            registry.record_route(
                call_id,
                action="forward",
                destination=destination,
                source="automation",
            )
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        session_endpoint = browser_phone(
            hass,
            str(
                (session.metadata if session is not None else {}).get("endpoint_id")
                or ""
            ),
        )
        if session_endpoint is None and initial_selection:
            session_endpoint = preferred_browser_phone(hass)
        if session_endpoint is None:
            raise _service_error(
                f"call_id {call_id} has no Home Assistant phone owner",
                "call_phone_owner_missing",
                call_id=call_id,
            )
        session_endpoint_id = session_endpoint.endpoint_id
        if (
            not initial_selection
            and target_browser_endpoint is not None
            and target_browser_endpoint.endpoint_id == session_endpoint_id
        ):
            raise _service_error(
                "a Home Assistant phone cannot forward a call to itself",
                "phone_self_target",
            )
        session_device_id = session_endpoint.device_id
        original_callee = session.callee if session is not None else invite.target
        original_route_kind = session.route_kind if session is not None else ""
        # A trunk call is already SIP-answered while its DTMF/automation
        # route is being selected.  It has not populated the HA softphone
        # store yet, but HA is still its default owner.  Preserve that
        # ownership so ``on_failure: resume`` can enter normal ringing
        # instead of leaving the answered caller on silent RTP.
        ha_claimed = (
            bool(session is not None and session.owner == "ha_softphone")
            or registry.resource_for(call_id, "preanswered") is not None
            or bool(
                session is not None and session.metadata.get("automation_resume_ha")
            )
        )
        if session is not None:
            session.metadata["automation_resume_ha"] = ha_claimed
            route_already_claimed = bool(
                session.state == CallState.CONNECTING.value
                and session.owner == "router"
                and session.callee == destination
            )
            if not route_already_claimed:
                claimed = registry.transition(
                    call_id,
                    state=CallState.CONNECTING.value,
                    owner="router",
                    callee=destination,
                    route_kind=decision.action.value,
                    expected_revision=session.revision,
                    expected_owner=session.owner,
                    automation_resume_ha=ha_claimed,
                )
                if claimed is None:
                    raise _service_error(
                        f"call_id {call_id} changed while forwarding ownership was claimed",
                        "call_changed_during_operation",
                        call_id=call_id,
                    )
        else:
            route_already_claimed = False
        _release_ha_softphone_claim(
            hass,
            call_id,
            destination=destination,
            endpoint_id=session_endpoint_id,
        )
        if not route_already_claimed:
            if session is not None:
                publish_bridge_projection(
                    hass,
                    session,
                    peer_name=destination,
                    direction="incoming",
                    route_source="automation",
                    route_kind=decision.action.value,
                    event_type="forwarding",
                    last_sip_event="ROUTE_FORWARD",
                )
    except Exception:
        call_artifacts.forward_claim = False
        raise

    forward_abort = RouteAbortContext(
        hass,
        registry,
        call_id,
        consume_source=True,
        resume_callee=original_callee,
        resume_route_kind=original_route_kind,
        transition_resume=session is not None,
        publish_resume=partial(
            runtime.publish_pending_ringing,
            invite,
            route_kind=original_route_kind,
            endpoint_id=session_endpoint_id,
            endpoint_device_id=session_device_id,
            callee=original_callee,
            last_sip_event="ROUTE_RESUME",
        )
        if ha_claimed
        else None,
    )

    async def _commit_source_answer(
        answer_sdp: str,
        *,
        owner: str,
        callee: str,
        route_kind: str,
        response_already_sent: bool,
    ) -> AnswerCommitResult:
        return await async_commit_runtime_answer(
            registry,
            call_id,
            answer_sdp,
            send_final_response=_sip_send_final_response,
            response_context=hass,
            owner=owner,
            caller=invite.caller,
            callee=callee,
            route_kind=route_kind,
            response_already_sent=response_already_sent,
        )

    async def _run_forward() -> None:
        client = None
        reservation = None
        reservation_from_preanswer = False
        video_relay = None
        dest_call_id = ""

        async def _cleanup_failed_route(reason: str) -> None:
            nonlocal video_relay
            attached_client = False
            if dest_call_id:
                registry.forget_bridge_link(call_id)
                attached_client = await registry.close_leg(
                    call_id,
                    dest_call_id,
                    reason=reason,
                )
            if reservation is not None and not reservation_from_preanswer:
                reservation.release()
            if video_relay is not None:
                await video_relay.stop()
                video_relay = None
            if client is not None and not attached_client:
                await async_cleanup_sip_runtime(
                    client=client,
                    terminate_client=True,
                )

        try:
            preanswered = registry.resource_for(call_id, "preanswered")
            if decision.action is RouteAction.ANSWER_HA:
                endpoint = target_browser_endpoint
                if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
                    raise RuntimeError("target Home Assistant phone disappeared")
                if endpoint.dnd:
                    raise RuntimeError("target Home Assistant phone is in DND")
                if endpoint.active_call_id and endpoint.active_call_id != call_id:
                    raise RuntimeError("target Home Assistant phone is busy")
                if endpoint.availability is EndpointAvailability.UNAVAILABLE:
                    raise RuntimeError("target Home Assistant phone is disabled")
                session_id = registry.resolve_session_id(call_id)
                claims = registry.endpoint_claims_for(session_id)
                target_was_claimed = endpoint.endpoint_id in claims
                old_was_claimed = session_endpoint_id in claims
                target_claimed = False
                old_released = False
                try:
                    registry.claim_endpoint(
                        call_id,
                        endpoint.endpoint_id,
                        role="destination",
                    )
                    target_claimed = not target_was_claimed
                    if session_endpoint_id != endpoint.endpoint_id:
                        old_released = (
                            registry.release_endpoint_claim(
                                call_id,
                                session_endpoint_id,
                            )
                            or old_was_claimed
                        )
                    runtime.defer_invite_to_softphone(
                        invite,
                        route_kind=decision.action.value,
                        endpoint_id=endpoint.endpoint_id,
                        endpoint_device_id=endpoint.device_id,
                        callee=endpoint.name,
                        sip_uri=decision.sip_uri,
                        last_sip_event="ROUTE_FORWARD",
                    )
                except Exception:
                    if target_claimed:
                        registry.release_endpoint_claim(
                            call_id,
                            endpoint.endpoint_id,
                        )
                    if old_released and old_was_claimed:
                        registry.claim_endpoint(
                            call_id,
                            session_endpoint_id,
                            role="destination",
                        )
                    raise
                return

            reservation = (preanswered or {}).get("rtp_reservation")
            reservation_from_preanswer = reservation is not None
            if reservation is None:
                reservation = RtpPortReservation.allocate(hass)
            source_relay_port, dest_relay_port = reservation.ports

            if decision.action is RouteAction.GROUP:
                entry = decision.entry
                if entry is None:
                    raise RuntimeError("ring group has no roster entry")
                members = _unique_group_members(entry.metadata.get("members"))
                candidates = GroupCandidates()
                attempts = candidates.attempts
                browser_legs = candidates.browser_legs
                endpoint_registry = endpoint_directory(hass)

                settle_group = partial(
                    _settle_ring_browser_candidates,
                    hass,
                    registry,
                    browser_legs,
                    call_id=call_id,
                    caller=invite.caller,
                    callee=entry.display_name,
                    route_kind=GROUP_TYPE_RING,
                )

                def _settle_browser_candidates(state, reason, **kwargs) -> None:
                    settle_group(state=state, reason=reason, **kwargs)

                async def _abort_group(reason, *, cleanup=True) -> None:
                    await async_abort_route(
                        RouteAbortContext(
                            hass,
                            registry,
                            call_id,
                            settle=_settle_browser_candidates,
                            attempts=attempts if cleanup else (),
                        ),
                        RouteAbortIntent(reason, "cleanup"),
                    )

                try:
                    await async_prepare_group_candidates(
                        candidates,
                        GroupCandidateRuntime(
                            registry=registry,
                            endpoint_registry=endpoint_registry,
                            browser_leg_for_member=runtime.browser_leg_for_member,
                            prepare_outbound_leg=runtime.prepare_outbound_leg,
                        ),
                        invite=invite,
                        members=members,
                        peers=peers,
                        roster_entries=roster_entries,
                        local_name=invite.caller or _ha_peer_name(hass),
                        initial_selection=initial_selection,
                    )
                except Exception:
                    await _abort_group(
                        TerminalReason.PROTOCOL_ERROR.value,
                    )
                    raise
                if not attempts and not browser_legs:
                    raise RuntimeError("ring group has no reachable members")

                current_session = registry.sessions.get(
                    registry.resolve_session_id(call_id)
                )
                call_generation = (
                    current_session.generation if current_session is not None else 0
                )
                pbx_runtime = sip_endpoint_runtime(hass)
                authoritative_session = (
                    pbx_runtime.get_session(
                        call_id,
                        generation=call_generation,
                    )
                    if pbx_runtime is not None and call_generation
                    else None
                )
                if authoritative_session is None:
                    await _abort_group(
                        TerminalReason.CANCELLED.value,
                    )
                    raise RuntimeError("forward source call is no longer current")

                browser_route_future = asyncio.get_running_loop().create_future()
                if browser_legs:
                    registry.upsert(
                        call_id,
                        state=CallState.RINGING.value,
                        owner="router",
                        caller=invite.caller,
                        callee=entry.display_name,
                        route_kind=GROUP_TYPE_RING,
                        ring_endpoint_ids=tuple(
                            leg.endpoint_id for leg in browser_legs
                        ),
                    )
                    registry.set_pending_route(
                        call_id,
                        {
                            "invite": invite,
                            "future": browser_route_future,
                            "ring_group_endpoint_ids": tuple(
                                leg.endpoint_id for leg in browser_legs
                            ),
                            "declined_endpoint_ids": set(),
                        },
                    )
                    for browser_leg in browser_legs:
                        runtime.publish_pending_ringing(
                            invite,
                            route_kind=GROUP_TYPE_RING,
                            endpoint_id=browser_leg.endpoint_id,
                            endpoint_device_id=browser_leg.device_id,
                            callee=entry.display_name,
                            last_sip_event="ROUTE_FORWARD",
                        )

                def _publish_group_ringing() -> None:
                    ringing_session = registry.transition(
                        call_id,
                        state=CallState.REMOTE_RINGING.value,
                        caller=invite.caller,
                        callee=entry.display_name,
                        route_kind=GROUP_TYPE_RING,
                    )
                    if ringing_session is not None:
                        publish_bridge_projection(
                            hass,
                            ringing_session,
                            peer_name=entry.display_name,
                            direction="incoming",
                            route_source="automation",
                            route_kind=GROUP_TYPE_RING,
                            last_sip_event="SIP_RESPONSE",
                        )

                (
                    fork_candidates,
                    candidate_payloads,
                    browser_decision,
                ) = build_ring_group_fork(
                    sip_port=int(cfg["sip_port"]),
                    route_future=browser_route_future,
                    attempts=attempts,
                    browser_legs=browser_legs,
                    preflight_failures=[],
                    on_ringing=_publish_group_ringing,
                )
                try:
                    fork_result = await DialForkController(
                        authoritative_session,
                        fork_candidates,
                    ).run(
                        lambda _candidate, _outcome: registry.is_generation_current(
                            call_id,
                            call_generation,
                        )
                    )
                except asyncio.CancelledError:
                    await _abort_group(
                        TerminalReason.CANCELLED.value,
                        cleanup=False,
                    )
                    raise
                except Exception:
                    await _abort_group(
                        TerminalReason.PROTOCOL_ERROR.value,
                    )
                    raise

                winner = (
                    candidate_payloads.get(fork_result.winner.candidate_id)
                    if fork_result.winner is not None
                    else None
                )
                reroute_decision = (
                    dict(browser_decision)
                    if fork_result.outcome.disposition is DialDisposition.REROUTE
                    else None
                )
                if reroute_decision is not None:
                    route = registry.take_pending_route(call_id) or {}
                    _settle_browser_candidates(
                        CallState.IDLE.value,
                        "forwarded",
                    )
                    handoff = route.get("forward_handoff")
                    if handoff is not None and not handoff.done():
                        handoff.set_result(dict(reroute_decision))
                    return
                if winner is None:
                    failure = terminal_reason(
                        fork_result.outcome.disposition,
                        fork_result.outcome.reason or "transport_unreachable",
                    )
                    await _abort_group(
                        TerminalReason.TRANSPORT_UNREACHABLE.value,
                        cleanup=False,
                    )
                    raise RuntimeError(failure)

                if isinstance(winner, BrowserLeg):
                    registry.take_pending_route(call_id)
                    _settle_browser_candidates(
                        CallState.CANCELLED.value,
                        TerminalReason.CANCELLED.value,
                        keep_endpoint_id=winner.endpoint_id,
                    )
                    registry.upsert(
                        call_id,
                        state=CallState.RINGING.value,
                        owner="ha_softphone",
                        caller=invite.caller,
                        callee=entry.display_name,
                        route_kind=GROUP_TYPE_RING,
                        endpoint_id=winner.endpoint_id,
                        session_device_id=winner.device_id,
                    )
                    registry.set_pending_invite(call_id, invite)
                    call_artifacts.answer_commit = True
                    try:
                        await hass.services.async_call(
                            DOMAIN,
                            "answer",
                            {
                                "call_id": call_id,
                                # Service selectors are HA Device IDs; the
                                # endpoint ID remains runtime metadata.
                                "device_id": winner.device_id,
                                "media_client_id": str(
                                    browser_decision.get("media_client_id") or ""
                                ),
                                "send_video": bool(
                                    browser_decision.get("send_video", False)
                                ),
                            },
                            blocking=True,
                            context=registry.ha_context(call_id),
                        )
                    finally:
                        call_artifacts.answer_commit = False
                    return

                registry.take_pending_route(call_id)
                _settle_browser_candidates(
                    CallState.CANCELLED.value,
                    TerminalReason.CANCELLED.value,
                )

                client = winner.client
                dest_call_id = client.dialog_ids.call_id
                dest_relay_port = winner.ports.ports[1]
                source_ports = reservation.ports
                winner_ports = winner.ports.ports
                response_already_sent = _source_dialog_is_answered(preanswered)
                connected_party = str(winner.member or "").strip()

                committed = await async_commit_outbound_bridge(
                    hass,
                    registry,
                    BridgeCommitData(
                        invite=invite,
                        winner=winner,
                        source_relay_port=source_relay_port,
                        dest_relay_port=dest_relay_port,
                        local_ip=local_ip,
                        release_port_pairs=(source_ports, winner_ports),
                        detach_reservations=(reservation, winner.ports),
                        enable_video_transcoding=bool(
                            cfg.get(CONF_VIDEO_TRANSCODING, False)
                        ),
                    ),
                    BridgeCommitPolicy(
                        route_kind=GROUP_TYPE_RING,
                        caller=invite.caller,
                        callee=entry.display_name,
                        connected_party=connected_party,
                        source_role=(
                            "trunk" if preanswered is not None else "caller"
                        ),
                        source_state=(
                            CallState.IN_CALL.value
                            if response_already_sent
                            else CallState.CONNECTING.value
                        ),
                        response_already_sent=response_already_sent,
                    ),
                )
                if committed is None:
                    raise RuntimeError(TerminalReason.CANCELLED.value)
                if session is not None:
                    publish_bridge_projection(
                        hass,
                        session,
                        peer_name=connected_party,
                        dialed_target=entry.display_name,
                        connected_party=connected_party,
                        answered_by=connected_party,
                        direction="incoming",
                        route_source="automation",
                        route_kind=GROUP_TYPE_RING,
                        sip_status_code=200,
                        last_sip_event="SIP_RESPONSE",
                        sip_uri=str(winner.uri),
                        video_active=bool(committed.relay.video_relay),
                        video_requested=invite.video_format is not None,
                        video_negotiated=bool(committed.relay.video_relay),
                        video_failure_reason=committed.video_failure_reason,
                    )
                return

            if decision.action is RouteAction.ASSIST:
                current = registry.sessions.get(registry.resolve_session_id(call_id))
                if current is not None:
                    claimed_assist = registry.transition(
                        call_id,
                        state=CallState.CONNECTING.value,
                        owner="assist",
                        callee=destination,
                        expected_revision=current.revision,
                        expected_owner=current.owner,
                    )
                    if claimed_assist is None:
                        raise RuntimeError("Assist route ownership changed")
                await runtime.start_local_assist_bridge(
                    invite,
                    reservation=reservation,
                    local_rtp_port=source_relay_port,
                    roster_entries=roster_entries,
                    source="trunk" if preanswered is not None else "sip",
                    called_extension=str(
                        (decision.entry.extension if decision.entry is not None else "")
                        or destination
                    ),
                    release_reservation_on_failure=preanswered is None,
                )
                response_already_sent = _source_dialog_is_answered(preanswered)
                answer = ""
                if not response_already_sent:
                    answer = build_answer_directional(
                        local_ip,
                        local_ip,
                        source_relay_port,
                        invite.send_format,
                        invite.recv_format,
                        remote_sdp=invite.remote_sdp,
                    )
                registry.take_pending_invite(call_id)
                registry.take_media(call_id, provisional=True)
                answer_result = await _commit_source_answer(
                    answer,
                    owner="assist",
                    callee=destination,
                    route_kind=decision.action.value,
                    response_already_sent=response_already_sent,
                )
                if not answer_result.committed:
                    raise RuntimeError(answer_result.reason)
                return

            bridge_to_trunk = decision.action is RouteAction.TRUNK
            bridge_uri = None
            peer_target = _peer_for_target(decision.target or destination, peers)
            if bridge_to_trunk:
                trunk_cfg = _get_trunk_config(hass)
                bridge_uri = parse_sip_uri(
                    f"sip:{decision.target or destination}@{trunk_cfg[CONF_TRUNK_SERVER]}:"
                    f"{int(trunk_cfg[CONF_TRUNK_PORT])};"
                    f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
                )
            else:
                bridge_uri, _peer, member_entry = runtime.sip_uri_for_member(
                    decision.target or destination,
                    peers,
                    roster_entries,
                )
                if bridge_uri is None and decision.sip_uri:
                    bridge_uri = parse_sip_uri(decision.sip_uri)
                if bridge_uri is None:
                    raise RuntimeError(
                        f"destination {destination} has no reachable SIP URI"
                    )

            remote_tx_formats = _peer_audio_formats(
                peer_target, "tx_formats"
            ) or _roster_entry_formats(
                decision.entry,
                "tx_formats",
            )
            remote_rx_formats = _peer_audio_formats(
                peer_target, "rx_formats"
            ) or _roster_entry_formats(
                decision.entry,
                "rx_formats",
            )
            sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
                remote_tx_formats=remote_tx_formats,
                remote_rx_formats=remote_rx_formats,
                target=decision.target or destination,
            )
            bridge_to_registered = bool(
                decision.entry is not None
                and decision.entry.sip_uri
                and decision.entry.metadata.get("registered")
            )
            if bridge_to_trunk or bridge_to_registered:
                sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
                sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
            trunk_cfg = _get_trunk_config(hass)
            endpoint_registry = endpoint_directory(hass)
            source_route_endpoint_id = str(
                ((session.metadata if session is not None else {}) or {}).get(
                    "source_endpoint_id"
                )
                or ""
            ).strip()
            target_route_endpoint_id = str(
                (
                    (decision.entry.metadata if decision.entry is not None else {})
                    or {}
                ).get("endpoint_id")
                or ""
            ).strip()
            source_route_endpoint = (
                endpoint_registry.get(source_route_endpoint_id)
                if source_route_endpoint_id
                else None
            )
            target_route_endpoint = (
                endpoint_registry.get(target_route_endpoint_id)
                if target_route_endpoint_id
                else None
            )
            forward_video_enabled = bool(
                preanswered is None
                and cfg.get(CONF_SIP_VIDEO, False)
                and invite.video_format is not None
                and (
                    source_route_endpoint is None
                    or source_route_endpoint.supports("video")
                )
                and (
                    target_route_endpoint is None
                    or target_route_endpoint.supports("video")
                )
            )
            video_transcoding_enabled = bool(
                forward_video_enabled and cfg.get(CONF_VIDEO_TRANSCODING, False)
            )
            video_dest_port = 0
            video_failure_reason = ""
            if forward_video_enabled:
                video_reservation = None
                sockets = ()
                try:
                    video_reservation, sockets = reserve_sip_video_relay_media(hass)
                    source_video_port, video_dest_port = video_reservation.ports
                    video_relay = build_pending_invite_video_relay(
                        invite,
                        remote_host=str(bridge_uri.host),
                        left_port=source_video_port,
                        right_port=video_dest_port,
                        sockets=sockets,
                        on_release=lambda ports: _release_sip_rtp_port_pair(
                            hass, ports
                        ),
                    )
                    # The relay owns all four bound sockets from here.
                    video_reservation.detach()
                except (OSError, RuntimeError) as err:
                    for sock in sockets:
                        sock.close()
                    if video_reservation is not None:
                        video_reservation.release()
                    video_relay = None
                    video_dest_port = 0
                    video_failure_reason = "local_video_resources_unavailable"
                    _LOGGER.warning(
                        "SIP forward video relay unavailable; continuing audio-only: %s",
                        err,
                    )
            client = SipCallClient(
                local_ip=local_ip,
                local_name=invite.caller or _ha_peer_name(hass),
                local_uri_user=(
                    str(trunk_cfg.get(CONF_TRUNK_USERNAME) or _ha_peer_name(hass))
                    if bridge_to_trunk
                    else invite.routing_caller or _ha_peer_name(hass)
                ),
                local_sip_port=int(cfg["sip_port"]),
                local_rtp_port=dest_relay_port,
                supported_send_formats=sip_send_formats,
                supported_recv_formats=sip_recv_formats,
                signaling_transport=_sip_uri_transport(bridge_uri),
                auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or "")
                if bridge_to_trunk
                else "",
                username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or "")
                if bridge_to_trunk
                else "",
                password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "")
                if bridge_to_trunk
                else "",
                outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "")
                if bridge_to_trunk
                else "",
                include_common_codecs=bridge_to_trunk or bridge_to_registered,
                peer_user_agent=(
                    str(((decision.entry.metadata or {}).get("user_agent")) or "")
                    if bridge_to_registered and decision.entry is not None
                    else ""
                ),
                local_video_rtp_port=video_dest_port,
                video_formats=(
                    video_bridge_offer_formats(
                        invite.video_format,
                        enable_transcoding=video_transcoding_enabled,
                        target_codec=_peer_video_codec(
                            peer_target,
                            member_entry or decision.entry,
                        ),
                    )
                    if video_relay is not None and invite.video_format is not None
                    else ()
                ),
                video_direction=(
                    invite.video_format.direction
                    if video_relay is not None
                    else "inactive"
                ),
                generic_video_relay=video_relay is not None,
            )
            if not bridge_to_trunk:
                _enable_reused_sip_tcp_connection(
                    hass,
                    client,
                    bridge_uri,
                    target=decision.target or destination,
                    default_sip_port=int(cfg["sip_port"]),
                )
            result = await client.invite(
                target=bridge_uri.user,
                target_display_name=(
                    decision.entry.display_name
                    if decision.entry is not None
                    else destination
                ),
                remote_host=bridge_uri.host,
                remote_sip_port=bridge_uri.port or int(cfg["sip_port"]),
                request_uri=str(bridge_uri),
                timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
            )
            if call_artifacts.trunk_closed:
                call_artifacts.trunk_closed = False
                raise RuntimeError(TerminalReason.CANCELLED.value)
            if result not in {"ringing", "in_call"}:
                raise RuntimeError(result)

            response_already_sent = _source_dialog_is_answered(preanswered)
            if result == "ringing":
                ringing_session = registry.transition(
                    call_id,
                    state=CallState.REMOTE_RINGING.value,
                    caller=invite.caller,
                    callee=destination,
                )
                if ringing_session is not None:
                    ringing_session.metadata["bridge_dest_call_id"] = (
                        client.dialog_ids.call_id
                    )
                    publish_bridge_projection(
                        hass,
                        ringing_session,
                        peer_name=destination,
                        direction="incoming",
                        route_source="automation",
                        last_sip_event="SIP_RESPONSE",
                    )
            direct_winner = OutboundLeg(
                destination,
                bridge_uri,
                client,
                reservation,
                video_relay=video_relay,
                video_failure_reason=video_failure_reason,
            )
            committed = await async_commit_outbound_bridge(
                hass,
                registry,
                BridgeCommitData(
                    invite=invite,
                    winner=direct_winner,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    local_ip=local_ip,
                    release_port_pairs=(reservation.ports,),
                    detach_reservations=(reservation,),
                    enable_video_transcoding=video_transcoding_enabled,
                ),
                BridgeCommitPolicy(
                    route_kind=decision.action.value,
                    caller=invite.caller,
                    callee=destination,
                    connected_party=destination,
                    source_role="trunk" if preanswered is not None else "caller",
                    source_state=(
                        CallState.IN_CALL.value
                        if response_already_sent
                        else CallState.CONNECTING.value
                    ),
                    bridge_state=(
                        CallState.REMOTE_RINGING.value
                        if result == "ringing"
                        else CallState.CONNECTING.value
                    ),
                    dest_state=result,
                    response_already_sent=response_already_sent,
                ),
            )
            if committed is None:
                raise RuntimeError(TerminalReason.CANCELLED.value)
            failure = committed.video_failure_reason or video_failure_reason
            if session is not None:
                publish_bridge_projection(
                    hass,
                    session,
                    peer_name=destination,
                    direction="incoming",
                    route_source="automation",
                    answered_by=destination,
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=decision.action.value,
                    sip_uri=str(bridge_uri),
                    video_active=bool(committed.relay.video_relay),
                    video_requested=forward_video_enabled,
                    video_negotiated=bool(committed.relay.video_relay),
                    video_failure_reason=failure,
                )
        except asyncio.CancelledError:
            await _cleanup_failed_route(TerminalReason.CANCELLED.value)
            raise
        except Exception as err:  # noqa: BLE001 - convert route failures to policy.
            reason = str(err or TerminalReason.TRANSPORT_UNREACHABLE.value)
            _LOGGER.info(
                "SIP automation forward failed call_id=%s destination=%s reason=%s",
                call_id,
                destination,
                reason,
            )
            await _cleanup_failed_route(reason)
            await async_abort_route(
                forward_abort,
                RouteAbortIntent(
                    reason,
                    on_failure,
                ),
            )
        finally:
            call_artifacts.forward_claim = False

    task = create_runtime_task(hass, _run_forward())
    if not artifacts.own_task(call_id, task, name="forward"):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        call_artifacts.forward_claim = False
        raise _service_error(
            f"call_id {call_id} is no longer active",
            "call_inactive",
            call_id=call_id,
        )
