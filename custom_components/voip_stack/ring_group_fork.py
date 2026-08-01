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
from .ring_group_candidates import PreflightFailure

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
                timeout=RING_GROUP_TIMEOUT_S,
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
        if action in {"answer_ha", "default"}:
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

    async def _wait_caller_cancel() -> tuple[str, dict]:
        try:
            decision = await asyncio.wait_for(
                route_future,
                timeout=RING_GROUP_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return "timeout", {
                "member": "__caller__",
                "caller_control": True,
            }
        action = str((decision or {}).get("action") or "").strip().lower()
        return (
            "cancelled" if action == "cancel" else "ignored",
            {"member": "__caller__", "caller_control": True},
        )

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
            result = await client.invite(
                target=uri.user or outbound.member,
                target_display_name=outbound.member,
                remote_host=uri.host,
                remote_sip_port=uri.port or sip_port,
                request_uri=str(uri),
                timeout=8.0,
            )
            if result == "ringing":
                if on_ringing is not None and not ringing_published:
                    ringing_published = True
                    on_ringing()
                result = await client.wait_for_final(
                    timeout=RING_GROUP_TIMEOUT_S
                )
            if result == "in_call" and client.dialog is None:
                return DialOutcome(
                    DialDisposition.PROTOCOL_ERROR,
                    500,
                    "protocol_error",
                )
            return _outcome(result)

        async def _close_sip(
            mode: LegCloseMode,
            outbound: OutboundLeg = attempt,
        ) -> None:
            await async_close_outbound_leg(
                outbound,
                bye_or_cancel=mode
                in {LegCloseMode.CANCEL_OR_BYE, LegCloseMode.BYE},
            )

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
        wait_control = _wait_caller_cancel

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
