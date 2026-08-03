"""Authoritative HA-side SIP call session registry."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import time
from typing import Any, Literal, Protocol

from .endpoint_session import CleanupStage
from .automation_routing import CALL_EVENT_SCHEMA_VERSION
from .media_reservation import release_media_reservation
from .session_cleanup import async_cleanup_sip_runtime


LegRole = Literal["caller", "callee", "trunk", "ha_softphone", "esp", "softphone", "router", "assist", "local_phone"]
CallOwner = Literal["", "ha_softphone", "router", "bridge", "assist", "local_bridge", "terminal"]
TERMINAL_STATES = {
    "idle",
    "busy",
    "declined",
    "cancelled",
    "media_incompatible",
    "transport_unreachable",
    "auth_required_unsupported",
    "protocol_error",
    "error",
}
MAX_TERMINATED_CALL_IDS = 512
MAX_TERMINAL_SUMMARY_IDS = 512


def _owner_observation_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Exclude fields computed by the authoritative PBX projection itself."""

    return {key: value for key, value in metadata.items() if key != "pbx_phase"}


class CallSessionOwner(Protocol):
    """Minimal synchronous boundary implemented by ``SipEndpointRuntime``."""

    def ensure_session(self, call_id: str, **metadata: Any) -> Any: ...

    def observe_call(
        self,
        call_id: str,
        *,
        state: str = "",
        generation: int | None = None,
        **metadata: Any,
    ) -> bool: ...

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
        closer: Any | None = None,
        generation: int | None = None,
    ) -> bool: ...

    def release_leg(
        self,
        call_id: str,
        leg_id: str,
        *,
        dialog: Any | None = None,
        generation: int | None = None,
    ) -> bool: ...

    def own_resource(
        self,
        call_id: str,
        name: str,
        value: Any,
        closer: Any,
        *,
        stage: CleanupStage = CleanupStage.RESERVATION,
        generation: int | None = None,
    ) -> bool: ...

    def release_resource(
        self,
        call_id: str,
        name: str,
        *,
        value: Any | None = None,
        generation: int | None = None,
    ) -> bool: ...

    def own_task(
        self,
        call_id: str,
        task: Any,
        *,
        generation: int | None = None,
    ) -> bool: ...

    def release_task(
        self,
        call_id: str,
        task: Any,
        *,
        generation: int | None = None,
    ) -> bool: ...

    def request_termination(
        self,
        call_id: str,
        reason: str,
        *,
        generation: int | None = None,
    ) -> Any: ...


@dataclass(slots=True)
class CallLeg:
    leg_id: str
    role: LegRole
    sip_call_id: str = ""
    state: str = "idle"
    local_uri: str = ""
    remote_uri: str = ""
    media: Any | None = None


@dataclass(slots=True)
class CallSession:
    """Observable compatibility view of one authoritative PBX session.

    ``generation`` distinguishes a new call lifetime from late callbacks that
    still carry the same external Call-ID.  ``revision`` orders projections
    within that lifetime.  Neither field is a SIP CSeq: SIP transaction and
    dialog ordering remain owned by the signaling layer.
    """

    id: str
    generation: int = 0
    revision: int = 0
    state: str = "new"
    owner: CallOwner = ""
    outcome: str = ""
    caller: str = ""
    callee: str = ""
    route_kind: str = ""
    terminal_reason: str = ""
    legs: dict[str, CallLeg] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CallEventContext:
    """Small bounded automation-facing history for one logical call."""

    sequence: int = 0
    state: str = ""
    previous_state: str = ""
    route_history: list[dict[str, Any]] = field(default_factory=list)
    connected_at: float = 0.0
    duration_seconds: int | None = None


