"""Immutable destination facts selected before handing an existing call to its target."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from homeassistant.core import HomeAssistant

from .session_cleanup import async_wait_for_cleanup

if TYPE_CHECKING:
    from .account_dialing import AccountCallPlan
    from .call_state import CallPrecondition
    from .endpoint_routing import EndpointRouteResolver
    from .endpoint_session import EndpointCallSession
    from .local_application import LocalCallApplication
    from .local_softphone_runtime import PendingLocalSource
    from .peer import Peer
    from .pending_sip_destination import PendingSipDestination
    from .phone_endpoint import PhoneEndpoint
    from .roster import RosterEntry
    from .router import RouteDecision
    from .sip_listener import SipInvite


def source_dialog_is_answered(early_media: dict | None) -> bool:
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
    committed: bool = False
    owner: str = ""
    generation: int = 0
    call_id: str = ""
    continuation: asyncio.Task[TransferResult | None] | None = field(default=None, repr=False, compare=False)
    sip_status: int = 0

    def committed_for(self, session) -> bool:
        """Match the committed receipt, even after a later application transition."""
        return bool(self.committed and self.owner and session is not None
                    and session.call_id == self.call_id and session.generation == self.generation)


async def await_forward_result(task: asyncio.Task[TransferResult | None]) -> TransferResult | None:
    """Await the same session's configured route chain, including its fallback."""
    visited = set()
    try:
        while task is not None:
            if task in visited:
                raise RuntimeError("Forward continuation cycle")
            visited.add(task)
            result = await asyncio.shield(task)
            if result is None or result.continuation is None:
                return result
            task = result.continuation
    except asyncio.CancelledError:
        # A completed route may already have handed ownership to its fallback
        # before this waiter receives cancellation. Drain the current route,
        # rather than cancelling only the original, already completed task.
        while task.done() and not task.cancelled() and task.exception() is None:
            result = task.result()
            if result is None or result.continuation is None or result.continuation in visited:
                break
            task = result.continuation
            visited.add(task)
        if not task.done():
            task.cancel()
        await async_wait_for_cleanup(asyncio.gather(task, return_exceptions=True))
        raise
    return None


@dataclass(frozen=True, slots=True)
class ForwardPlan:
    """Admitted destination snapshot; session ownership is rechecked at commit."""

    destination: str
    decision: RouteDecision
    peers: tuple[Peer, ...]
    roster_entries: tuple[RosterEntry, ...]
    account: AccountCallPlan | None
    browser_endpoint: PhoneEndpoint | None
    initial_selection: bool
    preselected: bool
    ring_timeout: float | None
    on_failure: str
    ring_policy: Any = None


@dataclass(frozen=True, slots=True)
class ForwardSource:
    """References to the original call owners needed for commit or rollback."""

    call_id: str
    invite: SipInvite
    application: LocalCallApplication | None
    local: PendingLocalSource | None
    native_destination: PendingSipDestination | None
    native_caller: bool
    session: EndpointCallSession | None = None
    receiver_endpoint_id: str = ""
    owned_sip_source: Any | None = None


@dataclass(frozen=True, slots=True)
class ForwardRequest:
    call_id: str
    destination: str
    on_failure: str
    expected_state: str
    expected_sequence: int
    initial_selection: bool
    trunk_id: str
    precondition: CallPrecondition | None
    control_endpoint_id: str
    resolved_decision: RouteDecision | None
    ring_timeout: float | None
    queue_offer: Any

    @property
    def initial_deposit(self) -> bool:
        from .router import RouteAction

        decision = self.resolved_decision
        return bool(
            self.initial_selection
            and self.queue_offer is None
            and decision is not None
            and decision.action is RouteAction.VOICEMAIL
            and decision.entry is not None
            and decision.entry.metadata.get("mailbox_mode") == "deposit"
        )
