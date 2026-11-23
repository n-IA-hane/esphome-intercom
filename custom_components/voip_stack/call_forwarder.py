"""Admit a forward against the current PBX owners, then hand the session one task."""

from __future__ import annotations

import asyncio
import logging
from contextvars import copy_context
from dataclasses import replace
from functools import partial

from homeassistant.exceptions import ServiceValidationError

from .call_projection import stage_bridge_presentation
from .call_state import CallPrecondition, validate_call_snapshot
from .endpoint_lifecycle import call_registry as _call_registry
from .endpoint_lifecycle import create_runtime_task
from .endpoint_routing import (
    roster_from_peers as _roster_from_peers,
)
from .forward_operation import ForwardOperation, validate_resolved_pickup
from .forward_plan import (
    ForwardPlan,
    ForwardRequest,
    ForwardRuntime,
    ForwardSource,
    TransferResult,
)
from .fsm import (
    CallState,
)
from .groups import GROUP_TYPE_RING
from .peer_snapshot import async_build_peer_snapshot as _async_build_peer_snapshot
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .route_abort import (
    RouteAbortContext,
    RouteAbortIntent,
    async_abort_route,
)
from .router import RouteAction
from .runtime_data import (
    browser_phone,
    call_runtime_artifacts,
    endpoint_directory,
    preferred_browser_phone,
)
from .service_errors import service_error as _service_error
from .trunk_policy import registry_external_call_denied
from .browser_phone_runtime import _release_ha_softphone_claim

_LOGGER = logging.getLogger(__name__)


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
    """Admit a forward and return the task owned by the existing call session."""
    request = ForwardRequest(
        call_id=str(call_id or "").strip(),
        destination=str(destination or "").strip(),
        on_failure=str(on_failure or "resume").strip().lower(),
        expected_state=str(expected_state or "").strip().lower(),
        expected_sequence=expected_sequence,
        initial_selection=initial_selection,
        trunk_id=trunk_id,
        precondition=precondition,
        control_endpoint_id=control_endpoint_id,
        resolved_decision=_resolved_decision,
        ring_timeout=_ring_timeout,
        queue_offer=_queue_offer,
    )
    if not request.call_id or not request.destination:
        raise _service_error(
            "call_id and destination are required", "call_id_destination_required"
        )
    if request.on_failure not in {"resume", "terminate", "busy"}:
        raise _service_error(
            "on_failure must be resume, terminate, or busy",
            "forward_failure_policy_invalid",
        )
    registry = _call_registry(runtime.hass)
    session_id = registry.resolve_session_id(request.call_id)
    session = registry.get_session(session_id)
    token = session.token if session is not None else None
    try:
        pending = registry.artifact_for(request.call_id, "pending_route")
        if pending is not None and pending.get("ring_group_endpoint_ids"):
            validate_call_snapshot(
                registry,
                request.call_id,
                expected_state=request.expected_state,
                expected_sequence=request.expected_sequence,
                precondition=request.precondition,
                endpoint_id=request.control_endpoint_id,
            )
            from .route_decisions import async_release_pending_fork

            if await async_release_pending_fork(
                runtime.hass,
                registry,
                request.call_id,
                {
                    "destination": request.destination,
                    "expected_state": request.expected_state,
                    "expected_sequence": request.expected_sequence,
                    "on_failure": request.on_failure,
                    "trunk_id": request.trunk_id,
                },
            ):
                request = replace(
                    request,
                    expected_state="",
                    expected_sequence=0,
                    precondition=CallPrecondition.capture(session),
                    control_endpoint_id="",
                )
        return await _ForwardAdmission(runtime, request, registry).prepare()
    except _ForwardRouteFailure as failure:
        if (
            session is None
            or token is None
            or not session.owns(token)
            or registry.get_session(session_id) is not session
        ):
            raise failure.error from failure
        reason = str(
            getattr(failure.error, "translation_key", "") or "transport_unreachable"
        )
        if request.on_failure != "resume":
            await async_abort_route(
                RouteAbortContext(
                    runtime.hass, registry, session_id, consume_source=True
                ),
                RouteAbortIntent(reason, request.on_failure),
            )

        async def completed():
            return TransferResult(
                "failed_resumed" if request.on_failure == "resume" else "ended",
                request.destination,
                reason,
            )

        return create_runtime_task(runtime.hass, completed())


