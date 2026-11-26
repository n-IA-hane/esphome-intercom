"""Follow a local account's 302 inside its existing owned fork branch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields, replace
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from .endpoint_registry import EndpointRegistry
    from .endpoint_session import EndpointCallSession
    from .group_candidates import GroupCandidateRuntime
    from .outbound_attempts import BrowserLeg
    from .pbx_runtime import SipEndpointRuntime
    from .peer import Peer
    from .roster import RosterEntry
    from .sip_listener import SipInvite

from .core import sip
from .dial_fork import DialCandidate, DialDisposition, DialForkController, DialOutcome, LegCloseMode, reduce_dial_failures
from .dial_plan import RingPolicy
from .duration_policy import resolve_service_timeout
from .endpoint_routing import EndpointRouteResolver
from .group_candidates import GroupCandidates, async_prepare_account_candidates
from .inbound_routing.targets import resolve_phone_target, validate_target_endpoint
from .outbound_attempts import OutboundLeg, async_close_outbound_leg
from .phone_endpoint import EndpointKind
from .ring_group_fork import build_offer_candidates
from .router import RouteAction
from .runtime_data import require_runtime_data
from .trunk_policy import call_external_call_denied, is_any_trunk_uri
from .sip_registration_policy import LOCAL_REDIRECT_HOPS_FIELD, LOCAL_REDIRECT_MAX_HOPS


def redirect_contacts(headers: Iterable[str]) -> list[tuple[float, sip.SipUri]]:
    """Parse RFC Contact lists with the common SIP quoting/splitting rules."""
    rows = []
    for header in headers:
        for item in sip._split_comma_header_values(header):
            uri = sip.parse_sip_uri(sip.name_addr_uri(item))
            if ">" in item:
                tail = item.split(">", 1)[1]
                preference = re.search(r"(?:^|;)\s*q=([^;]*)(?:;|$)", tail, re.IGNORECASE)
                q = float(preference[1]) if preference else 1.0
            else:
                q = float(next((value for key, value in uri.params if key.casefold() == "q"), "1"))
                uri = replace(uri, params=tuple((key, value) for key, value in uri.params if key.casefold() != "q"))
            if not 0 <= q <= 1:
                raise ValueError("Invalid redirect Contact preference")
            rows.append((q, len(rows), uri))
    return [(q, uri) for q, _index, uri in sorted(rows, key=lambda row: (-row[0], row[1])) if q > 0]


def redirect_tier_ranks(priorities: Iterable[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """Keep Contact preference ahead of every registration priority tier."""
    return {priority: rank for rank, priority in enumerate(sorted(set(priorities)))}


@dataclass(slots=True)
class LocalAccountRedirect:
    """Borrow admission and arbitration; never create a call or media owner."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    session: EndpointCallSession
    registry: SipEndpointRuntime
    endpoint_registry: EndpointRegistry
    preparation: GroupCandidateRuntime
    invite: SipInvite
    peers: list[Peer]
    entries: list[RosterEntry]
    source_endpoint_id: str
    route_future: asyncio.Future
    browser_legs: list[BrowserLeg]
    publish_browser: Callable[[list[BrowserLeg]], None]
    on_leg_state: Callable[[OutboundLeg, str], None] | None
    overall_timeout: float

    async def follow(self, outbound, dial_sip):
        endpoint = self.endpoint_registry.get(outbound.endpoint_id)
        if outbound.trunk_id or endpoint is None or endpoint.kind is not EndpointKind.SIP_ACCOUNT:
            return None
        limit = LOCAL_REDIRECT_HOPS_FIELD.validate(self.config[LOCAL_REDIRECT_MAX_HOPS])
        path = outbound.redirect_path or (outbound.endpoint_id,)
        if len(path) > limit:
            return DialOutcome(DialDisposition.UNAVAILABLE, 483, "redirect_hop_limit", "Too Many Hops")
        try:
            contacts = redirect_contacts(getattr(outbound.client, "last_redirect_contacts", ()))
        except (ValueError, sip.SipError):
            return DialOutcome(DialDisposition.UNAVAILABLE, 400, "invalid_redirect", "Bad Request")
        if not contacts:
            return None
        resolver = EndpointRouteResolver(self.hass, self.local_ip, int(self.config["sip_port"]),
            source_endpoint_id=self.source_endpoint_id)
        targets = []
        failures = []
        seen_targets = set()
        for preference, contact in contacts:
            # The Contact supplies a destination, never a caller identity or
            # permission. Local aliases and external URIs use the normal plan.
            member = contact.user if sip.sip_uri_targets_listener(contact,
                listener_hosts=(self.local_ip, str(self.config.get("advertise_host") or "")),
                listener_port=int(self.config["sip_port"])) else str(contact)
            resolution = resolve_phone_target(decision=resolver.route(member, self.entries),
                endpoint_registry=self.endpoint_registry, roster_entries=self.entries,
                route_resolver=resolver.route, call_id=self.session.call_id)
            decision, target = resolution.decision, resolution.endpoint
            if resolution.failure is not None:
                failures.append(DialOutcome(DialDisposition.UNAVAILABLE, resolution.failure.status,
                    "redirect_refused", resolution.failure.reason))
                continue
            if decision.action is RouteAction.REJECT:
                failures.append(DialOutcome(DialDisposition.UNAVAILABLE, decision.status or 480,
                    "redirect_refused", "Not Found" if decision.status == 404 else "Temporarily Unavailable"))
                continue
            if decision.action not in {RouteAction.DIRECT, RouteAction.BRIDGE,
                    RouteAction.FORWARD, RouteAction.ANSWER_HA, RouteAction.TRUNK}:
                failures.append(DialOutcome(DialDisposition.UNAVAILABLE, 480,
                    "redirect_destination_unsupported", "Temporarily Unavailable"))
                continue
            runtime = require_runtime_data(self.hass)
            registration_only = (self.invite.authenticated_username
                and not self.invite.received_via_trunk and not self.invite.identified_source_endpoint_id)
            if (decision.action is RouteAction.TRUNK or is_any_trunk_uri(runtime, decision.sip_uri)) and (
                    registration_only or call_external_call_denied(runtime, self.session.call_id, self.source_endpoint_id)):
                failures.append(DialOutcome(DialDisposition.UNAVAILABLE, 403, "external_calls_disabled", "Forbidden"))
                continue
            failure = validate_target_endpoint(invite=self.invite, endpoint=target)
            if failure is not None:
                failures.append(DialOutcome(DialDisposition.UNAVAILABLE, failure.status,
                    "redirect_refused", failure.reason))
                continue
            identity = target.endpoint_id if target is not None else decision.sip_uri or member
            if identity in path:
                failures.append(DialOutcome(DialDisposition.UNAVAILABLE, 482, "redirect_loop", "Loop Detected"))
                continue
            if identity not in seen_targets:
                targets.append((decision.entry.id if decision.entry else member, identity, preference))
                seen_targets.add(identity)
        if not targets:
            return reduce_dial_failures(failures)

        self.session.ensure_live(self.session.token)
        await async_close_outbound_leg(outbound)
        if self.on_leg_state is not None:
            self.on_leg_state(outbound, "closed")
        # Retire the old account only after its final physical branch closes.
        # Other original SIP Contacts and the existing HA offer remain eligible.
        other_sip = any(leg.endpoint_id == outbound.endpoint_id and leg.dialog is not None
            and (getattr(leg.dialog, "_invite_transaction_active", False) or getattr(leg.dialog, "dialog", None) is not None)
            for leg in self.session.legs.values())
        other_browser = any(leg.endpoint_id == outbound.endpoint_id for leg in self.browser_legs)
        if not other_sip and not other_browser:
            self.registry.release_endpoint_claim(self.session.call_id, outbound.endpoint_id)
            self.session.update_metadata(**{key: "" for key in ("dest_endpoint_id", "target_endpoint_id")
                if self.session.metadata.get(key) == outbound.endpoint_id})
        prepared = GroupCandidates()
        transferred = None
        try:
            policy = RingPolicy(step_timeout=resolve_service_timeout(self.hass, "contact_step_timeout"))
            browser_tiers = {}
            sip_tiers = {}
            tiers = {q: index for index, q in enumerate(sorted({item[2] for item in targets}, reverse=True))}
            for member, endpoint_id, preference in targets:
                first_attempt = len(prepared.attempts)
                first_browser = len(prepared.browser_legs)
                await async_prepare_account_candidates(prepared, self.preparation,
                    call_id=self.session.call_id, members=[member], peers=self.peers,
                    roster_entries=self.entries, local_name=self.invite.caller,
                    ring_policy=policy, source_endpoint_id=self.source_endpoint_id,
                    outbound_context={"invite": self.invite})
                for attempt in prepared.attempts[first_attempt:]:
                    attempt.redirect_path = (*path, endpoint_id)
                    sip_tiers[attempt.candidate_id] = (tiers[preference], attempt.tier)
                browser_tiers.update({leg.endpoint_id: (tiers[preference], 0)
                    for leg in prepared.browser_legs[first_browser:]})
            ranked = redirect_tier_ranks((*sip_tiers.values(), *browser_tiers.values()))
            for attempt in prepared.attempts:
                attempt.tier = ranked[sip_tiers[attempt.candidate_id]]
            browser_tiers = {identity: ranked[priority] for identity, priority in browser_tiers.items()}

            async def close(attempt, mode):
                await async_close_outbound_leg(attempt,
                    bye_or_cancel=mode is not LegCloseMode.CLOSE)
                if self.on_leg_state is not None:
                    self.on_leg_state(attempt, "closed")

            def browser_candidate(leg):
                async def wait_outer_browser():
                    route = self.registry.artifact_for(self.session.call_id, "pending_route")
                    if route is None or self.route_future.done():
                        return DialOutcome(DialDisposition.CANCELLED)
                    if not any(existing.endpoint_id == leg.endpoint_id for existing in self.browser_legs):
                        self.browser_legs.append(leg)
                        route["ring_group_endpoint_ids"] = tuple(item.endpoint_id for item in self.browser_legs)
                        self.publish_browser(self.browser_legs)
                    # The original outer control candidate accepts the answer;
                    # this branch only holds its admitted offer open.
                    await asyncio.shield(self.route_future)
                    return DialOutcome(DialDisposition.CANCELLED)

                async def close_waiter(_mode):
                    return None

                return DialCandidate(f"redirect:browser:{leg.endpoint_id}", wait_outer_browser,
                    close_waiter, tier=browser_tiers[leg.endpoint_id], endpoint_id=leg.endpoint_id)

            candidates, payloads = build_offer_candidates(attempts=prepared.attempts,
                browser_legs=prepared.browser_legs, preflight_failures=prepared.preflight_failures,
                dial_sip=dial_sip, close_sip=close, browser_candidate=browser_candidate)
            result = await DialForkController(self.session, candidates,
                overall_timeout=self.overall_timeout, step_timeout=policy.step_timeout).run(lambda *_: True)
            if result.winner is not None:
                transferred = payloads[result.winner.candidate_id]
                # The outer candidate retains its identity. Only its adopted
                # physical transport/media changes to the admitted winner.
                candidate_id, tier, order = outbound.candidate_id, outbound.tier, outbound.order
                for descriptor in fields(OutboundLeg):
                    setattr(outbound, descriptor.name, getattr(transferred, descriptor.name))
                outbound.candidate_id, outbound.tier, outbound.order = candidate_id, tier, order
            return result.outcome
        finally:
            await asyncio.gather(*(async_close_outbound_leg(attempt, bye_or_cancel=True)
                for attempt in prepared.attempts if attempt is not transferred))
