"""Authoritative PBX runtime built without binding a second SIP listener."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
import inspect
import logging
from typing import TYPE_CHECKING, Any

from .call_registry import CallRuntimeApi, committed_operation
from .call_state import CallStateSnapshot
from .endpoint_session import (
    CallArtifacts,
    CallEventContext,
    CallLeg,
    CallToken,
    CleanupStage,
    EndpointCallSession,
    LegKind,
    LegPhase,
    SessionPhase,
    SessionTerminationResult,
    TerminationInitiator,
    TerminationIntent,
    TerminationObserver,
    TerminationSignaler,
)
from .session_cleanup import async_wait_for_cleanup
from .phone_endpoint import EndpointKind


if TYPE_CHECKING:
    from .call_forwarder import TransferResult


_LOGGER = logging.getLogger(__name__)


class RuntimePhase(StrEnum):
    """Lifecycle of the endpoint-wide PBX owner."""

    DARK = "dark"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"


ComponentCloser = Callable[[], Awaitable[None] | None]


_PUBLIC_PHASES: dict[str, SessionPhase] = {
    "new": SessionPhase.NEW,
    "route_requested": SessionPhase.ROUTING,
    "calling": SessionPhase.CALLING,
    "remote_ringing": SessionPhase.RINGING,
    "ringing": SessionPhase.RINGING,
    "connecting": SessionPhase.CONNECTING,
    "answered": SessionPhase.ESTABLISHED,
    "in_call": SessionPhase.ESTABLISHED,
    "held": SessionPhase.HELD,
    "transferring": SessionPhase.TRANSFERRING,
}

_OBSERVED_PHASE_TRANSITIONS: dict[SessionPhase, frozenset[SessionPhase]] = {
    SessionPhase.NEW: frozenset(
        {
            SessionPhase.ROUTING,
            SessionPhase.CALLING,
            SessionPhase.RINGING,
            SessionPhase.CONNECTING,
            SessionPhase.ESTABLISHED,
        }
    ),
    SessionPhase.ROUTING: frozenset(
        {
            SessionPhase.CALLING,
            SessionPhase.RINGING,
            SessionPhase.CONNECTING,
            SessionPhase.ESTABLISHED,
        }
    ),
    SessionPhase.CALLING: frozenset(
        {
            SessionPhase.RINGING,
            SessionPhase.CONNECTING,
            SessionPhase.ESTABLISHED,
        }
    ),
    SessionPhase.RINGING: frozenset(
        {SessionPhase.CONNECTING, SessionPhase.ESTABLISHED}
    ),
    # A failed forward may deliberately resume the original ringing endpoint.
    SessionPhase.CONNECTING: frozenset(
        {SessionPhase.RINGING, SessionPhase.ESTABLISHED}
    ),
    SessionPhase.ESTABLISHED: frozenset({SessionPhase.HELD, SessionPhase.TRANSFERRING}),
    SessionPhase.HELD: frozenset({SessionPhase.ESTABLISHED, SessionPhase.TRANSFERRING}),
    SessionPhase.TRANSFERRING: frozenset({SessionPhase.ESTABLISHED}),
}

_PUBLIC_LEG_PHASES: dict[str, LegPhase] = {
    "new": LegPhase.NEW,
    "calling": LegPhase.INVITING,
    "connecting": LegPhase.INVITING,
    "remote_ringing": LegPhase.RINGING,
    "ringing": LegPhase.RINGING,
    "answered": LegPhase.ANSWERED,
    "in_call": LegPhase.ANSWERED,
    "held": LegPhase.HELD,
    "idle": LegPhase.CLOSED,
    "cancelled": LegPhase.CLOSED,
    "declined": LegPhase.CLOSED,
    "busy": LegPhase.CLOSED,
    "error": LegPhase.CLOSED,
    "media_incompatible": LegPhase.CLOSED,
    "transport_unreachable": LegPhase.CLOSED,
}

_LEG_KINDS: dict[str, LegKind] = {
    "trunk": LegKind.TRUNK,
    "ha_softphone": LegKind.BROWSER,
    "softphone": LegKind.BROWSER,
    "local_phone": LegKind.BROWSER,
    "esp": LegKind.ESPHOME,
    "assist": LegKind.ASSIST,
    "conference": LegKind.CONFERENCE,
}


@dataclass(slots=True)
class _OwnedComponent:
    value: Any
    closer: ComponentCloser | None = None


class SipEndpointRuntime(CallRuntimeApi):
    """Own endpoint components and every logical call generation.

    Constructing this object is intentionally side-effect free: it does not
    open UDP/TCP sockets or start a trunk. Endpoint setup hands the complete
    component set to this sole lifecycle owner, then calls :meth:`activate`
    exactly once.
    """

    _COMPONENT_STOP_ORDER = (
        "trunk",
        "conference_manager",
        "registrar",
        "tcp_listener",
        "udp_listener",
    )

    def __init__(
        self,
        *,
        allow_dark_sessions: bool = False,
    ) -> None:
        self.phase = RuntimePhase.DARK
        self.calls: dict[str, EndpointCallSession] = {}
        self.sessions = self.calls
        self.leg_index: dict[str, str] = {}
        self.terminated_call_ids: OrderedDict[
            str, tuple[int, CallEventContext | None]
        ] = OrderedDict()
        self.media_controller_lock = asyncio.Lock()
        self._generation = 0
        self._allow_dark_sessions = allow_dark_sessions
        self._state_depth = 0
        self._changed_sessions: dict[CallToken, EndpointCallSession] = {}
        self._state_listeners: set[Callable[[CallStateSnapshot], None]] = set()
        self._components: dict[str, _OwnedComponent] = {}
        self._endpoint_registry: Any | None = None
        self._termination_signaler: TerminationSignaler | None = None
        self._termination_observer: TerminationObserver | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self.trunk_stop_task: asyncio.Task[Any] | None = None
        self.softphone_start_locks: dict[str, asyncio.Lock] = {}
        self.forward_call: Callable[..., Awaitable[asyncio.Task[TransferResult]]] | None = None
        self.refer_call: Any | None = None
        self.start_ring_group_from_ha: Any | None = None
        self.ring_conference_members_from_ha: Any | None = None

    @property
    def active(self) -> bool:
        return self.phase is RuntimePhase.ACTIVE

    @contextmanager
    def state_operation(self):
        """Batch nested synchronous changes before exposing a complete result."""
        self._state_depth += 1
        try:
            yield
        finally:
            self._state_depth -= 1
            if not self._state_depth:
                pending, self._changed_sessions = self._changed_sessions, {}
                committed = [self._commit_state(session, notify=False)
                             for session in pending.values()]
                for snapshot in committed:
                    if snapshot is not None:
                        self._publish_state(snapshot)

    def subscribe_state(self, listener: Callable[[CallStateSnapshot], None]) -> Callable[[], None]:
        self._state_listeners.add(listener)
        return lambda: self._state_listeners.discard(listener)

    def _state_changed(self, session: EndpointCallSession) -> None:
        if self._state_depth:
            self._changed_sessions[session.token] = session
        else:
            self._commit_state(session)

    def _state_snapshot(self, session: EndpointCallSession) -> CallStateSnapshot:
        """Read media attributes from their SIP owners, without a BLF state copy."""
        manager = self.component("udp_listener")
        media = []
        for leg in session.legs.values():
            contract = getattr(leg.dialog, "dialog", None)
            if contract is None and manager is not None:
                reader = getattr(manager, "invite_for_call", None)
                if reader is not None:
                    contract = reader(leg.sip_call_id)
            if contract is not None:
                video = getattr(contract, "video_format", None)
                media.append((leg.leg_id, tuple(str(value) for value in (
                    getattr(contract, "local_audio_direction", "sendrecv"),
                    getattr(contract, "remote_audio_direction", "sendrecv"),
                    getattr(contract, "remote_audio_connection_held", False),
                    getattr(contract, "local_video_direction", ""),
                    getattr(video, "direction", ""),
                    video is not None,
                    getattr(contract, "remote_video_connection_held", False),
                ))))
        return CallStateSnapshot.capture(session, media)

    def _commit_state(self, session: EndpointCallSession, *, notify: bool = True) -> CallStateSnapshot | None:
        if session._state_notification_depth:
            session._state_notification_pending = True
            return
        if self.sessions.get(session.call_id) is not session:
            return
        if session.phase is SessionPhase.TERMINATING:
            return
        snapshot = self._state_snapshot(session)
        if snapshot == session.committed_state:
            return
        context = session.event_context
        if context.state != session.state:
            self._advance_event_context(context, session.state)
        elif session.committed_state is not None:
            context.sequence += 1
        session.committed_state = self._state_snapshot(session)
        if notify:
            self._publish_state(session.committed_state)
        return session.committed_state

    def _publish_state(self, snapshot: CallStateSnapshot) -> None:
        for listener in tuple(self._state_listeners):
            try:
                listener(snapshot)
            except Exception:
                _LOGGER.exception("Committed call observer failed")

    def attach_component(
        self,
        name: str,
        value: Any,
        *,
        closer: ComponentCloser | None = None,
    ) -> None:
        """Transfer one endpoint component before the atomic activation."""

        if self.phase is not RuntimePhase.DARK:
            raise RuntimeError("PBX components can only be attached while dark")
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("component name must not be empty")
        if clean_name in self._components:
            raise ValueError(f"duplicate PBX component {clean_name!r}")
        self._components[clean_name] = _OwnedComponent(value, closer)

    def adopt_component(
        self,
        name: str,
        value: Any,
        *,
        closer: ComponentCloser | None = None,
    ) -> None:
        """Adopt an optional component created after listener activation.

        Trunks and conference managers are optional and may be constructed
        lazily.  Adoption transfers them to the already-active endpoint owner;
        it never starts I/O and cannot replace a different live component.
        """

        if self.phase not in {RuntimePhase.DARK, RuntimePhase.ACTIVE}:
            raise RuntimeError(f"cannot adopt PBX component while {self.phase.value}")
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("component name must not be empty")
        current = self._components.get(clean_name)
        if current is not None:
            if current.value is not value:
                raise ValueError(f"duplicate PBX component {clean_name!r}")
            if closer is not None:
                current.closer = closer
            return
        self._components[clean_name] = _OwnedComponent(value, closer)

    def release_component(self, name: str, value: Any) -> bool:
        """Release a component only when the caller still owns that instance."""

        clean_name = str(name or "").strip()
        current = self._components.get(clean_name)
        if current is None or current.value is not value:
            return False
        self._components.pop(clean_name, None)
        return True

    def component(self, name: str) -> Any | None:
        owned = self._components.get(str(name or "").strip())
        return owned.value if owned is not None else None

    def _bind_endpoint_registry(self, registry: Any | None) -> None:
        """Bind the phone directory used by session-owned busy claims."""

        if registry is self._endpoint_registry:
            return
        if any(session.endpoint_claims for session in self.calls.values()):
            raise RuntimeError(
                "cannot replace endpoint registry while calls are active"
            )
        self._endpoint_registry = registry

    @property
    def endpoint_registry(self) -> Any | None:
        return self._endpoint_registry

    def bind_termination_signaler(self, signaler: TerminationSignaler) -> None:
        """Bind the single adapter that performs terminal SIP signaling."""

        if (
            self._termination_signaler is not None
            and self._termination_signaler is not signaler
        ):
            raise RuntimeError("PBX termination signaler is already bound")
        self._termination_signaler = signaler

    @property
    def has_termination_signaler(self) -> bool:
        return self._termination_signaler is not None

    def bind_termination_observer(self, observer: TerminationObserver) -> None:
        """Bind the single HA projection observer for terminal sessions."""

        if self._termination_observer is not None:
            raise RuntimeError("PBX termination observer is already bound")
        self._termination_observer = observer

    @property
    def has_termination_observer(self) -> bool:
        return self._termination_observer is not None

    def endpoint_claims_snapshot(self) -> dict[str, dict[str, str]]:
        """Return a detached compatibility projection of session claims."""

        return {
            call_id: dict(session.endpoint_claims)
            for call_id, session in self.calls.items()
            if session.endpoint_claims
        }

    def relays_snapshot(self) -> dict[str, Any]:
        """Return the live relay index derived from session-owned resources."""

        return self.resources_snapshot("relay")

    def resources_snapshot(self, prefix: str) -> dict[str, Any]:
        """Return one named class of resources from authoritative sessions."""
        return dict(self.resource_items(prefix)) if str(prefix or "").strip() else {}

    def sip_clients_snapshot(self) -> dict[str, Any]:
        """Return dialogs indexed by their SIP Call-ID from owned legs."""
        return dict(self.sip_client_items())

    def client_watchers_snapshot(self) -> dict[str, asyncio.Task[Any]]:
        """Return lifecycle watchers derived from session-owned named tasks."""

        watchers: dict[str, asyncio.Task[Any]] = {}
        for session in self.calls.values():
            for name, task in session.named_tasks.items():
                if name.startswith("client_watcher:"):
                    watchers[name.removeprefix("client_watcher:")] = task
        return watchers

    def bridge_links_snapshot(self) -> dict[str, str]:
        """Return source-to-destination links stored by authoritative sessions."""
        return dict(self.bridge_link_items())

    def artifacts_for(self, call_id: str) -> CallArtifacts | None:
        """Return transient state owned by the current call generation."""

        session = self.get_session(call_id)
        return session.artifacts if session is not None else None

    def task_for(self, call_id: str, name: str) -> asyncio.Task[Any] | None:
        """Return one task owned by the current call generation."""

        session = self.get_session(call_id)
        return session.named_tasks.get(name) if session is not None else None

    def cancel_task(self, call_id: str, name: str) -> asyncio.Task[Any] | None:
        """Cancel and detach one named task from the current generation."""

        session = self.get_session(call_id)
        task = session.named_tasks.get(name) if session is not None else None
        if task is None:
            return None
        session.release_task(task)
        if not task.done():
            task.cancel()
        return task

    def replace_task(
        self,
        call_id: str,
        task: asyncio.Task[Any],
        *,
        name: str,
    ) -> bool:
        """Replace one named call task without creating parallel ownership."""

        session = self.get_session(call_id)
        if session is None or not session.live:
            return False
        current = session.named_tasks.get(name)
        if current is not None and current is not task:
            session.release_task(current)
            if not current.done():
                current.cancel()
        session.own_task(task, name=name)
        return True

    def named_task_count(self, name: str) -> int:
        """Count unfinished tasks with one session-owned role."""

        return sum(
            1
            for session in self.calls.values()
            if (task := session.named_tasks.get(name)) is not None and not task.done()
        )

    def set_bridge_link(self, source_call_id: str, dest_call_id: str) -> None:
        """Attach one destination dialog identity to its source session."""

        session = self.get_session(source_call_id)
        clean_dest_call_id = str(dest_call_id or "").strip()
        if session is None or not session.live or not clean_dest_call_id:
            raise RuntimeError(f"call session {source_call_id!r} is unavailable")
        session.update_metadata(bridge_dest_call_id=clean_dest_call_id)

    def forget_bridge_link(self, source_call_id: str) -> str:
        """Remove and return one destination link without ending the session."""

        session = self.get_session(source_call_id)
        if session is None or session.phase is SessionPhase.TERMINATED:
            return ""
        dest_call_id = str(session.metadata.pop("bridge_dest_call_id", "") or "")
        return dest_call_id

    def attach_relay(self, call_id: str, relay: Any) -> None:
        """Make one RTP relay a media resource of its call session."""

        clean_call_id = str(call_id or "").strip()

        async def _stop_relay(_reason: str) -> None:
            await relay.stop()

        if not self.own_resource(
            clean_call_id,
            f"relay:{clean_call_id}",
            relay,
            _stop_relay,
            stage=CleanupStage.MEDIA,
        ):
            raise RuntimeError(f"call session {call_id!r} is unavailable")

    @committed_operation
    def claim_endpoint(
        self,
        call_id: str,
        endpoint_id: str,
        *,
        role: str = "endpoint",
        adopt_transport: bool = False,
    ) -> bool:
        """Reserve one phone as a resource of the authoritative session."""

        registry = self._endpoint_registry
        session_id = self.resolve_session_id(str(call_id or "").strip())
        clean_endpoint_id = str(endpoint_id or "").strip()
        if registry is None or not clean_endpoint_id:
            return False
        if not session_id:
            raise ValueError("call_id must not be empty")
        session = self.sessions.get(session_id) or self.upsert(session_id, state="new")
        endpoint = registry.get(clean_endpoint_id) if hasattr(registry, "get") else None
        active_id = getattr(endpoint, "active_call_id", "")
        active = self.sessions.get(self.resolve_session_id(active_id)) if active_id else None
        answered_destination = False
        if active is not None and active.live:
            destination_leg = active.legs.get(self.bridge_link_for(active.call_id))
            answered_destination = bool(
                destination_leg is not None
                and destination_leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}
                and (destination_leg.phase in {LegPhase.ANSWERED, LegPhase.HELD}
                     or getattr(destination_leg.dialog, "dialog", None) is not None)
                and clean_endpoint_id == (
                    destination_leg.endpoint_id
                    or active.metadata.get("dest_endpoint_id")
                    or active.metadata.get("target_endpoint_id")
                )
            )
        consultation = (
            role == "source"
            and getattr(endpoint, "kind", None) in {EndpointKind.SIP_ACCOUNT, EndpointKind.ESPHOME}
            and active is not None
            and active.call_id != session.call_id
            and clean_endpoint_id in active.endpoint_claims
            # Native SIP peers may stage another source dialog for consultation
            # or REFER while their established conversation remains owned.
            # A confirmed callee also qualifies before the bridge commits.
            and (active.phase in {SessionPhase.ESTABLISHED, SessionPhase.HELD}
                 or answered_destination)
        )
        previous = session.endpoint_claims.get(clean_endpoint_id)
        revision = session.revision
        self._own_endpoint_claim(session, clean_endpoint_id, role)
        try:
            if consultation:
                # Keep the established busy projection until it closes.
                pass
            elif adopt_transport and hasattr(registry, "adopt_transport_call"):
                self.adopt_event_observation(f"physical:{clean_endpoint_id}", session.call_id)
                registry.adopt_transport_call(clean_endpoint_id, session.call_id)
            else:
                registry.claim_call(clean_endpoint_id, session.call_id)
        except Exception:
            if previous is None:
                session.endpoint_claims.pop(clean_endpoint_id, None)
                session.release_resource(f"endpoint_claim:{clean_endpoint_id}", value=registry)
            else:
                session.endpoint_claims[clean_endpoint_id] = previous
            session.revision = revision
            raise
        session.state_changed()
        return True

    def _own_endpoint_claim(self, session: EndpointCallSession, clean_endpoint_id: str, role: str) -> None:
        registry = self._endpoint_registry
        previous = session.endpoint_claims.get(clean_endpoint_id)
        session.endpoint_claims[clean_endpoint_id] = str(role or "endpoint")
        resource_name = f"endpoint_claim:{clean_endpoint_id}"
        if not any(resource.name == resource_name for resource in session.resources):

            def _release_claim(_reason: str) -> None:
                current = session.endpoint_claims.pop(clean_endpoint_id, None)
                if current is not None and (
                    not hasattr(registry, "get")
                    or registry.get(clean_endpoint_id) is not None
                ):
                    self._release_endpoint_projection(clean_endpoint_id, session.call_id)

            session.add_resource(
                resource_name,
                registry,
                _release_claim,
                stage=CleanupStage.RESERVATION,
            )
        if previous != session.endpoint_claims[clean_endpoint_id]:
            session.revision += 1

    def _release_endpoint_projection(self, endpoint_id: str, call_id: str) -> bool:
        """Keep a SIP phone busy while another owned dialog remains."""
        remaining = next((
            item.call_id for item in self.sessions.values()
            if item.call_id != call_id and item.phase is not SessionPhase.TERMINATED
            and endpoint_id in item.endpoint_claims
        ), "")
        if remaining:
            return bool(self._endpoint_registry.release_call(
                endpoint_id, call_id, remaining_call_id=remaining,
            ))
        return bool(self._endpoint_registry.release_call(endpoint_id, call_id))

    def endpoint_claim_projection(self, endpoint_ids) -> dict[str, str]:
        """Read each affected phone's canonical selected call, including consultation."""
        if self._endpoint_registry is None:
            return {}
        return {endpoint_id: endpoint.active_call_id for endpoint_id in endpoint_ids
                if (endpoint := self._endpoint_registry.get(endpoint_id)) is not None}

    def restore_endpoint_claim_projection(self, claims: dict[str, str]) -> None:
        """Restore projections after a synchronous handoff restored its owners."""
        if self._endpoint_registry is None:
            return
        for endpoint_id, owner in claims.items():
            session = self.get_session(owner)
            if session is None or not session.live or endpoint_id not in session.endpoint_claims:
                raise RuntimeError("handoff claim owner changed")
            endpoint = self._endpoint_registry.get(endpoint_id)
            if endpoint is not None and endpoint.active_call_id != owner:
                self._endpoint_registry.release_call(endpoint_id, endpoint.active_call_id, remaining_call_id=owner)

    @committed_operation
    def transfer_endpoint_claim(self, source_id: str, destination_id: str, endpoint_id: str) -> None:
        """Move a live claim without briefly publishing the phone as idle."""
        source, destination = self.get_session(source_id), self.get_session(destination_id)
        if source is None or destination is None or source is destination:
            raise ValueError("claim transfer requires two distinct sessions")
        source.ensure_live()
        destination.ensure_live()
        endpoint = self._endpoint_registry.require(endpoint_id)
        if endpoint.active_call_id != source_id or endpoint_id not in source.endpoint_claims:
            raise ValueError("source no longer owns the phone claim")
        if endpoint_id in destination.endpoint_claims:
            raise ValueError("destination already owns the phone claim")
        role = source.endpoint_claims.pop(endpoint_id)
        source.release_resource(f"endpoint_claim:{endpoint_id}", value=self._endpoint_registry)
        self._own_endpoint_claim(destination, endpoint_id, role)
        self._endpoint_registry.release_call(endpoint_id, source_id, remaining_call_id=destination_id)
        source.revision += 1
        source.state_changed()
        destination.state_changed()

    @committed_operation
    def release_endpoint_claim(
        self,
        call_id: str,
        endpoint_id: str,
    ) -> bool:
        """Release one losing phone without terminating the whole call."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        clean_endpoint_id = str(endpoint_id or "").strip()
        if (
            session is None
            or not session.live
            or clean_endpoint_id not in session.endpoint_claims
        ):
            return False
        registry = self._endpoint_registry
        session.endpoint_claims.pop(clean_endpoint_id, None)
        session.release_resource(
            f"endpoint_claim:{clean_endpoint_id}",
            value=registry,
        )
        browser_leg = session.legs.get(f"browser:{clean_endpoint_id}")
        if browser_leg is not None and browser_leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}:
            self.observe_leg(session.call_id, browser_leg.leg_id, role=browser_leg.role,
                             endpoint_id=clean_endpoint_id, state="cancelled",
                             generation=session.generation)
        released = False
        if registry is not None and (
            not hasattr(registry, "get") or registry.get(clean_endpoint_id) is not None
        ):
            released = self._release_endpoint_projection(clean_endpoint_id, session.call_id)
        if released:
            session.revision += 1
        session.state_changed()
        return released

    def release_endpoint_claims(self, call_id: str) -> None:
        """Release every phone claim owned by one live call."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            return
        for endpoint_id in tuple(session.endpoint_claims):
            self.release_endpoint_claim(session.call_id, endpoint_id)

    def activate(self) -> None:
        """Make this owner authoritative without starting any I/O itself."""

        if self.phase is not RuntimePhase.DARK:
            raise RuntimeError(f"cannot activate PBX runtime from {self.phase.value}")
        self.phase = RuntimePhase.ACTIVE

    def _on_terminated(
        self,
        session: EndpointCallSession,
        _result: SessionTerminationResult,
    ) -> None:
        if self.calls.get(session.call_id) is not session:
            return
        self._retire_observation(session)
        self.calls.pop(session.call_id, None)
        self._publish_terminal_state(session)

    def _publish_terminal_state(self, session):
        session.committed_state = CallStateSnapshot.capture(session)
        self._publish_state(session.committed_state)

    def create_session(
        self,
        call_id: str,
        *,
        phase: SessionPhase = SessionPhase.NEW,
        **metadata: Any,
    ) -> EndpointCallSession:
        """Create one unique live generation and publish its initial state."""

        if self.phase is not RuntimePhase.ACTIVE and not (
            self.phase is RuntimePhase.DARK and self._allow_dark_sessions
        ):
            raise RuntimeError("PBX runtime is not active")
        clean_call_id = str(call_id or "").strip()
        if not clean_call_id:
            raise ValueError("call_id must not be empty")
        current = self.calls.get(clean_call_id)
        if current is not None:
            raise ValueError(f"call_id {clean_call_id!r} is already active")
        self._generation += 1
        session = EndpointCallSession(
            clean_call_id,
            self._generation,
            phase=phase,
            termination_signaler=self._termination_signaler,
            termination_observer=self._termination_observer,
            on_terminated=self._on_terminated,
            state_runtime=self,
        )
        session.metadata.update(metadata)
        self.calls[clean_call_id] = session
        session._state_observer = self._state_changed
        return session

    def ensure_session(
        self,
        call_id: str,
        **metadata: Any,
    ) -> EndpointCallSession:
        """Return the live authoritative generation, creating it if needed."""

        session = self.get_session(call_id)
        if session is not None and not session.live and self.phase is RuntimePhase.DARK:
            self._retire_observation(session)
            self.calls.pop(session.call_id, None)
            session = None
        if session is None:
            return self.create_session(call_id, **metadata)
        if metadata:
            session.update_metadata(**metadata)
        return session

    def _resolve_observation(
        self,
        session: EndpointCallSession,
        state: str,
    ) -> tuple[bool, SessionPhase | None]:
        """Resolve an accepted public observation to its authoritative phase."""

        next_phase = _PUBLIC_PHASES.get(str(state or "").strip())
        if next_phase is None:
            return True, None
        if session.phase is next_phase:
            return True, next_phase
        if next_phase in _OBSERVED_PHASE_TRANSITIONS.get(session.phase, frozenset()):
            return True, next_phase
        _LOGGER.debug(
            "Ignoring stale PBX call phase call_id=%s generation=%s "
            "current=%s observed=%s",
            session.call_id,
            session.generation,
            session.phase.value,
            next_phase.value,
        )
        return False, None

    def observe_leg(
        self,
        call_id: str,
        leg_id: str,
        *,
        role: str,
        state: str = "",
        sip_call_id: str = "",
        endpoint_id: str = "",
        dialog: Any | None = None,
        media: Any | None = None,
        closer: Callable[[str], Awaitable[None] | None] | None = None,
        generation: int | None = None,
    ) -> bool:
        """Create/update one projected leg under the authoritative session."""

        session = self.get_session(call_id, generation=generation)
        if session is None or not session.live:
            return False
        clean_leg_id = str(leg_id or "").strip()
        if not clean_leg_id:
            return False
        leg = session.legs.get(clean_leg_id)
        if leg is None:
            leg = session.add_leg(
                CallLeg(
                    leg_id=clean_leg_id,
                    kind=_LEG_KINDS.get(str(role or "").strip(), LegKind.SIP),
                    endpoint_id=str(endpoint_id or "").strip(),
                    sip_call_id=str(sip_call_id or clean_leg_id).strip(),
                    dialog=dialog,
                    media=media,
                    role=str(role or ""),
                    state=str(state or ""),
                    closer=closer,
                )
            )
        next_phase = _PUBLIC_LEG_PHASES.get(str(state or "").strip())
        if next_phase is not None:
            leg.phase = next_phase
        if role:
            leg.role = str(role)
        if state:
            leg.state = str(state)
        if endpoint_id:
            leg.endpoint_id = str(endpoint_id).strip()
        if sip_call_id:
            leg.sip_call_id = str(sip_call_id).strip()
        if dialog is not None:
            leg.dialog = dialog
        if media is not None:
            leg.media = media
        if closer is not None:
            leg.closer = closer
        session.revision += 1
        session.state_changed()
        return True

    def release_leg(
        self,
        call_id: str,
        leg_id: str,
        *,
        dialog: Any | None = None,
        generation: int | None = None,
    ) -> bool:
        """Transfer a leg out of the session before an explicit cleanup."""

        session = self.get_session(call_id, generation=generation)
        if session is None or session.phase is SessionPhase.TERMINATED:
            return False
        released = session.release_leg(leg_id, dialog=dialog) is not None
        if released:
            session.state_changed()
        return released

    def restore_leg_state(self, call_id, leg, previous, *, generation):
        """Restore only a failed attempt's current leg, through its state owner."""
        session = self.get_session(call_id, generation=generation)
        if session is None or not session.live or session.legs.get(leg.leg_id) is not leg:
            return False
        if previous is None:
            leg.state, leg.phase = "idle", LegPhase.CLOSED
        else:
            leg.state, leg.phase, leg.role, leg.endpoint_id = previous
        session.revision += 1
        session.state_changed()
        return True

    def retire_leg(self, call_id, leg, *, endpoint_id, generation):
        """Mark the owned old transfer participant as closing without duplication."""
        session = self.get_session(call_id, generation=generation)
        if session is None or not session.live or session.legs.get(leg.leg_id) is not leg:
            return False
        leg.phase = LegPhase.CLOSING
        leg.endpoint_id = endpoint_id
        session.revision += 1
        session.state_changed()
        return True

    def own_resource(
        self,
        call_id: str,
        name: str,
        value: Any,
        closer: Callable[[str], Awaitable[None] | None],
        *,
        stage: CleanupStage = CleanupStage.RESERVATION,
        generation: int | None = None,
    ) -> bool:
        """Transfer one concrete call resource to its authoritative session."""

        session = self.get_session(call_id, generation=generation)
        if session is None or not session.live:
            return False
        current = next(
            (resource for resource in session.resources if resource.name == name),
            None,
        )
        if current is not None:
            if current.value is value:
                return True
            raise ValueError(f"duplicate PBX resource {name!r}")
        session.add_resource(name, value, closer, stage=stage)
        return True

    def release_resource(
        self,
        call_id: str,
        name: str,
        *,
        value: Any | None = None,
        generation: int | None = None,
    ) -> bool:
        """Transfer one concrete resource to an explicit cleanup path."""

        session = self.get_session(call_id, generation=generation)
        if session is None or session.phase is SessionPhase.TERMINATED:
            return False
        return session.release_resource(name, value=value) is not None

    def own_task(
        self,
        call_id: str,
        task: asyncio.Task[Any],
        *,
        name: str = "",
        generation: int | None = None,
    ) -> bool:
        """Make one background call task part of the cleanup barrier."""

        session = self.get_session(call_id, generation=generation)
        if session is None or not session.live:
            return False
        session.own_task(task, name=name)
        return True

    def release_task(
        self,
        call_id: str,
        task: asyncio.Task[Any],
        *,
        generation: int | None = None,
    ) -> bool:
        """Transfer a task to an explicit cleanup path."""

        session = self.get_session(call_id, generation=generation)
        if session is None or session.phase is SessionPhase.TERMINATED:
            return False
        return session.release_task(task)

    def request_termination(
        self,
        call_id: str,
        intent: TerminationIntent,
        *,
        generation: int | None = None,
    ) -> asyncio.Task[SessionTerminationResult] | None:
        """Make a synchronous terminal decision own one cleanup barrier."""

        session = self.get_session(call_id, generation=generation)
        if session is None:
            return None
        return session.start_termination(intent)

    def claim_termination(
        self,
        call_id: str,
        intent: TerminationIntent,
        *,
        generation: int | None = None,
    ) -> bool:
        """Claim one terminal transition before legacy adapter handoff."""

        session = self.get_session(call_id, generation=generation)
        if session is None:
            return False
        return session.claim_termination(intent)

    def get_session(
        self,
        call_id: str,
        *,
        generation: int | None = None,
    ) -> EndpointCallSession | None:
        session = self.calls.get(str(call_id or "").strip())
        if session is None or (
            generation is not None and session.generation != int(generation)
        ):
            return None
        return session

    async def _close_component(self, component: _OwnedComponent) -> None:
        if component.closer is None:
            return
        result = component.closer()
        if inspect.isawaitable(result):
            await result

    async def _run_shutdown(self) -> None:
        self.phase = RuntimePhase.STOPPING
        sessions = tuple(self.calls.values())
        if sessions:
            await asyncio.gather(
                *(
                    session.terminate(
                        TerminationIntent(
                            "runtime_shutdown",
                            initiator=TerminationInitiator.RUNTIME,
                            response_status=503,
                        )
                    )
                    for session in sessions
                ),
                return_exceptions=True,
            )
        ordered_names = [
            name for name in self._COMPONENT_STOP_ORDER if name in self._components
        ]
        ordered_names.extend(
            name
            for name in reversed(tuple(self._components))
            if name not in ordered_names
        )
        for name in ordered_names:
            try:
                await self._close_component(self._components[name])
            except BaseException:
                _LOGGER.exception("PBX component cleanup failed component=%s", name)
        self.phase = RuntimePhase.STOPPED

    async def shutdown(self) -> None:
        """Stop calls before transports; repeated/cancelled waiters are safe."""

        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._run_shutdown(),
                name="voip-pbx-runtime-shutdown",
            )
        await async_wait_for_cleanup(self._shutdown_task)