class _ForwardAdmission:
    """Own the temporary forward claim only until it is handed to its worker."""

    def __init__(self, runtime, request, registry):
        self.runtime = runtime
        self.request = request
        self.registry = registry
        self.pickup_claim = None

    def validate_snapshot(self):
        return validate_call_snapshot(
            self.registry,
            self.request.call_id,
            expected_state=self.request.expected_state,
            expected_sequence=self.request.expected_sequence,
            precondition=self.request.precondition,
            endpoint_id=self.request.control_endpoint_id,
        )

    def submitted_result(self, status, reason=""):
        async def result():
            return TransferResult(status, self.request.destination, reason)

        # These routes submit an existing decision without a new B-leg worker.
        # Still return the same concrete task contract as ordinary forwarding.
        task = create_runtime_task(self.runtime.hass, result())
        self.registry.own_task(self.request.call_id, task)
        return task

    def validate_pickup(self):
        decision = self.request.resolved_decision
        endpoint_id = str(
            (decision.entry.metadata if decision.entry else {}).get("endpoint_id") or ""
        )
        return validate_resolved_pickup(
            self.registry, self.request.call_id, endpoint_id, self.pickup_claim
        )

    def validate_request(self, context):
        if (
            self.request.resolved_decision is not None
            and self.request.resolved_decision.action is not RouteAction.GROUP
            and not self.request.initial_deposit
        ):
            if (
                self.request.resolved_decision.action is not RouteAction.ANSWER_HA
                or self.request.queue_offer is not None
            ):
                raise ValueError(
                    "Internal queue attempts require a resolved ring group"
                )
            self.pickup_claim = self.validate_pickup()
        if (
            self.request.ring_timeout is not None
            and not 0 < float(self.request.ring_timeout) <= 120
        ):
            raise ValueError(
                "Internal ring timeout must be positive and at most 120 seconds"
            )
        if (
            self.request.resolved_decision is None
            and context is not None
            and context.route_hops >= 8
        ):
            raise _service_error(
                f"call_id {self.request.call_id} exceeded 8 routing hops",
                "route_hop_limit_exceeded",
                call_id=self.request.call_id,
                limit=8,
            )

    def check_external_route(self, decision):
        if not registry_external_call_denied(
            self.runtime.config, self.registry, self.request.call_id
        ):
            return
        from .runtime_data import require_runtime_data
        from .trunk_policy import is_any_trunk_uri

        access_runtime = require_runtime_data(self.runtime.hass)
        if decision.action is RouteAction.TRUNK or is_any_trunk_uri(
            access_runtime, decision.sip_uri
        ):
            raise _service_error(
                "External calls are disabled for this phone", "external_calls_disabled"
            )

    def current_application(self):
        return next(
            (
                value
                for kind in (
                    "parking",
                    "automation",
                    "queue",
                    "queue_admission",
                    "assist_application",
                )
                if (value := self.registry.resource_for(self.request.call_id, kind))
                is not None
            ),
            None,
        )

    async def capture_source(self, context):
        registry = self.registry
        hass = self.runtime.hass
        call_id = self.request.call_id
        destination = self.request.destination
        from .pending_sip_destination import PendingSipDestination

        native_destination = PendingSipDestination.capture(
            hass, registry, registry.get_session(call_id)
        )
        local_source = registry.resource_for(call_id, "browser_source")
        application = self.current_application()
        invite = registry.artifact_for(call_id, "pending_invite")
        from .owned_sip_source import OwnedSipSource

        owned_sip_source = None
        if local_source is None and application is None and native_destination is None:
            owned_sip_source = OwnedSipSource.capture(hass, registry.get_session(call_id),
                                                     self.request.control_endpoint_id)
        if owned_sip_source is not None:
            invite = owned_sip_source.invite
        if invite is None and application is not None:
            application.require_current()
            invite = application.invite
            if invite is not None:
                registry.set_pending_invite(call_id, invite)
        if invite is None and native_destination is not None:
            invite = native_destination.invite
            registry.set_pending_invite(call_id, invite)
        native_caller = (
            invite is not None
            and registry.artifact_for(call_id, "caller_invite") is invite
        )
        if invite is None:
            from .local_softphone_runtime import local_softphone_bridge

            bridge = local_softphone_bridge(hass)
            local_call = bridge.get_call(call_id) if bridge is not None else None
            if local_call is not None:
                route_resolver = self.runtime.route_resolver.for_call(
                    call_id, trunk_id=self.request.trunk_id
                )
                peers = await _async_build_peer_snapshot(hass)
                self.validate_snapshot()
                if bridge.get_call(call_id) != local_call:
                    raise _service_error(
                        f"call_id {call_id} changed while the route was being resolved",
                        "call_changed_during_operation",
                        call_id=call_id,
                    )
                roster_entries = _roster_from_peers(
                    hass, peers, _registered_roster_entries(hass)
                )
                decision = route_resolver.route(destination, roster_entries)
                self.check_external_route(decision)
                target = self.runtime.route_resolver.logical_endpoint(
                    decision.target or destination,
                    peers,
                    roster_entries,
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
                        if self.request.on_failure != "resume":
                            from .endpoint_session import TerminationInitiator
                            from .endpoint_termination import EndpointTerminationHandler

                            await EndpointTerminationHandler(hass).terminate_reason(
                                call_id,
                                "busy",
                                TerminationInitiator.ROUTING,
                            )
                        return None, self.submitted_result(
                            "failed_resumed"
                            if self.request.on_failure == "resume"
                            else "ended",
                            "busy",
                        )
                    bridge.redirect_ringing(call_id, target.endpoint_id)
                    registry.record_route(
                        call_id,
                        action="forward",
                        destination=destination,
                        source="automation",
                    )
                    return None, self.submitted_result("ringing")
                if decision.action in {
                    RouteAction.DIRECT,
                    RouteAction.FORWARD,
                    RouteAction.BRIDGE,
                    RouteAction.TRUNK,
                    RouteAction.ASSIST,
                    RouteAction.GROUP,
                    RouteAction.AUTOMATION,
                    RouteAction.VOICEMAIL,
                    RouteAction.PARKING,
                    RouteAction.QUEUE,
                    RouteAction.PAGING,
                }:
                    from .local_softphone_runtime import PendingLocalSource

                    local_source = PendingLocalSource(
                        hass,
                        local_call,
                        self.runtime.local_ip,
                        int(self.runtime.config["sip_port"]),
                    )
                    registry.own_resource(
                        call_id, f"browser_source:{call_id}", local_source, None
                    )
                    invite = local_source.invite
                    registry.set_pending_invite(call_id, invite)
            if (
                invite is None
                and context is not None
                and context.state
                in {
                    CallState.CALLING.value,
                    CallState.CONNECTING.value,
                    CallState.REMOTE_RINGING.value,
                }
            ):
                current = registry.get_session(call_id)
                if (
                    current is not None
                    and current.owner == "ha_softphone"
                    and current.metadata.get("source_endpoint_id")
                ):
                    from .local_softphone_runtime import PendingLocalSource

                    local_source = PendingLocalSource(
                        hass,
                        None,
                        self.runtime.local_ip,
                        int(self.runtime.config["sip_port"]),
                        call_id=call_id,
                    )
                    registry.own_resource(
                        call_id, f"browser_source:{call_id}", local_source, None
                    )
                    invite = local_source.invite
                    registry.set_pending_invite(call_id, invite)
            if invite is None:
                raise _service_error(
                    f"call_id {call_id} is not a forwardable pending or ringing HA-owned call",
                    "call_not_forwardable",
                    call_id=call_id,
                )
        source = ForwardSource(
            call_id=call_id,
            invite=invite,
            application=application,
            local=local_source,
            native_destination=native_destination,
            native_caller=native_caller,
            owned_sip_source=owned_sip_source,
        )
        return source, None

    def previous_worker(self, artifacts, call_artifacts):
        registry = self.registry
        call_id = self.request.call_id
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
            destination_leg = (
                current_session.legs.get(destination_id) if current_session else None
            )
            destination_client = (
                registry.sip_client_for(destination_id) if destination_id else None
            )
            ringing_leg = bool(
                destination_leg is not None
                and destination_leg.state == CallState.REMOTE_RINGING.value
                and destination_client is not None
                and destination_client.dialog is None
            )
            if current_context is None or (
                current_context.state != CallState.REMOTE_RINGING.value
                and not ringing_leg
            ):
                raise _service_error(
                    f"call_id {call_id} is already being forwarded",
                    "call_forwarding",
                    call_id=call_id,
                )
        if call_artifacts.forward_claim and (
            current_forward is None or current_forward.done()
        ):
            raise _service_error(
                f"call_id {call_id} is already being forwarded",
                "call_forwarding",
                call_id=call_id,
            )
        return current_forward

    async def acquire_claim(
        self, source, artifacts, call_artifacts, previous, resolver
    ):
        hass = self.runtime.hass
        call_id = self.request.call_id
        if registry_external_call_denied(self.runtime.config, self.registry, call_id):
            # Reject before cancelling an existing ringing attempt or claiming its
            # route. The normal generation checks below still guard this await.
            try:
                access_peers = await _async_build_peer_snapshot(hass)
                access_roster = _roster_from_peers(
                    hass, access_peers, _registered_roster_entries(hass)
                )
                self.check_external_route(
                    _route_preflight(
                        lambda: resolver.route(self.request.destination, access_roster)
                    )
                )
            except BaseException:
                if source.local is not None:
                    source.local.resume()
                raise
        # Reject concurrent owners before discovery, then finish preparation before
        # retiring the previous leg and changing our own committed topology.
        peers = await _async_build_peer_snapshot(hass)
        context = self.validate_snapshot()
        if artifacts.task_for(call_id, "forward") is not previous:
            raise _service_error(
                "The forward owner changed during preparation",
                "call_changed_during_operation",
                call_id=call_id,
            )
        if previous is not None and not previous.done():
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        if previous is not None and previous.done():
            # Completion callbacks run on the next loop turn. Retire this exact
            # finished task before assigning its name to the next forward.
            self.registry.release_task(call_id, previous)
        if call_artifacts.forward_claim:
            raise _service_error(
                f"call_id {call_id} is already being forwarded",
                "call_forwarding",
                call_id=call_id,
            )
        return peers, context

    def resolve_target(self, source, peers, context, route_resolver):
        registry = self.registry
        hass = self.runtime.hass
        call_id = self.request.call_id
        application = source.application
        destination = self.request.destination
        target_browser_endpoint = None
        if source.owned_sip_source is not None:
            source.owned_sip_source.validate()
        elif registry.artifact_for(call_id, "pending_invite") is not source.invite:
            raise _service_error(
                f"call_id {call_id} changed while the route was being resolved",
                "call_changed_during_operation",
                call_id=call_id,
            )
        if source.native_destination is not None:
            source.native_destination.validate()
        if self.current_application() is not application:
            raise _service_error(
                "The local application changed during preparation",
                "call_changed_during_operation",
                call_id=call_id,
            )
        if application is not None:
            application.require_current()
        roster_entries = _roster_from_peers(
            hass,
            peers,
            _registered_roster_entries(hass),
        )
        def resolve_destination(target):
            from .inbound_routing.targets import resolve_phone_target

            selected = self.request.resolved_decision or route_resolver.route(target, roster_entries)
            resolution = resolve_phone_target(
                decision=selected, endpoint_registry=endpoint_directory(hass),
                roster_entries=roster_entries, route_resolver=route_resolver.route,
                call_id=call_id, apply_policy=self.request.resolved_decision is None,
            )
            if resolution.failure is not None:
                return replace(resolution.decision, action=RouteAction.REJECT,
                               status=resolution.failure.status)
            return resolution.decision

        decision = _route_preflight(lambda: resolve_destination(destination))
        if self.pickup_claim is not None:
            self.validate_pickup()
        rejected_queues = set()
        while decision.action is RouteAction.QUEUE and decision.entry is not None:
            from .call_queues import QueueExit, get_queue_manager

            queues = get_queue_manager(hass)
            if queues is None:
                break
            queue_id = decision.entry.metadata["queue_id"]
            if (
                application is not None
                and application.owner == "queue"
                and application.ticket.queue_id == queue_id
            ):
                break
            rejection = queues.admission_rejection(queue_id)
            if rejection is None:
                break
            if queue_id in rejected_queues or len(rejected_queues) >= 8 - (context.route_hops if context else 0):
                raise _routing_service_error(
                    f"call_id {call_id} exceeded 8 routing hops",
                    "route_hop_limit_exceeded",
                    call_id=call_id,
                    limit=8,
                )
            rejected_queues.add(queue_id)
            queues.record_rejection(
                queue_id,
                call_id,
                registry.get_session(call_id).generation,
                rejection.cause,
            )
            if not rejection.destination:
                break
            destination = rejection.destination
            decision = _route_preflight(
                lambda: resolve_destination(destination)
            )
            if (
                application is not None
                and application.owner == "queue"
                and application.ticket.exit is not None
            ):
                departure = application.ticket.exit
                application.ticket.exit = QueueExit(
                    departure.cause, departure.disposition, destination
                )
        self.check_external_route(decision)
        from .account_dialing import account_call_plan, account_dial_decision

        decision = account_dial_decision(hass, decision, all_transports=True)
        account_plan = (
            account_call_plan(decision.entry)
            if decision.action is RouteAction.GROUP
            else None
        )
        account_endpoint = endpoint_directory(hass).get(account_plan.endpoint_id) if account_plan else None
        if account_plan is not None:
            account_plan = replace(account_plan, route_kind="transfer")
        if decision.action is RouteAction.ANSWER_HA:
            endpoint_id = str(decision.entry.metadata.get("endpoint_id") or "") if decision.entry is not None else ""
            if endpoint_id:
                target_browser_endpoint = endpoint_directory(hass).get(endpoint_id)
            else:
                target_browser_endpoint = self.runtime.route_resolver.logical_endpoint(
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
            if (
                self.pickup_claim is not None
                and not self.pickup_claim.authorizes_browser_answer(
                    registry.get_session(call_id), target_browser_endpoint.endpoint_id
                )
            ):
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
        if self.request.resolved_decision is None and not (
            last_route.get("action") in {"forward", "bridge"}
            and last_route.get("destination") == destination
        ):
            registry.record_route(
                call_id,
                action="forward",
                destination=destination,
                source="automation",
            )
        return ForwardPlan(
            destination=destination,
            decision=decision,
            peers=tuple(peers),
            roster_entries=tuple(roster_entries),
            account=account_plan,
            browser_endpoint=target_browser_endpoint,
            initial_selection=self.request.initial_selection,
            preselected=self.request.resolved_decision is not None,
            ring_timeout=(self.request.ring_timeout if self.request.ring_timeout is not None
                          else account_endpoint.policy.ringing_timeout if account_endpoint is not None
                          else target_browser_endpoint.policy.ringing_timeout
                          if target_browser_endpoint is not None else None),
            on_failure=self.request.on_failure,
        )

    def resolve_receiver(self, plan, source, session):
        hass = self.runtime.hass
        local_source = source.local
        owner_endpoint_id = (
            (
                local_source.call.caller_endpoint_id
                if local_source.outgoing or local_source.committed
                else local_source.call.callee_endpoint_id
            )
            if local_source is not None
            else str(
                (session.metadata if session is not None else {}).get("endpoint_id")
                or ""
            )
        )
        # Session ownership must not fall back to the user's preferred phone.
        session_endpoint = (
            browser_phone(hass, owner_endpoint_id)
            if owner_endpoint_id and not (source.native_caller and local_source is None)
            else None
        )
        if session_endpoint is None and source.native_caller and session is not None:
            session_endpoint = endpoint_directory(hass).get(
                str(
                    session.metadata.get("dest_endpoint_id")
                    or session.metadata.get("target_endpoint_id")
                    or ""
                )
            ) or endpoint_directory(hass).resolve(session.callee)
        if (
            session_endpoint is None
            and self.request.initial_selection
            and not self.request.initial_deposit
        ):
            session_endpoint = preferred_browser_phone(hass)
        if (
            session_endpoint is None
            and source.application is None
            and not source.native_caller
            and not self.request.initial_deposit
        ):
            raise _service_error(
                f"call_id {self.request.call_id} has no Home Assistant phone owner",
                "call_phone_owner_missing",
                call_id=self.request.call_id,
            )
        session_endpoint_id = (
            session_endpoint.endpoint_id if session_endpoint is not None else ""
        )
        if (
            not self.request.initial_selection
            and plan.browser_endpoint is not None
            and plan.browser_endpoint.endpoint_id == session_endpoint_id
        ):
            raise _service_error(
                "a Home Assistant phone cannot forward a call to itself",
                "phone_self_target",
            )
        return session_endpoint

    def claim_source(self, source, plan, session, ha_claimed):
        call_id = self.request.call_id
        decision = plan.decision
        destination = plan.destination
        local_source = source.local
        account_plan = plan.account
        if source.native_destination is not None:
            source.native_destination.validate()
        if session is not None:
            if (
                local_source is not None
                and local_source.outgoing
                and not local_source.established
                and not local_source.retired
                and local_source.previous_client.dialog is not None
            ):
                raise _service_error(
                    "The original destination already answered",
                    "call_changed_during_operation",
                    call_id=call_id,
                )
            session.metadata["automation_resume_ha"] = ha_claimed
            route_already_claimed = bool(
                session.state == CallState.CONNECTING.value
                and session.owner == "router"
                and session.callee == destination
            )
            if not route_already_claimed:
                claimed = self.registry.transition(
                    call_id,
                    state=CallState.IN_CALL.value
                    if source.application and source.application.answered
                    else CallState.CONNECTING.value,
                    owner="router",
                    callee=destination,
                    route_kind=plan.account.route_kind
                    if plan.account
                    else plan.decision.action.value,
                    expected_revision=session.revision,
                    expected_owner=session.owner,
                    automation_resume_ha=ha_claimed,
                    **(
                        {"outbound_trunk_id": decision.trunk_id}
                        if getattr(decision, "trunk_id", "")
                        else {}
                    ),
                    **(
                        {
                            "dest_endpoint_id": account_plan.endpoint_id,
                            "target_endpoint_id": account_plan.endpoint_id,
                        }
                        if account_plan
                        else {
                            "dest_endpoint_id": str(
                                (decision.entry.metadata if decision.entry else {}).get(
                                    "endpoint_id"
                                )
                                or ""
                            ),
                            "target_endpoint_id": str(
                                (decision.entry.metadata if decision.entry else {}).get(
                                    "endpoint_id"
                                )
                                or ""
                            ),
                        }
                        if local_source is not None
                        else {}
                    ),
                )
                if claimed is None:
                    raise _service_error(
                        f"call_id {call_id} changed while forwarding ownership was claimed",
                        "call_changed_during_operation",
                        call_id=call_id,
                    )
                stage_bridge_presentation(
                    claimed,
                    peer_name=destination,
                    direction="outgoing" if local_source is not None else "incoming",
                    route_source="automation",
                    event_type="forwarding",
                    last_sip_event="ROUTE_FORWARD",
                )
        else:
            route_already_claimed = False

    async def retire_receiver(self, source, plan, receiver_id):
        if source.owned_sip_source is not None:
            source.owned_sip_source.validate()
            return
        local_source = source.local
        if source.native_destination is not None:
            await source.native_destination.retire()
        if local_source is not None:
            await local_source.retire_destination()
            local_source.pause()
        elif source.application is None and not source.native_caller and receiver_id:
            _release_ha_softphone_claim(
                self.runtime.hass,
                self.request.call_id,
                destination=plan.destination,
                endpoint_id=receiver_id,
            )

    async def restore_preparation(self, source):
        if source.local is not None:
            source.local.resume()
        if (
            source.native_destination is not None
            and not source.native_destination.retired
        ):
            await source.native_destination.restore_preparation()
        if (
            source.native_destination is not None
            and source.native_destination.retired
            and source.session is not None
            and source.session.live
        ):
            if self.request.on_failure == "resume":
                await source.native_destination.resume()
            else:
                await self.registry.terminate_call_wait(
                    self.request.call_id, reason="forward_cancelled"
                )

    def abort_context(
        self,
        source,
        original_callee,
        original_route_kind,
        original_trunk_id,
        receiver_device_id,
        ha_claimed,
    ):
        application = source.application
        local_source = source.local
        forward_abort = RouteAbortContext(
            self.runtime.hass,
            self.registry,
            self.request.call_id,
            consume_source=True,
            resume_callee=original_callee,
            resume_route_kind=original_route_kind,
            transition_resume=source.session is not None,
            resume_owner=application.owner
            if application
            else ("router" if local_source.outgoing else "local_bridge")
            if local_source is not None
            else "ha_softphone",
            resume_state=(
                "in_call"
                if application and application.answered
                else "in_call"
                if local_source is not None and local_source.established
                else "remote_ringing"
                if local_source is not None and local_source.outgoing
                else "ringing"
            ),
            publish_resume=application.resume
            if application
            else partial(
                self.runtime.publish_pending_ringing,
                source.invite,
                route_kind=original_route_kind,
                endpoint_id=source.receiver_endpoint_id,
                endpoint_device_id=receiver_device_id,
                callee=original_callee,
                last_sip_event="ROUTE_RESUME",
            )
            if (application is not None or ha_claimed)
            else None,
        )
        if source.native_destination is not None:
            forward_abort.resume_owner = "router"
            forward_abort.publish_resume = source.native_destination.resume
        if local_source is not None:
            forward_abort.publish_resume = lambda: local_source.resume(
                redial=local_source.outgoing,
                destination=original_callee,
                trunk_id=original_trunk_id,
            )
        if source.owned_sip_source is not None:
            forward_abort.resume_owner = "ha_softphone"
            forward_abort.resume_state = "in_call"
            forward_abort.publish_resume = None
        return forward_abort

    async def prepare(self):
        from .automation_context import current_execution

        request = self.request
        call_id = request.call_id
        pickup = self.registry.resource_for(call_id, "pickup")
        if pickup is not None and pickup.claimed:
            raise _service_error(
                "This call is being picked up", "call_forwarding", call_id=call_id
            )
        context = self.validate_snapshot()
        pending = self.registry.artifact_for(call_id, "pending_route")
        if pending is not None and pending.get("outgoing_automation"):
            from .route_decisions import set_pending_route_decision

            set_pending_route_decision(
                self.runtime.hass,
                {
                    "call_id": call_id,
                    "action": "forward",
                    "destination": request.destination,
                    "expected_state": request.expected_state,
                    "expected_sequence": request.expected_sequence,
                    "on_failure": request.on_failure,
                    "trunk_id": request.trunk_id,
                },
            )
            return self.submitted_result("started")
        self.validate_request(context)
        source, submitted = await self.capture_source(context)
        if submitted is not None:
            return submitted
        artifacts = call_runtime_artifacts(self.runtime.hass)
        call_artifacts = artifacts.artifacts_for(call_id)
        previous = self.previous_worker(artifacts, call_artifacts)
        resolver = _route_preflight(
            lambda: self.runtime.route_resolver.for_call(
                call_id, trunk_id=request.trunk_id
            )
        )
        peers, context = await self.acquire_claim(
            source, artifacts, call_artifacts, previous, resolver
        )
        try:
            plan = self.resolve_target(source, peers, context, resolver)
            session = self.registry.sessions.get(
                self.registry.resolve_session_id(call_id)
            )
            if self.current_application() is not source.application:
                raise _service_error(
                    "The local application changed during preparation",
                    "call_changed_during_operation",
                    call_id=call_id,
                )
            if source.application is not None:
                source.application.require_current()
            receiver = self.resolve_receiver(plan, source, session)
            source = replace(
                source,
                session=session,
                receiver_endpoint_id=receiver.endpoint_id
                if receiver is not None
                else "",
            )
            original_callee = (
                session.callee if session is not None else source.invite.target
            )
            original_trunk_id = (
                str(session.metadata.get("outbound_trunk_id") or "")
                if session is not None
                else ""
            )
            original_route_kind = session.route_kind if session is not None else ""
            ha_claimed = (
                source.application is None
                and not request.initial_deposit
                and (
                    bool(session is not None and session.owner == "ha_softphone")
                    or self.registry.resource_for(call_id, "preanswered") is not None
                    or bool(
                        session is not None
                        and session.metadata.get("automation_resume_ha")
                    )
                )
            )
            self.claim_source(source, plan, session, ha_claimed)
            await self.retire_receiver(source, plan, source.receiver_endpoint_id)
        except BaseException:
            call_artifacts.forward_claim = False
            await self.restore_preparation(source)
            raise
        abort = self.abort_context(
            source,
            original_callee,
            original_route_kind,
            original_trunk_id,
            receiver.device_id if receiver is not None else "",
            ha_claimed,
        )
        operation = ForwardOperation(
            self.runtime,
            self.registry,
            source,
            plan,
            call_artifacts,
            abort,
            queue_offer=request.queue_offer,
            pickup_claim=self.pickup_claim,
        )
        # The session owns this task after the public HA action returns.
        forward_context = copy_context()
        forward_context.run(current_execution.set, None)
        task = forward_context.run(
            create_runtime_task, self.runtime.hass, operation.run()
        )
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
