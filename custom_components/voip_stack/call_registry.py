"""Observable HA call projection and compatibility resource indexes."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Iterator, Literal

from .endpoint_session import (
    CallEventContext,
    CallLeg,
    CleanupStage,
    EndpointCallSession,
    SessionPhase,
    TerminationIntent,
)
from .automation_routing import CALL_EVENT_SCHEMA_VERSION
from .media_reservation import release_media_reservation
from .session_cleanup import async_cleanup_sip_runtime

LegRole = Literal[
    "caller",
    "callee",
    "trunk",
    "ha_softphone",
    "esp",
    "softphone",
    "router",
    "assist",
    "local_phone",
]
CallOwner = Literal[
    "", "ha_softphone", "router", "bridge", "assist", "local_bridge", "terminal"
]
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
_EVENT_IDENTITY_KEYS = (
    "endpoint_id", "source_endpoint_id", "dest_endpoint_id", "target_endpoint_id",
    "device_id", "source_device_id", "dest_device_id", "target_device_id",
    "ingress", "origin",
)


class CallRuntimeApi:
    """Call operations implemented directly by the authoritative runtime."""

    def _set_state(self, session: EndpointCallSession, state: str) -> bool:
        """Atomically update the public state and its authoritative phase."""

        accepted, phase = self._resolve_observation(session, state)
        return accepted and session.apply_observation(state, phase)

    @staticmethod
    def _advance_event_context(context: CallEventContext, state: str) -> None:
        if state and state != context.state:
            context.previous_state = context.state
            context.state = state
            context.sequence += 1
        if state == "in_call" and not context.connected_at:
            context.connected_at = time.monotonic()
        if state in TERMINAL_STATES and context.connected_at and context.duration_seconds is None:
            context.duration_seconds = max(
                0, round(time.monotonic() - context.connected_at)
            )

    @staticmethod
    def _event_identity_fields(
        metadata: dict[str, Any], endpoint_claims: Iterable[str] = ()
    ) -> dict[str, Any]:
        fields = {
            key: metadata[key]
            for key in _EVENT_IDENTITY_KEYS
            if metadata.get(key) not in (None, "")
        }
        participants = {
            str(fields[key]).strip()
            for key in _EVENT_IDENTITY_KEYS[:4]
            if key in fields
        }
        participants.update(endpoint_claims)
        participants.discard("")
        if participants:
            fields["participant_endpoint_ids"] = sorted(participants)
        return fields

    def artifact_for(self, call_id: str, name: str) -> Any | None:
        session = self.get_session(self.resolve_session_id(call_id))
        return getattr(session.artifacts, name) if session is not None else None

    def artifact_items(self, name: str) -> Iterator[tuple[str, Any]]:
        return (
            (call_id, value)
            for call_id, session in self.sessions.items()
            if (value := getattr(session.artifacts, name)) not in (None, False)
        )

    def resource_for(self, call_id: str, kind: str) -> Any | None:
        session = self.get_session(self.resolve_session_id(call_id))
        if session is None:
            return None
        name = f"{kind}:{call_id}"
        return next((r.value for r in session.resources if r.name == name), None)

    def resource_items(self, kind: str) -> Iterator[tuple[str, Any]]:
        marker = f"{kind}:"
        return (
            (resource.name.removeprefix(marker), resource.value)
            for session in self.sessions.values()
            for resource in session.resources
            if resource.name.startswith(marker)
        )

    def sip_client_for(self, call_id: str) -> Any | None:
        session = self.get_session(self.resolve_session_id(call_id))
        if session is None:
            return None
        leg = session.legs.get(call_id)
        if leg is not None:
            return leg.dialog
        return next(
            (leg.dialog for leg in session.legs.values() if leg.sip_call_id == call_id),
            None,
        )

    def sip_client_items(self) -> Iterator[tuple[str, Any]]:
        return (
            (leg.sip_call_id or leg.leg_id, leg.dialog)
            for session in self.sessions.values()
            for leg in session.legs.values()
            if leg.dialog is not None
        )

    def bridge_link_for(self, call_id: str) -> str:
        session = self.get_session(call_id)
        return (
            str(session.metadata.get("bridge_dest_call_id") or "")
            if session is not None
            else ""
        )

    def bridge_link_items(self) -> Iterator[tuple[str, str]]:
        return (
            (call_id, dest_call_id)
            for call_id in self.sessions
            if (dest_call_id := self.bridge_link_for(call_id))
        )

    def endpoint_claims_for(self, call_id: str) -> dict[str, str]:
        session = self.get_session(self.resolve_session_id(call_id))
        return session.endpoint_claims if session is not None else {}

    def _set_artifact(self, call_id: str, name: str, value: Any) -> None:
        session = self.get_session(call_id)
        if session is None or not session.live:
            raise RuntimeError(f"call session {call_id!r} is unavailable")
        setattr(session.artifacts, name, value)

    def _take_artifact(self, call_id: str, name: str) -> Any | None:
        session = self.get_session(call_id)
        if session is None or session.phase is SessionPhase.TERMINATED:
            return None
        value = getattr(session.artifacts, name)
        setattr(session.artifacts, name, False if isinstance(value, bool) else None)
        return value

    def cache_video_parameter_sets(
        self, call_id: str, parameter_sets: tuple[bytes, ...]
    ) -> None:
        """Cache video configuration on the current call generation."""

        self._set_artifact(call_id, "video_parameter_sets", tuple(parameter_sets))

    def clear_video_parameter_sets(self, call_id: str) -> None:
        """Clear video configuration without mutating a detached projection."""

        self._take_artifact(call_id, "video_parameter_sets")

    def set_pending_route(self, call_id: str, route: dict[str, Any]) -> None:
        """Begin routing on the authoritative owner and attach its decision state."""

        if self.get_session(call_id) is None:
            self.upsert(call_id, state="connecting", owner="router")

        self._set_artifact(call_id, "pending_route", route)

    def take_pending_route(self, call_id: str) -> dict[str, Any] | None:
        """Detach route state from its authoritative call generation."""

        return self._take_artifact(call_id, "pending_route")

    def set_pending_invite(self, call_id: str, invite: Any) -> None:
        """Attach an INVITE to the sole current call owner."""

        self._set_artifact(call_id, "pending_invite", invite)

    def take_pending_invite(self, call_id: str) -> Any | None:
        """Take an INVITE without mutating a detached compatibility view."""

        return self._take_artifact(call_id, "pending_invite")

    def _retire_observation(self, session: EndpointCallSession) -> None:
        """Drop derived indexes after authoritative cleanup completes."""

        call_id = session.call_id
        self._remember_terminated(call_id, generation=session.generation)
        context = session.event_context
        self._advance_event_context(context, session.state)
        self.terminated_call_ids[call_id] = (session.generation, context)
        for leg_id in tuple(session.legs):
            self.leg_index.pop(leg_id, None)
        self.leg_index.pop(call_id, None)

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
            previous = self.terminated_call_ids.get(clean_call_id)
            self.terminated_call_ids[clean_call_id] = (
                int(generation), previous[1] if previous is not None else None
            )
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
        current = self.sessions.get(session_id)
        for candidate in (call_id, session_id):
            if candidate not in self.terminated_call_ids:
                continue
            terminal_generation = self.terminated_call_ids[candidate][0]
            if (
                generation is None
                and current is not None
                and current.generation != terminal_generation
            ):
                continue
            if generation is None or terminal_generation == int(generation):
                return True
        return False

    def begin_termination(
        self,
        call_id: str,
        intent: TerminationIntent,
    ) -> bool:
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
        if session is not None:
            if not self.claim_termination(
                session_id,
                intent,
                generation=session.generation,
            ):
                return False
            for leg_id, owner_id in tuple(self.leg_index.items()):
                if owner_id == session_id:
                    self.leg_index.pop(leg_id, None)
        self._remember_terminated(
            call_id,
            session_id,
            generation=session.generation if session is not None else 0,
        )
        if session is not None:
            session.revision += 1
        return True

    def bind_endpoint_registry(self, registry: Any | None) -> None:
        """Bind the logical endpoint registry used for atomic busy claims.

        The call registry deliberately depends only on the tiny ``claim_call`` /
        ``release_call`` protocol.  This keeps the SIP session model reusable in
        pure tests while making teardown the single owner of endpoint release.
        """
        if registry is self.endpoint_registry:
            return
        if any(session.endpoint_claims for session in self.sessions.values()):
            self._release_all_endpoint_claims()
        self._bind_endpoint_registry(registry)

    def _release_all_endpoint_claims(self) -> None:
        for session in tuple(self.sessions.values()):
            if session.endpoint_claims:
                self.release_endpoint_claims(session.call_id)

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
        session = self.sessions.get(call_id)
        terminal = self.terminated_call_ids.get(call_id)
        summary = terminal[1] if terminal is not None else None
        if session is None and state in TERMINAL_STATES and summary is not None:
            context = summary
            revision = 0
            generation = terminal[0] if terminal is not None else 0
            phase = SessionPhase.TERMINATED.value
            owner = "terminal"
            metadata = {}
        else:
            if session is None:
                session = self.ensure_session(call_id, event_only=True)
            context = session.event_context
            revision = session.revision
            generation = session.generation
            phase = session.phase.value
            owner = session.owner
            metadata = session.metadata
        self._advance_event_context(context, state)
        fields = {
            "schema_version": CALL_EVENT_SCHEMA_VERSION,
            "sequence": context.sequence,
            "revision": revision,
            "generation": generation,
            "pbx_phase": phase,
            "owner": owner,
            "previous_state": context.previous_state,
            "route_history": [dict(item) for item in context.route_history],
        }
        if state in TERMINAL_STATES and context.duration_seconds is not None:
            fields["duration_seconds"] = context.duration_seconds
        # Event entities must be attributed from call ownership, never by
        # resolving a caller-controlled display name. Preserve the explicit
        # source/destination metadata and include every atomically claimed
        # phone for ring groups and conferences.
        fields.update(
            self._event_identity_fields(
                metadata,
                session.endpoint_claims if session is not None else (),
            )
        )
        if session is not None and state in TERMINAL_STATES and session.metadata.get(
            "event_only"
        ) is True:
            self._retire_observation(session)
            self.sessions.pop(call_id, None)
        return fields

    def claim_terminal_summary(self, call_id: str) -> bool:
        """Claim the single Logbook summary emitted for one logical call."""

        call_id = self.resolve_session_id(str(call_id or "").strip())
        context = self.event_context(call_id)
        if context is None or context.terminal_summary_claimed:
            return False
        context.terminal_summary_claimed = True
        return True

    def event_context(self, call_id: str) -> CallEventContext | None:
        """Return the current automation event context for a call or leg."""
        call_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(call_id)
        if session is not None:
            return session.event_context
        terminal = self.terminated_call_ids.get(call_id)
        return terminal[1] if terminal is not None else None

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
        session = self.sessions.get(call_id)
        if session is None:
            session = self.ensure_session(call_id, event_only=True)
        context = session.event_context
        context.route_history.append(
            {
                "action": str(action or "").strip(),
                "destination": str(destination or "").strip(),
                "source": str(source or "automation").strip(),
            }
        )
        del context.route_history[:-8]
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
    ) -> EndpointCallSession:
        ownership_metadata = dict(metadata)
        if ingress:
            ownership_metadata["ingress"] = ingress
        if origin:
            ownership_metadata["origin"] = origin
        authoritative = self.ensure_session(
            call_id,
            caller=caller,
            callee=callee,
            route_kind=route_kind,
            **ownership_metadata,
        )
        session = authoritative
        changed = False
        if state and self._set_state(session, state):
            changed = True
        for attribute, value in (
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
        if any(
            session.metadata.get(key) != value for key, value in clean_metadata.items()
        ):
            session.metadata.update(clean_metadata)
            changed = True
        if changed:
            session.revision += 1
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
    ) -> EndpointCallSession | None:
        """Apply one guarded control mutation and advance its revision once."""
        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if expected_revision is not None and session.revision != int(expected_revision):
            return None
        if expected_generation is not None and session.generation != int(
            expected_generation
        ):
            return None
        if expected_owner is not None and session.owner != expected_owner:
            return None
        if session.owner == "terminal" or session.state in TERMINAL_STATES:
            return None
        if state:
            self._set_state(session, state)
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
                key: value for key, value in metadata.items() if value not in (None, "")
            }
            if any(
                session.metadata.get(key) != value
                for key, value in clean_metadata.items()
            ):
                session.metadata.update(clean_metadata)
                session.revision += 1
        self.observe_leg(
            call_id,
            leg_id,
            role=role,
            state=state,
            sip_call_id=sip_call_id or leg_id,
            endpoint_id=str(metadata.get("endpoint_id") or ""),
            generation=session.generation,
        )
        leg = session.legs[leg_id]
        leg.role = role
        self.leg_index[leg_id] = call_id
        session.revision += 1
        return leg

    def remove_leg(self, call_id: str, leg_id: str) -> CallLeg | None:
        """Remove one destination leg without ending its source call."""
        session_id = self.resolve_session_id(call_id)
        session = self.sessions.get(session_id)
        if session is None:
            return None
        leg = session.legs.get(leg_id)
        if leg is not None and self.release_leg(
            session_id,
            leg_id,
            generation=session.generation,
        ):
            self.leg_index.pop(leg_id, None)
            session.revision += 1
            return leg
        return None

    async def close_leg(
        self,
        call_id: str,
        leg_id: str,
        *,
        reason: str,
    ) -> bool:
        """Close one failed fork leg without escaping session ownership."""

        session_id = self.resolve_session_id(call_id)
        session = self.sessions.get(session_id)
        if session is None or not session.live:
            return False
        watcher = session.named_tasks.get(f"client_watcher:{leg_id}")
        if watcher is not None:
            session.release_task(watcher)
            if watcher is not asyncio.current_task() and not watcher.done():
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        leg = self.remove_leg(session_id, leg_id)
        if leg is None:
            return False
        await leg.close(reason)
        return True

    def attach_sip_client(
        self,
        source_call_id: str,
        dest_call_id: str,
        client: Any,
        *,
        role: LegRole = "callee",
        state: str = "",
    ) -> None:
        session_id = self.resolve_session_id(str(source_call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"call session {source_call_id!r} is unavailable")

        async def _relay_refer(target: Any) -> int:
            current = self.get_session(session_id)
            if current is None or not current.live:
                return 503
            candidates = {
                id(leg.dialog): leg.dialog
                for leg in current.legs.values()
                if leg.dialog is not None
                and leg.dialog is not client
                and getattr(leg.dialog, "dialog", None) is not None
                and callable(getattr(leg.dialog, "refer", None))
            }
            if len(candidates) != 1:
                return 603
            try:
                result = await next(iter(candidates.values())).refer(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                return 500
            status = int(getattr(result, "status", 0) or 0)
            return status if 200 <= status <= 699 else 500

        if hasattr(client, "on_refer") and client.on_refer is None:
            client.on_refer = _relay_refer

        async def _close_client(_reason: str) -> None:
            await async_cleanup_sip_runtime(client=client, terminate_client=True)

        self.observe_leg(
            session_id,
            dest_call_id,
            role=role,
            state=state,
            sip_call_id=dest_call_id,
            dialog=client,
            closer=_close_client,
            generation=session.generation,
        )

    def attach_client_watcher(self, call_id: str, task: Any) -> None:
        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"call session {call_id!r} is unavailable")
        self.own_task(
            session_id,
            task,
            name=f"client_watcher:{call_id}",
            generation=session.generation,
        )

    def attach_media(
        self,
        call_id: str,
        media: dict[str, Any],
        *,
        provisional: bool = False,
    ) -> None:
        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        prefix = "preanswered" if provisional else "softphone_media"
        if session is None:
            raise RuntimeError(f"call session {call_id!r} is unavailable")
        resource_name = f"{prefix}:{call_id}"

        def _release_media(_reason: str) -> None:
            release_media_reservation(media)

        self.own_resource(
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
        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        prefix = "preanswered" if provisional else "softphone_media"
        media = self.resource_for(call_id, prefix)
        if media is None:
            media = default
        if media is default or session is None:
            return media
        self.release_resource(
            session_id,
            f"{prefix}:{call_id}",
            value=media,
            generation=session.generation,
        )
        return media

    def update_media(
        self,
        call_id: str,
        *,
        provisional: bool = False,
        **values: Any,
    ) -> bool:
        """Update an owned media record without mutating a detached view."""

        media = self.resource_for(
            call_id, "preanswered" if provisional else "softphone_media"
        )
        if not isinstance(media, dict):
            return False
        media.update(values)
        return True

    def resolve_session_id(self, call_id: str) -> str:
        return self.leg_index.get(call_id, call_id)

    def bind_controller(
        self,
        call_id: str,
        *,
        context: Any | None = None,
        user_id: str = "",
        endpoint_id: str = "",
    ) -> EndpointCallSession:
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
        scoped = bool(requested_endpoint_id and session.metadata.get("local_bridge"))
        if scoped:
            controllers = session.metadata.setdefault("controller_user_ids", {})
            current_user_id = str(controllers.get(requested_endpoint_id) or "").strip()
        else:
            current_user_id = str(
                session.metadata.get("controller_user_id") or ""
            ).strip()
        if (
            current_user_id
            and requested_user_id
            and current_user_id != requested_user_id
        ):
            raise ValueError(
                f"call_id {session_id}"
                + (f" endpoint {requested_endpoint_id}" if scoped else "")
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

        session = self.sessions.get(self.resolve_session_id(str(call_id or "").strip()))
        return session.metadata.get("ha_context") if session is not None else None

    def _discard_dark_session(
        self,
        call_id: str,
        intent: TerminationIntent,
    ) -> EndpointCallSession | None:
        """Synchronously discard an I/O-free session used by pure model tests."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            return None
        self.release_endpoint_claims(session_id)
        self.forget_bridge_link(session_id)
        session.artifacts.settle()
        session.owner = "terminal"
        session.outcome = intent.reason
        session.terminal_reason = intent.reason
        session.apply_observation(intent.public_state, None)
        session.revision += 1
        self._retire_observation(session)
        self.sessions.pop(session_id, None)
        return session

    def bridge_for(self, call_id: str) -> tuple[str, str]:
        if dest_call_id := self.bridge_link_for(call_id):
            return call_id, dest_call_id
        for source, session in self.sessions.items():
            if self.bridge_link_for(source) == call_id:
                return source, call_id
        return "", ""

    async def terminate_call_wait(
        self,
        call_id: str,
        *,
        reason: str = "",
        intent: TerminationIntent | None = None,
    ) -> EndpointCallSession | None:
        """Remove one call and wait for its authoritative cleanup barrier."""

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        barrier = None
        if session is not None:
            terminal_intent = intent or TerminationIntent(
                reason or session.terminal_reason or session.outcome or "removed",
            )
            barrier = self.request_termination(
                session_id,
                terminal_intent,
                generation=session.generation,
            )
        removed = session
        if barrier is not None:
            if session is not None and session.cleanup_waits_for(
                asyncio.current_task()
            ):
                # An owned lifecycle task may handle its cancellation by
                # requesting the same terminal outcome. Waiting here would
                # form a cycle: the barrier waits for this task while this
                # task waits for the barrier. The authoritative cleanup is
                # already running and remains the sole resource owner.
                return removed
            await barrier
        return removed

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
    ) -> EndpointCallSession | None:
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
        if lifecycle_task is None and self.active:
            raise ValueError("SIP bridge requires a lifecycle task")
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
        self.set_bridge_link(source_call_id, dest_call_id)
        self.add_leg(
            source_call_id,
            source_call_id,
            role=source_role,
            state=source_state or state,
        )
        self.add_leg(
            source_call_id, dest_call_id, role=dest_role, state=dest_state or state
        )
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
        if self.calls:
            raise RuntimeError("cannot clear projection before PBX shutdown")
        self._release_all_endpoint_claims()
        self.sessions.clear()
        self.leg_index.clear()
        self.terminated_call_ids.clear()

    def active_count(self, *, include_ha_softphone: bool = True) -> int:
        count = 0
        for session in self.sessions.values():
            if session.state in TERMINAL_STATES:
                continue
            if include_ha_softphone or not any(
                leg.role == "ha_softphone" for leg in session.legs.values()
            ):
                count += 1
        return count

    def snapshot(self) -> dict[str, Any]:
        pending_routes = dict(self.artifact_items("pending_route"))
        pending_invites = dict(self.artifact_items("pending_invite"))
        preanswered = self.resources_snapshot("preanswered")
        media = self.resources_snapshot("softphone_media")
        bridges = self.bridge_links_snapshot()
        claims = self.endpoint_claims_snapshot()
        resource_counts = {
            "sessions": len(self.sessions),
            "legs": sum(len(session.legs) for session in self.sessions.values()),
            "pending_routes": len(pending_routes),
            "pending_invites": len(pending_invites),
            "preanswered": len(preanswered),
            "softphone_media": len(media),
            "sip_clients": len(self.sip_clients_snapshot()),
            "client_watchers": len(self.client_watchers_snapshot()),
            "relays": len(self.relays_snapshot()),
            "bridges": len(bridges),
            "endpoint_claims": sum(len(item) for item in claims.values()),
        }
        return {
            "sessions": len(self.sessions),
            "active_sessions": self.active_count(),
            "terminated_calls": len(self.terminated_call_ids),
            "resource_counts": resource_counts,
            "call_ids": sorted(self.sessions),
            "pending_call_ids": sorted(pending_invites),
            "media_call_ids": sorted(media),
            "bridge_call_ids": sorted(bridges),
            "endpoint_claims": {
                call_id: dict(item) for call_id, item in sorted(claims.items())
            },
        }
