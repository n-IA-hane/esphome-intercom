"""Forwarding orchestration for an existing HA-owned SIP call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant

from . import sdp as sip_sdp
from .audio_format import HA_TRUNK_AUDIO_FORMATS
from .config import debug_mode as _debug_mode, trunk_config as _get_trunk_config
from .const import (
    CONF_SIP_VIDEO,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    HA_SOFTPHONE_DEVICE_ID,
)
from .dtmf_events import attach_dtmf_event_bridge as _attach_dtmf_event_bridge
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .endpoint_registry import EndpointBusyError
from .endpoint_routing import (
    EndpointRouteResolver,
    peer_audio_formats as _peer_audio_formats,
    peer_for_target as _peer_for_target,
    roster_entry_formats as _roster_entry_formats,
    roster_from_peers as _roster_from_peers,
    sip_target_audio_profile as _sip_target_audio_profile,
)
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .groups import GROUP_TYPE_RING
from .media_ports import (
    RtpPortReservation,
    release_media_reservation as _release_media_reservation,
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
    release_video_media_reservation as _release_video_media_reservation,
    reserve_sip_video_relay_media,
)
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
    async_cancel_and_join_tasks as _cancel_and_join_tasks,
    async_cleanup_outbound_attempts as _cleanup_outbound_attempts,
    async_close_outbound_leg as _close_outbound_leg,
)
from .pbx_routing import (
    browser_endpoint_can_ring as _browser_endpoint_can_ring,
    caller_matches_group_member as _caller_matches_member,
    unique_group_members as _unique_group_members,
)
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointAvailability,
    EndpointKind,
)
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import RouteAction
from .sdp import build_answer_directional
from .session_cleanup import async_cleanup_sip_runtime
from .sip import parse_sip_uri
from .sip_bridge import (
    build_invite_client_relay,
    build_pending_invite_video_relay,
    configure_answered_invite_video_relay,
)
from .sip_client import SIP_TIMER_B, SipCallClient
from .sip_runtime import (
    enable_reused_tcp_connection as _enable_reused_sip_tcp_connection,
    send_bye as _sip_send_bye,
    send_final_response as _sip_send_final_response,
    uri_transport as _sip_uri_transport,
)
from .softphone_termination import (
    async_terminate_sip_bridge_session as _terminate_sip_bridge,
)
from .peer_snapshot import async_build_peer_snapshot as _async_build_peer_snapshot
from .websocket_api import (
    _ha_peer_name,
    _release_ha_softphone_claim,
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)

_LOGGER = logging.getLogger(__name__)
RING_GROUP_TIMEOUT_S = 30.0
MAX_RING_GROUP_ATTEMPTS = 16


def _invite_dtmf_format(invite):
    formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
    return formats[0] if formats else None


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
    hass = runtime.hass
    cfg = runtime.config
    local_ip = runtime.local_ip
    _ha_router_decision = runtime.route_resolver.route
    _logical_endpoint_for_member = runtime.route_resolver.logical_endpoint
    _attach_client_media_update = runtime.attach_client_media_update
    _browser_leg_for_member = runtime.browser_leg_for_member
    _defer_invite_to_ha_softphone = runtime.defer_invite_to_softphone
    _prepare_outbound_leg = runtime.prepare_outbound_leg
    _publish_pending_ha_softphone_ringing = runtime.publish_pending_ringing
    _sip_uri_for_member = runtime.sip_uri_for_member
    _start_local_assist_bridge = runtime.start_local_assist_bridge

    """Route or move one HA-owned pending/ringing call to a target.

    ``initial_selection`` is used only by the bounded ``route_requested``
    decision point.  Unlike a later forward, it must not exclude browser
    phones from a ring group merely because the pre-answered trunk dialog
    is temporarily anchored on the default HA phone.
    """
    from homeassistant.exceptions import ServiceValidationError

    call_id = str(call_id or "").strip()
    destination = str(destination or "").strip()
    on_failure = str(on_failure or "resume").strip().lower()
    if not call_id or not destination:
        raise ServiceValidationError("call_id and destination are required")
    if on_failure not in {"resume", "terminate", "busy"}:
        raise ServiceValidationError(
            "on_failure must be resume, terminate, or busy"
        )

    registry = _call_registry(hass)
    context = registry.event_context(call_id)
    expected_state = str(expected_state or "").strip().lower()
    if expected_state and (context is None or context.state != expected_state):
        actual = context.state if context is not None else "ended"
        raise ServiceValidationError(
            f"call_id {call_id} is {actual}, expected {expected_state}"
        )
    if expected_sequence and (
        context is None or context.sequence != int(expected_sequence)
    ):
        actual = context.sequence if context is not None else 0
        raise ServiceValidationError(
            f"call_id {call_id} sequence is {actual}, expected {expected_sequence}"
        )
    if context is not None and len(context.route_history) >= 8:
        raise ServiceValidationError(f"call_id {call_id} exceeded 8 routing hops")

    invite = registry.pending_invites.get(call_id)
    if invite is None:
        raise ServiceValidationError(
            f"call_id {call_id} is not a forwardable pending or ringing HA-owned call"
        )
    forward_tasks = hass.data.setdefault(DOMAIN, {}).setdefault("forward_tasks", {})
    forward_claims = hass.data.setdefault(DOMAIN, {}).setdefault(
        "forward_claims", set()
    )
    current_forward = forward_tasks.get(call_id)
    if current_forward is not None and not current_forward.done():
        current_context = registry.event_context(call_id)
        if (
            current_context is None
            or current_context.state != CallState.REMOTE_RINGING.value
        ):
            raise ServiceValidationError(
                f"call_id {call_id} is already being forwarded"
            )
        current_forward.cancel()
        await asyncio.gather(current_forward, return_exceptions=True)
    if call_id in forward_claims:
        raise ServiceValidationError(
            f"call_id {call_id} is already being forwarded"
        )
    forward_claims.add(call_id)
    target_browser_endpoint = None
    try:
        peers = await _async_build_peer_snapshot(hass)
        if registry.pending_invites.get(call_id) is not invite:
            raise ServiceValidationError(
                f"call_id {call_id} changed while the route was being resolved"
            )
        context = registry.event_context(call_id)
        if expected_state and (context is None or context.state != expected_state):
            actual = context.state if context is not None else "ended"
            raise ServiceValidationError(
                f"call_id {call_id} is {actual}, expected {expected_state}"
            )
        if expected_sequence and (
            context is None or context.sequence != int(expected_sequence)
        ):
            actual = context.sequence if context is not None else 0
            raise ServiceValidationError(
                f"call_id {call_id} sequence is {actual}, expected {expected_sequence}"
            )
        roster_entries = _roster_from_peers(
            hass,
            peers,
            _registered_roster_entries(hass),
        )
        decision = _ha_router_decision(destination, roster_entries)
        endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
        if decision.action is RouteAction.ANSWER_HA:
            target_browser_endpoint = _logical_endpoint_for_member(
                decision.target or destination,
                peers,
                roster_entries,
            )
            if (
                target_browser_endpoint is None
                or target_browser_endpoint.kind is not EndpointKind.BROWSER
            ):
                raise ServiceValidationError(
                    f"destination {destination} is not a configured Home Assistant phone"
                )
        if decision.action is RouteAction.REJECT:
            raise ServiceValidationError(
                f"destination {destination} is not a forwardable SIP dial-plan target"
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
            raise ServiceValidationError(
                "forwarding an already-ringing call is currently limited to ring groups"
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
        session_endpoint_id = str(
            (session.metadata if session is not None else {}).get("endpoint_id")
            or DEFAULT_ENDPOINT_ID
        ).strip()
        if (
            not initial_selection
            and target_browser_endpoint is not None
            and target_browser_endpoint.endpoint_id == session_endpoint_id
        ):
            raise ServiceValidationError(
                "a Home Assistant phone cannot forward a call to itself"
            )
        session_endpoint = (
            endpoint_registry.get(session_endpoint_id)
            if endpoint_registry is not None
            else None
        )
        session_device_id = str(
            getattr(session_endpoint, "device_id", "")
            or HA_SOFTPHONE_DEVICE_ID
        )
        original_callee = session.callee if session is not None else invite.target
        original_route_kind = session.route_kind if session is not None else ""
        # A trunk call is already SIP-answered while its DTMF/automation
        # route is being selected.  It has not populated the HA softphone
        # store yet, but HA is still its default owner.  Preserve that
        # ownership so ``on_failure: resume`` can enter normal ringing
        # instead of leaving the answered caller on silent RTP.
        ha_claimed = (
            bool(session is not None and session.owner == "ha_softphone")
            or call_id in registry.preanswered
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
                    raise ServiceValidationError(
                        f"call_id {call_id} changed while forwarding ownership was claimed"
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
            _set_sip_bridge_call_state(
                hass,
                CallState.CONNECTING.value,
                caller=invite.caller,
                callee=destination,
                peer_name=destination,
                call_id=call_id,
                direction="incoming",
                route_source="automation",
                route_kind=decision.action.value,
                event_type="forwarding",
                last_sip_event="ROUTE_FORWARD",
            )
    except Exception:
        forward_claims.discard(call_id)
        raise

    async def _restore_or_terminate(reason: str) -> None:
        preanswered = registry.preanswered.get(call_id)
        if on_failure == "resume" and call_id not in hass.data.setdefault(
            DOMAIN, {}
        ).get("trunk_closed_calls", set()):
            if session is not None:
                current = registry.sessions.get(
                    registry.resolve_session_id(call_id)
                )
                if current is None or current.owner not in {
                    "router",
                    "bridge",
                    "assist",
                }:
                    return
                resumed = registry.transition(
                    call_id,
                    state=CallState.RINGING.value,
                    owner="ha_softphone",
                    callee=original_callee,
                    route_kind=original_route_kind,
                    expected_revision=current.revision,
                    expected_owner=current.owner,
                )
                if resumed is None:
                    return
            if ha_claimed:
                _publish_pending_ha_softphone_ringing(
                    invite,
                    route_kind=original_route_kind,
                    endpoint_id=session_endpoint_id,
                    endpoint_device_id=session_device_id,
                    callee=original_callee,
                    last_sip_event="ROUTE_RESUME",
                )
            return

        registry.pending_invites.pop(call_id, None)
        preanswered = registry.take_media(call_id, provisional=True)
        if preanswered is not None:
            _release_media_reservation(preanswered)
        if _source_dialog_is_answered(preanswered):
            _sip_send_bye(hass, call_id)
        else:
            status = 486 if on_failure == "busy" else 480
            _sip_send_final_response(
                hass,
                call_id,
                status,
                "Busy Here" if status == 486 else "Temporarily Unavailable",
            decline_reason=reason,
        )

        terminal_state = (
            CallState.BUSY.value
            if on_failure == "busy"
            else CallState.TRANSPORT_UNREACHABLE.value
        )
        current = registry.sessions.get(registry.resolve_session_id(call_id))
        if current is not None:
            registry.transition(
                call_id,
                state=terminal_state,
                owner="terminal",
                outcome=reason,
                expected_revision=current.revision,
                expected_owner=current.owner,
            )
        _set_sip_bridge_call_state(
            hass,
            terminal_state,
            caller=invite.caller,
            callee=destination,
            call_id=call_id,
            reason=reason,
            terminal_reason=reason,
            origin="self",
            last_sip_event=(
                "BYE"
                if _source_dialog_is_answered(preanswered)
                else "SIP_RESPONSE"
            ),
        )
        registry.finish_and_pop(call_id, reason=reason, state=terminal_state)

    async def _run_forward() -> None:
        client = None
        reservation = None
        reservation_from_preanswer = False
        video_relay = None
        dest_call_id = ""
        try:
            preanswered = registry.preanswered.get(call_id)
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
                claims = registry.endpoint_claims.get(session_id, {})
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
                        old_released = registry.release_endpoint_claim(
                            call_id,
                            session_endpoint_id,
                        ) or old_was_claimed
                    _defer_invite_to_ha_softphone(
                        invite,
                        route_kind=decision.action.value,
                        endpoint_id=endpoint.endpoint_id,
                        endpoint_device_id=str(
                            endpoint.device_id or HA_SOFTPHONE_DEVICE_ID
                        ),
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
                attempts: list[OutboundLeg] = []
                browser_legs: list[BrowserLeg] = []
                endpoint_registry = hass.data.get(DOMAIN, {}).get(
                    "endpoint_registry"
                )
                for member in members:
                    # A later forward moves a call away from HA and must
                    # not ring HA again.  Initial routing is different: the
                    # trunk dialog is only parked on HA while DTMF and
                    # automations choose its real destination, so browser
                    # members remain valid ring-group candidates.
                    browser_leg = _browser_leg_for_member(
                        member, peers, roster_entries
                    )
                    if browser_leg is not None:
                        if not initial_selection:
                            continue
                        endpoint = (
                            endpoint_registry.get(browser_leg.endpoint_id)
                            if endpoint_registry is not None
                            else None
                        )
                        if not _browser_endpoint_can_ring(endpoint):
                            continue
                        try:
                            registry.claim_endpoint(
                                call_id,
                                browser_leg.endpoint_id,
                                role="group_candidate",
                            )
                        except EndpointBusyError:
                            continue
                        browser_legs.append(browser_leg)
                        continue
                    if _caller_matches_member(
                        invite.caller, invite.source_host, member, peers
                    ):
                        continue
                    if len(attempts) >= MAX_RING_GROUP_ATTEMPTS:
                        break
                    attempt = _prepare_outbound_leg(
                        member=member,
                        peers=peers,
                        roster_entries=roster_entries,
                        local_name=invite.caller or _ha_peer_name(hass),
                        local_rtp_port_index=1,
                    )
                    if attempt is not None:
                        attempts.append(attempt)
                if not attempts and not browser_legs:
                    raise RuntimeError("ring group has no reachable members")

                def _settle_browser_candidates(
                    state: str,
                    reason: str,
                    *,
                    keep_endpoint_id: str = "",
                ) -> None:
                    for browser_leg in browser_legs:
                        if browser_leg.endpoint_id == keep_endpoint_id:
                            continue
                        registry.release_endpoint_claim(
                            call_id, browser_leg.endpoint_id
                        )
                        _set_ha_softphone_call_state(
                            hass,
                            state,
                            endpoint_id=browser_leg.endpoint_id,
                            session_device_id=browser_leg.device_id,
                            caller=invite.caller,
                            callee=entry.display_name,
                            peer_name=invite.caller,
                            direction="incoming",
                            call_id=call_id,
                            reason=reason,
                            terminal_reason=reason,
                            route_kind=GROUP_TYPE_RING,
                            last_sip_event="SIP_RESPONSE",
                        )

                browser_route_future: asyncio.Future | None = None
                if browser_legs:
                    browser_route_future = (
                        asyncio.get_running_loop().create_future()
                    )
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
                    registry.pending_routes[call_id] = {
                        "invite": invite,
                        "future": browser_route_future,
                        "ring_group_endpoint_ids": tuple(
                            leg.endpoint_id for leg in browser_legs
                        ),
                        "declined_endpoint_ids": set(),
                    }
                    for browser_leg in browser_legs:
                        _publish_pending_ha_softphone_ringing(
                            invite,
                            route_kind=GROUP_TYPE_RING,
                            endpoint_id=browser_leg.endpoint_id,
                            endpoint_device_id=browser_leg.device_id,
                            callee=entry.display_name,
                            last_sip_event="ROUTE_FORWARD",
                        )
                group_ringing_published = False

                async def _dial_group_member(
                    attempt: OutboundLeg,
                ) -> tuple[str, OutboundLeg]:
                    nonlocal group_ringing_published
                    result = await attempt.client.invite(
                        target=attempt.uri.user or attempt.member,
                        remote_host=attempt.uri.host,
                        remote_sip_port=attempt.uri.port or int(cfg["sip_port"]),
                        request_uri=str(attempt.uri),
                        timeout=8.0,
                    )
                    if result == "ringing":
                        if not group_ringing_published:
                            group_ringing_published = True
                            _set_sip_bridge_call_state(
                                hass,
                                CallState.REMOTE_RINGING.value,
                                caller=invite.caller,
                                callee=entry.display_name,
                                peer_name=entry.display_name,
                                call_id=call_id,
                                direction="incoming",
                                route_source="automation",
                                route_kind=GROUP_TYPE_RING,
                                last_sip_event="SIP_RESPONSE",
                            )
                        result = await attempt.client.wait_for_final(
                            timeout=RING_GROUP_TIMEOUT_S
                        )
                    return result, attempt

                async def _wait_browser_group_member():
                    if browser_route_future is None:
                        return "timeout", None, {}
                    try:
                        browser_decision = await asyncio.wait_for(
                            browser_route_future,
                            timeout=RING_GROUP_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        return "timeout", None, {}
                    action = str(
                        (browser_decision or {}).get("action") or ""
                    ).strip().lower()
                    endpoint_id = str(
                        (browser_decision or {}).get("endpoint_id") or ""
                    ).strip()
                    selected = next(
                        (
                            leg
                            for leg in browser_legs
                            if leg.endpoint_id == endpoint_id
                        ),
                        None,
                    )
                    if action in {"answer_ha", "default"} and selected is not None:
                        return "in_call_browser", selected, browser_decision
                    if action in {"forward", "bridge"}:
                        return "reroute", None, browser_decision
                    if action == "cancel":
                        return "cancelled", None, browser_decision
                    return "declined", selected, browser_decision

                tasks = [
                    asyncio.create_task(_dial_group_member(attempt))
                    for attempt in attempts
                ]
                if browser_route_future is not None:
                    tasks.append(asyncio.create_task(_wait_browser_group_member()))
                winner: OutboundLeg | BrowserLeg | None = None
                browser_decision: dict = {}
                reroute_decision: dict | None = None
                failure = "timeout"
                try:
                    deadline = (
                        asyncio.get_running_loop().time() + RING_GROUP_TIMEOUT_S
                    )
                    pending_tasks = set(tasks)
                    while pending_tasks and winner is None:
                        timeout = max(
                            0.0, deadline - asyncio.get_running_loop().time()
                        )
                        if timeout <= 0:
                            break
                        done, pending_tasks = await asyncio.wait(
                            pending_tasks,
                            timeout=timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            break
                        for task in tasks:
                            if task not in done:
                                continue
                            try:
                                task_result = task.result()
                            except Exception as err:  # noqa: BLE001
                                failure = str(err or failure)
                                continue
                            if len(task_result) == 3:
                                result, attempt, decision_data = task_result
                            else:
                                result, attempt = task_result
                                decision_data = {}
                            if (
                                result == "in_call"
                                and isinstance(attempt, OutboundLeg)
                                and attempt.client.dialog is not None
                            ):
                                winner = attempt
                                break
                            if (
                                result == "in_call_browser"
                                and isinstance(attempt, BrowserLeg)
                            ):
                                winner = attempt
                                browser_decision = dict(decision_data or {})
                                break
                            if result == "reroute":
                                reroute_decision = dict(decision_data or {})
                                break
                            failure = result or failure
                        if reroute_decision is not None:
                            pending_tasks.clear()
                            break
                except asyncio.CancelledError:
                    registry.pending_routes.pop(call_id, None)
                    _settle_browser_candidates(
                        CallState.CANCELLED.value,
                        TerminalReason.CANCELLED.value,
                    )
                    await _cleanup_outbound_attempts(tasks, attempts)
                    raise
                finally:
                    await _cancel_and_join_tasks(tasks)

                losers = [attempt for attempt in attempts if attempt is not winner]

                async def _cancel_losing_sip_legs() -> None:
                    await asyncio.gather(
                        *(
                            _close_outbound_leg(attempt, cancel=True)
                            for attempt in losers
                        ),
                        return_exceptions=True,
                    )

                # A browser answer is the fork commit point.  Do not make
                # its final SIP response wait for remote losing legs to
                # acknowledge CANCEL; mature PBX implementations cancel
                # those branches as a consequence of winner selection.
                if isinstance(winner, BrowserLeg):
                    create_runtime_task(hass, _cancel_losing_sip_legs())
                else:
                    await _cancel_losing_sip_legs()
                if reroute_decision is not None:
                    route = registry.pending_routes.pop(call_id, None) or {}
                    _settle_browser_candidates(
                        CallState.IDLE.value,
                        "forwarded",
                    )
                    handoff = route.get("forward_handoff")
                    if handoff is not None and not handoff.done():
                        handoff.set_result(dict(reroute_decision))
                    return
                if winner is None:
                    registry.pending_routes.pop(call_id, None)
                    _settle_browser_candidates(
                        CallState.TRANSPORT_UNREACHABLE.value,
                        TerminalReason.TRANSPORT_UNREACHABLE.value,
                    )
                    raise RuntimeError(failure)

                if isinstance(winner, BrowserLeg):
                    registry.pending_routes.pop(call_id, None)
                    _settle_browser_candidates(
                        CallState.CANCELLED.value,
                        TerminalReason.CANCELLED.value,
                        keep_endpoint_id=winner.endpoint_id,
                    )
                    registry.pending_invites[call_id] = invite
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
                    answer_commits = hass.data.setdefault(DOMAIN, {}).setdefault(
                        "ring_group_answer_commits", set()
                    )
                    answer_commits.add(call_id)
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
                        answer_commits.discard(call_id)
                    return

                registry.pending_routes.pop(call_id, None)
                _settle_browser_candidates(
                    CallState.CANCELLED.value,
                    TerminalReason.CANCELLED.value,
                )

                client = winner.client
                dest_call_id = client.dialog_ids.call_id
                dest_relay_port = winner.ports.ports[1]
                registry.register_bridge(
                    source_call_id=call_id,
                    dest_call_id=dest_call_id,
                    client=client,
                    state=CallState.CONNECTING.value,
                    caller=invite.caller,
                    callee=entry.display_name,
                    route_kind=GROUP_TYPE_RING,
                    source_role="trunk" if preanswered is not None else "caller",
                    source_state=(
                        CallState.IN_CALL.value
                        if _source_dialog_is_answered(preanswered)
                        else CallState.CONNECTING.value
                    ),
                    dest_state=CallState.IN_CALL.value,
                )

                source_ports = reservation.ports
                winner_ports = winner.ports.ports

                def _release_group_ports(_ports) -> None:
                    _release_sip_rtp_port_pair(hass, source_ports)
                    _release_sip_rtp_port_pair(hass, winner_ports)

                relay = build_invite_client_relay(
                    invite=invite,
                    client=client,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    debug_capture=_debug_mode(hass),
                    on_release=_release_group_ports,
                )
                _attach_dtmf_event_bridge(
                    hass,
                    relay,
                    call_id=call_id,
                    dest_call_id=dest_call_id,
                    caller=invite.caller,
                    callee=winner.member,
                    client=client,
                )
                try:
                    await relay.start()
                except Exception:
                    registry.bridge_clients.pop(call_id, None)
                    registry.take_sip_client(dest_call_id)
                    registry.take_client_watcher(dest_call_id)
                    registry.remove_leg(call_id, dest_call_id)
                    await _close_outbound_leg(winner, bye_or_cancel=True)
                    raise
                reservation.detach()
                winner.ports.detach()
                _attach_client_media_update(
                    client,
                    relay,
                    source_call_id=call_id,
                )
                registry.attach_relay(call_id, relay)
                registry.pending_invites.pop(call_id, None)
                consumed_preanswer = registry.take_media(
                    call_id, provisional=True
                )
                # The audio reservation moved to Assist ownership above;
                # any pre-answer video sockets did not and must be released.
                _release_video_media_reservation(consumed_preanswer)
                if preanswered is None or not bool(
                    preanswered.get("final_response_sent", True)
                ):
                    answer = build_answer_directional(
                        local_ip,
                        local_ip,
                        source_relay_port,
                        invite.send_format,
                        invite.recv_format,
                        dtmf=_invite_dtmf_format(invite),
                        remote_sdp=invite.remote_sdp,
                    )
                    _sip_send_final_response(
                        hass, call_id, 200, "OK", answer_sdp=answer
                    )
                connected_party = str(winner.member or "").strip()
                _set_sip_bridge_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    caller=invite.caller,
                    callee=entry.display_name,
                    peer_name=connected_party,
                    call_id=call_id,
                    dest_call_id=dest_call_id,
                    dialed_target=entry.display_name,
                    connected_party=connected_party,
                    answered_by=connected_party,
                    direction="incoming",
                    route_source="automation",
                    route_kind=GROUP_TYPE_RING,
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    sip_uri=str(winner.uri),
                )
                current_task = asyncio.current_task()
                if current_task is not None:
                    registry.attach_client_watcher(dest_call_id, current_task)
                terminal = await client.wait_for_dialog_termination()
                terminal_reason = (
                    TerminalReason.REMOTE_HANGUP.value
                    if terminal == "remote_hangup"
                    else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                )
                await _terminate_sip_bridge(
                    hass, dest_call_id, terminal_reason=terminal_reason
                )
                return

            if decision.action is RouteAction.ASSIST:
                current = registry.sessions.get(
                    registry.resolve_session_id(call_id)
                )
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
                await _start_local_assist_bridge(
                    invite,
                    reservation=reservation,
                    local_rtp_port=source_relay_port,
                    roster_entries=roster_entries,
                    source="trunk" if preanswered is not None else "sip",
                    called_extension=str(
                        (
                            decision.entry.extension
                            if decision.entry is not None
                            else ""
                        )
                        or destination
                    ),
                    release_reservation_on_failure=preanswered is None,
                )
                if preanswered is None or not bool(
                    preanswered.get("final_response_sent", True)
                ):
                    answer = build_answer_directional(
                        local_ip,
                        local_ip,
                        source_relay_port,
                        invite.send_format,
                        invite.recv_format,
                        remote_sdp=invite.remote_sdp,
                    )
                    _sip_send_final_response(
                        hass,
                        call_id,
                        200,
                        "OK",
                        answer_sdp=answer,
                    )
                registry.pending_invites.pop(call_id, None)
                registry.take_media(call_id, provisional=True)
                current = registry.sessions.get(
                    registry.resolve_session_id(call_id)
                )
                if current is not None:
                    registry.transition(
                        call_id,
                        state=CallState.IN_CALL.value,
                        owner="assist",
                        callee=destination,
                        expected_revision=current.revision,
                        expected_owner=current.owner,
                    )
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
                bridge_uri, _peer, member_entry = _sip_uri_for_member(
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
            endpoint_registry = hass.data.get(DOMAIN, {}).get(
                "endpoint_registry"
            )
            source_route_endpoint_id = str(
                ((session.metadata if session is not None else {}) or {}).get(
                    "source_endpoint_id"
                )
                or ""
            ).strip()
            target_route_endpoint_id = str(
                ((decision.entry.metadata if decision.entry is not None else {}) or {}).get(
                    "endpoint_id"
                )
                or ""
            ).strip()
            source_route_endpoint = (
                endpoint_registry.get(source_route_endpoint_id)
                if endpoint_registry is not None and source_route_endpoint_id
                else None
            )
            target_route_endpoint = (
                endpoint_registry.get(target_route_endpoint_id)
                if endpoint_registry is not None and target_route_endpoint_id
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
                    video_failure_reason = (
                        "local_video_resources_unavailable"
                    )
                    _LOGGER.warning(
                        "SIP forward video relay unavailable; continuing audio-only: %s",
                        err,
                    )
            client = SipCallClient(
                local_ip=local_ip,
                local_name=(
                    str(trunk_cfg.get(CONF_TRUNK_USERNAME) or _ha_peer_name(hass))
                    if bridge_to_trunk
                    else invite.caller or _ha_peer_name(hass)
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
                    str(
                        ((decision.entry.metadata or {}).get("user_agent"))
                        or ""
                    )
                    if bridge_to_registered and decision.entry is not None
                    else ""
                ),
                local_video_rtp_port=video_dest_port,
                video_formats=(invite.video_format,) if video_relay is not None else (),
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
                remote_host=bridge_uri.host,
                remote_sip_port=bridge_uri.port or int(cfg["sip_port"]),
                request_uri=str(bridge_uri),
                timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
            )
            bucket = hass.data.setdefault(DOMAIN, {})
            if call_id in bucket.get("trunk_closed_calls", set()):
                bucket["trunk_closed_calls"].discard(call_id)
                raise RuntimeError(TerminalReason.CANCELLED.value)
            if result not in {"ringing", "in_call"}:
                raise RuntimeError(result)

            dest_call_id = client.dialog_ids.call_id
            registry.register_bridge(
                source_call_id=call_id,
                dest_call_id=dest_call_id,
                client=client,
                state=CallState.REMOTE_RINGING.value
                if result == "ringing"
                else CallState.CONNECTING.value,
                caller=invite.caller,
                callee=destination,
                route_kind=decision.action.value,
                source_role="trunk" if preanswered is not None else "caller",
                source_state=(
                    CallState.IN_CALL.value
                    if _source_dialog_is_answered(preanswered)
                    else CallState.CONNECTING.value
                ),
                dest_state=result,
            )
            current_task = asyncio.current_task()
            if current_task is not None:
                registry.attach_client_watcher(dest_call_id, current_task)
            if result == "ringing":
                _set_sip_bridge_call_state(
                    hass,
                    CallState.REMOTE_RINGING.value,
                    caller=invite.caller,
                    callee=destination,
                    peer_name=destination,
                    call_id=call_id,
                    dest_call_id=dest_call_id,
                    direction="incoming",
                    route_source="automation",
                    last_sip_event="SIP_RESPONSE",
                )
                result = await client.wait_for_final()
            if result != "in_call" or client.dialog is None:
                raise RuntimeError(result)

            selected_video = None
            selected_video_direction = "inactive"
            if video_relay is not None:
                video_answer = configure_answered_invite_video_relay(
                    invite, client.dialog, video_relay
                )
                if video_answer is None:
                    _LOGGER.info(
                        "SIP forward video rejected: destination did not accept an exact codec call_id=%s",
                        call_id,
                    )
                    await video_relay.stop()
                    video_relay = None
                    video_failure_reason = "remote_video_rejected"
                else:
                    selected_video = video_answer.video_format
                    selected_video_direction = video_answer.direction

            relay = build_invite_client_relay(
                invite=invite,
                client=client,
                source_relay_port=source_relay_port,
                dest_relay_port=dest_relay_port,
                debug_capture=_debug_mode(hass),
                on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports),
            )
            _attach_dtmf_event_bridge(
                hass,
                relay,
                call_id=call_id,
                dest_call_id=dest_call_id,
                caller=invite.caller,
                callee=destination,
                client=client,
            )
            if video_relay is not None:
                relay.attach_video_relay(video_relay)
            await relay.start()
            reservation.detach()
            _attach_client_media_update(
                client,
                relay,
                source_call_id=call_id,
            )
            registry.attach_relay(call_id, relay)
            registry.pending_invites.pop(call_id, None)
            registry.take_media(call_id, provisional=True)
            if preanswered is None or not bool(
                preanswered.get("final_response_sent", True)
            ):
                answer = build_answer_directional(
                    local_ip,
                    local_ip,
                    source_relay_port,
                    invite.send_format,
                    invite.recv_format,
                    dtmf=_invite_dtmf_format(invite),
                    remote_sdp=invite.remote_sdp,
                    video_port=(
                        video_relay.left_port if video_relay is not None else 0
                    ),
                    video_format=selected_video,
                    video_direction=selected_video_direction,
                )
                _sip_send_final_response(
                    hass, call_id, 200, "OK", answer_sdp=answer
                )
            registry.upsert(
                call_id,
                state=CallState.IN_CALL.value,
                owner="bridge",
                caller=invite.caller,
                callee=destination,
                route_kind=decision.action.value,
            )
            _set_sip_bridge_call_state(
                hass,
                CallState.IN_CALL.value,
                caller=invite.caller,
                callee=destination,
                peer_name=destination,
                call_id=call_id,
                dest_call_id=dest_call_id,
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
                video_active=bool(video_relay is not None),
                video_requested=forward_video_enabled,
                video_negotiated=bool(video_relay is not None),
                video_status=(
                    "degraded"
                    if video_failure_reason
                    == "local_video_resources_unavailable"
                    else "rejected"
                    if video_failure_reason
                    else "active"
                    if video_relay is not None
                    else "inactive"
                ),
                video_failure_reason=video_failure_reason,
                video_format=(
                    selected_video.wire_token() if selected_video else ""
                ),
            )
            terminal = await client.wait_for_dialog_termination()
            terminal_reason = (
                TerminalReason.REMOTE_HANGUP.value
                if terminal == "remote_hangup"
                else _sip_terminal_reason(terminal, _sip_public_state(terminal))
            )
            await _terminate_sip_bridge(
                hass,
                dest_call_id,
                terminal_reason=terminal_reason,
            )
        except asyncio.CancelledError:
            if dest_call_id:
                registry.bridge_clients.pop(call_id, None)
                registry.take_sip_client(dest_call_id)
                registry.take_client_watcher(dest_call_id)
                registry.remove_leg(call_id, dest_call_id)
            if reservation is not None and not reservation_from_preanswer:
                reservation.release()
            if video_relay is not None:
                await video_relay.stop()
                video_relay = None
            if client is not None:
                await async_cleanup_sip_runtime(
                    client=client,
                    terminate_client=True,
                )
            raise
        except Exception as err:  # noqa: BLE001 - convert route failures to policy.
            reason = str(err or TerminalReason.TRANSPORT_UNREACHABLE.value)
            _LOGGER.info(
                "SIP automation forward failed call_id=%s destination=%s reason=%s",
                call_id,
                destination,
                reason,
            )
            if dest_call_id:
                registry.bridge_clients.pop(call_id, None)
                registry.take_sip_client(dest_call_id)
                registry.take_client_watcher(dest_call_id)
                registry.remove_leg(call_id, dest_call_id)
            if reservation is not None and not reservation_from_preanswer:
                reservation.release()
            if video_relay is not None:
                await video_relay.stop()
                video_relay = None
            if client is not None:
                await async_cleanup_sip_runtime(
                    client=client,
                    terminate_client=True,
                )
            await _restore_or_terminate(reason)
        finally:
            forward_tasks.pop(call_id, None)
            forward_claims.discard(call_id)

    task = create_runtime_task(hass, _run_forward())
    forward_tasks[call_id] = task