class CallRegistry:
    """Compatibility index shared by softphone, router and trunk adapters.

    Once ``bind_session_owner`` installs the PBX runtime, that owner is the
    lifecycle authority.  This registry mirrors its snapshots and indexes the
    concrete clients, relays and reservations still needed by older adapters;
    callers must not build a second state machine from these dictionaries.
    Generation guards and ``begin_termination`` prevent late transport, media
    or UI callbacks from resurrecting an already terminated call.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, CallSession] = {}
        self.leg_index: dict[str, str] = {}
        self.pending_routes: dict[str, dict[str, Any]] = {}
        self.pending_invites: dict[str, Any] = {}
        self.preanswered: dict[str, dict[str, Any]] = {}
        self.softphone_media: dict[str, dict[str, Any]] = {}
        self.video_parameter_sets: dict[str, tuple[bytes, ...]] = {}
        self.sip_clients: dict[str, Any] = {}
        self.client_watchers: dict[str, Any] = {}
        self.relays: dict[str, Any] = {}
        self.bridge_clients: dict[str, str] = {}
        self.event_contexts: dict[str, CallEventContext] = {}
        self.terminal_summary_ids: OrderedDict[str, None] = OrderedDict()
        self.endpoint_claims: dict[str, dict[str, str]] = {}
        self.terminated_call_ids: OrderedDict[str, int] = OrderedDict()
        self._generation = 0
        self._endpoint_registry: Any | None = None
        self._session_owner: CallSessionOwner | None = None

    def bind_session_owner(self, owner: CallSessionOwner | None) -> None:
        """Bind the authoritative PBX session owner at listener cutover."""

        if owner is self._session_owner:
            return
        if self._session_owner is not None and owner is not None and self.sessions:
            raise RuntimeError("cannot replace the PBX owner while calls are active")
        self._session_owner = owner
        if owner is None:
            return
        for session in tuple(self.sessions.values()):
            authoritative = owner.ensure_session(
                session.id,
                caller=session.caller,
                callee=session.callee,
                route_kind=session.route_kind,
            )
            session.generation = int(authoritative.generation)
            observation = _owner_observation_metadata(session.metadata)
            observation.update(
                owner=session.owner,
                caller=session.caller,
                callee=session.callee,
                route_kind=session.route_kind,
            )
            owner.observe_call(
                session.id,
                state=session.state,
                generation=session.generation,
                **observation,
            )

    def publish(self, snapshot: Any) -> None:
        """Receive an observable snapshot without taking lifecycle ownership."""

        call_id = str(snapshot.call_id or "").strip()
        if not call_id:
            return
        session = self.sessions.get(call_id)
        if session is None:
            session = CallSession(id=call_id, generation=int(snapshot.generation))
            self.sessions[call_id] = session
        elif session.generation != int(snapshot.generation):
            return
        self._generation = max(self._generation, int(snapshot.generation))
        phase = str(getattr(snapshot.phase, "value", snapshot.phase) or "")
        changed = session.metadata.get("pbx_phase") != phase
        if phase:
            session.metadata["pbx_phase"] = phase
        for key, value in dict(snapshot.metadata).items():
            if key == "pbx_phase":
                continue
            if session.metadata.get(key) != value:
                session.metadata[key] = value
                changed = True
        terminal_reason = str(snapshot.terminal_reason or "")
        if terminal_reason and session.terminal_reason != terminal_reason:
            session.terminal_reason = terminal_reason
            changed = True
        if changed:
            session.revision += 1

    def remove(self, snapshot: Any) -> None:
        """Record owner completion; legacy indexes are cleared by their caller.

        During the atomic cutover synchronous SIP callbacks still consume the
        observable record until ``finish_and_pop`` returns.  Owner completion
        may race that callback, so this projection hook must never delete it.
        """

        session = self.sessions.get(str(snapshot.call_id or "").strip())
        if session is None or session.generation != int(snapshot.generation):
            return
        session.metadata["pbx_phase"] = "terminated"
        if snapshot.terminal_reason:
            session.terminal_reason = str(snapshot.terminal_reason)

    def _remember_terminated(
        self,
        *call_ids: str,
        generation: int = 0,
    ) -> None:
        """Remember terminal calls with deterministic oldest-first eviction."""

        for call_id in call_ids:
            clean_call_id = str(call_id or "").strip()
            if not clean_call_id:
                continue
            self.terminated_call_ids[clean_call_id] = int(generation)
            self.terminated_call_ids.move_to_end(clean_call_id)
        while len(self.terminated_call_ids) > MAX_TERMINATED_CALL_IDS:
            self.terminated_call_ids.popitem(last=False)

    def is_terminated(
        self,
        call_id: str,
        *,
        generation: int | None = None,
    ) -> bool:
        """Return whether a call generation has already reached terminal state."""

        call_id = str(call_id or "").strip()
        session_id = self.resolve_session_id(call_id)
        for candidate in (call_id, session_id):
            if candidate not in self.terminated_call_ids:
                continue
            terminal_generation = self.terminated_call_ids[candidate]
            if generation is None or terminal_generation == int(generation):
                return True
        return False

    def begin_termination(self, call_id: str) -> bool:
        """Atomically claim teardown ownership for a call or one of its legs.

        SIP transports, client watchers and local UI actions may all observe
        the same terminal event.  Exactly one of them may perform teardown;
        later notifications are acknowledgements, not new state transitions.
        """
        call_id = str(call_id or "").strip()
        if not call_id:
            return False
        session_id = self.resolve_session_id(call_id)
        if self.is_terminated(call_id):
            return False
        session = self.sessions.get(session_id)
        self._remember_terminated(
            call_id,
            session_id,
            generation=session.generation if session is not None else 0,
        )
        if session is not None and session.metadata.get("pbx_phase") != "terminating":
            # The terminal decision is synchronous even when legacy adapters
            # still detach their concrete resources before starting the
            # authoritative cleanup barrier. Events emitted in that interval
            # must not advertise the previous ringing/established phase.
            session.metadata["pbx_phase"] = "terminating"
            session.revision += 1
        return True

    def bind_endpoint_registry(self, registry: Any | None) -> None:
        """Bind the logical endpoint registry used for atomic busy claims.

        The call registry deliberately depends only on the tiny ``claim_call`` /
        ``release_call`` protocol.  This keeps the SIP session model reusable in
        pure tests while making teardown the single owner of endpoint release.
        """
        if registry is self._endpoint_registry:
            return
        if self.endpoint_claims:
            self._release_all_endpoint_claims()
        self._endpoint_registry = registry

    def claim_endpoint(
        self,
        call_id: str,
        endpoint_id: str,
        *,
        role: str = "endpoint",
        adopt_transport: bool = False,
    ) -> bool:
        """Atomically reserve an endpoint for this logical call.

        Returns ``False`` only when no endpoint registry is configured (the
        supported YAML-only compatibility mode).  Busy errors from the bound
        registry remain authoritative and are intentionally propagated.
        ``adopt_transport`` may replace only the provisional ``physical:``
        claim emitted by an ESP state entity; it can never steal a real call.
        """
        registry = self._endpoint_registry
        endpoint_id = str(endpoint_id or "").strip()
        if registry is None or not endpoint_id:
            return False
        session_id = self.resolve_session_id(str(call_id or "").strip())
        if not session_id:
            raise ValueError("call_id must not be empty")
        if adopt_transport and hasattr(registry, "adopt_transport_call"):
            registry.adopt_transport_call(endpoint_id, session_id)
        else:
            registry.claim_call(endpoint_id, session_id)
        claims = self.endpoint_claims.setdefault(session_id, {})
        previous = claims.get(endpoint_id)
        claims[endpoint_id] = str(role or "endpoint")
        session = self.sessions.get(session_id)
        if session is not None and self._session_owner is not None:

            def _release_claim(_reason: str) -> None:
                self.release_endpoint_claim(session_id, endpoint_id)

            self._session_owner.own_resource(
                session_id,
                f"endpoint_claim:{endpoint_id}",
                self,
                _release_claim,
                stage=CleanupStage.RESERVATION,
                generation=session.generation,
            )
        if previous != claims[endpoint_id]:
            if session is not None:
                session.revision += 1
        return True

    def release_endpoint_claims(self, call_id: str) -> None:
        """Release every logical endpoint owned by a call or one of its legs."""
        session_id = self.resolve_session_id(str(call_id or "").strip())
        claims = self.endpoint_claims.get(session_id, {})
        registry = self._endpoint_registry
        if registry is None:
            self.endpoint_claims.pop(session_id, None)
            return
        for endpoint_id in tuple(claims):
            # Config removal may have already detached the endpoint from a
            # third-party registry implementation. Teardown must continue and
            # release every remaining participant rather than leak a session.
            if not hasattr(registry, "get") or registry.get(endpoint_id) is not None:
                registry.release_call(endpoint_id, session_id)
            claims.pop(endpoint_id, None)
        self.endpoint_claims.pop(session_id, None)

    def release_endpoint_claim(self, call_id: str, endpoint_id: str) -> bool:
        """Release one losing/finished endpoint leg without ending the call."""
        session_id = self.resolve_session_id(str(call_id or "").strip())
        endpoint_id = str(endpoint_id or "").strip()
        claims = self.endpoint_claims.get(session_id)
        if not endpoint_id or claims is None or endpoint_id not in claims:
            return False
        registry = self._endpoint_registry
        released = False
        if registry is not None and (
            not hasattr(registry, "get") or registry.get(endpoint_id) is not None
        ):
            released = bool(registry.release_call(endpoint_id, session_id))
        claims.pop(endpoint_id, None)
        if not claims:
            self.endpoint_claims.pop(session_id, None)
        session = self.sessions.get(session_id)
        if session is not None:
            session.revision += 1
            if self._session_owner is not None:
                self._session_owner.release_resource(
                    session_id,
                    f"endpoint_claim:{endpoint_id}",
                    value=self,
                    generation=session.generation,
                )
        return released

    def _release_all_endpoint_claims(self) -> None:
        for session_id in tuple(self.endpoint_claims):
            self.release_endpoint_claims(session_id)

    def event_fields(self, call_id: str, state: str) -> dict[str, Any]:
        """Return stable automation fields, advancing only on a state change."""
        call_id = str(call_id or "").strip()
        state = str(state or "").strip()
        if not call_id:
            return {
                "schema_version": CALL_EVENT_SCHEMA_VERSION,
                "sequence": 0,
                "revision": 0,
                "generation": 0,
                "pbx_phase": "",
                "owner": "",
                "previous_state": "",
                "route_history": [],
            }
        call_id = self.resolve_session_id(call_id)
        context = self.event_contexts.get(call_id)
        if context is None:
            if len(self.event_contexts) >= 256:
                self.event_contexts.pop(next(iter(self.event_contexts)))
            context = CallEventContext()
            self.event_contexts[call_id] = context
        starts_new_lifecycle = bool(
            state
            and state not in TERMINAL_STATES
            and (not context.state or context.state in TERMINAL_STATES)
        )
        if starts_new_lifecycle:
            context.connected_at = 0.0
            context.duration_seconds = None
            self.terminal_summary_ids.pop(call_id, None)
        if state and state != context.state:
            context.previous_state = context.state
            context.state = state
            context.sequence += 1
        if state == "in_call" and not context.connected_at:
            context.connected_at = time.monotonic()
        session = self.sessions.get(call_id)
        fields = {
            "schema_version": CALL_EVENT_SCHEMA_VERSION,
            "sequence": context.sequence,
            "revision": session.revision if session is not None else 0,
            "generation": session.generation if session is not None else 0,
            "pbx_phase": (
                str(session.metadata.get("pbx_phase") or session.state)
                if session is not None
                else ""
            ),
            "owner": session.owner if session is not None else "",
            "previous_state": context.previous_state,
            "route_history": [dict(item) for item in context.route_history],
        }
        if state in TERMINAL_STATES and context.connected_at:
            if context.duration_seconds is None:
                context.duration_seconds = max(
                    0,
                    round(time.monotonic() - context.connected_at),
                )
            fields["duration_seconds"] = context.duration_seconds
        if session is None:
            return fields

        # Event entities must be attributed from call ownership, never by
        # resolving a caller-controlled display name. Preserve the explicit
        # source/destination metadata and include every atomically claimed
        # phone for ring groups and conferences.
        identity_keys = (
            "endpoint_id",
            "source_endpoint_id",
            "dest_endpoint_id",
            "target_endpoint_id",
            "device_id",
            "source_device_id",
            "dest_device_id",
            "target_device_id",
        )
        fields.update(
            {
                key: value
                for key in identity_keys
                if (value := session.metadata.get(key)) not in (None, "")
            }
        )
        fields.update(
            {
                key: value
                for key in ("ingress", "origin")
                if (value := session.metadata.get(key)) not in (None, "")
            }
        )
        participant_endpoint_ids = {
            str(value).strip()
            for key in (
                "endpoint_id",
                "source_endpoint_id",
                "dest_endpoint_id",
                "target_endpoint_id",
            )
            if (value := session.metadata.get(key)) not in (None, "")
        }
        participant_endpoint_ids.update(self.endpoint_claims.get(call_id, {}))
        participant_endpoint_ids.discard("")
        if participant_endpoint_ids:
            fields["participant_endpoint_ids"] = sorted(
                participant_endpoint_ids
            )
        return fields

    def claim_terminal_summary(self, call_id: str) -> bool:
        """Claim the single Logbook summary emitted for one logical call."""

        call_id = self.resolve_session_id(str(call_id or "").strip())
        if not call_id or call_id in self.terminal_summary_ids:
            return False
        self.terminal_summary_ids[call_id] = None
        self.terminal_summary_ids.move_to_end(call_id)
        while len(self.terminal_summary_ids) > MAX_TERMINAL_SUMMARY_IDS:
            self.terminal_summary_ids.popitem(last=False)
        return True

    def event_context(self, call_id: str) -> CallEventContext | None:
        """Return the current automation event context for a call or leg."""
        return self.event_contexts.get(self.resolve_session_id(str(call_id or "").strip()))

    def record_route(
        self,
        call_id: str,
        *,
        action: str,
        destination: str = "",
        source: str = "automation",
    ) -> list[dict[str, Any]]:
        """Append one bounded routing decision to the call history."""
        call_id = self.resolve_session_id(str(call_id or "").strip())
        context = self.event_contexts.get(call_id)
        if context is None:
            self.event_fields(call_id, "")
            context = self.event_contexts[call_id]
        context.route_history.append(
            {
                "action": str(action or "").strip(),
                "destination": str(destination or "").strip(),
                "source": str(source or "automation").strip(),
            }
        )
        del context.route_history[:-8]
        session = self.sessions.get(call_id)
        if session is not None:
            session.revision += 1
        return [dict(item) for item in context.route_history]

    def upsert(
        self,
        call_id: str,
        *,
        state: str,
        caller: str = "",
        callee: str = "",
        route_kind: str = "",
        ingress: str = "",
        origin: str = "",
        terminal_reason: str = "",
        owner: CallOwner = "",
        **metadata: Any,
    ) -> CallSession:
        authoritative = None
        ownership_metadata = dict(metadata)
        if ingress:
            ownership_metadata["ingress"] = ingress
        if origin:
            ownership_metadata["origin"] = origin
        if self._session_owner is not None:
            authoritative = self._session_owner.ensure_session(
                call_id,
                caller=caller,
                callee=callee,
                route_kind=route_kind,
                **ownership_metadata,
            )
        session = self.sessions.get(call_id)
        if session is None:
            if authoritative is None:
                self._generation += 1
                generation = self._generation
            else:
                generation = int(authoritative.generation)
                self._generation = max(self._generation, generation)
            session = CallSession(id=call_id, generation=generation)
            self.sessions[call_id] = session
        changed = False
        for attribute, value in (
            ("state", state),
            ("owner", owner),
            ("caller", caller),
            ("callee", callee),
            ("route_kind", route_kind),
            ("terminal_reason", terminal_reason),
        ):
            if value and getattr(session, attribute) != value:
                setattr(session, attribute, value)
                changed = True
        clean_metadata = {
            key: value
            for key, value in ownership_metadata.items()
            if value not in (None, "")
        }
        if any(session.metadata.get(key) != value for key, value in clean_metadata.items()):
            session.metadata.update(clean_metadata)
            changed = True
        if changed:
            session.revision += 1
        if self._session_owner is not None:
            observation = _owner_observation_metadata(session.metadata)
            observation.update(
                owner=session.owner,
                caller=session.caller,
                callee=session.callee,
                route_kind=session.route_kind,
                terminal_reason=session.terminal_reason,
            )
            self._session_owner.observe_call(
                call_id,
                state=session.state,
                generation=session.generation,
                **observation,
            )
        return session

    def transition(
        self,
        call_id: str,
        *,
        state: str = "",
        owner: CallOwner | None = None,
        outcome: str | None = None,
        caller: str = "",
        callee: str = "",
        route_kind: str = "",
        expected_revision: int | None = None,
        expected_generation: int | None = None,
        expected_owner: CallOwner | None = None,
        **metadata: Any,
    ) -> CallSession | None:
        """Apply one guarded control mutation and advance its revision once."""
        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if expected_revision is not None and session.revision != int(expected_revision):
            return None
        if expected_generation is not None and session.generation != int(expected_generation):
            return None
        if expected_owner is not None and session.owner != expected_owner:
            return None
        if session.owner == "terminal" or session.state in TERMINAL_STATES:
            return None
        if state:
            session.state = state
        if owner is not None:
            session.owner = owner
        if outcome is not None:
            session.outcome = outcome
        if caller:
            session.caller = caller
        if callee:
            session.callee = callee
        if route_kind:
            session.route_kind = route_kind
        session.metadata.update(
            {key: value for key, value in metadata.items() if value not in (None, "")}
        )
        session.revision += 1
        if self._session_owner is not None:
            observation = _owner_observation_metadata(session.metadata)
            observation.update(
                owner=session.owner,
                outcome=session.outcome,
                caller=session.caller,
                callee=session.callee,
                route_kind=session.route_kind,
            )
            self._session_owner.observe_call(
                session_id,
                state=session.state,
                generation=session.generation,
                **observation,
            )
        return session

    def is_current(
        self,
        call_id: str,
        *,
        revision: int,
        generation: int | None = None,
        owner: CallOwner | None = None,
    ) -> bool:
        """Return whether an asynchronous callback still owns this revision."""
        session = self.sessions.get(self.resolve_session_id(str(call_id or "").strip()))
        return bool(
            session is not None
            and session.revision == int(revision)
            and (generation is None or session.generation == int(generation))
            and (owner is None or session.owner == owner)
        )

    def is_generation_current(self, call_id: str, generation: int) -> bool:
        """Return whether an async operation still belongs to a live call."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        return bool(
            session is not None
            and session.generation == int(generation)
            and session.owner != "terminal"
            and session.state not in TERMINAL_STATES
            and not self.is_terminated(call_id, generation=generation)
        )

    def add_leg(
        self,
        call_id: str,
        leg_id: str,
        *,
        role: LegRole,
        state: str = "",
        sip_call_id: str = "",
        **metadata: Any,
    ) -> CallLeg:
        session = self.sessions.get(call_id)
        if session is None:
            session = self.upsert(call_id, state=state or "active", **metadata)
        else:
            clean_metadata = {
                key: value
                for key, value in metadata.items()
                if value not in (None, "")
            }
            if any(
                session.metadata.get(key) != value
                for key, value in clean_metadata.items()
            ):
                session.metadata.update(clean_metadata)
                session.revision += 1
        leg = session.legs.get(leg_id)
        changed = False
        if leg is None:
            leg = CallLeg(leg_id=leg_id, role=role, sip_call_id=sip_call_id or leg_id)
            session.legs[leg_id] = leg
            changed = True
        next_state = state or leg.state
        next_sip_call_id = sip_call_id or leg.sip_call_id
        if leg.role != role or leg.state != next_state or leg.sip_call_id != next_sip_call_id:
            changed = True
        leg.role = role
        leg.state = next_state
        leg.sip_call_id = next_sip_call_id
        self.leg_index[leg_id] = call_id
        if changed:
            session.revision += 1
        if self._session_owner is not None:
            self._session_owner.observe_leg(
                call_id,
                leg_id,
                role=role,
                state=leg.state,
                sip_call_id=leg.sip_call_id,
                endpoint_id=str(metadata.get("endpoint_id") or ""),
                generation=session.generation,
            )
        return leg

    def remove_leg(self, call_id: str, leg_id: str) -> CallLeg | None:
        """Remove one destination leg without ending its source call."""
        session_id = self.resolve_session_id(call_id)
        session = self.sessions.get(session_id)
        if session is None:
            return None
        leg = session.legs.pop(leg_id, None)
        self.leg_index.pop(leg_id, None)
        if leg is not None:
            session.revision += 1
            if self._session_owner is not None:
                self._session_owner.release_leg(
                    session_id,
                    leg_id,
                    generation=session.generation,
                )
        return leg

    def attach_sip_client(
        self,
        source_call_id: str,
        dest_call_id: str,
        client: Any,
        *,
        role: LegRole = "callee",
        state: str = "",
    ) -> None:
        """Index a SIP client while its session leg owns cleanup."""

        session_id = self.resolve_session_id(str(source_call_id or "").strip())
        session = self.sessions.get(session_id)
        self.sip_clients[dest_call_id] = client
        if session is None or self._session_owner is None:
            return

        async def _close_client(_reason: str) -> None:
            if self.sip_clients.get(dest_call_id) is client:
                self.sip_clients.pop(dest_call_id, None)
            await async_cleanup_sip_runtime(client=client, terminate_client=True)

        self._session_owner.observe_leg(
            session_id,
            dest_call_id,
            role=role,
            state=state,
            sip_call_id=dest_call_id,
            dialog=client,
            closer=_close_client,
            generation=session.generation,
        )

    def take_sip_client(self, call_id: str) -> Any | None:
        """Transfer a SIP client to an explicit cleanup caller."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        client = self.sip_clients.pop(call_id, None)
        if client is not None and session is not None and self._session_owner is not None:
            self._session_owner.release_leg(
                session_id,
                call_id,
                dialog=client,
                generation=session.generation,
            )
        return client

    def attach_relay(self, call_id: str, relay: Any) -> None:
        """Index a media relay while the call session owns its stop barrier."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        self.relays[call_id] = relay
        if session is None or self._session_owner is None:
            return
        resource_name = f"relay:{call_id}"

        async def _stop_relay(_reason: str) -> None:
            if self.relays.get(call_id) is relay:
                self.relays.pop(call_id, None)
            await relay.stop()

        self._session_owner.own_resource(
            session_id,
            resource_name,
            relay,
            _stop_relay,
            stage=CleanupStage.MEDIA,
            generation=session.generation,
        )

    def take_relay(self, call_id: str) -> Any | None:
        """Transfer a relay to an explicit cleanup caller."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        relay = self.relays.pop(call_id, None)
        if relay is not None and session is not None and self._session_owner is not None:
            self._session_owner.release_resource(
                session_id,
                f"relay:{call_id}",
                value=relay,
                generation=session.generation,
            )
        return relay

    def attach_client_watcher(self, call_id: str, task: Any) -> None:
        """Index a watcher while the owning session controls cancellation."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        self.client_watchers[call_id] = task
        if session is not None and self._session_owner is not None:
            self._session_owner.own_task(
                session_id,
                task,
                generation=session.generation,
            )

        def _forget(completed: Any) -> None:
            if self.client_watchers.get(call_id) is completed:
                self.client_watchers.pop(call_id, None)

        task.add_done_callback(_forget)

    def take_client_watcher(self, call_id: str) -> Any | None:
        """Transfer a watcher to an explicit cleanup caller."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        task = self.client_watchers.pop(call_id, None)
        if task is not None and session is not None and self._session_owner is not None:
            self._session_owner.release_task(
                session_id,
                task,
                generation=session.generation,
            )
        return task

    def attach_media(
        self,
        call_id: str,
        media: dict[str, Any],
        *,
        provisional: bool = False,
    ) -> None:
        """Index reserved media while the call session owns its release."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        index = self.preanswered if provisional else self.softphone_media
        index[call_id] = media
        if session is None or self._session_owner is None:
            return
        prefix = "preanswered" if provisional else "softphone_media"
        resource_name = f"{prefix}:{call_id}"

        def _release_media(_reason: str) -> None:
            if index.get(call_id) is media:
                index.pop(call_id, None)
            release_media_reservation(media)

        self._session_owner.own_resource(
            session_id,
            resource_name,
            media,
            _release_media,
            stage=CleanupStage.RESERVATION,
            generation=session.generation,
        )

    def take_media(
        self,
        call_id: str,
        *,
        provisional: bool = False,
        default: Any | None = None,
    ) -> Any:
        """Transfer reserved media to another owner or cleanup caller."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        index = self.preanswered if provisional else self.softphone_media
        media = index.pop(call_id, default)
        if media is default or session is None or self._session_owner is None:
            return media
        prefix = "preanswered" if provisional else "softphone_media"
        self._session_owner.release_resource(
            session_id,
            f"{prefix}:{call_id}",
            value=media,
            generation=session.generation,
        )
        return media

    def resolve_session_id(self, call_id: str) -> str:
        return self.leg_index.get(call_id, call_id)

    def bind_controller(
        self,
        call_id: str,
        *,
        context: Any | None = None,
        user_id: str = "",
        endpoint_id: str = "",
    ) -> CallSession:
        """Bind one logical call to its initiating HA user and context.

        The user identity is deliberately sticky for the whole call.  A later
        browser reconnect may reclaim media only as that same user; it cannot
        silently transfer a microphone or camera to another authenticated HA
        session.  A local browser-to-browser call instead owns one sticky user
        per endpoint leg, allowing two tablets with different HA users to talk
        without granting either user access to the other leg. Internal
        automations still retain their original HA Context so lifecycle events
        preserve trace/parent provenance.
        """

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"unknown call_id {call_id!r}")
        requested_user_id = str(
            user_id or getattr(context, "user_id", "") or ""
        ).strip()
        requested_endpoint_id = str(endpoint_id or "").strip()
        scoped = bool(
            requested_endpoint_id and session.metadata.get("local_bridge")
        )
        if scoped:
            controllers = session.metadata.setdefault("controller_user_ids", {})
            current_user_id = str(
                controllers.get(requested_endpoint_id) or ""
            ).strip()
        else:
            current_user_id = str(
                session.metadata.get("controller_user_id") or ""
            ).strip()
        if current_user_id and requested_user_id and current_user_id != requested_user_id:
            raise ValueError(
                f"call_id {session_id}"
                + (
                    f" endpoint {requested_endpoint_id}"
                    if scoped
                    else ""
                )
                + " is already controlled by another HA user"
            )
        changed = False
        if requested_user_id and not current_user_id:
            if scoped:
                controllers[requested_endpoint_id] = requested_user_id
            else:
                session.metadata["controller_user_id"] = requested_user_id
            changed = True
        if context is not None and session.metadata.get("ha_context") is None:
            session.metadata["ha_context"] = context
            changed = True
        if changed:
            session.revision += 1
        return session

    def ha_context(self, call_id: str) -> Any | None:
        """Return the original HA Context for a call or one of its legs."""

        session = self.sessions.get(
            self.resolve_session_id(str(call_id or "").strip())
        )
        return session.metadata.get("ha_context") if session is not None else None

    def finish(self, call_id: str, *, reason: str = "", state: str = "idle") -> CallSession | None:
        session_id = self.resolve_session_id(call_id)
        session = self.sessions.get(session_id)
        if session is None:
            return None
        session.state = state
        session.terminal_reason = reason or session.terminal_reason
        session.owner = "terminal"
        session.outcome = reason or session.outcome
        session.revision += 1
        return session

    def pop(self, call_id: str) -> CallSession | None:
        call_id = str(call_id or "").strip()
        session_id = self.resolve_session_id(call_id)
        if self._session_owner is None:
            self.release_endpoint_claims(session_id)
        session = self.sessions.get(session_id)
        if session is not None and self._session_owner is not None:
            self._session_owner.request_termination(
                session_id,
                session.terminal_reason or session.outcome or "removed",
                generation=session.generation,
            )
        self.sessions.pop(session_id, None)
        self.bridge_clients.pop(session_id, None)
        event_context_ids = {call_id, session_id}
        if session is not None:
            for leg_id in list(session.legs):
                event_context_ids.add(leg_id)
                self.leg_index.pop(leg_id, None)
        self.leg_index.pop(call_id, None)
        for context_id in event_context_ids:
            self.event_contexts.pop(context_id, None)
        self.pending_invites.pop(session_id, None)
        self.video_parameter_sets.pop(session_id, None)
        self.video_parameter_sets.pop(call_id, None)
        route = self.pending_routes.pop(session_id, None)
        if route is not None:
            future = route.get("future")
            if (
                future is not None
                and hasattr(future, "done")
                and not future.done()
            ):
                future.cancel()
        return session

    def bridge_for(self, call_id: str) -> tuple[str, str]:
        source_call_id = call_id if call_id in self.bridge_clients else ""
        dest_call_id = self.bridge_clients.get(source_call_id, "") if source_call_id else ""
        if source_call_id:
            return source_call_id, dest_call_id
        for source, dest in self.bridge_clients.items():
            if dest == call_id:
                return source, dest
        return "", ""

    def detach_bridge(self, call_id: str) -> tuple[str, str, Any | None, Any | None, Any | None, bool]:
        source_call_id, dest_call_id = self.bridge_for(call_id)
        if not source_call_id:
            return "", "", None, None, None, False
        called_by_dest = call_id == dest_call_id
        self.bridge_clients.pop(source_call_id, None)
        relay = self.take_relay(source_call_id)
        client = self.take_sip_client(dest_call_id) if dest_call_id else None
        watcher = self.take_client_watcher(dest_call_id) if dest_call_id else None
        return source_call_id, dest_call_id, relay, client, watcher, called_by_dest

    def finish_and_pop(self, call_id: str, *, reason: str = "", state: str = "idle") -> CallSession | None:
        session_id = self.resolve_session_id(str(call_id or "").strip())
        if session_id:
            session = self.sessions.get(session_id)
            self._remember_terminated(
                str(call_id or "").strip(),
                session_id,
                generation=session.generation if session is not None else 0,
            )
        self.finish(call_id, reason=reason, state=state)
        return self.pop(call_id)

    def discard_bridge_session(
        self,
        source_call_id: str,
        dest_call_id: str = "",
        *,
        reason: str = "",
        state: str = "idle",
    ) -> Any | None:
        dest = dest_call_id or self.bridge_clients.get(source_call_id, "")
        self.bridge_clients.pop(source_call_id, None)
        client = self.take_sip_client(dest) if dest else None
        self.finish_and_pop(source_call_id, reason=reason, state=state)
        return client

    def detach_client(self, call_id: str) -> tuple[Any | None, Any | None]:
        client = self.take_sip_client(call_id)
        watcher = self.take_client_watcher(call_id)
        return client, watcher

    def register_bridge(
        self,
        *,
        source_call_id: str,
        dest_call_id: str,
        client: Any,
        lifecycle_task: Any | None = None,
        state: str,
        caller: str = "",
        callee: str = "",
        route_kind: str = "",
        ingress: str = "",
        origin: str = "",
        source_role: LegRole = "caller",
        dest_role: LegRole = "callee",
        source_state: str = "",
        dest_state: str = "",
        expected_generation: int | None = None,
    ) -> CallSession | None:
        """Attach one destination dialog and its mandatory lifecycle owner.

        Async dial/fork tasks may finish after the source transaction has
        already been cancelled.  The generation guard must run before any
        bridge index is mutated, otherwise a late winner can recreate a
        terminal session and leak its client or relay.

        Production registries are bound to a session owner.  Requiring the
        lifecycle task at this boundary makes it impossible for a route to
        create a bridge whose destination BYE is never observed.
        """

        if expected_generation is not None and not self.is_generation_current(
            source_call_id,
            expected_generation,
        ):
            return None
        if lifecycle_task is None and self._session_owner is not None:
            raise ValueError("SIP bridge requires a lifecycle task")
        self.bridge_clients[source_call_id] = dest_call_id
        session = self.upsert(
            source_call_id,
            state=state,
            owner="bridge",
            caller=caller,
            callee=callee,
            route_kind=route_kind,
            ingress=ingress,
            origin=origin,
        )
        self.add_leg(source_call_id, source_call_id, role=source_role, state=source_state or state)
        self.add_leg(source_call_id, dest_call_id, role=dest_role, state=dest_state or state)
        self.attach_sip_client(
            source_call_id,
            dest_call_id,
            client,
            role=dest_role,
            state=dest_state or state,
        )
        if lifecycle_task is not None:
            self.attach_client_watcher(dest_call_id, lifecycle_task)
        return session

    def clear_runtime(self) -> None:
        self._release_all_endpoint_claims()
        self.sessions.clear()
        self.leg_index.clear()
        self.pending_routes.clear()
        self.pending_invites.clear()
        self.preanswered.clear()
        self.softphone_media.clear()
        self.video_parameter_sets.clear()
        self.sip_clients.clear()
        self.client_watchers.clear()
        self.relays.clear()
        self.bridge_clients.clear()
        self.event_contexts.clear()
        self.terminal_summary_ids.clear()
        self.terminated_call_ids.clear()

    def active_count(self, *, include_ha_softphone: bool = True) -> int:
        count = 0
        for session in self.sessions.values():
            if session.state in TERMINAL_STATES:
                continue
            if include_ha_softphone or not any(leg.role == "ha_softphone" for leg in session.legs.values()):
                count += 1
        return count

    def snapshot(self) -> dict[str, Any]:
        resource_counts = {
            "sessions": len(self.sessions),
            "legs": sum(len(session.legs) for session in self.sessions.values()),
            "pending_routes": len(self.pending_routes),
            "pending_invites": len(self.pending_invites),
            "preanswered": len(self.preanswered),
            "softphone_media": len(self.softphone_media),
            "sip_clients": len(self.sip_clients),
            "client_watchers": len(self.client_watchers),
            "relays": len(self.relays),
            "bridges": len(self.bridge_clients),
            "endpoint_claims": sum(
                len(claims) for claims in self.endpoint_claims.values()
            ),
        }
        return {
            "sessions": len(self.sessions),
            "active_sessions": self.active_count(),
            "terminated_calls": len(self.terminated_call_ids),
            "resource_counts": resource_counts,
            "call_ids": sorted(self.sessions),
            "pending_call_ids": sorted(self.pending_invites),
            "media_call_ids": sorted(self.softphone_media),
            "bridge_call_ids": sorted(self.bridge_clients),
            "endpoint_claims": {
                call_id: dict(claims)
                for call_id, claims in sorted(self.endpoint_claims.items())
            },
        }
