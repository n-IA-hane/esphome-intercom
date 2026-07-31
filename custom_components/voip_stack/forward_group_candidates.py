"""Candidate preparation for forwarding into an existing ring group."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .call_registry import CallRegistry
from .endpoint_registry import EndpointBusyError, EndpointRegistry
from .outbound_attempts import BrowserLeg, OutboundLeg
from .pbx_routing import (
    browser_endpoint_can_ring,
    caller_matches_group_member,
)

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry
    from .sip_listener import SipInvite


MAX_FORWARD_GROUP_ATTEMPTS = 16


@dataclass(slots=True)
class ForwardGroupCandidateRuntime:
    """Dependencies needed to prepare forwarding candidates."""

    registry: CallRegistry
    endpoint_registry: EndpointRegistry | None
    browser_leg_for_member: Callable[..., BrowserLeg | None]
    prepare_outbound_leg: Callable[..., OutboundLeg | None]


@dataclass(slots=True)
class ForwardGroupCandidates:
    """Prepared candidates retained by the forwarding lifecycle owner."""

    attempts: list[OutboundLeg] = field(default_factory=list)
    browser_legs: list[BrowserLeg] = field(default_factory=list)


def prepare_forward_group_candidates(
    result: ForwardGroupCandidates,
    runtime: ForwardGroupCandidateRuntime,
    *,
    invite: SipInvite,
    members: list[str],
    peers: list[Peer],
    roster_entries: list[RosterEntry],
    local_name: str,
    initial_selection: bool,
) -> None:
    """Populate candidates without starting calls or owning their lifecycle."""

    for member_order, member in enumerate(members):
        browser_leg = runtime.browser_leg_for_member(
            member,
            peers,
            roster_entries,
        )
        if browser_leg is not None:
            if not initial_selection:
                continue
            endpoint = (
                runtime.endpoint_registry.get(browser_leg.endpoint_id)
                if runtime.endpoint_registry is not None
                else None
            )
            if not browser_endpoint_can_ring(endpoint):
                continue
            try:
                runtime.registry.claim_endpoint(
                    invite.call_id,
                    browser_leg.endpoint_id,
                    role="group_candidate",
                )
            except EndpointBusyError:
                continue
            result.browser_legs.append(browser_leg)
            continue

        if caller_matches_group_member(
            invite.caller,
            invite.source_host,
            member,
            peers,
        ):
            continue
        if len(result.attempts) >= MAX_FORWARD_GROUP_ATTEMPTS:
            break
        attempt = runtime.prepare_outbound_leg(
            member=member,
            peers=peers,
            roster_entries=roster_entries,
            local_name=local_name,
            local_rtp_port_index=1,
        )
        if attempt is None:
            continue
        if not attempt.candidate_id:
            attempt.candidate_id = f"forward:{member_order}"
        attempt.order = member_order
        result.attempts.append(attempt)
