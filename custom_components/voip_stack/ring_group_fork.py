"""Dial fork adapters for PBX ring-group calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .dial_fork import (
    DialCandidate,
    DialDisposition,
    DialOutcome,
    LegCloseMode,
)
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
    async_close_outbound_leg,
)
from .group_candidates import PreflightFailure
from .core.sip import sip_default_port

ForkPayload = OutboundLeg | BrowserLeg | dict[str, Any]


def _outcome(result: str, *, sip_status: int = 0, response_reason: str = "") -> DialOutcome:
    disposition = {
        "in_call": DialDisposition.ANSWERED,
        "in_call_browser": DialDisposition.ANSWERED,
        "busy": DialDisposition.BUSY,
        "dnd": DialDisposition.DND,
        "declined": DialDisposition.DECLINED,
        "timeout": DialDisposition.TIMEOUT,
        "media_incompatible": DialDisposition.MEDIA_INCOMPATIBLE,
        "auth_required_unsupported": DialDisposition.AUTH_FAILED,
        "proxy_auth_required_unsupported": DialDisposition.AUTH_FAILED,
        "cancelled": DialDisposition.CANCELLED,
        "reroute": DialDisposition.REROUTE,
    }.get(result, DialDisposition.UNAVAILABLE)
    return DialOutcome(disposition, status=sip_status if 300 <= sip_status <= 699 else 0,
                       reason=result,
                       response_reason=response_reason if 300 <= sip_status <= 699 else "")


def account_offer_outcome(results: list[str], *, pending: bool) -> str:
    """Reduce a completed account offer, rather than one physical Contact."""
    from .dial_fork import reduce_dial_failures, terminal_reason

    if pending or "timeout" in results:
        return "timeout"
    outcome = reduce_dial_failures([_outcome(result) for result in results])
    return terminal_reason(outcome.disposition, "transport_unreachable")


def build_offer_candidates(
    *,
    attempts: list[OutboundLeg],
    browser_legs: list[BrowserLeg],
    preflight_failures: list[PreflightFailure],
    dial_sip: Callable[[OutboundLeg], Awaitable[DialOutcome]],
    close_sip: Callable[[OutboundLeg, LegCloseMode], Awaitable[None]],
    browser_candidate: Callable[[BrowserLeg], DialCandidate | None] | None = None,
    before_close_sip: Callable[[OutboundLeg, LegCloseMode], None] | None = None,
) -> tuple[list[DialCandidate], dict[str, ForkPayload]]:
    """Adapt admitted account branches once; callers select first or all answers.

    Transport callbacks own signaling and media preparation. Source routing and
    pickup controls are added only by the ordinary-call adapter, never paging.
    """
    candidates: list[DialCandidate] = []
    payloads: dict[str, ForkPayload] = {}
    for candidate_id, endpoint_id, disposition, tier, order in preflight_failures:
        async def dial_preflight(result=disposition):
            return DialOutcome(result)

        async def close_preflight(_mode):
            return None

        candidates.append(DialCandidate(candidate_id, dial_preflight, close_preflight,
            tier=tier, order=order, endpoint_id=endpoint_id))

    for attempt in attempts:
        candidate_id = attempt.candidate_id or f"sip:{attempt.client.dialog_ids.call_id}"
        payloads[candidate_id] = attempt

        async def dial(outbound=attempt):
            return await dial_sip(outbound)

        async def close(mode, outbound=attempt):
            await close_sip(outbound, mode)

        def prepare_close(mode, outbound=attempt):
            if before_close_sip is not None:
                before_close_sip(outbound, mode)

        candidates.append(DialCandidate(candidate_id, dial, close,
            tier=attempt.tier, order=attempt.order, endpoint_id=attempt.endpoint_id,
            before_close=prepare_close if before_close_sip is not None else None))

    if browser_candidate is not None:
        for leg in browser_legs:
            candidate = browser_candidate(leg)
            if candidate is not None:
                candidates.append(candidate)
                payloads[candidate.candidate_id] = leg
    return candidates, payloads


def build_ring_group_fork(
    *,
    sip_port: int,
    route_future: asyncio.Future,
    attempts: list[OutboundLeg],
    browser_legs: list[BrowserLeg],
    preflight_failures: list[PreflightFailure],
    on_ringing: Callable[[], None] | None = None,
    on_leg_state: Callable[[OutboundLeg, str], None] | None = None,
    ring_timeout: float | None = None,
    follow_redirect: Callable[..., Awaitable[DialOutcome]] | None = None,
) -> tuple[
    list[DialCandidate],
    dict[str, ForkPayload],
    dict[str, Any],
]:
    """Adapt prepared branches to the common deterministic fork controller."""

    candidate_payloads: dict[str, ForkPayload] = {}
    browser_decision: dict[str, Any] = {}
    ringing_published = False

    async def _wait_browser() -> tuple[str, BrowserLeg | dict]:
        try:
            decision = await asyncio.wait_for(
                asyncio.shield(route_future),
                timeout=ring_timeout,
            )
        except asyncio.TimeoutError:
            return "timeout", {"member": "__browser__", "browser": True}
        action = str((decision or {}).get("action") or "").strip().lower()
        browser_decision.update(decision or {})
        selected_endpoint_id = str(
            (decision or {}).get("endpoint_id") or ""
        ).strip()
        selected = next(
            (
                leg
                for leg in browser_legs
                if leg.endpoint_id == selected_endpoint_id
            ),
            None,
        )
        if action == "answer_ha":
            if selected is None:
                return "declined", {
                    "member": "__browser__",
                    "browser": True,
                }
            return "in_call_browser", selected
        if action in {"forward", "bridge"}:
            return "reroute", dict(decision or {})
        if action == "busy":
            return "busy", selected or {
                "member": "__browser__",
                "browser": True,
            }
        if action == "cancel":
            return "cancelled", selected or {
                "member": "__caller__",
                "caller_control": True,
            }
        return "declined", selected or {
            "member": "__browser__",
            "browser": True,
        }

    async def _dial_sip(
        outbound: OutboundLeg,
    ) -> DialOutcome:
        nonlocal ringing_published
        if outbound.redirect_pending and follow_redirect is not None:
            outbound.redirect_pending = False
            redirected = await follow_redirect(outbound, _dial_sip)
            if redirected is not None:
                return redirected
            return _outcome("sip_302", sip_status=302)
        client = outbound.client
        uri = outbound.uri
        if on_leg_state is not None:
            on_leg_state(outbound, "calling")
        result = await client.invite(
            target=uri.user or outbound.member,
            target_display_name=outbound.member,
            remote_host=uri.host,
            remote_sip_port=sip_default_port(uri),
            request_uri=str(uri),
            timeout=8.0 if ring_timeout is None else min(8.0, ring_timeout),
        )
        if result == "ringing":
            if on_leg_state is not None:
                on_leg_state(outbound, "remote_ringing")
            if on_ringing is not None and not ringing_published:
                ringing_published = True
                on_ringing()
            result = await client.wait_for_final(
                timeout=ring_timeout
            )
        if result == "in_call" and client.dialog is None:
            return DialOutcome(
                DialDisposition.PROTOCOL_ERROR,
                500,
                "protocol_error",
            )
        if on_leg_state is not None:
            on_leg_state(outbound, result)
        if client.last_sip_status_code == 302 and follow_redirect is not None:
            redirected = await follow_redirect(outbound, _dial_sip)
            if redirected is not None:
                return redirected
        return _outcome(result, sip_status=client.last_sip_status_code,
                        response_reason=getattr(client, "last_sip_reason", ""))

    async def _close_sip(
        outbound: OutboundLeg,
        mode: LegCloseMode,
    ) -> None:
        if mode is LegCloseMode.ANSWERED_ELSEWHERE:
            outbound.client.cancel_reason = "answered_elsewhere"
        try:
            await async_close_outbound_leg(
                outbound, bye_or_cancel=mode in {LegCloseMode.CANCEL_OR_BYE, LegCloseMode.BYE,
                                               LegCloseMode.ANSWERED_ELSEWHERE},
            )
        finally:
            if on_leg_state is not None:
                on_leg_state(outbound, "closed")

    def _prepare_close(outbound: OutboundLeg, mode: LegCloseMode) -> None:
        if mode is LegCloseMode.ANSWERED_ELSEWHERE:
            outbound.client.cancel_reason = "answered_elsewhere"

    fork_candidates, candidate_payloads = build_offer_candidates(
        attempts=attempts, browser_legs=browser_legs, preflight_failures=preflight_failures,
        dial_sip=_dial_sip, close_sip=_close_sip, before_close_sip=_prepare_close,
    )

    control_tier = min(
        (candidate.tier for candidate in fork_candidates),
        default=0,
    )
    if browser_legs:
        control_candidate_id = "browser:route-control"
        wait_control = _wait_browser
    else:
        control_candidate_id = "caller:route-control"
        wait_control = _wait_browser

    async def _dial_control() -> DialOutcome:
        result, selected = await wait_control()
        candidate_payloads[control_candidate_id] = selected
        if result == "cancelled":
            return DialOutcome(
                DialDisposition.SOURCE_CANCELLED,
                487,
                result,
            )
        return _outcome(result)

    async def _close_control(_mode: LegCloseMode) -> None:
        # A cancelled child waiter cannot cancel its owner's decision. Once
        # the fork itself closes this offer, no late answer may reuse it.
        if not route_future.done():
            route_future.cancel()

    fork_candidates.append(
        DialCandidate(
            control_candidate_id,
            _dial_control,
            _close_control,
            tier=control_tier,
            order=-2,
            control=True,
            keeps_offer_open=bool(browser_legs),
        )
    )
    return fork_candidates, candidate_payloads, browser_decision


def group_leg_observer(hass, registry, session):
    """Project fork branches through the authoritative call session."""
    from .call_projection import publish_bridge_projection

    token = session.token

    def observe(outbound, state):
        if not session.owns(token):
            return
        identity = outbound.client.dialog_ids.call_id
        if state == "closed":
            registry.remove_leg(token.call_id, identity)
            return
        registry.add_leg(
            token.call_id,
            identity,
            role="callee",
            state=state,
            endpoint_id=outbound.endpoint_id,
        )
        registry.observe_leg(
            token.call_id,
            identity,
            role="callee",
            state=state,
            endpoint_id=outbound.endpoint_id,
            dialog=outbound.client,
            generation=token.generation,
        )
        publish_bridge_projection(hass, session, event_type="state_changed")

    return observe


async def run_owned_ring_group_fork(
    session,
    candidates,
    payloads,
    *,
    invite,
    attempts,
    browser_legs,
    on_leg_state,
    commit_winner,
    **policy,
):
    """Keep external pickup and ordinary answers under one fork arbiter."""
    from .dial_fork import DialForkController, ForkAnswerGate

    gate = ForkAnswerGate(
        attempts=attempts,
        browser_legs=browser_legs,
        invite=invite,
        on_leg_state=on_leg_state,
    )
    candidate = DialCandidate(
        "external:pickup", gate.dial, gate.reject, control=True, confirm=gate.confirm,
        keeps_offer_open=False,
    )
    candidates.append(candidate)
    payloads[candidate.candidate_id] = gate
    resource = f"fork_answer:{session.call_id}"
    session.add_resource(resource, gate, gate.close)
    try:
        result = await DialForkController(session, candidates, **policy).run(
            commit_winner
        )
        return result, gate
    finally:
        await gate.close()
        if session.live:
            session.release_resource(resource, value=gate)
