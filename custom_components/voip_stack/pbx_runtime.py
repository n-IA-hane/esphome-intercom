"""Authoritative PBX runtime built without binding a second SIP listener."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
import inspect
import logging
from typing import Any, Protocol

from .endpoint_session import (
    CallLeg,
    CleanupStage,
    EndpointCallSession,
    LegKind,
    LegPhase,
    SessionPhase,
    SessionTerminationResult,
)
from .session_cleanup import async_wait_for_cleanup


_LOGGER = logging.getLogger(__name__)


class RuntimePhase(StrEnum):
    """Lifecycle of the endpoint-wide PBX owner."""

    DARK = "dark"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class CallProjectionSnapshot:
    """Read-only observable projection of one authoritative call session."""

    call_id: str
    generation: int
    phase: SessionPhase
    terminal_reason: str
    leg_ids: tuple[str, ...]
    metadata: dict[str, Any]


class CallProjection(Protocol):
    """Projection boundary; implementations never own call resources."""

    def publish(self, snapshot: CallProjectionSnapshot) -> None: ...

    def remove(self, snapshot: CallProjectionSnapshot) -> None: ...


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
    SessionPhase.ESTABLISHED: frozenset(
        {SessionPhase.HELD, SessionPhase.TRANSFERRING}
    ),
    SessionPhase.HELD: frozenset(
        {SessionPhase.ESTABLISHED, SessionPhase.TRANSFERRING}
    ),
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


class SipEndpointRuntime:
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

    def __init__(self, *, projection: CallProjection | None = None) -> None:
        self.phase = RuntimePhase.DARK
        self.calls: dict[str, EndpointCallSession] = {}
        self._generation = 0
        self._projection = projection
        self._components: dict[str, _OwnedComponent] = {}
        self._endpoint_registry: Any | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self.trunk_stop_task: asyncio.Task[Any] | None = None
        self.forward_tasks: dict[str, asyncio.Task[Any]] = {}
        self.forward_claims: set[str] = set()
        self.deadlines: dict[str, asyncio.Task[Any]] = {}
        self.trunk_info_queues: dict[str, asyncio.Queue[Any]] = {}
        self.trunk_closed_calls: set[str] = set()
        self.forward_call: Any | None = None

    @property
    def active(self) -> bool:
        return self.phase is RuntimePhase.ACTIVE

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

    def bind_endpoint_registry(self, registry: Any | None) -> None:
        """Bind the phone directory used by session-owned busy claims."""

        if registry is self._endpoint_registry:
            return
        if any(session.endpoint_claims for session in self.calls.values()):
            raise RuntimeError("cannot replace endpoint registry while calls are active")
        self._endpoint_registry = registry

    def endpoint_claims_snapshot(self) -> dict[str, dict[str, str]]:
        """Return a detached compatibility projection of session claims."""

        return {
            call_id: dict(session.endpoint_claims)
            for call_id, session in self.calls.items()
            if session.endpoint_claims
        }

    def relays_snapshot(self) -> dict[str, Any]:
        """Return the live relay index derived from session-owned resources."""

        relays: dict[str, Any] = {}
        for session in self.calls.values():
            for resource in session.resources:
                if resource.name.startswith("relay:"):
                    relays[resource.name.removeprefix("relay:")] = resource.value
        return relays

    def resources_snapshot(self, prefix: str) -> dict[str, Any]:
        """Return one named class of resources from authoritative sessions."""

        marker = f"{str(prefix or '').strip()}:"
        if marker == ":":
            return {}
        return {
            resource.name.removeprefix(marker): resource.value
            for session in self.calls.values()
            for resource in session.resources
            if resource.name.startswith(marker)
        }

    def sip_clients_snapshot(self) -> dict[str, Any]:
        """Return dialogs indexed by their SIP Call-ID from owned legs."""

        clients: dict[str, Any] = {}
        for session in self.calls.values():
            for leg in session.legs.values():
                if leg.dialog is not None:
                    clients[leg.sip_call_id or leg.leg_id] = leg.dialog
        return clients

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

        return {
            call_id: dest_call_id
            for call_id, session in self.calls.items()
            if (dest_call_id := str(session.metadata.get("bridge_dest_call_id") or ""))
        }

    def artifacts_snapshot(self, name: str) -> dict[str, Any]:
        """Return one class of generation-owned call artifacts."""

        return {
            call_id: value
            for call_id, session in self.calls.items()
            if (value := session.artifacts.get(name)) is not None
        }

    def set_artifact(self, call_id: str, name: str, value: Any) -> bool:
        """Attach an artifact to the current call generation."""

        session = self.get_session(call_id)
        if session is None or not session.live:
            return False
        session.artifacts[name] = value
        return True

    def take_artifact(self, call_id: str, name: str) -> Any | None:
        """Detach an artifact for explicit completion or cancellation."""

        session = self.get_session(call_id)
        if session is None or session.phase is SessionPhase.TERMINATED:
            return None
        return session.artifacts.pop(name, None)

    def set_bridge_link(self, source_call_id: str, dest_call_id: str) -> bool:
        """Attach one destination dialog identity to its source session."""

        session = self.get_session(source_call_id)
        clean_dest_call_id = str(dest_call_id or "").strip()
        if session is None or not session.live or not clean_dest_call_id:
            return False
        session.update_metadata(bridge_dest_call_id=clean_dest_call_id)
        return True

    def forget_bridge_link(self, source_call_id: str) -> str:
        """Remove and return one destination link without ending the session."""

        session = self.get_session(source_call_id)
        if session is None or session.phase is SessionPhase.TERMINATED:
            return ""
        dest_call_id = str(session.metadata.pop("bridge_dest_call_id", "") or "")
        if dest_call_id:
            self._publish(session)
        return dest_call_id

    def attach_relay(self, call_id: str, relay: Any) -> bool:
        """Make one RTP relay a media resource of its call session."""

        clean_call_id = str(call_id or "").strip()

        async def _stop_relay(_reason: str) -> None:
            await relay.stop()

        return self.own_resource(
            clean_call_id,
            f"relay:{clean_call_id}",
            relay,
            _stop_relay,
            stage=CleanupStage.MEDIA,
        )

    def take_relay(self, call_id: str) -> Any | None:
        """Transfer one relay out of the authoritative cleanup barrier."""

        clean_call_id = str(call_id or "").strip()
        session = self.get_session(clean_call_id)
        if session is None or session.phase is SessionPhase.TERMINATED:
            return None
        resource_name = f"relay:{clean_call_id}"
        resource = next(
            (item for item in session.resources if item.name == resource_name),
            None,
        )
        if resource is None:
            return None
        session.release_resource(resource_name, value=resource.value)
        return resource.value

    def claim_endpoint(
        self,
        call_id: str,
        endpoint_id: str,
        *,
        role: str = "endpoint",
        adopt_transport: bool = False,
        generation: int | None = None,
    ) -> bool:
        """Reserve one phone as a resource of the authoritative session."""

        registry = self._endpoint_registry
        session = self.get_session(call_id, generation=generation)
        clean_endpoint_id = str(endpoint_id or "").strip()
        if registry is None or session is None or not clean_endpoint_id:
            return False
        if adopt_transport and hasattr(registry, "adopt_transport_call"):
            registry.adopt_transport_call(clean_endpoint_id, session.call_id)
        else:
            registry.claim_call(clean_endpoint_id, session.call_id)
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
                    registry.release_call(clean_endpoint_id, session.call_id)

            session.add_resource(
                resource_name,
                registry,
                _release_claim,
                stage=CleanupStage.RESERVATION,
            )
        if previous != session.endpoint_claims[clean_endpoint_id]:
            self._publish(session)
        return True

    def release_endpoint_claim(
        self,
        call_id: str,
        endpoint_id: str,
        *,
        generation: int | None = None,
    ) -> bool:
        """Release one losing phone without terminating the whole call."""

        session = self.get_session(call_id, generation=generation)
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
        released = False
        if registry is not None and (
            not hasattr(registry, "get") or registry.get(clean_endpoint_id) is not None
        ):
            released = bool(registry.release_call(clean_endpoint_id, session.call_id))
        self._publish(session)
        return released

    def release_endpoint_claims(
        self,
        call_id: str,
        *,
        generation: int | None = None,
    ) -> None:
        """Release every phone claim owned by one live call."""

        session = self.get_session(call_id, generation=generation)
        if session is None:
            return
        for endpoint_id in tuple(session.endpoint_claims):
            self.release_endpoint_claim(
                session.call_id,
                endpoint_id,
                generation=session.generation,
            )

    def activate(self) -> None:
        """Make this owner authoritative without starting any I/O itself."""

        if self.phase is not RuntimePhase.DARK:
            raise RuntimeError(f"cannot activate PBX runtime from {self.phase.value}")
        self.phase = RuntimePhase.ACTIVE

    def _snapshot(self, session: EndpointCallSession) -> CallProjectionSnapshot:
        return CallProjectionSnapshot(
            call_id=session.call_id,
            generation=session.generation,
            phase=session.phase,
            terminal_reason=session.terminal_reason,
            leg_ids=tuple(session.legs),
            metadata=dict(session.metadata),
        )

    def _publish(self, session: EndpointCallSession) -> None:
        if self._projection is not None:
            try:
                self._projection.publish(self._snapshot(session))
            except Exception:
                _LOGGER.exception(
                    "PBX call projection publish failed call_id=%s generation=%s",
                    session.call_id,
                    session.generation,
                )

    def _on_terminated(
        self,
        session: EndpointCallSession,
        _result: SessionTerminationResult,
    ) -> None:
        if self.calls.get(session.call_id) is not session:
            return
        snapshot = self._snapshot(session)
        self.calls.pop(session.call_id, None)
        if self._projection is not None:
            try:
                self._projection.remove(snapshot)
            except Exception:
                _LOGGER.exception(
                    "PBX call projection removal failed call_id=%s generation=%s",
                    session.call_id,
                    session.generation,
                )

    def create_session(
        self,
        call_id: str,
        *,
        phase: SessionPhase = SessionPhase.NEW,
        **metadata: Any,
    ) -> EndpointCallSession:
        """Create one unique live generation and publish its initial state."""

        if self.phase is not RuntimePhase.ACTIVE:
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
            on_changed=self._publish,
            on_terminated=self._on_terminated,
        )
        session.metadata.update(metadata)
        self.calls[clean_call_id] = session
        self._publish(session)
        return session

    def ensure_session(
        self,
        call_id: str,
        **metadata: Any,
    ) -> EndpointCallSession:
        """Return the live authoritative generation, creating it if needed."""

        session = self.get_session(call_id)
        if session is None:
            return self.create_session(call_id, **metadata)
        if metadata:
            session.update_metadata(**metadata)
        return session

    def observe_call(
        self,
        call_id: str,
        *,
        state: str = "",
        generation: int | None = None,
        **metadata: Any,
    ) -> bool:
        """Project a legacy/public state mutation onto the owning session."""

        session = self.get_session(call_id, generation=generation)
        if session is None or not session.live:
            return False
        next_phase = _PUBLIC_PHASES.get(str(state or "").strip())
        changed = False
        if next_phase is not None and session.phase is not next_phase:
            if next_phase in _OBSERVED_PHASE_TRANSITIONS.get(
                session.phase, frozenset()
            ):
                session.phase = next_phase
                changed = True
            else:
                _LOGGER.debug(
                    "Ignoring stale PBX call phase call_id=%s generation=%s "
                    "current=%s observed=%s",
                    session.call_id,
                    session.generation,
                    session.phase.value,
                    next_phase.value,
                )
        clean_metadata = {
            key: value for key, value in metadata.items() if value not in (None, "")
        }
        if any(session.metadata.get(key) != value for key, value in clean_metadata.items()):
            session.metadata.update(clean_metadata)
            changed = True
        if changed:
            self._publish(session)
        return True

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
                    closer=closer,
                )
            )
        next_phase = _PUBLIC_LEG_PHASES.get(str(state or "").strip())
        if next_phase is not None:
            leg.phase = next_phase
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
        self._publish(session)
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
        return session.release_leg(leg_id, dialog=dialog) is not None

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
        reason: str,
        *,
        generation: int | None = None,
    ) -> asyncio.Task[SessionTerminationResult] | None:
        """Make a synchronous terminal decision own one cleanup barrier."""

        session = self.get_session(call_id, generation=generation)
        if session is None:
            return None
        return session.start_termination(reason)

    def claim_termination(
        self,
        call_id: str,
        reason: str,
        *,
        generation: int | None = None,
    ) -> bool:
        """Claim one terminal transition before legacy adapter handoff."""

        session = self.get_session(call_id, generation=generation)
        if session is None:
            return False
        return session.claim_termination(reason)

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

    async def terminate_session(
        self,
        call_id: str,
        reason: str,
        *,
        generation: int | None = None,
    ) -> SessionTerminationResult | None:
        session = self.get_session(call_id, generation=generation)
        if session is None:
            return None
        return await session.terminate(reason)

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
                *(session.terminate("runtime_shutdown") for session in sessions),
                return_exceptions=True,
            )
        ordered_names = [
            name for name in self._COMPONENT_STOP_ORDER if name in self._components
        ]
        ordered_names.extend(
            name for name in reversed(tuple(self._components)) if name not in ordered_names
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
