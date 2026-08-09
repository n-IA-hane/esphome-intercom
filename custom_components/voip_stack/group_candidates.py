"""Shared candidate preparation for PBX group dialing."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, Callable

from .dial_fork import DialDisposition
from .dial_plan import RingPolicy, build_sip_contact_targets
from .endpoint_registry import EndpointBusyError, EndpointRegistry
from .outbound_attempts import BrowserLeg, OutboundLeg, async_close_outbound_leg
from .pbx_routing import browser_endpoint_can_ring, caller_matches_group_member
from .ring_group import endpoint_is_esphome, endpoint_preflight_disposition

if TYPE_CHECKING:
    from .pbx_runtime import SipEndpointRuntime
    from .peer import Peer
    from .roster import RosterEntry
    from .sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)
MAX_GROUP_ATTEMPTS = 16
PreflightFailure = tuple[str, str, DialDisposition, int, int]


@dataclass(slots=True)
class GroupCandidateRuntime:
    """Dependencies used to prepare group candidates."""

    registry: SipEndpointRuntime
    endpoint_registry: EndpointRegistry | None
    browser_leg_for_member: Callable[..., BrowserLeg | None]
    prepare_outbound_leg: Callable[..., OutboundLeg | None]
    logical_endpoint_for_member: Callable[..., Any] | None = None


@dataclass(slots=True)
class GroupCandidates:
    """Prepared candidates retained by the owning call lifecycle."""

    attempts: list[OutboundLeg] = field(default_factory=list)
    browser_legs: list[BrowserLeg] = field(default_factory=list)
    preflight_failures: list[PreflightFailure] = field(default_factory=list)


def _preflight_failure(
    member_order: int,
    endpoint_id: str,
    disposition: DialDisposition,
    tier: int,
) -> PreflightFailure:
    return (
        f"preflight:{member_order}:{disposition.value}:{endpoint_id}",
        endpoint_id,
        disposition,
        tier,
        member_order * 1000,
    )


def _caller_matches(
    invite: SipInvite,
    member: str,
    peers: list[Peer],
    source_endpoint_id: str | None = None,
) -> bool:
    kwargs = (
        {"source_endpoint_id": source_endpoint_id}
        if source_endpoint_id is not None
        else {}
    )
    return caller_matches_group_member(
        getattr(invite, "routing_caller", invite.caller),
        invite.source_host,
        member,
        peers,
        **kwargs,
    )


async def async_prepare_group_candidates(
    result: GroupCandidates,
    runtime: GroupCandidateRuntime,
    *,
    invite: SipInvite,
    members: list[str],
    peers: list[Peer],
    roster_entries: list[RosterEntry],
    local_name: str,
    ring_policy: RingPolicy | None = None,
    source_endpoint_id: str = "",
    group_name: str = "",
    initial_selection: bool = True,
) -> None:
    """Populate a bounded set using forwarding or ring-group policy."""

    detailed_preflight = ring_policy is not None
    for member_order, member in enumerate(members):
        member_tier = (
            ring_policy.member_tiers.get(member.casefold(), 0)
            if ring_policy is not None
            else 0
        )
        if detailed_preflight and _caller_matches(
            invite, member, peers, source_endpoint_id
        ):
            continue
        browser_leg = runtime.browser_leg_for_member(member, peers, roster_entries)
        if browser_leg is not None:
            if not initial_selection or (
                detailed_preflight and browser_leg.endpoint_id == source_endpoint_id
            ):
                continue
            endpoint = (
                runtime.endpoint_registry.get(browser_leg.endpoint_id)
                if runtime.endpoint_registry is not None
                else None
            )
            disposition = (
                endpoint_preflight_disposition(
                    endpoint, call_id=invite.call_id, browser=True
                )
                if detailed_preflight
                else (
                    None
                    if browser_endpoint_can_ring(endpoint)
                    else DialDisposition.UNAVAILABLE
                )
            )
            if disposition is not None:
                if detailed_preflight:
                    result.preflight_failures.append(
                        _preflight_failure(
                            member_order,
                            browser_leg.endpoint_id,
                            disposition,
                            member_tier,
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
                if detailed_preflight:
                    result.preflight_failures.append(
                        _preflight_failure(
                            member_order,
                            browser_leg.endpoint_id,
                            DialDisposition.BUSY,
                            member_tier,
                        )
                    )
                continue
            result.browser_legs.append(browser_leg)
            continue
        if not detailed_preflight and _caller_matches(invite, member, peers):
            continue

        logical_endpoint = (
            runtime.logical_endpoint_for_member(member, peers, roster_entries)
            if runtime.logical_endpoint_for_member is not None
            else None
        )
        logical_endpoint_id = str(
            getattr(logical_endpoint, "endpoint_id", "") or ""
        ).strip()
        if detailed_preflight and logical_endpoint_id == source_endpoint_id:
            continue
        if detailed_preflight:
            disposition = endpoint_preflight_disposition(
                logical_endpoint, call_id=invite.call_id, browser=False
            )
            if disposition is not None:
                result.preflight_failures.append(
                    _preflight_failure(
                        member_order,
                        logical_endpoint_id,
                        disposition,
                        member_tier,
                    )
                )
                continue

        targets = (
            build_sip_contact_targets(
                (member,),
                roster_entries,
                policy=ring_policy,
                exclude_endpoint_id=source_endpoint_id,
            )
            if detailed_preflight
            else ()
        ) or (None,)
        for contact_order, target in enumerate(targets):
            if len(result.attempts) >= MAX_GROUP_ATTEMPTS:
                if detailed_preflight:
                    _LOGGER.warning(
                        "SIP ring group %s has more than %d dialable contacts; "
                        "excess contacts were skipped",
                        group_name,
                        MAX_GROUP_ATTEMPTS,
                    )
                return
            kwargs = {
                "member": member,
                "peers": peers,
                "roster_entries": roster_entries,
                "local_name": local_name,
                "local_rtp_port_index": 1,
                "invite": invite,
            }
            if detailed_preflight:
                candidate_id = (
                    target.candidate_id
                    if target is not None
                    else f"sip:{member_order}:{contact_order}:{member}"
                )
                kwargs.update(
                    uri_override=target.uri if target is not None else "",
                    endpoint_id_override=getattr(target, "endpoint_id", ""),
                    peer_user_agent_override=getattr(target, "user_agent", ""),
                    candidate_id=candidate_id,
                    tier=target.tier if target is not None else member_tier,
                    order=(
                        target.order
                        if target is not None
                        else member_order * 1000 + contact_order
                    ),
                )
            try:
                leg = runtime.prepare_outbound_leg(**kwargs)
            except RuntimeError as err:
                if not detailed_preflight:
                    raise
                _LOGGER.warning(
                    "SIP ring group RTP port allocation failed member=%s: %s",
                    member,
                    err,
                )
                return
            if leg is None:
                continue
            if not detailed_preflight:
                leg.candidate_id = leg.candidate_id or f"forward:{member_order}"
                leg.order = member_order
                result.attempts.append(leg)
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
                        adopt_transport=endpoint_is_esphome(logical_endpoint),
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
