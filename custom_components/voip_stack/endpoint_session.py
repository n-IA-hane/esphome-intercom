"""Explicit PBX call-session ownership and cancellation-safe teardown."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields
from enum import IntEnum, StrEnum
import inspect
import logging
from typing import Any, TypeAlias, TYPE_CHECKING
from weakref import WeakKeyDictionary, ref

from .fsm import sip_public_state
from .session_cleanup import async_wait_for_cleanup

if TYPE_CHECKING:
    from .call_state import CallStateSnapshot
    from .pbx_runtime import SipEndpointRuntime


_LOGGER = logging.getLogger(__name__)
_TASK_LINEAGE = ContextVar("voip_owned_task_lineage", default=())

AsyncCloser: TypeAlias = Callable[[str], Awaitable[None] | None]
TerminationSignaler: TypeAlias = Callable[
    [str, "TerminationIntent"], Awaitable[None] | None
]
TerminationObserver: TypeAlias = Callable[
    ["EndpointCallSession", "TerminationIntent"], Awaitable[None] | None
]


class SessionPhase(StrEnum):
    """Internal lifecycle of one logical PBX call."""

    NEW = "new"
    ROUTING = "routing"
    CALLING = "calling"
    RINGING = "ringing"
    CONNECTING = "connecting"
    ESTABLISHED = "established"
    HELD = "held"
    TRANSFERRING = "transferring"
    TERMINATING = "terminating"
    TERMINATED = "terminated"


class LegKind(StrEnum):
    SIP = "sip"
    BROWSER = "browser"
    ESPHOME = "esphome"
    TRUNK = "trunk"
    ASSIST = "assist"
    CONFERENCE = "conference"


class LegPhase(StrEnum):
    NEW = "new"
    INVITING = "inviting"
    RINGING = "ringing"
    ANSWERED = "answered"
    HELD = "held"
    CLOSING = "closing"
    CLOSED = "closed"


class TerminationInitiator(StrEnum):
    """Actor or subsystem that made the terminal decision."""

    LOCAL_USER = "local_user"
    REMOTE_PEER = "remote_peer"
    TIMEOUT = "timeout"
    ROUTING = "routing"
    MEDIA = "media"
    RUNTIME = "runtime"
    INTERNAL = "internal"


class SipTerminationDisposition(StrEnum):
    """Semantic SIP operation selected later by the owning leg transport."""

    AUTO = "auto"
    NONE = "none"
    CANCEL = "cancel"
    BYE = "bye"
    FINAL_RESPONSE = "final_response"


@dataclass(frozen=True, slots=True)
class TerminationIntent:
    """One immutable terminal decision shared by all cleanup observers."""

    reason: str
    initiator: TerminationInitiator = TerminationInitiator.INTERNAL
    public_state: str = ""
    sip_disposition: SipTerminationDisposition = SipTerminationDisposition.AUTO
    response_status: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", str(self.reason or "terminated").strip())
        state = str(self.public_state or "").strip()
        object.__setattr__(self, "public_state", state or sip_public_state(self.reason))

    @classmethod
    def bye(
        cls,
        reason: str,
        initiator: TerminationInitiator = TerminationInitiator.LOCAL_USER,
        *,
        response_status: int = 0,
    ) -> "TerminationIntent":
        return cls(
            reason,
            initiator=initiator,
            sip_disposition=SipTerminationDisposition.BYE,
            response_status=response_status,
        )

    @classmethod
    def final_response(
        cls,
        reason: str,
        status: int,
        initiator: TerminationInitiator = TerminationInitiator.ROUTING,
    ) -> "TerminationIntent":
        return cls(
            reason,
            initiator=initiator,
            sip_disposition=SipTerminationDisposition.FINAL_RESPONSE,
            response_status=status,
        )


class CleanupStage(IntEnum):
    """Teardown order; higher stages close before lower stages."""

    OBSERVER = 40
    MEDIA = 30
    LEG = 20
    RESERVATION = 10


@dataclass(frozen=True, slots=True)
class CallToken:
    call_id: str
    generation: int


async def _run_closer(closer: AsyncCloser | None, reason: str) -> None:
    if closer is None:
        return
    result = closer(reason)
    if inspect.isawaitable(result):
        await result


@dataclass(slots=True)
class CallLeg:
    """One independently closable signaling/media leg of a call."""

    leg_id: str
    kind: LegKind
    endpoint_id: str = ""
    sip_call_id: str = ""
    phase: LegPhase = LegPhase.NEW
    dialog: Any | None = None
    media: Any | None = None
    role: str = ""
    state: str = ""
    local_uri: str = ""
    remote_uri: str = ""
    trunk_id: str = ""
    closer: AsyncCloser | None = field(default=None, repr=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self.phase is LegPhase.CLOSED

    async def close(self, reason: str) -> None:
        """Close exactly once and keep the close operation cancellation-safe."""

        if self._close_task is None:
            self.phase = LegPhase.CLOSING

            async def _close() -> None:
                try:
                    await _run_closer(self.closer, reason)
                finally:
                    self.phase = LegPhase.CLOSED

            self._close_task = asyncio.create_task(
                _close(),
                name=f"voip-call-leg-close-{self.leg_id}",
            )
        await async_wait_for_cleanup(self._close_task)


@dataclass(slots=True)
class ManagedResource:
    """One non-leg resource owned by a call session."""

    name: str
    value: Any
    closer: AsyncCloser
    stage: CleanupStage = CleanupStage.RESERVATION
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def close(self, reason: str) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                _run_closer(self.closer, reason),
                name=f"voip-call-resource-close-{self.name}",
            )
        await async_wait_for_cleanup(self._close_task)


@dataclass(frozen=True, slots=True)
class SessionTerminationResult:
    reason: str
    closed_legs: tuple[str, ...]
    closed_resources: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(slots=True)
class CallEventContext:
    """Bounded automation history owned by one call generation."""

    sequence: int = 0
    state: str = ""
    previous_state: str = ""
    route_history: list[dict[str, Any]] = field(default_factory=list)
    connected_at: float = 0.0
    duration_seconds: int | None = None
    terminal_summary_claimed: bool = False
    terminal_endpoint_ids: frozenset[str] = field(default_factory=frozenset)
    terminal_identity_fields: dict[str, Any] = field(default_factory=dict)
    trigger_sequences: WeakKeyDictionary[object, tuple[int, str]] | None = field(default=None, repr=False)

    def accept_trigger(self, subscriber: object, sequence: int, destination: str) -> bool:
        """Publish a call transition once per native subscription and destination."""
        if self.trigger_sequences is None:
            self.trigger_sequences = WeakKeyDictionary()
        previous = self.trigger_sequences.get(subscriber)
        if previous is not None and (sequence < previous[0] or previous == (sequence, destination)):
            return False
        self.trigger_sequences[subscriber] = (sequence, destination)
        return True


@dataclass(slots=True)
class CallArtifacts:
    """Transient coordination state owned by one call generation."""

    pending_invite: Any | None = None
    caller_invite: Any | None = None
    pending_route: dict[str, Any] | None = None
    video_parameter_sets: tuple[bytes, ...] | None = None
    forward_claim: bool = False
    answer_commit: bool = False
    trunk_info_queue: asyncio.Queue[Any] | None = None
    trunk_closed: bool = False
    delayed_offer_ports: Any | None = None

    def settle(self, intent: TerminationIntent | None = None) -> None:
        """Make every pending coordination artifact terminal exactly once."""

        route = self.pending_route
        if route is not None:
            future = route.get("future")
            if future is not None and hasattr(future, "done") and not future.done():
                if intent is None:
                    future.cancel()
                else:
                    future.set_result(
                        {
                            "action": "cancel",
                            "reason": "Request Terminated",
                            "decline_reason": intent.reason,
                        }
                    )
        self.pending_invite = None
        self.caller_invite = None
        self.pending_route = None
        self.video_parameter_sets = None
        self.forward_claim = False
        self.answer_commit = False
        self.trunk_info_queue = None
        self.trunk_closed = True
        delayed_offer_ports = self.delayed_offer_ports
        self.delayed_offer_ports = None
        if delayed_offer_ports is not None:
            delayed_offer_ports.release()


class EndpointCallSession:
    """Authoritative owner of one PBX call and all of its resources.

    A session generation is immutable.  Async dial, media and UI callbacks
    must present the current generation before mutating it, which turns late
    completions into harmless stale observations.  Termination first closes
    new mutation, then drains resources in ``CleanupStage`` order exactly once.
    """

    def __init__(
        self,
        call_id: str,
        generation: int,
        *,
        phase: SessionPhase = SessionPhase.NEW,
        termination_signaler: TerminationSignaler | None = None,
        termination_observer: TerminationObserver | None = None,
        on_terminated: Callable[["EndpointCallSession", SessionTerminationResult], None]
        | None = None,
        state_runtime: "SipEndpointRuntime | None" = None,
    ) -> None:
        clean_call_id = str(call_id or "").strip()
        if not clean_call_id:
            raise ValueError("call_id must not be empty")
        if int(generation) <= 0:
            raise ValueError("generation must be positive")
        self.call_id = clean_call_id
        self.state_runtime = state_runtime
        self.generation = int(generation)
        self.revision = 0
        self._state = phase.value
        self.owner = ""
        self.outcome = ""
        self.caller = ""
        self.callee = ""
        self.route_kind = ""
        self.phase = phase
        self.terminal_reason = ""
        self.termination_intent: TerminationIntent | None = None
        self.answer_committed = False
        self.legs: dict[str, CallLeg] = {}
        self.resources: list[ManagedResource] = []
        self.tasks: set[asyncio.Task[Any]] = set()
        self.named_tasks: dict[str, asyncio.Task[Any]] = {}
        self.video_intent_lock = asyncio.Lock()
        self.endpoint_claims: dict[str, str] = {}
        self.event_context = CallEventContext()
        self.committed_state: CallStateSnapshot | None = None
        self._state_observer: Callable[[EndpointCallSession], None] | None = None
        self._state_notification_depth = 0
        self._state_notification_pending = False
        self.artifacts = CallArtifacts()
        self.metadata: dict[str, Any] = {}
        self.termination_started = asyncio.Event()
        self.terminated = asyncio.Event()
        self._termination_task: asyncio.Task[SessionTerminationResult] | None = None
        self._termination_initiator: asyncio.Task[Any] | None = None
        self._termination_signaler = termination_signaler
        self._termination_observer = termination_observer
        self._on_terminated = on_terminated

    @property
    def id(self) -> str:
        return self.call_id

    @property
    def state(self) -> str:
        """Return the public projection of the authoritative phase."""

        return self._state

    def apply_observation(
        self,
        state: str,
        phase: SessionPhase | None,
    ) -> bool:
        """Apply one public state and accepted phase as one mutation."""

        changed = self._state != state or (
            phase is not None and self.phase is not phase
        )
        self._state = state
        if phase is not None:
            self.phase = phase
        return changed

    @property
    def token(self) -> CallToken:
        return CallToken(self.call_id, self.generation)

    @property
    def live(self) -> bool:
        return self.phase not in {SessionPhase.TERMINATING, SessionPhase.TERMINATED}

    def owns(self, token: CallToken) -> bool:
        return bool(
            self.live
            and token.call_id == self.call_id
            and token.generation == self.generation
        )

    def claim_answer(self, token: CallToken, claim: Callable[[], bool]) -> bool:
        """Commit one successful answer transition for this generation."""

        if not self.owns(token) or self.answer_committed:
            return False
        if not bool(claim()) or not self.owns(token):
            return False
        self.answer_committed = True
        return True

    def ensure_live(self, token: CallToken | None = None) -> None:
        if not self.live or (token is not None and not self.owns(token)):
            raise RuntimeError(f"call session {self.call_id!r} is no longer current")

    def ensure_transferable(self) -> None:
        """Allow terminal adapters to detach resources before cleanup starts."""

        if self.phase is SessionPhase.TERMINATED or self._termination_task is not None:
            raise RuntimeError(f"call session {self.call_id!r} cleanup has started")

    def update_metadata(self, **values: Any) -> None:
        """Update observable call metadata while this generation is live."""

        self.ensure_live()
        self.metadata.update(values)
        self.state_changed()

    def state_changed(self) -> None:
        """Notify the existing runtime after an owner mutation, never from a UI."""
        if self._state_notification_depth:
            self._state_notification_pending = True
        elif self._state_observer is not None:
            self._state_observer(self)

    @contextmanager
    def defer_state_notifications(self):
        """Keep answer/resource commit failures out of successful projections."""
        self._state_notification_depth += 1
        try:
            yield
        finally:
            self._state_notification_depth -= 1
            if not self._state_notification_depth and self._state_notification_pending:
                self._state_notification_pending = False
                self.state_changed()

    def add_leg(self, leg: CallLeg) -> CallLeg:
        self.ensure_live()
        if not leg.leg_id or leg.leg_id in self.legs:
            raise ValueError(f"duplicate or empty leg_id {leg.leg_id!r}")
        self.legs[leg.leg_id] = leg
        return leg

    def add_resource(
        self,
        name: str,
        value: Any,
        closer: AsyncCloser,
        *,
        stage: CleanupStage = CleanupStage.RESERVATION,
    ) -> ManagedResource:
        self.ensure_live()
        if not str(name or "").strip():
            raise ValueError("resource name must not be empty")
        if any(resource.name == str(name) for resource in self.resources):
            raise ValueError(f"duplicate resource name {name!r}")
        resource = ManagedResource(str(name), value, closer, stage)
        self.resources.append(resource)
        return resource

    def release_resource(
        self,
        name: str,
        *,
        value: Any | None = None,
    ) -> ManagedResource | None:
        """Transfer a live resource away without closing it."""

        self.ensure_transferable()
        for index, resource in enumerate(self.resources):
            if resource.name == name and (value is None or resource.value is value):
                return self.resources.pop(index)
        return None

    def release_leg(
        self,
        leg_id: str,
        *,
        dialog: Any | None = None,
    ) -> CallLeg | None:
        """Transfer a leg to an explicit legacy cleanup path."""

        self.ensure_transferable()
        leg = self.legs.get(str(leg_id or "").strip())
        if leg is None or (dialog is not None and leg.dialog is not dialog):
            return None
        return self.legs.pop(leg.leg_id)

    def own_task(
        self,
        task: asyncio.Task[Any],
        *,
        name: str = "",
    ) -> asyncio.Task[Any]:
        self.ensure_live()
        clean_name = str(name or "").strip()
        if clean_name:
            current = self.named_tasks.get(clean_name)
            if current is not None and current is not task:
                raise ValueError(f"duplicate task name {clean_name!r}")
            self.named_tasks[clean_name] = task
        self.tasks.add(task)
        # Child cleanup tasks inherit this context. Weak references keep an
        # inherited dependency from extending either owner's lifetime.
        lineage = (ref(self), ref(task))
        if task is asyncio.current_task(loop=task.get_loop()):
            _TASK_LINEAGE.set((*_TASK_LINEAGE.get(), lineage))
        elif (context := task.get_context()) is not None:
            context.run(_TASK_LINEAGE.set, (*context.get(_TASK_LINEAGE, ()), lineage))

        def _forget(completed: asyncio.Task[Any]) -> None:
            self.tasks.discard(completed)
            if clean_name and self.named_tasks.get(clean_name) is completed:
                self.named_tasks.pop(clean_name, None)

        task.add_done_callback(_forget)
        return task

    def release_task(self, task: asyncio.Task[Any]) -> bool:
        """Transfer a background task away from session cancellation."""

        self.ensure_transferable()
        if task not in self.tasks:
            return False
        self.tasks.discard(task)
        for name, owned in tuple(self.named_tasks.items()):
            if owned is task:
                self.named_tasks.pop(name, None)
        return True

    def cleanup_waits_for(self, task: asyncio.Task[Any] | None) -> bool:
        """Return whether the terminal barrier is currently draining ``task``."""

        if task is None or self._termination_task is None:
            return False
        if task is self._termination_task or task in self.tasks:
            return True
        return any(session() is self and (owner() is self._termination_task or owner() in self.tasks)
                   for session, owner in _TASK_LINEAGE.get())

    def create_task(
        self,
        coroutine: Awaitable[Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        self.ensure_live()
        return self.own_task(asyncio.create_task(coroutine, name=name, eager_start=False), name=name)

    async def _close_resources(
        self,
        resources: list[ManagedResource],
        reason: str,
        closed: list[str],
        errors: list[str],
    ) -> None:
        for resource in resources:
            try:
                await resource.close(reason)
                closed.append(resource.name)
            except BaseException as err:  # teardown must continue through all owners
                errors.append(f"resource:{resource.name}:{type(err).__name__}")
                _LOGGER.debug(
                    "PBX session resource cleanup failed call_id=%s resource=%s",
                    self.call_id,
                    resource.name,
                    exc_info=True,
                )

    async def _run_termination(
        self,
        intent: TerminationIntent,
    ) -> SessionTerminationResult:
        _TASK_LINEAGE.set((*_TASK_LINEAGE.get(), (ref(self), ref(asyncio.current_task()))))
        errors: list[str] = []
        closed_legs: list[str] = []
        closed_resources: list[str] = []
        try:
            if self._termination_signaler is not None:
                result = self._termination_signaler(self.call_id, intent)
                if inspect.isawaitable(result):
                    await result
        except BaseException as err:  # signaling failure must not block cleanup
            errors.append(f"signaling:{type(err).__name__}")
            _LOGGER.debug(
                "PBX session terminal signaling failed call_id=%s",
                self.call_id,
                exc_info=True,
            )
        self.artifacts.settle(intent)

        current = asyncio.current_task()
        tasks = [
            task
            for task in tuple(self.tasks)
            if task is not current and task is not self._termination_initiator
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results, strict=True):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    errors.append(f"task:{task.get_name()}:{type(result).__name__}")

        ordered = sorted(
            reversed(self.resources),
            key=lambda resource: int(resource.stage),
            reverse=True,
        )
        before_legs = [
            resource for resource in ordered if resource.stage >= CleanupStage.LEG
        ]
        after_legs = [
            resource for resource in ordered if resource.stage < CleanupStage.LEG
        ]
        await self._close_resources(
            before_legs,
            self.terminal_reason,
            closed_resources,
            errors,
        )

        for leg in reversed(tuple(self.legs.values())):
            try:
                await leg.close(self.terminal_reason)
                closed_legs.append(leg.leg_id)
            except BaseException as err:  # teardown must continue through all legs
                errors.append(f"leg:{leg.leg_id}:{type(err).__name__}")
                _LOGGER.debug(
                    "PBX session leg cleanup failed call_id=%s leg_id=%s",
                    self.call_id,
                    leg.leg_id,
                    exc_info=True,
                )

        await self._close_resources(
            after_legs,
            self.terminal_reason,
            closed_resources,
            errors,
        )
        self._state = intent.public_state
        self.outcome = intent.reason
        self.phase = SessionPhase.TERMINATED
        result = SessionTerminationResult(
            reason=self.terminal_reason,
            closed_legs=tuple(closed_legs),
            closed_resources=tuple(closed_resources),
            errors=tuple(errors),
        )
        if self._on_terminated is not None:
            try:
                self._on_terminated(self, result)
            except Exception:
                _LOGGER.exception(
                    "PBX session termination observer failed call_id=%s",
                    self.call_id,
                )
        try:
            if self._termination_observer is not None:
                observed = self._termination_observer(self, intent)
                if inspect.isawaitable(observed):
                    await observed
        except BaseException as err:  # projection failure must not block cleanup
            errors.append(f"observer:{type(err).__name__}")
            _LOGGER.debug(
                "PBX session terminal projection failed call_id=%s",
                self.call_id,
                exc_info=True,
            )
            result = SessionTerminationResult(
                reason=self.terminal_reason,
                closed_legs=tuple(closed_legs),
                closed_resources=tuple(closed_resources),
                errors=tuple(errors),
            )
        finally:
            self.terminated.set()
        return result

    async def terminate(self, intent: TerminationIntent) -> SessionTerminationResult:
        """Terminate once; every caller waits for the same cleanup barrier."""

        return await async_wait_for_cleanup(self.start_termination(intent))

    def claim_termination(self, intent: TerminationIntent) -> bool:
        """Make the terminal decision once without starting resource cleanup.

        A signaling callback may still need to detach a legacy adapter before
        cleanup starts. The session remains the sole owner of the terminal
        transition while the caller completes that synchronous handoff.
        """

        if not self.live:
            return False
        self.phase = SessionPhase.TERMINATING
        self.termination_intent = intent
        self.terminal_reason = intent.reason
        self.artifacts.trunk_closed = True
        self.termination_started.set()
        return True

    def start_termination(
        self,
        intent: TerminationIntent,
    ) -> asyncio.Task[SessionTerminationResult]:
        """Start teardown synchronously and return its unique cleanup barrier.

        Signalling callbacks are deliberately synchronous at several ownership
        boundaries (CANCEL/BYE registry updates, transport disconnects).  They
        must be able to make the session terminal before yielding without
        spawning a second, untracked wrapper task.
        """

        if self._termination_task is None:
            if self.live:
                self.claim_termination(intent)
            elif self.termination_intent is None:
                self.termination_intent = intent
                self.terminal_reason = intent.reason
            elif self.termination_intent.reason == intent.reason:
                current = self.termination_intent
                self.termination_intent = TerminationIntent(
                    current.reason,
                    initiator=current.initiator,
                    public_state=intent.public_state,
                    sip_disposition=current.sip_disposition,
                    response_status=current.response_status,
                )
            self._termination_initiator = asyncio.current_task()
            if self._termination_initiator is not None:
                self.tasks.discard(self._termination_initiator)
            self._termination_task = asyncio.create_task(
                self._run_termination(self.termination_intent or intent),
                name=f"voip-call-session-terminate-{self.call_id}",
            )
        return self._termination_task


class SessionHandoff:
    """Undo a synchronous ownership cutover before any resource is destroyed.

    The participating sessions remain the only resource owners. Watcher
    cancellation and retirement must occur after this context commits.
    """

    def __init__(self, registry, *sessions):
        self.registry = registry
        self.sessions = tuple(dict.fromkeys(sessions))
        self.saved = []
        self.attributes = []
        self.index = {}
        self.claims = {}
        self.leg_states = []
        self._state_batch = None

    def preserve(self, owner, *names):
        self.attributes.extend((owner, name, getattr(owner, name)) for name in names)

    def __enter__(self):
        for session in self.sessions:
            session.ensure_live()
            self.preserve(session, "caller", "callee", "route_kind")
            self.preserve(session.artifacts, *(item.name for item in fields(CallArtifacts)))
            self.saved.append((session, dict(session.legs), list(session.resources),
                               dict(session.metadata), dict(session.endpoint_claims),
                               set(session.tasks), dict(session.named_tasks),
                               session.answer_committed, session.owner, session._state, session.phase))
            self.leg_states.extend((leg, leg.state, leg.phase, leg.role,
                                    leg.endpoint_id, leg.sip_call_id)
                                   for leg in session.legs.values())
            for key in session.legs:
                if self.registry is not None:
                    self.index[key] = self.registry.leg_index.get(key)
            for endpoint_id in session.endpoint_claims:
                self.claims[endpoint_id] = session.call_id
        if self.registry is not None:
            self.claims = self.registry.endpoint_claim_projection(self.claims)
        self._state_batch = self.registry.state_operation() if self.registry is not None else nullcontext()
        self._state_batch.__enter__()
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is not None:
                self._restore()
            for session in self.sessions:
                session.state_changed()
        finally:
            self._state_batch.__exit__(error_type, error, traceback)
        return False

    def _restore(self):
        for leg, state, phase, role, endpoint, wire in self.leg_states:
            leg.state, leg.phase, leg.role = state, phase, role
            leg.endpoint_id, leg.sip_call_id = endpoint, wire
        for owner, name, value in reversed(self.attributes):
            setattr(owner, name, value)
        for session, legs, resources, metadata, claims, tasks, named, answered, owner, state, phase in self.saved:
            session.ensure_live()
            for key in set(session.legs) - set(legs):
                if self.registry is not None and self.registry.leg_index.get(key) == session.call_id:
                    self.registry.leg_index.pop(key, None)
            for task in session.tasks - tasks:
                task.cancel()
            # Keep cancelled new tasks owned until the ordinary cleanup barrier
            # drains them; no SIP leg or media closer is invoked by rollback.
            session.named_tasks = named
            session.legs = legs
            session.resources = resources
            session.metadata.clear()
            session.metadata.update(metadata)
            session.endpoint_claims = claims
            session.answer_committed, session.owner = answered, owner
            session._state, session.phase = state, phase
        for key, owner in self.index.items():
            if owner is None:
                self.registry.leg_index.pop(key, None)
            else:
                self.registry.leg_index[key] = owner
        if self.registry is not None:
            self.registry.restore_endpoint_claim_projection(self.claims)
        return False
