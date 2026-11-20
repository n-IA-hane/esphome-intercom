"""Prepare and settle group candidates through the PBX's existing fork controller."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .call_projection import publish_bridge_projection
from .const import (
    CONF_VIDEO_TRANSCODING,
    DOMAIN,
)
from .dial_fork import DialDisposition, terminal_reason
from .forward_plan import TransferResult, source_dialog_is_answered
from .fsm import (
    CallState,
    TerminalReason,
)
from .group_candidates import (
    GroupCandidateRuntime,
    GroupCandidates,
    async_prepare_group_candidates,
)
from .groups import GROUP_TYPE_RING
from .outbound_attempts import (
    BrowserLeg,
)
from .outbound_bridge_commit import (
    BridgeCommitData,
    BridgeCommitPolicy,
    async_commit_outbound_bridge,
)
from .pbx_routing import unique_group_members as _unique_group_members
from .ring_group import (
    settle_browser_candidates as _settle_ring_browser_candidates,
)
from .ring_group_fork import (
    account_offer_outcome,
    build_ring_group_fork,
    group_leg_observer,
    run_owned_ring_group_fork,
)
from .route_abort import (
    RouteAbortContext,
    RouteAbortIntent,
    async_abort_route,
)
from .runtime_data import (
    endpoint_directory,
    sip_endpoint_runtime,
)
from .websocket_api import (
    _ha_peer_name,
)

if TYPE_CHECKING:
    from .forward_operation import ForwardOperation

_LOGGER = logging.getLogger(f"{__package__}.call_forwarder")


class ForwardGroup:
    """One group's candidate list, observer and results within the forward task."""

    def __init__(self, operation: ForwardOperation):
        self.operation = operation
        self.entry = operation.plan.decision.entry
        if self.entry is None:
            raise RuntimeError("ring group has no roster entry")
        self.candidates = GroupCandidates()
        self.endpoints = endpoint_directory(operation.runtime.hass)
        self.route_kind = (
            operation.plan.account.route_kind
            if operation.plan.account
            else GROUP_TYPE_RING
        )
        self.offers = {}
        self.branch_results = {}

    def settle(self, state, reason, **kwargs):
        _settle_ring_browser_candidates(
            self.operation.runtime.hass,
            self.operation.registry,
            self.candidates.browser_legs,
            call_id=self.operation.source.call_id,
            caller=self.operation.invite.caller,
            callee=self.entry.display_name,
            route_kind=self.route_kind,
            state=state,
            reason=reason,
            **kwargs,
        )

    async def abort(self, reason, *, cleanup=True):
        await async_abort_route(
            RouteAbortContext(
                self.operation.runtime.hass,
                self.operation.registry,
                self.operation.source.call_id,
                settle=self.settle,
                attempts=self.candidates.attempts if cleanup else (),
                resume_owner="local_bridge"
                if self.operation.source.local is not None
                else "ha_softphone",
            ),
            RouteAbortIntent(reason, "cleanup"),
        )

    async def prepare(self):
        registry = self.operation.registry
        account_plan = self.operation.plan.account
        members = _unique_group_members(self.entry.metadata.get("members"))
        if account_plan and self.endpoints.get(account_plan.endpoint_id) is None:
            raise RuntimeError("account disappeared before forwarding")
        try:
            from .dial_plan import RingPolicy

            await async_prepare_group_candidates(
                self.candidates,
                GroupCandidateRuntime(
                    registry=registry,
                    endpoint_registry=self.endpoints,
                    browser_leg_for_member=self.operation.runtime.browser_leg_for_member,
                    prepare_outbound_leg=self.operation.runtime.prepare_outbound_leg,
                    logical_endpoint_for_member=self.operation.runtime.route_resolver.logical_endpoint,
                ),
                invite=self.operation.invite,
                members=members,
                peers=self.operation.plan.peers,
                roster_entries=self.operation.plan.roster_entries,
                local_name=self.operation.invite.caller
                or _ha_peer_name(self.operation.runtime.hass),
                ring_policy=(
                    RingPolicy(
                        overall_timeout=self.operation.plan.ring_timeout,
                        step_timeout=self.operation.plan.ring_timeout,
                    )
                    if self.operation.plan.preselected
                    and self.operation.plan.ring_timeout is not None
                    else account_plan.ring_policy
                    if account_plan
                    else None
                ),
                source_endpoint_id=str(
                    (
                        self.operation.source.session.metadata
                        if self.operation.source.session is not None
                        else {}
                    ).get("source_endpoint_id")
                    or ""
                ),
                excluded_endpoint_ids=(
                    frozenset({self.operation.source.receiver_endpoint_id})
                    if not self.operation.plan.initial_selection
                    and self.operation.source.application is None
                    and self.operation.source.receiver_endpoint_id
                    else frozenset()
                ),
            )
            if self.operation.retained_media is not None:
                for attempt in self.candidates.attempts:
                    attempt.client.local_rtp_port = (
                        self.operation.retained_media.reservation.ports[1]
                    )
        except Exception:
            await self.abort(
                TerminalReason.PROTOCOL_ERROR.value,
            )
            raise
        if not self.candidates.attempts and not self.candidates.browser_legs:
            raise RuntimeError("ring group has no reachable members")

        current_session = registry.sessions.get(
            registry.resolve_session_id(self.operation.source.call_id)
        )
        self.generation = (
            current_session.generation if current_session is not None else 0
        )
        pbx_runtime = sip_endpoint_runtime(self.operation.runtime.hass)
        self.session = (
            pbx_runtime.get_session(
                self.operation.source.call_id,
                generation=self.generation,
            )
            if pbx_runtime is not None and self.generation
            else None
        )
        if self.session is None:
            await self.abort(
                TerminalReason.CANCELLED.value,
            )
            raise RuntimeError("forward source call is no longer current")

    def publish_offers(self):
        invite = self.operation.invite
        self.route_future = asyncio.get_running_loop().create_future()
        self.operation.registry.set_pending_route(
            self.operation.source.call_id,
            {
                "invite": invite,
                "future": self.route_future,
                "ring_group_endpoint_ids": tuple(
                    leg.endpoint_id for leg in self.candidates.browser_legs
                ),
                "declined_endpoint_ids": set(),
            },
        )
        if self.candidates.browser_legs:
            self.operation.registry.upsert(
                self.operation.source.call_id,
                state=CallState.RINGING.value,
                owner="router",
                caller=invite.caller,
                callee=self.entry.display_name,
                route_kind=self.route_kind,
                ring_endpoint_ids=tuple(
                    leg.endpoint_id for leg in self.candidates.browser_legs
                ),
            )
            for browser_leg in self.candidates.browser_legs:
                self.operation.runtime.publish_pending_ringing(
                    invite,
                    route_kind=self.route_kind,
                    endpoint_id=browser_leg.endpoint_id,
                    endpoint_device_id=browser_leg.device_id,
                    callee=self.entry.display_name,
                    last_sip_event="ROUTE_FORWARD",
                )

    def publish_ringing(self):
        ringing_session = self.operation.registry.transition(
            self.operation.source.call_id,
            state=CallState.REMOTE_RINGING.value,
            caller=self.operation.invite.caller,
            callee=self.entry.display_name,
            route_kind=self.route_kind,
        )
        if ringing_session is not None:
            publish_bridge_projection(
                self.operation.runtime.hass,
                ringing_session,
                peer_name=self.entry.display_name,
                direction="outgoing"
                if self.operation.source.local is not None
                else "incoming",
                route_source="automation",
                route_kind=self.route_kind,
                last_sip_event="SIP_RESPONSE",
            )

    def offer_start(self, endpoint_id, *, rang=False):
        if self.operation.queue_offer is None or not endpoint_id:
            return
        manager, ticket = self.operation.queue_offer
        if endpoint_id not in self.offers:
            self.offers[endpoint_id] = [
                manager.begin_offer(ticket, endpoint_id),
                rang,
                False,
            ]
        elif rang:
            self.offers[endpoint_id][1] = True

    def offer_end(self, endpoint_id, outcome):
        row = self.offers.get(endpoint_id)
        if self.operation.queue_offer is not None and row is not None and not row[2]:
            manager, ticket = self.operation.queue_offer
            manager.finish_offer(ticket, endpoint_id, outcome, row[1], row[0])
            row[2] = True

    def leg_state(self, outbound, state):
        self.observe_leg(outbound, state)
        if state in {"calling", "remote_ringing"}:
            self.offer_start(outbound.endpoint_id, rang=state == "remote_ringing")
        elif state != "closed" and self.operation.queue_offer is not None:
            self.branch_results.setdefault(outbound.endpoint_id, {})[
                outbound.client.dialog_ids.call_id
            ] = state

    def finish_offers(self, fork_result, winner, pickup_gate):
        winning_leg = (
            pickup_gate.current.payload
            if winner is pickup_gate and pickup_gate is not None
            else winner
        )
        winner_id = str(getattr(winning_leg, "endpoint_id", "") or "")
        for endpoint_id in self.offers:
            if endpoint_id == winner_id:
                # QueueCall.connected owns the media-ready answer,
                # for SIP and HA transports of the same account.
                continue
            if (
                winner_id
                or fork_result is None
                or fork_result.outcome.disposition
                in {
                    DialDisposition.CANCELLED,
                    DialDisposition.SOURCE_CANCELLED,
                    DialDisposition.REROUTE,
                }
            ):
                outcome = "cancelled"
            else:
                results = self.branch_results.get(endpoint_id, {})
                # Owner close withdraws an unanswered future; it is
                # not an agent's busy/decline decision.
                pending_browser = (
                    not self.route_future.done() or self.route_future.cancelled()
                ) and any(
                    leg.endpoint_id == endpoint_id
                    for leg in self.candidates.browser_legs
                )
                pending_sip = (
                    fork_result.outcome.disposition is DialDisposition.TIMEOUT
                    and any(
                        attempt.endpoint_id == endpoint_id
                        and attempt.client.dialog_ids.call_id not in results
                        for attempt in self.candidates.attempts
                    )
                )
                outcome = account_offer_outcome(
                    list(results.values()), pending=pending_browser or pending_sip
                )
            self.offer_end(endpoint_id, outcome)

    async def select_winner(self):
        account_plan = self.operation.plan.account
        self.observe_leg = group_leg_observer(
            self.operation.runtime.hass, self.operation.registry, self.session
        )
        for browser_leg in self.candidates.browser_legs:
            self.offer_start(browser_leg.endpoint_id, rang=True)
        (
            fork_candidates,
            candidate_payloads,
            browser_decision,
        ) = build_ring_group_fork(
            sip_port=int(self.operation.runtime.config["sip_port"]),
            route_future=self.route_future,
            attempts=self.candidates.attempts,
            browser_legs=self.candidates.browser_legs,
            preflight_failures=[],
            # A SIP branch's 180 must leave the same account's incoming
            # browser offer answerable until either client wins.
            on_ringing=None
            if account_plan
            and self.candidates.browser_legs
            and self.operation.source.local is None
            else self.publish_ringing,
            on_leg_state=self.leg_state,
        )
        fork_result = None
        pickup_gate = winner = None
        try:
            fork_result, pickup_gate = await run_owned_ring_group_fork(
                self.session,
                fork_candidates,
                candidate_payloads,
                invite=self.operation.invite,
                attempts=self.candidates.attempts,
                browser_legs=self.candidates.browser_legs,
                on_leg_state=self.leg_state,
                commit_winner=lambda _candidate, _outcome: (
                    self.operation.registry.is_generation_current(
                        self.operation.source.call_id, self.generation
                    )
                ),
                **(
                    {"overall_timeout": self.operation.plan.ring_timeout}
                    if self.operation.plan.ring_timeout is not None
                    else {
                        "overall_timeout": account_plan.ring_policy.overall_timeout,
                        "step_timeout": account_plan.ring_policy.step_timeout,
                    }
                    if account_plan
                    else {}
                ),
            )
        except asyncio.CancelledError:
            await self.abort(
                TerminalReason.CANCELLED.value,
                cleanup=False,
            )
            raise
        except Exception:
            await self.abort(
                TerminalReason.PROTOCOL_ERROR.value,
            )
            raise
        finally:
            if fork_result is not None and fork_result.winner is not None:
                winner = candidate_payloads.get(fork_result.winner.candidate_id)
            self.finish_offers(fork_result, winner, pickup_gate)
        return fork_result, winner, pickup_gate, browser_decision

    async def commit_browser(self, winner, browser_decision):
        registry = self.operation.registry
        call_id = self.operation.source.call_id
        destination = self.operation.plan.destination
        application = self.operation.source.application
        invite = self.operation.invite
        registry.take_pending_route(call_id)
        self.settle(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
            keep_endpoint_id=winner.endpoint_id,
        )
        if (
            application is not None
            and application.media is not None
            and not self.operation.local_answer_authorized(winner.endpoint_id)
        ):
            from .browser_application_handoff import forward_application_to_browser

            endpoint = self.endpoints.get(winner.endpoint_id)
            if endpoint is None:
                raise RuntimeError("The selected browser disappeared")
            await forward_application_to_browser(
                self.operation.runtime,
                registry,
                self.operation.source.session,
                invite,
                endpoint,
                application,
                timeout=self.operation.plan.ring_timeout or 30,
                accepted_decision=browser_decision,
            )
            if not self.operation.borrowed_reservation:
                self.operation.reservation.release()
            return TransferResult("connected", destination)
        if self.operation.source.local is not None:
            self.operation.source.local.connect_browser(
                winner.endpoint_id,
                str(browser_decision.get("media_client_id") or ""),
                send_video=bool(browser_decision.get("send_video", False)),
                accept_video=bool(browser_decision.get("accept_video", True)),
            )
            self.operation.reservation.release()
            return TransferResult("connected", destination)
        registry.upsert(
            call_id,
            state=CallState.RINGING.value,
            owner="ha_softphone",
            caller=invite.caller,
            callee=self.entry.display_name,
            route_kind=self.route_kind,
            endpoint_id=winner.endpoint_id,
            session_device_id=winner.device_id,
        )
        registry.set_pending_invite(call_id, invite)
        if application is not None:
            await application.release_media_for_forward()
        self.operation.artifacts.answer_commit = True
        try:
            await self.operation.runtime.hass.services.async_call(
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
                    "send_video": bool(browser_decision.get("send_video", False)),
                    **(
                        {"accept_video": browser_decision["accept_video"]}
                        if "accept_video" in browser_decision
                        else {}
                    ),
                },
                blocking=True,
                context=registry.ha_context(call_id),
            )
        finally:
            self.operation.artifacts.answer_commit = False
        await self.operation.retire_application()
        if not self.operation.borrowed_reservation:
            self.operation.reservation.release()
        if winner.pickup:
            publish_bridge_projection(
                self.operation.runtime.hass,
                self.operation.source.session,
                event_type="picked_up",
                route_kind="pickup",
            )
        return TransferResult("connected", destination)

    async def commit_sip(self, winner):
        session = self.operation.source.session
        application = self.operation.source.application
        local_source = self.operation.source.local
        invite = self.operation.invite
        self.operation.registry.take_pending_route(self.operation.source.call_id)
        self.settle(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
            retain_claim_endpoint_id=winner.endpoint_id,
        )

        self.operation.client = winner.client
        self.operation.destination_call_id = self.operation.client.dialog_ids.call_id
        dest_relay_port = winner.ports.ports[1]
        source_ports = self.operation.reservation.ports
        winner_ports = winner.ports.ports
        if self.operation.retained_media is not None:
            dest_relay_port = self.operation.retained_media.reservation.ports[1]
            winner.ports.release()
        response_already_sent = source_dialog_is_answered(self.operation.preanswered)
        connected_party = str(winner.member or "").strip()

        if application is not None:
            await application.release_media_for_forward()
        if local_source is not None:
            local_source.commit()
        committed = await async_commit_outbound_bridge(
            self.operation.runtime.hass,
            self.operation.registry,
            BridgeCommitData(
                invite=invite,
                winner=winner,
                source_relay_port=self.operation.reservation.ports[0],
                dest_relay_port=dest_relay_port,
                local_ip=self.operation.runtime.local_ip,
                release_port_pairs=(
                    (source_ports,)
                    if self.operation.retained_media is not None
                    else (source_ports, winner_ports)
                ),
                detach_reservations=(
                    (self.operation.reservation,)
                    if self.operation.retained_media is not None
                    else (self.operation.reservation, winner.ports)
                ),
                enable_video_transcoding=bool(
                    self.operation.runtime.config.get(CONF_VIDEO_TRANSCODING, False)
                ),
                local_endpoint_id=local_source.call.caller_endpoint_id
                if local_source is not None
                else "",
                source_client=application.source_client
                if application is not None
                else None,
                retained_media=self.operation.retained_media,
            ),
            BridgeCommitPolicy(
                route_kind=self.route_kind,
                caller=invite.caller,
                callee=self.entry.display_name,
                connected_party=connected_party,
                source_role=(
                    "trunk" if self.operation.preanswered is not None else "caller"
                ),
                source_state=(
                    CallState.IN_CALL.value
                    if response_already_sent
                    else CallState.CONNECTING.value
                ),
                response_already_sent=response_already_sent,
                local_source=local_source is not None,
                endpoint_id=local_source.call.caller_endpoint_id
                if local_source is not None
                else "",
                source_endpoint_id=local_source.call.caller_endpoint_id
                if local_source is not None
                else str(self.session.metadata.get("source_endpoint_id") or ""),
                dest_endpoint_id=winner.endpoint_id,
                media_client_id=local_source.call.caller_media_owner_id
                if local_source is not None
                else "",
                expected_generation=session.generation if session is not None else None,
            ),
        )
        if committed is None:
            raise RuntimeError(TerminalReason.CANCELLED.value)
        await self.operation.retire_application()
        if local_source is not None:
            local_source.connected(connected_party, self.route_kind)
        if session is not None:
            publish_bridge_projection(
                self.operation.runtime.hass,
                session,
                peer_name=connected_party,
                dialed_target=self.entry.display_name,
                connected_party=connected_party,
                answered_by=connected_party,
                direction="outgoing" if local_source is not None else "incoming",
                route_source="automation",
                route_kind=self.route_kind,
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                sip_uri=str(winner.uri),
                video_active=bool(committed.relay.video_relay),
                video_requested=invite.video_format is not None,
                video_negotiated=bool(committed.relay.video_relay),
                video_failure_reason=committed.video_failure_reason,
            )
        return

    async def run(self):
        await self.prepare()
        self.publish_offers()
        fork_result, winner, pickup_gate, browser_decision = await self.select_winner()
        if winner is pickup_gate:
            if pickup_gate.current.payload is None:
                self.operation.registry.take_pending_route(
                    self.operation.source.call_id
                )
                self.settle(CallState.CANCELLED.value, TerminalReason.CANCELLED.value)
                if not self.operation.borrowed_reservation:
                    self.operation.reservation.release()
                return
            winner = pickup_gate.current.payload
        reroute_decision = (
            dict(browser_decision)
            if fork_result.outcome.disposition is DialDisposition.REROUTE
            else None
        )
        if reroute_decision is not None:
            route = (
                self.operation.registry.take_pending_route(
                    self.operation.source.call_id
                )
                or {}
            )
            self.settle(
                CallState.IDLE.value,
                "forwarded",
            )
            if not self.operation.borrowed_reservation:
                self.operation.reservation.release()
            handoff = route.get("forward_handoff")
            if handoff is not None and not handoff.done():
                handoff.set_result(dict(reroute_decision))
            return
        if winner is None:
            failure = terminal_reason(
                fork_result.outcome.disposition,
                fork_result.outcome.reason or "transport_unreachable",
            )
            await self.abort(
                TerminalReason.TRANSPORT_UNREACHABLE.value,
                cleanup=False,
            )
            raise RuntimeError(failure)
        if isinstance(winner, BrowserLeg):
            return await self.commit_browser(winner, browser_decision)
        return await self.commit_sip(winner)
