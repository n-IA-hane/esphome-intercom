"""Candidate preparation for PBX ring-group calls."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, Callable

from .dial_fork import DialDisposition
from .dial_plan import RingPolicy, build_sip_contact_targets
from .endpoint_registry import EndpointBusyError, EndpointRegistry
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
    async_close_outbound_leg,
)
from .pbx_routing import caller_matches_group_member
from .ring_group import (
    endpoint_is_esphome,
    endpoint_preflight_disposition,
)

if TYPE_CHECKING:
    from .pbx_runtime import SipEndpointRuntime
    from .peer import Peer
    from .roster import RosterEntry
    from .sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)
MAX_RING_GROUP_ATTEMPTS = 16
PreflightFailure = tuple[str, str, DialDisposition, int, int]


@dataclass(slots=True)
class RingGroupCandidateRuntime:
    """Dependencies used while resolving physical ring candidates."""

    registry: SipEndpointRuntime
    endpoint_registry: EndpointRegistry | None
    browser_leg_for_member: Callable[..., BrowserLeg | None]
    logical_endpoint_for_member: Callable[..., Any]
    prepare_outbound_leg: Callable[..., OutboundLeg | None]


@dataclass(slots=True)
class RingGroupCandidates:
    """Prepared candidates retained for cancellation-safe cleanup."""

    attempts: list[OutboundLeg] = field(default_factory=list)
    browser_legs: list[BrowserLeg] = field(default_factory=list)
    preflight_failures: list[PreflightFailure] = field(default_factory=list)


async def async_prepare_ring_group_candidates(
    result: RingGroupCandidates,
    runtime: RingGroupCandidateRuntime,
    *,
    invite: SipInvite,
    group_name: str,
    members: list[str],
    peers: list[Peer],
    roster_entries: list[RosterEntry],
    ring_policy: RingPolicy,
    source_endpoint_id: str,
    local_name: str,
) -> None:
    """Populate a bounded candidate set without owning the call lifecycle."""

    for member_order, member in enumerate(members):
        if caller_matches_group_member(
            getattr(invite, "routing_caller", invite.caller),
            invite.source_host,
            member,
            peers,
            source_endpoint_id=source_endpoint_id,
        ):
            continue

        browser_leg = runtime.browser_leg_for_member(
            member,
            peers,
            roster_entries,
        )
        if browser_leg is not None:
            if browser_leg.endpoint_id == source_endpoint_id:
                continue
            endpoint = (
                runtime.endpoint_registry.get(browser_leg.endpoint_id)
                if runtime.endpoint_registry is not None
                else None
            )
            disposition = endpoint_preflight_disposition(
                endpoint,
                call_id=invite.call_id,
                browser=True,
            )
            if disposition is not None:
                result.preflight_failures.append(
                    (
                        f"preflight:{member_order}:{disposition.value}:{browser_leg.endpoint_id}",
                        browser_leg.endpoint_id,
                        disposition,
                        ring_policy.member_tiers.get(member.casefold(), 0),
                        member_order * 1000,
                    )
                )
                continue
            try:
                runtime.registry.claim_endpoint(
                    invite.call_id,
                    browser_leg.endpoint_id,
                    role="group_candidate",
                )
            except EndpointBusyError:
                result.preflight_failures.append(
                    (
                        f"preflight:{member_order}:busy:{browser_leg.endpoint_id}",
                        browser_leg.endpoint_id,
                        DialDisposition.BUSY,
                        ring_policy.member_tiers.get(member.casefold(), 0),
                        member_order * 1000,
                    )
                )
                continue
            result.browser_legs.append(browser_leg)
            continue

        logical_endpoint = runtime.logical_endpoint_for_member(
            member,
            peers,
            roster_entries,
        )
        logical_endpoint_id = str(
            getattr(logical_endpoint, "endpoint_id", "") or ""
        ).strip()
        if logical_endpoint_id == source_endpoint_id:
            continue
        disposition = endpoint_preflight_disposition(
            logical_endpoint,
            call_id=invite.call_id,
            browser=False,
        )
        if disposition is not None:
            result.preflight_failures.append(
                (
                    f"preflight:{member_order}:{disposition.value}:{logical_endpoint_id}",
                    logical_endpoint_id,
                    disposition,
                    ring_policy.member_tiers.get(member.casefold(), 0),
                    member_order * 1000,
                )
            )
            continue

        contact_targets = build_sip_contact_targets(
            (member,),
            roster_entries,
            policy=ring_policy,
            exclude_endpoint_id=source_endpoint_id,
        )
        target_specs = contact_targets or (None,)
        for contact_order, target_spec in enumerate(target_specs):
            if len(result.attempts) >= MAX_RING_GROUP_ATTEMPTS:
                _LOGGER.warning(
                    "SIP ring group %s has more than %d dialable contacts; "
                    "excess contacts were skipped",
                    group_name,
                    MAX_RING_GROUP_ATTEMPTS,
                )
                return
            try:
                leg = runtime.prepare_outbound_leg(
                    member=member,
                    peers=peers,
                    roster_entries=roster_entries,
                    local_name=local_name,
                    local_rtp_port_index=1,
                    uri_override=(
                        target_spec.uri if target_spec is not None else ""
                    ),
                    endpoint_id_override=(
                        target_spec.endpoint_id
                        if target_spec is not None
                        else ""
                    ),
                    peer_user_agent_override=(
                        target_spec.user_agent
                        if target_spec is not None
                        else ""
                    ),
                    candidate_id=(
                        target_spec.candidate_id
                        if target_spec is not None
                        else f"sip:{member_order}:{contact_order}:{member}"
                    ),
                    tier=(
                        target_spec.tier
                        if target_spec is not None
                        else ring_policy.member_tiers.get(member.casefold(), 0)
                    ),
                    order=(
                        target_spec.order
                        if target_spec is not None
                        else member_order * 1000 + contact_order
                    ),
                    invite=invite,
                )
            except RuntimeError as err:
                _LOGGER.warning(
                    "SIP ring group RTP port allocation failed member=%s: %s",
                    member,
                    err,
                )
                return
            if leg is None:
                continue
            if leg.endpoint_id == source_endpoint_id:
                await async_close_outbound_leg(leg)
                continue
            try:
                if leg.endpoint_id:
                    runtime.registry.claim_endpoint(
                        invite.call_id,
                        leg.endpoint_id,
                        role="group_candidate",
                        adopt_transport=endpoint_is_esphome(
                            logical_endpoint
                        ),
                    )
            except EndpointBusyError:
                await async_close_outbound_leg(leg)
                result.preflight_failures.append(
                    (
                        f"preflight:{leg.candidate_id}:busy",
                        leg.endpoint_id,
                        DialDisposition.BUSY,
                        leg.tier,
                        leg.order,
                    )
                )
                continue
            result.attempts.append(leg)
