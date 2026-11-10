"""Dial fork adapters for PBX ring-group calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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

RING_GROUP_TIMEOUT_S = 30.0
ForkPayload = OutboundLeg | BrowserLeg | dict[str, Any]


def _outcome(result: str) -> DialOutcome:
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
    return DialOutcome(disposition, reason=result)


def build_ring_group_fork(
    *,
    sip_port: int,
    route_future: asyncio.Future,
    attempts: list[OutboundLeg],
    browser_legs: list[BrowserLeg],
    preflight_failures: list[PreflightFailure],
    on_ringing: Callable[[], None] | None = None,
    on_leg_state: Callable[[OutboundLeg, str], None] | None = None,
    ring_timeout: float = RING_GROUP_TIMEOUT_S,
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
                route_future,
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

    fork_candidates: list[DialCandidate] = []
    for candidate_id, endpoint_id, disposition, tier, order in preflight_failures:

        async def _dial_preflight(
            result: DialDisposition = disposition,
        ) -> DialOutcome:
            return DialOutcome(result)

        async def _close_preflight(_mode: LegCloseMode) -> None:
            return None

        fork_candidates.append(
            DialCandidate(
                candidate_id,
                _dial_preflight,
                _close_preflight,
                tier=tier,
                order=order,
                endpoint_id=endpoint_id,
            )
        )

    for attempt in attempts:
        candidate_id = attempt.candidate_id or (
            f"sip:{attempt.client.dialog_ids.call_id}"
        )
        candidate_payloads[candidate_id] = attempt

        async def _dial_sip(
            outbound: OutboundLeg = attempt,
        ) -> DialOutcome:
            nonlocal ringing_published
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
                timeout=min(8.0, ring_timeout),
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
            return _outcome(result)

        async def _close_sip(
            mode: LegCloseMode,
            outbound: OutboundLeg = attempt,
        ) -> None:
            try:
                await async_close_outbound_leg(
                    outbound, bye_or_cancel=mode in {LegCloseMode.CANCEL_OR_BYE, LegCloseMode.BYE},
                )
            finally:
                if on_leg_state is not None:
                    on_leg_state(outbound, "closed")

        fork_candidates.append(
            DialCandidate(
                candidate_id,
                _dial_sip,
                _close_sip,
                tier=attempt.tier,
                order=attempt.order,
                endpoint_id=attempt.endpoint_id,
            )
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
        return None

    fork_candidates.append(
        DialCandidate(
            control_candidate_id,
            _dial_control,
            _close_control,
            tier=control_tier,
            order=-2,
            control=True,
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
        "external:pickup", gate.dial, gate.reject, control=True, confirm=gate.confirm
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
