"""Forwarding orchestration for an existing HA-owned SIP call."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
import logging
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .config import (
    trunk_config as _get_trunk_config,
    trunk_identity_uri as _trunk_identity_uri,
)
from .call_projection import publish_bridge_projection
from .call_state import CallPrecondition, validate_call_snapshot
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
from .dial_fork import DialDisposition, terminal_reason
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .endpoint_dialing import OutboundLegPolicy
from .runtime_data import (
    call_runtime_artifacts,
    browser_phone,
    endpoint_directory,
    preferred_browser_phone,
    sip_endpoint_runtime,
)
from .endpoint_routing import (
    EndpointRouteResolver,
    roster_from_peers as _roster_from_peers,
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
from .media_ports import RtpPortReservation
from .media_reservation import release_video_media_reservation
from .outbound_attempts import (
    BrowserLeg,
)
from .outbound_bridge_commit import (
    BridgeCommitData,
    BridgeCommitPolicy,
    async_commit_outbound_bridge,
)
from .pbx_routing import unique_group_members as _unique_group_members
from .phone_endpoint import EndpointAvailability
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .ring_group import (
    settle_browser_candidates as _settle_ring_browser_candidates,
)
from .ring_group_fork import build_ring_group_fork, group_leg_observer, run_owned_ring_group_fork
from .router import RouteAction
from .trunk_policy import EXTERNAL_CALL_BLOCKED_ENDPOINTS, call_external_call_denied
from .route_abort import (
    RouteAbortContext,
    RouteAbortIntent,
    async_abort_route,
)
from .core.sdp import build_answer_directional
from .core.sip import sip_default_port
from .session_cleanup import async_cleanup_sip_runtime
from .service_errors import service_error as _service_error
from .sip_client import SIP_TIMER_B
from .sip_runtime import (
    send_final_response as _sip_send_final_response,
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
    browser_leg_for_member: Callable[..., Any]
    defer_invite_to_softphone: Callable[..., None]
    prepare_outbound_leg: Callable[..., Any]
    publish_pending_ringing: Callable[..., None]
    start_local_assist_bridge: Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class TransferResult:
    status: str
    destination: str = ""
    reason: str = ""


class _ForwardRouteFailure(Exception):
    """A route failure after request/source validation, before its worker."""

    def __init__(self, error):
        self.error = error
        super().__init__(str(error))


def _route_preflight(operation):
    try:
        return operation()
    except ServiceValidationError as error:
        raise _ForwardRouteFailure(error) from error


def _routing_service_error(*args, **kwargs):
    return _ForwardRouteFailure(_service_error(*args, **kwargs))


async def async_forward_existing_call(
    runtime: ForwardRuntime,
    *,
    call_id: str,
    destination: str,
    on_failure: str = "resume",
    expected_state: str = "",
    expected_sequence: int = 0,
    initial_selection: bool = False,
    trunk_id: str = "",
    precondition: CallPrecondition | None = None,
    control_endpoint_id: str = "",
    _resolved_decision=None,
    _ring_timeout: float | None = None,
    _queue_offer=None,
) -> asyncio.Task[TransferResult]:
    """Apply route policy after admission, preserving request/source errors."""
    arguments = locals().copy()
    arguments.pop("runtime")
    identity = str(call_id or "").strip()
    policy = str(on_failure or "resume").strip().lower()
    if not identity or not str(destination or "").strip() or policy not in {"resume", "terminate", "busy"}:
        return await _prepare_forward_existing_call(runtime, **arguments)
    registry = _call_registry(runtime.hass)
    session_id = registry.resolve_session_id(identity)
    session = registry.get_session(session_id)
    token = session.token if session is not None else None
    try:
        pending = registry.artifact_for(identity, "pending_route")
        if pending is not None and pending.get("ring_group_endpoint_ids"):
            # Admission is validated before relinquishing the receiving leg.
            # The barrier transfers this same generation, so subsequent checks
            # use its committed state rather than the retired branch sequence.
            validate_call_snapshot(
                registry, identity, expected_state=expected_state,
                expected_sequence=expected_sequence, precondition=precondition,
                endpoint_id=control_endpoint_id,
            )
            from .route_decisions import async_release_pending_fork
            if await async_release_pending_fork(runtime.hass, registry, identity, arguments):
                arguments.update(expected_state="", expected_sequence=0,
                    precondition=CallPrecondition.capture(session), control_endpoint_id="")
        return await _prepare_forward_existing_call(runtime, **arguments)
    except _ForwardRouteFailure as failure:
        if session is None or token is None or not session.owns(token) or registry.get_session(session_id) is not session:
            raise failure.error from failure
        reason = str(getattr(failure.error, "translation_key", "") or "transport_unreachable")
        if policy != "resume":
            await async_abort_route(RouteAbortContext(runtime.hass, registry, session_id, consume_source=True),
                RouteAbortIntent(reason, policy))
        async def completed():
            return TransferResult("failed_resumed" if policy == "resume" else "ended", destination, reason)
        return create_runtime_task(runtime.hass, completed())


async def _prepare_forward_existing_call(
    runtime: ForwardRuntime,
    *,
    call_id: str,
    destination: str,
    on_failure: str = "resume",
    expected_state: str = "",
    expected_sequence: int = 0,
    initial_selection: bool = False,
    trunk_id: str = "",
    precondition: CallPrecondition | None = None,
    control_endpoint_id: str = "",
    _resolved_decision=None,
    _ring_timeout: float | None = None,
    _queue_offer=None,
) -> asyncio.Task[TransferResult]:
    """Route or move one HA-owned pending/ringing call to a target.

    ``initial_selection`` is used only by the bounded ``route_requested``
    decision point. A later forward excludes its previous receiving phone
    from a group; an initial selection has no previous destination to exclude.
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
    pickup = registry.resource_for(call_id, "pickup")
    if pickup is not None and pickup.claimed:
        raise _service_error("This call is being picked up", "call_forwarding", call_id=call_id)
    expected_state = str(expected_state or "").strip().lower()

    def _validate_snapshot():
        return validate_call_snapshot(
            registry, call_id, expected_state=expected_state,
            expected_sequence=expected_sequence, precondition=precondition,
            endpoint_id=control_endpoint_id,
        )

    def submitted_result(status, reason=""):
        async def result():
            return TransferResult(status, destination, reason)
        # These routes submit an existing decision without a new B-leg worker.
        # Still return the same concrete task contract as ordinary forwarding.
        task = create_runtime_task(hass, result())
        registry.own_task(call_id, task)
        return task

    context = _validate_snapshot()
    pending = registry.artifact_for(call_id, "pending_route")
    if pending is not None and pending.get("outgoing_automation"):
        from .route_decisions import set_pending_route_decision

        set_pending_route_decision(hass, {
            "call_id": call_id, "action": "forward", "destination": destination,
            "expected_state": expected_state, "expected_sequence": expected_sequence,
            "on_failure": on_failure, "trunk_id": trunk_id,
        })
        return submitted_result("started")
    route_resolver = None
    resolved_pickup_claim = None
    def validate_resolved_pickup():
        from .parking import ParkingCall
        from .call_pickup import PickupClaim
        parked = registry.resource_for(call_id, "parking")
        directed = registry.resource_for(call_id, "pickup")
        endpoint_id = str((_resolved_decision.entry.metadata if _resolved_decision.entry else {}).get("endpoint_id") or "")
        claim = (directed if isinstance(directed, PickupClaim)
            else parked.parked.claim if isinstance(parked, ParkingCall) else None)
        if (claim is None or (resolved_pickup_claim is not None and claim is not resolved_pickup_claim)
                or not claim.authorizes_browser_answer(registry.get_session(call_id), endpoint_id)):
            raise ValueError("A resolved HA pickup requires its current pickup claim and exact endpoint")
        return claim
    if _resolved_decision is not None and _resolved_decision.action is not RouteAction.GROUP:
        if _resolved_decision.action is not RouteAction.ANSWER_HA or _queue_offer is not None:
            raise ValueError("Internal queue attempts require a resolved ring group")
        resolved_pickup_claim = validate_resolved_pickup()
    if _ring_timeout is not None and not 0 < float(_ring_timeout) <= 120:
        raise ValueError("Internal ring timeout must be positive and at most 120 seconds")
    if _resolved_decision is None and context is not None and len(context.route_history) >= 8:
        raise _service_error(
            f"call_id {call_id} exceeded 8 routing hops",
            "route_hop_limit_exceeded",
            call_id=call_id,
            limit=8,
        )

    def _check_external_route(decision):
        if not cfg.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS):
            return
        from .runtime_data import require_runtime_data
        from .trunk_policy import is_any_trunk_uri

        access_runtime = require_runtime_data(hass)
        if (decision.action is RouteAction.TRUNK or is_any_trunk_uri(
            access_runtime, decision.sip_uri,
        )) and call_external_call_denied(access_runtime, call_id):
            raise _service_error("External calls are disabled for this phone", "external_calls_disabled")

    def current_application():
        return next((value for kind in ("parking", "automation", "queue", "queue_admission", "assist_application")
                     if (value := registry.resource_for(call_id, kind)) is not None), None)

    from .pending_sip_destination import PendingSipDestination
    native_destination = PendingSipDestination.capture(hass, registry, registry.get_session(call_id))
    local_source = registry.resource_for(call_id, "browser_source")
    application = current_application()
    invite = registry.artifact_for(call_id, "pending_invite")
    if invite is None and application is not None:
        application.require_current()
        invite = application.invite
        if invite is not None:
            registry.set_pending_invite(call_id, invite)
    if invite is None and native_destination is not None:
        invite = native_destination.invite
        registry.set_pending_invite(call_id, invite)
    native_caller = invite is not None and registry.artifact_for(call_id, "caller_invite") is invite
    if invite is None:
        from .local_softphone_runtime import local_softphone_bridge

        bridge = local_softphone_bridge(hass)
        local_call = bridge.get_call(call_id) if bridge is not None else None
        if local_call is not None:
            route_resolver = runtime.route_resolver.for_call(call_id, trunk_id=trunk_id)
            peers = await _async_build_peer_snapshot(hass)
            _validate_snapshot()
            if bridge.get_call(call_id) != local_call:
                raise _service_error(
                    f"call_id {call_id} changed while the route was being resolved",
                    "call_changed_during_operation", call_id=call_id,
                )
            roster_entries = _roster_from_peers(hass, peers, _registered_roster_entries(hass))
            decision = route_resolver.route(destination, roster_entries)
            _check_external_route(decision)
            target = runtime.route_resolver.logical_endpoint(
                decision.target or destination, peers, roster_entries,
            )
            if (
                decision.action is RouteAction.ANSWER_HA
                and target is not None
                and target.supports_ha_client
            ):
                from .pbx_routing import browser_endpoint_can_ring

                if not browser_endpoint_can_ring(target) or (
                    target.active_call_id and target.active_call_id != call_id
                ):
                    if on_failure != "resume":
                        from .endpoint_termination import EndpointTerminationHandler
                        from .endpoint_session import TerminationInitiator

                        await EndpointTerminationHandler(hass).terminate_reason(
                            call_id, "busy", TerminationInitiator.ROUTING,
                        )
                    return submitted_result("failed_resumed" if on_failure == "resume" else "ended", "busy")
                bridge.redirect_ringing(call_id, target.endpoint_id)
                registry.record_route(call_id, action="forward", destination=destination, source="automation")
                return submitted_result("ringing")
            if decision.action in {
                RouteAction.DIRECT, RouteAction.FORWARD, RouteAction.BRIDGE,
                RouteAction.TRUNK, RouteAction.ASSIST, RouteAction.GROUP,
                RouteAction.AUTOMATION, RouteAction.VOICEMAIL, RouteAction.PARKING,
                RouteAction.QUEUE, RouteAction.PAGING,
            }:
                from .local_softphone_runtime import PendingLocalSource

                local_source = PendingLocalSource(hass, local_call, local_ip, int(cfg["sip_port"]))
                registry.own_resource(call_id, f"browser_source:{call_id}", local_source, None)
                invite = local_source.invite
                registry.set_pending_invite(call_id, invite)
        if invite is None and context is not None and context.state in {CallState.CALLING.value, CallState.CONNECTING.value, CallState.REMOTE_RINGING.value}:
            current = registry.get_session(call_id)
            if current is not None and current.owner == "ha_softphone" and current.metadata.get("source_endpoint_id"):
                from .local_softphone_runtime import PendingLocalSource

                local_source = PendingLocalSource(hass, None, local_ip, int(cfg["sip_port"]), call_id=call_id)
                registry.own_resource(call_id, f"browser_source:{call_id}", local_source, None)
                invite = local_source.invite
                registry.set_pending_invite(call_id, invite)
        if invite is None:
            raise _service_error(
                f"call_id {call_id} is not a forwardable pending or ringing HA-owned call",
                "call_not_forwardable", call_id=call_id,
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
        current_session = registry.get_session(call_id)
        destination_id = registry.bridge_link_for(call_id)
        destination_leg = current_session.legs.get(destination_id) if current_session else None
        destination_client = registry.sip_client_for(destination_id) if destination_id else None
        ringing_leg = bool(destination_leg is not None
            and destination_leg.state == CallState.REMOTE_RINGING.value
            and destination_client is not None and destination_client.dialog is None)
        if (
            current_context is None
            or (current_context.state != CallState.REMOTE_RINGING.value and not ringing_leg)
        ):
            raise _service_error(
                f"call_id {call_id} is already being forwarded",
                "call_forwarding",
                call_id=call_id,
            )
    if call_artifacts.forward_claim and (current_forward is None or current_forward.done()):
        raise _service_error(f"call_id {call_id} is already being forwarded", "call_forwarding", call_id=call_id)
    # Only a validated forwardable source may apply policy to route selection.
    route_resolver = _route_preflight(lambda: runtime.route_resolver.for_call(call_id, trunk_id=trunk_id))
    if cfg.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS):
        # Reject before cancelling an existing ringing attempt or claiming its
        # route. The normal generation checks below still guard this await.
        try:
            access_peers = await _async_build_peer_snapshot(hass)
            access_roster = _roster_from_peers(
                hass, access_peers, _registered_roster_entries(hass)
            )
            _check_external_route(_route_preflight(lambda: route_resolver.route(destination, access_roster)))
        except BaseException:
            if local_source is not None:
                local_source.resume()
            raise
    # Reject concurrent owners before discovery, then finish preparation before
    # retiring the previous leg and changing our own committed topology.
    peers = await _async_build_peer_snapshot(hass)
    context = _validate_snapshot()
    if artifacts.task_for(call_id, "forward") is not current_forward:
        raise _service_error("The forward owner changed during preparation",
                             "call_changed_during_operation", call_id=call_id)
    if current_forward is not None and not current_forward.done():
        current_forward.cancel()
        await asyncio.gather(current_forward, return_exceptions=True)
    if current_forward is not None and current_forward.done():
        # Completion callbacks run on the next loop turn. Retire this exact
        # finished task before assigning its name to the next forward.
        registry.release_task(call_id, current_forward)
    if call_artifacts.forward_claim:
        raise _service_error(
            f"call_id {call_id} is already being forwarded",
            "call_forwarding",
            call_id=call_id,
        )
    call_artifacts.forward_claim = True
    target_browser_endpoint = None
    try:
        if registry.artifact_for(call_id, "pending_invite") is not invite:
            raise _service_error(
                f"call_id {call_id} changed while the route was being resolved",
                "call_changed_during_operation",
                call_id=call_id,
            )
        if native_destination is not None:
            native_destination.validate()
        if current_application() is not application:
            raise _service_error("The local application changed during preparation",
                                 "call_changed_during_operation", call_id=call_id)
        if application is not None:
            application.require_current()
        roster_entries = _roster_from_peers(
            hass,
            peers,
            _registered_roster_entries(hass),
        )
        decision = _resolved_decision or _route_preflight(lambda: route_resolver.route(destination, roster_entries))
        if resolved_pickup_claim is not None:
            validate_resolved_pickup()
        rejected_queues = set()
        while decision.action is RouteAction.QUEUE and decision.entry is not None:
            from .call_queues import QueueExit, get_queue_manager
            queues = get_queue_manager(hass)
            if queues is None:
                break
            queue_id = decision.entry.metadata["queue_id"]
            if application is not None and application.owner == "queue" and application.ticket.queue_id == queue_id:
                break
            rejection = queues.admission_rejection(queue_id)
            if rejection is None:
                break
            if queue_id in rejected_queues or len(rejected_queues) >= 8 - len(context.route_history if context else ()):
                raise _routing_service_error(f"call_id {call_id} exceeded 8 routing hops", "route_hop_limit_exceeded",
                    call_id=call_id, limit=8)
            rejected_queues.add(queue_id)
            queues.record_rejection(queue_id, call_id, registry.get_session(call_id).generation, rejection.cause)
            if not rejection.destination:
                break
            destination = rejection.destination
            decision = _route_preflight(lambda: route_resolver.route(destination, roster_entries))
            if application is not None and application.owner == "queue" and application.ticket.exit is not None:
                departure = application.ticket.exit
                application.ticket.exit = QueueExit(departure.cause, departure.disposition, destination)
        _check_external_route(decision)
        from .account_dialing import account_dial_decision
        decision = account_dial_decision(hass, decision)
        if decision.action is RouteAction.ANSWER_HA:
            target_browser_endpoint = runtime.route_resolver.logical_endpoint(
                decision.target or destination,
                peers,
                roster_entries,
            )
            if (
                target_browser_endpoint is None
                or not target_browser_endpoint.supports_ha_client
            ):
                raise _routing_service_error(
                    f"destination {destination} is not a configured Home Assistant phone",
                    "destination_not_ha_phone",
                    destination=destination,
                )
            if resolved_pickup_claim is not None and not resolved_pickup_claim.authorizes_browser_answer(
                    registry.get_session(call_id), target_browser_endpoint.endpoint_id):
                raise ValueError("The resolved HA pickup endpoint changed")
        if decision.action is RouteAction.REJECT:
            raise _routing_service_error(
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
            raise _routing_service_error(
                "forwarding an already-ringing call is currently limited to ring groups",
                "forward_ring_group_only",
            )

        last_route = (
            context.route_history[-1] if context and context.route_history else {}
        )
        if _resolved_decision is None and not (
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
        if current_application() is not application:
            raise _service_error("The local application changed during preparation",
                                 "call_changed_during_operation", call_id=call_id)
        if application is not None:
            application.require_current()
        owner_endpoint_id = (
            (local_source.call.caller_endpoint_id if local_source.outgoing or local_source.committed else local_source.call.callee_endpoint_id)
            if local_source is not None else str(
                (session.metadata if session is not None else {}).get("endpoint_id")
                or ""
            )
        )
        # Session ownership must not fall back to the user's preferred phone.
        session_endpoint = (
            browser_phone(hass, owner_endpoint_id)
            if owner_endpoint_id and not (native_caller and local_source is None)
            else None
        )
        if session_endpoint is None and native_caller and session is not None:
            session_endpoint = endpoint_directory(hass).get(str(session.metadata.get("dest_endpoint_id")
                or session.metadata.get("target_endpoint_id") or "")) or endpoint_directory(hass).resolve(session.callee)
        if session_endpoint is None and initial_selection:
            session_endpoint = preferred_browser_phone(hass)
        if session_endpoint is None and application is None and not native_caller:
            raise _service_error(
                f"call_id {call_id} has no Home Assistant phone owner",
                "call_phone_owner_missing",
                call_id=call_id,
            )
        session_endpoint_id = session_endpoint.endpoint_id if session_endpoint is not None else ""
        if (
            not initial_selection
            and target_browser_endpoint is not None
            and target_browser_endpoint.endpoint_id == session_endpoint_id
        ):
            raise _service_error(
                "a Home Assistant phone cannot forward a call to itself",
                "phone_self_target",
            )
        session_device_id = session_endpoint.device_id if session_endpoint is not None else ""
        original_callee = session.callee if session is not None else invite.target
        original_trunk_id = str(session.metadata.get("outbound_trunk_id") or "") if session is not None else ""
        original_route_kind = session.route_kind if session is not None else ""
        # A trunk call is already SIP-answered while its DTMF/automation
        # route is being selected.  It has not populated the HA softphone
        # store yet, but HA is still its default owner.  Preserve that
        # ownership so ``on_failure: resume`` can enter normal ringing
        # instead of leaving the answered caller on silent RTP.
        ha_claimed = application is None and (
            bool(session is not None and session.owner == "ha_softphone")
            or registry.resource_for(call_id, "preanswered") is not None
            or bool(
                session is not None and session.metadata.get("automation_resume_ha")
            )
        )
        if native_destination is not None:
            native_destination.validate()
        if session is not None:
            if (local_source is not None and local_source.outgoing and not local_source.retired
                    and local_source.previous_client.dialog is not None):
                raise _service_error("The original destination already answered", "call_changed_during_operation",
                                     call_id=call_id)
            session.metadata["automation_resume_ha"] = ha_claimed
            route_already_claimed = bool(
                session.state == CallState.CONNECTING.value
                and session.owner == "router"
                and session.callee == destination
            )
            if not route_already_claimed:
                claimed = registry.transition(
                    call_id,
                    state=CallState.IN_CALL.value if application and application.answered else CallState.CONNECTING.value,
                    owner="router",
                    callee=destination,
                    route_kind=decision.action.value,
                    expected_revision=session.revision,
                    expected_owner=session.owner,
                    automation_resume_ha=ha_claimed,
                    **({"outbound_trunk_id": decision.trunk_id} if getattr(decision, "trunk_id", "") else {}),
                    **({"dest_endpoint_id": str((decision.entry.metadata if decision.entry else {}).get("endpoint_id") or ""),
                        "target_endpoint_id": str((decision.entry.metadata if decision.entry else {}).get("endpoint_id") or "")}
                       if local_source is not None else {}),
                )
                if claimed is None:
                    raise _service_error(
                        f"call_id {call_id} changed while forwarding ownership was claimed",
                        "call_changed_during_operation",
                        call_id=call_id,
                    )
        else:
            route_already_claimed = False
        if native_destination is not None:
            await native_destination.retire()
        if local_source is not None:
            await local_source.retire_destination()
            local_source.pause()
        elif application is None and not native_caller:
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
                    direction="outgoing" if local_source is not None else "incoming",
                    route_source="automation",
                    route_kind=decision.action.value,
                    event_type="forwarding",
                    last_sip_event="ROUTE_FORWARD",
                )
    except BaseException:
        call_artifacts.forward_claim = False
        if local_source is not None:
            local_source.resume()
        if native_destination is not None and not native_destination.retired:
            native_destination.restore_preparation()
        if native_destination is not None and native_destination.retired and session.live:
            if on_failure == "resume":
                native_destination.resume()
            else:
                await registry.terminate_call_wait(call_id, reason="forward_cancelled")
        raise

    forward_abort = RouteAbortContext(
        hass,
        registry,
        call_id,
        consume_source=True,
        resume_callee=original_callee,
        resume_route_kind=original_route_kind,
        transition_resume=session is not None,
        resume_owner=application.owner if application else ("router" if local_source.outgoing else "local_bridge") if local_source is not None else "ha_softphone",
        resume_state=("in_call" if application and application.answered
                      else "in_call" if local_source is not None and local_source.established
                      else "remote_ringing" if local_source is not None and local_source.outgoing else "ringing"),
        publish_resume=application.resume if application else partial(
            runtime.publish_pending_ringing,
            invite,
            route_kind=original_route_kind,
            endpoint_id=session_endpoint_id,
            endpoint_device_id=session_device_id,
            callee=original_callee,
            last_sip_event="ROUTE_RESUME",
        )
        if (application is not None or ha_claimed)
        else None,
    )
    if native_destination is not None:
        forward_abort.resume_owner = "router"
        forward_abort.publish_resume = native_destination.resume
    if local_source is not None:
        forward_abort.publish_resume = lambda: local_source.resume(
            redial=local_source.outgoing, destination=original_callee, trunk_id=original_trunk_id,
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
            response_already_sent=response_already_sent or local_source is not None,
            **({
                "endpoint_id": local_source.call.caller_endpoint_id,
                "source_endpoint_id": local_source.call.caller_endpoint_id,
                "media_client_id": local_source.call.caller_media_owner_id,
            } if local_source is not None else {}),
        )

    async def _perform_forward() -> TransferResult | None:
        nonlocal invite
        client = None
        reservation = None
        reservation_from_preanswer = False
        video_relay = None
        dest_call_id = ""
        direct_endpoint_id = ""
        prepared_media = None
        parked_audio_socket = None
        from .parking import ParkingCall
        from .local_call_media import LocalCallMedia
        def local_answer_authorized(endpoint_id):
            from .call_pickup import PickupClaim
            directed = registry.resource_for(call_id, "pickup")
            if isinstance(directed, PickupClaim) and directed.authorizes_browser_answer(session, endpoint_id):
                return True
            parked = application.parked.claim if isinstance(application, ParkingCall) else None
            return parked is not None and parked.authorizes_browser_answer(session, endpoint_id)
        parked_media = (application.media if isinstance(application, ParkingCall)
            and isinstance(application.media, LocalCallMedia)
            and application.media.video_relay is not None
            and registry.resource_for(call_id, "relay") is application.media else None)

        async def retire_application(*, pending_browser=False):
            if application is None:
                return
            if application.owner == "parking":
                claim = application.parked.claim
                if pending_browser and claim is not None and claim.current:
                    return
            await application.close("forwarded")
            registry.release_resource(call_id, application.resource_name, value=application)

        async def _cleanup_failed_route(reason: str) -> None:
            nonlocal video_relay, parked_audio_socket
            if parked_audio_socket is not None:
                parked_audio_socket.close()
                parked_audio_socket = None
            if prepared_media is not None:
                await prepared_media.rollback()
                if prepared_media.committed:
                    await parked_media.stop()
            attached_client = False
            if dest_call_id:
                if registry.bridge_link_for(call_id) == dest_call_id:
                    registry.forget_bridge_link(call_id)
                attached_client = await registry.close_leg(
                    call_id,
                    dest_call_id,
                    reason=reason,
                )
            if direct_endpoint_id and session is not None and session.live:
                registry.release_endpoint_claim(call_id, direct_endpoint_id)
                if session.metadata.get("dest_endpoint_id") == direct_endpoint_id:
                    session.update_metadata(dest_endpoint_id="", target_endpoint_id="")
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
            retained_media = (preanswered or {}).get("retained_source_media")
            if retained_media is not None:
                retained_media.validate(registry)
                invite = retained_media.caller_invite
            if decision.action is RouteAction.ANSWER_HA:
                if resolved_pickup_claim is not None:
                    validate_resolved_pickup()
                endpoint = target_browser_endpoint
                if endpoint is None or not endpoint.supports_ha_client:
                    raise RuntimeError("target Home Assistant phone disappeared")
                if endpoint.dnd:
                    raise RuntimeError("target Home Assistant phone is in DND")
                if endpoint.active_call_id and endpoint.active_call_id != call_id:
                    raise RuntimeError("target Home Assistant phone is busy")
                if endpoint.availability is EndpointAvailability.UNAVAILABLE:
                    raise RuntimeError("target Home Assistant phone is disabled")
                if application is not None and application.media is not None and not local_answer_authorized(endpoint.endpoint_id):
                    from .browser_application_handoff import forward_application_to_browser
                    await forward_application_to_browser(runtime, registry, session, invite, endpoint, application,
                                                         timeout=_ring_timeout or 30)
                    return TransferResult("connected", destination)
                if local_source is not None and (local_source.outgoing or local_source.committed):
                    if application is not None:
                        await application.release_media_for_forward()
                    local_source.redirect_browser(endpoint.endpoint_id)
                    await retire_application(pending_browser=True)
                    return TransferResult("ringing", destination)
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
                    source_id = str(session.metadata.get("source_endpoint_id") or "") if session is not None else ""
                    if session_endpoint_id != endpoint.endpoint_id and not (application is not None and session_endpoint_id == source_id):
                        old_released = (
                            registry.release_endpoint_claim(
                                call_id,
                                session_endpoint_id,
                            )
                            or old_was_claimed
                        )
                    if application is not None and parked_media is None:
                        await application.release_media_for_forward()
                    runtime.defer_invite_to_softphone(
                        invite,
                        route_kind=decision.action.value,
                        endpoint_id=endpoint.endpoint_id,
                        endpoint_device_id=endpoint.device_id,
                        callee=endpoint.name,
                        sip_uri=decision.sip_uri,
                        last_sip_event="ROUTE_FORWARD",
                    )
                    await retire_application(pending_browser=True)
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
                return TransferResult("ringing", destination)

            if decision.action is RouteAction.PARKING:
                from .parking import start_parking_application
                if decision.entry.metadata["parking_mode"] != "deposit":
                    raise RuntimeError("Forward calls to a parking destination, not a pickup slot")
                await start_parking_application(hass, session, invite, local_ip,
                    slot=decision.entry.metadata["parking_slot"], parker=session_endpoint_id, previous=application)
                return

            if decision.action is RouteAction.VOICEMAIL:
                from .voicemail import start_voicemail_application

                mailbox = endpoint_directory(hass).get(decision.entry.metadata["mailbox_endpoint_id"])
                if mailbox is None:
                    raise RuntimeError("The selected mailbox is no longer available")
                await start_voicemail_application(
                    hass, session, invite, mailbox, local_ip,
                    previous=application, local_source=local_source,
                )
                return

            if decision.action is RouteAction.AUTOMATION:
                from .automation_call import attach_automation_application, queue_automation_request

                current = registry.get_session(call_id)
                if current is None or decision.entry is None:
                    raise RuntimeError("Automation route is no longer available")
                claimed = registry.transition(
                    call_id, owner="automation",
                    state=CallState.IN_CALL.value if _source_dialog_is_answered(preanswered) else CallState.RINGING.value,
                    callee=decision.entry.display_name, route_kind="automation",
                    expected_owner=current.owner, expected_revision=current.revision,
                )
                if claimed is None:
                    raise RuntimeError("Automation route ownership changed")
                if application is None or application.owner != "automation":
                    next_application = attach_automation_application(
                        hass, current, invite, decision.entry, local_ip, local_source=local_source, previous=application,
                    )
                    if application is not None:
                        await application.close("forwarded")
                        name = "assist_application" if application.owner == "assist" else application.owner
                        registry.release_resource(call_id, f"{name}:{call_id}", value=application)
                else:
                    # Transfer the existing media/application resource rather
                    # than binding a second receiver to its RTP port.
                    application.cancel_deadline()
                    application.contact = decision.entry
                    application.controller = ""
                    next_application = application
                queue_automation_request(next_application)
                return

            if decision.action in {RouteAction.QUEUE, RouteAction.PAGING}:
                from .queue_call import start_queue_application, transfer_queue_application
                from .paging import start_paging_application

                if decision.entry is None:
                    raise RuntimeError("The application destination disappeared")
                if application is not None and application.owner == "queue" and decision.action is RouteAction.QUEUE:
                    await transfer_queue_application(hass, session, invite, decision.entry, local_ip, application)
                    await retire_application()
                    return TransferResult("connected", destination)
                if application is not None and application.owner == decision.action.value:
                    raise RuntimeError("A call cannot re-enter the same application")
                factory = start_queue_application if decision.action is RouteAction.QUEUE else start_paging_application
                factory(hass, session, invite, decision.entry, local_ip,
                        previous=application, local_source=local_source)
                if application is not None:
                    await application.close("forwarded")
                    name = "assist_application" if application.owner == "assist" else application.owner
                    registry.release_resource(call_id, f"{name}:{call_id}", value=application)
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
                            resume_owner="local_bridge" if local_source is not None else "ha_softphone",
                        ),
                        RouteAbortIntent(reason, "cleanup"),
                    )

                try:
                    from .dial_plan import RingPolicy
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
                        ring_policy=(RingPolicy(overall_timeout=_ring_timeout,
                                                step_timeout=_ring_timeout)
                                     if _resolved_decision is not None and _ring_timeout is not None else None),
                        source_endpoint_id=str(
                            (session.metadata if session is not None else {}).get(
                                "source_endpoint_id"
                            ) or ""
                        ),
                        excluded_endpoint_ids=(
                            frozenset({session_endpoint_id})
                            if not initial_selection and application is None and session_endpoint_id
                            else frozenset()
                        ),
                    )
                    if retained_media is not None:
                        for attempt in attempts:
                            attempt.client.local_rtp_port = retained_media.reservation.ports[1]
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
                            direction="outgoing" if local_source is not None else "incoming",
                            route_source="automation",
                            route_kind=GROUP_TYPE_RING,
                            last_sip_event="SIP_RESPONSE",
                        )

                base_leg_state = group_leg_observer(hass, registry, authoritative_session)
                offers = {}
                def offer_start(endpoint_id, *, rang=False):
                    if _queue_offer is None or not endpoint_id:
                        return
                    manager, ticket = _queue_offer
                    if endpoint_id not in offers:
                        offers[endpoint_id] = [manager.begin_offer(ticket, endpoint_id), rang, False]
                    elif rang:
                        offers[endpoint_id][1] = True
                def offer_end(endpoint_id, outcome):
                    row = offers.get(endpoint_id)
                    if _queue_offer is not None and row is not None and not row[2]:
                        manager, ticket = _queue_offer
                        manager.finish_offer(ticket, endpoint_id, outcome, row[1], row[0])
                        row[2] = True
                def leg_state(outbound, state):
                    base_leg_state(outbound, state)
                    if state in {"calling", "remote_ringing"}:
                        offer_start(outbound.endpoint_id, rang=state == "remote_ringing")
                    elif state != "closed":
                        offer_end(outbound.endpoint_id, state)
                for browser_leg in browser_legs:
                    offer_start(browser_leg.endpoint_id, rang=True)
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
                    on_leg_state=leg_state,
                    **({"ring_timeout": _ring_timeout} if _ring_timeout is not None else {}),
                )
                fork_result = None
                pickup_gate = winner = None
                try:
                    fork_result, pickup_gate = await run_owned_ring_group_fork(
                        authoritative_session, fork_candidates, candidate_payloads,
                        invite=invite, attempts=attempts, browser_legs=browser_legs, on_leg_state=leg_state,
                        commit_winner=lambda _candidate, _outcome: registry.is_generation_current(
                            call_id, call_generation),
                        **({"overall_timeout": _ring_timeout} if _ring_timeout is not None else {}),
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
                finally:
                    if fork_result is not None and fork_result.winner is not None:
                        winner = candidate_payloads.get(fork_result.winner.candidate_id)
                    winning_leg = pickup_gate.current.payload if winner is pickup_gate and pickup_gate is not None else winner
                    winner_id = str(getattr(winning_leg, "endpoint_id", "") or "")
                    missing = "timeout" if fork_result is not None and fork_result.outcome.disposition is DialDisposition.TIMEOUT else "cancelled"
                    for endpoint_id in offers:
                        if endpoint_id == winner_id and isinstance(winning_leg, BrowserLeg):
                            # The existing media-ready commit owns a browser answer.
                            continue
                        offer_end(endpoint_id, "in_call" if endpoint_id == winner_id else missing)

                if winner is pickup_gate:
                    if pickup_gate.current.payload is None:
                        registry.take_pending_route(call_id)
                        _settle_browser_candidates(CallState.CANCELLED.value, TerminalReason.CANCELLED.value)
                        if not reservation_from_preanswer:
                            reservation.release()
                        return
                    winner = pickup_gate.current.payload
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
                    if application is not None and application.media is not None and not local_answer_authorized(winner.endpoint_id):
                        from .browser_application_handoff import forward_application_to_browser
                        endpoint = endpoint_registry.get(winner.endpoint_id)
                        if endpoint is None:
                            raise RuntimeError("The selected browser disappeared")
                        await forward_application_to_browser(runtime, registry, session, invite, endpoint, application,
                            timeout=_ring_timeout or 30, accepted_decision=browser_decision)
                        if not reservation_from_preanswer:
                            reservation.release()
                        return TransferResult("connected", destination)
                    if local_source is not None:
                        local_source.connect_browser(
                            winner.endpoint_id,
                            str(browser_decision.get("media_client_id") or ""),
                            send_video=bool(browser_decision.get("send_video", False)),
                            accept_video=bool(browser_decision.get("accept_video", True)),
                        )
                        reservation.release()
                        return TransferResult("connected", destination)
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
                    if application is not None:
                        await application.release_media_for_forward()
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
                                **({"accept_video": browser_decision["accept_video"]}
                                   if "accept_video" in browser_decision else {}),
                            },
                            blocking=True,
                            context=registry.ha_context(call_id),
                        )
                    finally:
                        call_artifacts.answer_commit = False
                    await retire_application()
                    if not reservation_from_preanswer:
                        reservation.release()
                    if winner.pickup:
                        publish_bridge_projection(hass, session, event_type="picked_up", route_kind="pickup")
                    return TransferResult("connected", destination)

                registry.take_pending_route(call_id)
                _settle_browser_candidates(
                    CallState.CANCELLED.value,
                    TerminalReason.CANCELLED.value,
                    retain_claim_endpoint_id=winner.endpoint_id,
                )

                client = winner.client
                dest_call_id = client.dialog_ids.call_id
                dest_relay_port = winner.ports.ports[1]
                source_ports = reservation.ports
                winner_ports = winner.ports.ports
                if retained_media is not None:
                    dest_relay_port = retained_media.reservation.ports[1]
                    winner.ports.release()
                response_already_sent = _source_dialog_is_answered(preanswered)
                connected_party = str(winner.member or "").strip()

                if application is not None:
                    await application.release_media_for_forward()
                if local_source is not None:
                    local_source.commit()
                committed = await async_commit_outbound_bridge(
                    hass,
                    registry,
                    BridgeCommitData(
                        invite=invite,
                        winner=winner,
                        source_relay_port=source_relay_port,
                        dest_relay_port=dest_relay_port,
                        local_ip=local_ip,
                        release_port_pairs=((source_ports,) if retained_media is not None else (source_ports, winner_ports)),
                        detach_reservations=((reservation,) if retained_media is not None else (reservation, winner.ports)),
                        enable_video_transcoding=bool(
                            cfg.get(CONF_VIDEO_TRANSCODING, False)
                        ),
                        local_endpoint_id=local_source.call.caller_endpoint_id if local_source is not None else "",
                        source_client=application.source_client if application is not None else None,
                        retained_media=retained_media,
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
                        local_source=local_source is not None,
                        endpoint_id=local_source.call.caller_endpoint_id if local_source is not None else "",
                        source_endpoint_id=local_source.call.caller_endpoint_id if local_source is not None else str(authoritative_session.metadata.get("source_endpoint_id") or ""),
                        dest_endpoint_id=winner.endpoint_id,
                        media_client_id=local_source.call.caller_media_owner_id if local_source is not None else "",
                        expected_generation=session.generation if session is not None else None,
                    ),
                )
                if committed is None:
                    raise RuntimeError(TerminalReason.CANCELLED.value)
                await retire_application()
                if local_source is not None:
                    local_source.connected(connected_party, GROUP_TYPE_RING)
                if session is not None:
                    publish_bridge_projection(
                        hass,
                        session,
                        peer_name=connected_party,
                        dialed_target=entry.display_name,
                        connected_party=connected_party,
                        answered_by=connected_party,
                        direction="outgoing" if local_source is not None else "incoming",
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
                if application is not None and application.owner == "parking":
                    await application.stop_music()
                current = registry.sessions.get(registry.resolve_session_id(call_id))
                if current is not None:
                    claimed_assist = registry.transition(
                        call_id,
                        state=CallState.IN_CALL.value if application and application.answered else CallState.CONNECTING.value,
                        owner="assist",
                        callee=destination,
                        expected_revision=current.revision,
                        expected_owner=current.owner,
                    )
                    if claimed_assist is None:
                        raise RuntimeError("Assist route ownership changed")
                assist_invite = invite
                if local_source is not None:
                    assist_invite = local_source.stage_application_media(invite, source_relay_port, dest_relay_port)
                await runtime.start_local_assist_bridge(
                    assist_invite,
                    reservation=reservation,
                    local_rtp_port=source_relay_port,
                    roster_entries=roster_entries,
                    source="browser" if local_source is not None else "trunk" if preanswered is not None else "sip",
                    called_extension=str(
                        (decision.entry.extension if decision.entry is not None else "")
                        or destination
                    ),
                    release_reservation_on_failure=preanswered is None,
                    **({"existing_media": application.media} if application and application.media else {}),
                )
                if local_source is not None:
                    local_source.commit(keep_media=True)
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
                transferred_media = registry.take_media(call_id, provisional=True)
                if application is not None and application.media is not None:
                    application.media.release_reservation_on_stop = True
                release_video_media_reservation(transferred_media)
                answer_result = await _commit_source_answer(
                    answer,
                    owner="assist",
                    callee=destination,
                    route_kind=decision.action.value,
                    response_already_sent=response_already_sent,
                )
                if not answer_result.committed:
                    raise RuntimeError(answer_result.reason)
                await retire_application()
                if local_source is not None:
                    local_source.connected(destination, decision.action.value)
                return

            bridge_to_trunk = decision.action is RouteAction.TRUNK
            target = decision.target or destination
            if bridge_to_trunk:
                selected_trunk_id = getattr(decision, "trunk_id", "")
                trunk_cfg = _get_trunk_config(hass, trunk_id=selected_trunk_id) if selected_trunk_id else _get_trunk_config(hass)
                uri_override = (
                    f"sip:{target}@{trunk_cfg[CONF_TRUNK_SERVER]}:"
                    f"{int(trunk_cfg[CONF_TRUNK_PORT])};"
                    f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
                )
            else:
                uri_override = str(decision.sip_uri or "")

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
            forward_video_enabled = bool(
                (preanswered is None or parked_media is not None)
                and cfg.get(CONF_SIP_VIDEO, False)
                and invite.video_format is not None
            )
            video_transcoding_enabled = bool(
                forward_video_enabled and cfg.get(CONF_VIDEO_TRANSCODING, False)
            )
            selected_trunk_id = getattr(decision, "trunk_id", "")
            trunk_cfg = ((_get_trunk_config(hass, trunk_id=selected_trunk_id) if selected_trunk_id else _get_trunk_config(hass))
                         if bridge_to_trunk else {})
            direct_winner = runtime.prepare_outbound_leg(
                member=target,
                peers=peers,
                roster_entries=roster_entries,
                local_name=invite.caller or _ha_peer_name(hass),
                local_rtp_port_index=1,
                uri_override=uri_override,
                peer_user_agent_override=str(
                    (
                        ((decision.entry.metadata or {}).get("user_agent"))
                        if decision.entry is not None
                        else ""
                    )
                    or ""
                ),
                invite=invite,
                port_reservation=reservation,
                roster_entry_override=decision.entry,
                policy=OutboundLegPolicy(
                    local_uri_user=(
                        str(
                            trunk_cfg.get(CONF_TRUNK_USERNAME)
                            or _ha_peer_name(hass)
                        )
                        if bridge_to_trunk
                        else invite.routing_caller or _ha_peer_name(hass)
                    ),
                    local_identity_uri=(
                        _trunk_identity_uri(trunk_cfg) if bridge_to_trunk else ""
                    ),
                    auth_username=str(
                        trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or ""
                    ),
                    username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or ""),
                    password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or ""),
                    outbound_proxy=str(
                        trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or ""
                    ),
                    force_common_audio=bridge_to_trunk,
                    reuse_registered_flow=not bridge_to_trunk,
                    use_trunk_flow=bridge_to_trunk,
                    trunk_id=selected_trunk_id if bridge_to_trunk else "",
                    allow_video=forward_video_enabled,
                    retained_source_media=retained_media,
                ),
            )
            if direct_winner is None:
                raise RuntimeError(
                    f"destination {destination} has no reachable SIP leg"
                )
            bridge_uri = direct_winner.uri
            client = direct_winner.client
            dest_call_id = client.dialog_ids.call_id
            candidate_endpoint_id = direct_winner.endpoint_id or target_route_endpoint_id
            if candidate_endpoint_id and session is not None:
                if candidate_endpoint_id == source_route_endpoint_id:
                    raise RuntimeError("A caller cannot be forwarded to its own phone")
                if any(resource.name.startswith("retired_destination:")
                       and resource.value.endpoint_id == candidate_endpoint_id for resource in session.resources):
                    raise RuntimeError("The selected phone is still ending its previous call")
                registry.claim_endpoint(call_id, candidate_endpoint_id, role="destination")
                direct_endpoint_id = candidate_endpoint_id
                session.update_metadata(dest_endpoint_id=direct_endpoint_id, target_endpoint_id=direct_endpoint_id)
            registry.add_leg(call_id, dest_call_id, role="callee", state=CallState.CALLING.value)
            registry.attach_sip_client(call_id, dest_call_id, client, role="callee", state=CallState.CALLING.value,
                                      trunk_id=selected_trunk_id if bridge_to_trunk else "")
            if direct_endpoint_id:
                registry.observe_leg(call_id, dest_call_id, role="callee", endpoint_id=direct_endpoint_id,
                                     generation=session.generation)
            if session is not None and bridge_to_trunk:
                session.update_metadata(outbound_trunk_id=selected_trunk_id)
            video_relay = direct_winner.video_relay
            video_failure_reason = direct_winner.video_failure_reason
            if parked_media is not None:
                if video_relay is None:
                    raise RuntimeError("The pickup target cannot retain the parked video")
                # The held relay already owns its four video sockets. Retire
                # only this unused dial candidate, then advertise the held
                # destination port. Bind audio before the first INVITE too.
                await video_relay.stop()
                video_relay = direct_winner.video_relay = None
                client.local_video_rtp_port = parked_media.video_relay.right_port
                from .media_ports import bind_sip_audio_socket
                parked_audio_socket = direct_winner.ports.take_audio_socket(dest_relay_port)
                if parked_audio_socket is None:
                    parked_audio_socket = bind_sip_audio_socket(dest_relay_port)
            result = await client.invite(
                target=bridge_uri.user,
                target_display_name=(
                    decision.entry.display_name
                    if decision.entry is not None
                    else destination
                ),
                remote_host=bridge_uri.host,
                remote_sip_port=sip_default_port(bridge_uri),
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
                registry.observe_leg(call_id, dest_call_id, role="callee",
                    state=CallState.REMOTE_RINGING.value, generation=session.generation)
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
                        direction="outgoing" if local_source is not None else "incoming",
                        route_source="automation",
                        last_sip_event="SIP_RESPONSE",
                    )
                # Keep the source and reservation with the route until the
                # destination answers. A rejected ringing attempt must still
                # be eligible for on_failure: resume.
                result = await client.wait_for_final()
                if result != "in_call" or client.dialog is None:
                    raise RuntimeError(result)
            if parked_media is not None:
                from .local_call_media import ApplicationCallSource
                from .local_media_bridge import LocalMediaBridge
                destination_source = ApplicationCallSource.from_client(client, call_id=dest_call_id,
                    caller=destination, allow_video=True)
                update = await application.prepare_video_pickup(destination_source)
                prepared_media = await LocalMediaBridge.prepare(parked_media, destination_source,
                    video_update=update, right_socket=parked_audio_socket)
                parked_audio_socket = None
                prepared_media.validate()
            elif application is not None:
                await application.release_media_for_forward()
            if local_source is not None:
                local_source.commit()
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
                    local_endpoint_id=local_source.call.caller_endpoint_id if local_source is not None else "",
                    source_client=application.source_client if application is not None else None,
                    prepared_media=prepared_media,
                    retained_media=retained_media,
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
                    local_source=local_source is not None,
                    endpoint_id=local_source.call.caller_endpoint_id if local_source is not None else "",
                    source_endpoint_id=local_source.call.caller_endpoint_id if local_source is not None else source_route_endpoint_id,
                    dest_endpoint_id=direct_endpoint_id,
                    media_client_id=local_source.call.caller_media_owner_id if local_source is not None else "",
                    expected_generation=session.generation if session is not None else None,
                ),
            )
            if committed is None:
                raise RuntimeError(TerminalReason.CANCELLED.value)
            if parked_media is not None:
                await application.release_media_for_forward()
            await retire_application()
            if local_source is not None:
                local_source.connected(destination, decision.action.value)
            failure = committed.video_failure_reason or video_failure_reason
            if session is not None:
                publish_bridge_projection(
                    hass,
                    session,
                    peer_name=destination,
                    direction="outgoing" if local_source is not None else "incoming",
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
            resumable_media = parked_media or (application.media if application is not None else None)
            if (resumable_media is not None and session.live
                    and registry.resource_for(call_id, "relay") is resumable_media
                    and not resumable_media.closed.is_set()):
                if await async_abort_route(forward_abort, RouteAbortIntent(TerminalReason.CANCELLED.value, "resume")):
                    return TransferResult("failed_resumed", destination, "cancelled")
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
            resumed = await async_abort_route(
                forward_abort,
                RouteAbortIntent(
                    reason,
                    "terminate" if local_source is not None and local_source.committed and application is None else on_failure,
                ),
            )
            return TransferResult("failed_resumed" if resumed else "ended", destination, reason)
        finally:
            call_artifacts.forward_claim = False
        return TransferResult("connected" if session is not None and session.live else "ended", destination)

    async def _run_forward():
        result = await _perform_forward()
        if result is not None:
            return result
        if session is None or not session.live:
            return TransferResult("ended", destination)
        if session.state in {"ringing", "remote_ringing", "connecting", "calling"}:
            return TransferResult("ringing", destination)
        return TransferResult("connected", destination)

    from .automation_context import current_execution

    # The PBX owns the forward after the requesting HA action has returned.
    forward_context = copy_context()
    forward_context.run(current_execution.set, None)
    task = forward_context.run(create_runtime_task, hass, _run_forward())
    if not artifacts.own_task(call_id, task, name="forward"):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        call_artifacts.forward_claim = False
        raise _service_error(
            f"call_id {call_id} is no longer active",
            "call_inactive",
            call_id=call_id,
        )
    return task
