"""Queues derived from configured phones, with persistent agent intent.

Membership belongs to phones. This owner only stores queue policy, operator
availability and bounded waiting tickets; it never owns another SIP lifecycle.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import math
import time
from typing import Callable
from uuid import uuid4

from .phone_endpoint import EndpointAvailability
from .roster import RosterEntry, normalize_roster_key

EVENT_QUEUE_CHANGED = "voip_stack.queue_changed"
AGENT_STATES = frozenset({"available", "paused", "logged_out"})


def _storage_mapping(value):
    return value if isinstance(value, Mapping) else {}


def _storage_identity(value):
    return isinstance(value, str) and bool(value) and len(value) <= 256 and all(ord(char) >= 32 for char in value)


def _storage_timestamp(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except OverflowError:
        return None
    return timestamp if math.isfinite(timestamp) and timestamp >= 0 else None


def _normalized_agent_stats(value):
    value = _storage_mapping(value)
    misses = value.get("consecutive_misses", 0)
    misses = min(misses, 2**31 - 1) if isinstance(misses, int) and not isinstance(misses, bool) and misses >= 0 else 0
    reason = value.get("pause_reason", "")
    return {"consecutive_misses": misses, "pause_reason": reason[:256] if isinstance(reason, str) else "",
            "last_answered": _storage_timestamp(value.get("last_answered")),
            "last_ended": _storage_timestamp(value.get("last_ended"))}


def normalize_queue_operations(data):
    """Restore only known, bounded operational fields; preserve valid intent."""
    data = _storage_mapping(data)
    states = {queue_id: {endpoint_id: state if isinstance(state, str) and state in AGENT_STATES else "logged_out"
                        for endpoint_id, state in _storage_mapping(rows).items() if _storage_identity(endpoint_id)}
              for queue_id, rows in _storage_mapping(data.get("agent_states")).items() if _storage_identity(queue_id)}
    stats = {queue_id: {endpoint_id: _normalized_agent_stats(row)
                       for endpoint_id, row in _storage_mapping(rows).items() if _storage_identity(endpoint_id)}
             for queue_id, rows in _storage_mapping(data.get("agent_stats")).items() if _storage_identity(queue_id)}
    round_robin = {queue_id: endpoint_id for queue_id, endpoint_id in _storage_mapping(data.get("round_robin")).items()
                   if _storage_identity(queue_id) and _storage_identity(endpoint_id)}
    last_ended = {endpoint_id: timestamp for endpoint_id, value in _storage_mapping(data.get("last_ended")).items()
                  if _storage_identity(endpoint_id) and (timestamp := _storage_timestamp(value)) is not None}
    return {"agent_states": states, "agent_stats": stats, "round_robin": round_robin, "last_ended": last_ended}


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """Ring-all/FIFO defaults apply without creating a separate config entry."""

    enabled: bool = True
    max_waiting: int = 10
    max_wait: float = 60
    ring_timeout: float = 20
    no_agent_wait: float = 1
    retry_delay: float = 1
    wrap_up: float = 0
    fallback: str = ""
    hold_media: str = ""
    strategy: str = "ring_all"
    agent_order: tuple[str, ...] = ()
    auto_pause_after: int = 0
    fallback_full: str = ""
    fallback_timeout: str = ""
    fallback_no_agents: str = ""
    fallback_disabled: str = ""
    fallback_removed: str = ""

    def fallback_for(self, reason):
        return getattr(self, f"fallback_{reason.removeprefix('queue_')}", "") or self.fallback

    @classmethod
    def from_data(cls, data):
        values = dict(data or {})
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown queue setting")
        order = values.get("agent_order", ())
        if not isinstance(order, (list, tuple)) or len(order) > 128 or any(
                not isinstance(item, str) or not item or len(item) > 256 for item in order):
            raise ValueError("agent_order must contain configured phone identities")
        values["agent_order"] = tuple(dict.fromkeys(order))
        policy = cls(**values)
        if policy.strategy not in {"ring_all", "round_robin", "longest_idle", "ordered"}:
            raise ValueError("Unknown queue strategy")
        if isinstance(policy.auto_pause_after, bool) or not isinstance(policy.auto_pause_after, int) or not 0 <= policy.auto_pause_after <= 100:
            raise ValueError("auto_pause_after must be 0 (disabled) or between 1 and 100")
        for name in ("fallback_full", "fallback_timeout", "fallback_no_agents", "fallback_disabled", "fallback_removed"):
            if not isinstance(getattr(policy, name), str) or len(getattr(policy, name)) > 256:
                raise ValueError(f"{name} must be a destination of at most 256 characters")
        if not isinstance(policy.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if isinstance(policy.max_waiting, bool) or not isinstance(policy.max_waiting, int) or not 1 <= policy.max_waiting <= 64:
            raise ValueError("max_waiting must be between 1 and 64")
        for name, minimum, maximum in (
            ("max_wait", 1, 3600), ("ring_timeout", 1, 120),
            ("no_agent_wait", 0, 120), ("retry_delay", 0.1, 120),
            ("wrap_up", 0, 3600),
        ):
            value = getattr(policy, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if not isinstance(policy.fallback, str) or len(policy.fallback) > 256:
            raise ValueError("fallback must be a destination of at most 256 characters")
        if not isinstance(policy.hold_media, str) or len(policy.hold_media) > 2048:
            raise ValueError("hold_media must be an audio URI of at most 2048 characters")
        return policy


@dataclass(frozen=True, slots=True)
class QueueDefinition:
    queue_id: str
    name: str
    members: tuple[str, ...]
    extension: str = ""


@dataclass(slots=True, eq=False)
class QueueTicket:
    queue_id: str
    call_id: str
    generation: int
    enqueued: float
    deadline: float
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    reserved: tuple[str, ...] = ()
    closed: bool = False
    policy: QueuePolicy = field(default_factory=QueuePolicy)
    entry_id: str = field(default_factory=lambda: uuid4().hex)
    admitted: bool = True
    tried: set[str] = field(default_factory=set)
    offers: dict[str, tuple[str, float]] = field(default_factory=dict)
    connected_at: float | None = None
    completed: bool = False


class QueueManager:
    """One entry-owned policy and admission coordinator on the HA loop."""

    def __init__(self, hass, entry, *, store=None, clock=time.monotonic):
        self.hass, self.entry, self.store, self.clock = hass, entry, store, clock
        self.endpoints = entry.runtime_data.endpoints
        self.definitions: dict[str, QueueDefinition] = {}
        self.policies: dict[str, QueuePolicy] = {}
        self.agent_states: dict[str, dict[str, str]] = {}
        self.waiting: dict[str, deque[QueueTicket]] = {}
        self.staged_admissions: dict[str, QueueTicket] = {}
        self.reservations: dict[str, QueueTicket] = {}
        self.wrap_until: dict[str, float] = {}
        self.agent_stats: dict[str, dict[str, dict]] = {}
        self.round_robin: dict[str, str] = {}
        self.last_ended: dict[str, float] = {}
        # Operational projection of the existing endpoint claim, not a call owner.
        self.conversations: dict[str, QueueTicket] = {}
        self._listeners: set[Callable] = set()
        self._unsub = None
        self.closed = False

    def resolve(self, ref) -> QueueDefinition | None:
        key = str(ref or "").strip()
        for prefix in ("service:queue:", "queue:"):
            if key.startswith(prefix):
                key = key[len(prefix):]
                break
        direct = self.definitions.get(key)
        if direct is not None:
            return direct
        normalized = normalize_roster_key(key)
        return next((queue for queue in self.definitions.values()
                     if normalized in {normalize_roster_key(queue.name), normalize_roster_key(queue.extension)}), None)

    def reconcile(self, memberships, *, records=None):
        """Only persistent declarations create or remove queues, never presence."""
        previous_policies = dict(self.policies)
        grouped: dict[str, tuple[str, list[str]]] = {}
        for endpoint_id, names in memberships:
            for name in names:
                key = normalize_roster_key(name)
                if key:
                    row = grouped.setdefault(key, (name, []))
                    if endpoint_id not in row[1]:
                        row[1].append(endpoint_id)
        if records is None:
            wanted = {key: QueueDefinition(key, name, tuple(members), name if name.isdigit() else "")
                      for key, (name, members) in grouped.items()}
        else:
            wanted = {identity: QueueDefinition(identity, record["name"],
                tuple(grouped.get(normalize_roster_key(record["name"]), ("", []))[1]), record.get("extension", ""))
                for identity, record in records.items()}
            for identity, record in records.items():
                self.policies[identity] = QueuePolicy.from_data({**record.get("policy", {}), "enabled": record.get("enabled", True)})
        if wanted == self.definitions and self.policies == previous_policies:
            return
        for key in self.definitions.keys() - wanted.keys():
            self.policies.pop(key, None)
            self.agent_states.pop(key, None)
        self.definitions = wanted
        self.policies = {key: self.policies.get(key, QueuePolicy()) for key in wanted}
        self.agent_states = {
            key: {member: self.agent_states.get(key, {}).get(member, "available")
                  for member in definition.members}
            for key, definition in wanted.items()
        }
        self._changed(persist=True)

    def refresh(self):
        from .phone_queue_membership import iter_phone_queue_memberships
        from .group_config import group_records, reconcile_group_records
        reconcile_group_records(self.hass, self.entry)
        self.reconcile(iter_phone_queue_memberships(self.hass, self.entry), records=group_records(self.entry, "queue"))

    def policy(self, ref) -> QueuePolicy:
        queue = self.resolve(ref)
        if queue is None:
            raise ValueError("Unknown queue; declare it on at least one phone first")
        return self.policies[queue.queue_id]

    def configure(self, ref, changes):
        queue = self.resolve(ref)
        policy = self.policy(ref)
        updated = QueuePolicy.from_data({**asdict(policy), **changes})
        if any(member not in queue.members for member in updated.agent_order):
            raise ValueError("Agent order must contain only configured queue members")
        if self.hass is not None:
            from .group_config import group_subentries
            for item in group_subentries(self.entry):
                if item.data.get("group_id") == queue.queue_id:
                    self.hass.config_entries.async_update_subentry(self.entry, item,
                        data={**item.data, "policy": asdict(updated), "enabled": updated.enabled})
                    break
        self.policies[queue.queue_id] = updated
        self._event("policy_changed", queue_id=queue.queue_id)
        self._changed(persist=True)

    def set_agent_state(self, ref, endpoint_id, state, reason=""):
        queue = self.resolve(ref)
        if queue is None or endpoint_id not in queue.members:
            raise ValueError("The phone does not declare this queue")
        if state not in AGENT_STATES:
            raise ValueError("Agent state must be available, paused or logged_out")
        if not isinstance(reason, str) or len(reason) > 256:
            raise ValueError("Pause reason must be at most 256 characters")
        previous = self.agent_states[queue.queue_id][endpoint_id]
        stats = self._stats(queue.queue_id, endpoint_id)
        if state == "available":
            stats["consecutive_misses"] = 0
        stats["pause_reason"] = reason if state == "paused" else ""
        self.agent_states[queue.queue_id][endpoint_id] = state
        self._event("agent_state_changed", queue_id=queue.queue_id, endpoint_id=endpoint_id,
                    state=state, previous_state=previous, reason=stats["pause_reason"] or "manual")
        self._changed(persist=True)

    def eligible(self, ref, *, call_id="") -> tuple[str, ...]:
        queue = self.resolve(ref)
        if queue is None or not self.policies[queue.queue_id].enabled:
            return ()
        endpoints = self.endpoints
        now = self.clock()
        return tuple(member for member in queue.members
            if self.agent_states[queue.queue_id][member] == "available"
            and member not in self.reservations
            and self.wrap_until.get(member, 0) <= now
            and (endpoint := endpoints.get(member)) is not None
            and endpoint.availability is EndpointAvailability.AVAILABLE
            and not endpoint.dnd
            and not endpoint.active_call_id)

    def enqueue(self, ref, call_id, generation, *, replacing=None) -> QueueTicket:
        queue = self.resolve(ref)
        policy = self.policy(ref)
        if self.closed or not policy.enabled:
            raise ValueError("The queue is disabled")
        pending = self.waiting.get(queue.queue_id, ())
        held = sum(ticket.queue_id == queue.queue_id for ticket in self.staged_admissions.values())
        if len(pending) + held >= policy.max_waiting:
            raise ValueError("The queue is full")
        existing = [ticket for rows in self.waiting.values() for ticket in rows if ticket.call_id == call_id]
        if replacing is not None and (replacing.closed or replacing not in existing
                or replacing.call_id != call_id or replacing.generation != generation
                or replacing.queue_id == queue.queue_id):
            raise ValueError("The queue transfer source changed")
        if any(ticket is not replacing for ticket in existing) or any(
                ticket.call_id == call_id for ticket in self.staged_admissions.values()):
            raise ValueError("The call already has a queue ticket")
        now = self.clock()
        ticket = QueueTicket(queue.queue_id, call_id, generation, now, now + policy.max_wait, policy=policy)
        if replacing is not None:
            ticket.admitted = False
            self.staged_admissions[ticket.entry_id] = ticket
            return ticket
        self.waiting.setdefault(queue.queue_id, deque()).append(ticket)
        self._event("entered", ticket)
        self._changed()
        return ticket

    def record_rejection(self, ref, call_id, generation, reason):
        queue = self.resolve(ref)
        if queue is None:
            return
        now = self.clock()
        ticket = QueueTicket(queue.queue_id, call_id, generation, now, now,
                             policy=self.policy(queue.queue_id), closed=True, admitted=False)
        self._event("exited", ticket, reason=reason, outcome="rejected", wait_seconds=0)

    def prepare_transfer(self, replacing, ref):
        return self.enqueue(ref, replacing.call_id, replacing.generation, replacing=replacing)

    def commit_transfer(self, previous, prepared, *, notify=True):
        """Swap the admission in one synchronous section, before publishing it."""
        if (previous.closed or previous not in self.waiting.get(previous.queue_id, ())
                or self.staged_admissions.get(prepared.entry_id) is not prepared
                or prepared.closed or prepared.admitted or prepared.call_id != previous.call_id
                or prepared.generation != previous.generation or self.resolve(prepared.queue_id) is None
                or not self.policy(prepared.queue_id).enabled):
            raise ValueError("The queue transfer changed before commit")
        old = self.waiting[previous.queue_id]
        old.remove(previous)
        if not old:
            self.waiting.pop(previous.queue_id, None)
        for member in previous.reserved:
            if self.reservations.get(member) is previous:
                self.reservations.pop(member)
        previous.reserved = ()
        previous.closed = True
        prepared.admitted = True
        prepared.enqueued = self.clock()
        prepared.deadline = prepared.enqueued + prepared.policy.max_wait
        self.staged_admissions.pop(prepared.entry_id)
        self.waiting.setdefault(prepared.queue_id, deque()).append(prepared)
        if notify:
            self.publish_transfer(previous, prepared)

    def publish_transfer(self, previous, prepared):
        self._event("exited", previous, outcome="transferred", reason="queue_transfer",
                    wait_seconds=max(0, self.clock() - previous.enqueued))
        self._event("entered", prepared)
        self._changed()

    def rollback_transfer(self, prepared):
        if self.staged_admissions.get(prepared.entry_id) is prepared:
            self.staged_admissions.pop(prepared.entry_id)
            prepared.closed = True

    def claim_turn(self, ticket) -> tuple[str, ...]:
        """The FIFO head atomically reserves agents across all queues."""
        pending = self.waiting.get(ticket.queue_id, ())
        if (ticket.closed or self.resolve(ticket.queue_id) is None or not pending or pending[0] is not ticket
                or ticket.deadline <= self.clock()):
            return ()
        if ticket.reserved:
            return ticket.reserved
        members = self.eligible(ticket.queue_id, call_id=ticket.call_id)
        policy = self.policy(ticket.queue_id)
        definition = self.resolve(ticket.queue_id)
        order = tuple(dict.fromkeys((*policy.agent_order, *definition.members)))
        members = tuple(member for member in order if member in members)
        if policy.strategy != "ring_all" and members:
            untried = tuple(member for member in members if member not in ticket.tried)
            if not untried:
                ticket.tried.clear()
                untried = members
            if policy.strategy == "round_robin":
                last = self.round_robin.get(ticket.queue_id)
                if last in order:
                    i = order.index(last) + 1
                    rotated = order[i:] + order[:i]
                    untried = tuple(member for member in rotated if member in untried)
            elif policy.strategy == "longest_idle":
                untried = tuple(sorted(untried, key=lambda member: self.last_ended.get(member, 0)))
            members = untried[:1]
        for member in members:
            self.reservations[member] = ticket
        ticket.reserved = members
        return members

    def _stats(self, queue_id, endpoint_id):
        if not isinstance(self.agent_stats, dict):
            self.agent_stats = dict(_storage_mapping(self.agent_stats))
        rows = self.agent_stats.get(queue_id)
        if not isinstance(rows, dict):
            rows = dict(_storage_mapping(rows))
            self.agent_stats[queue_id] = rows
        rows[endpoint_id] = _normalized_agent_stats(rows.get(endpoint_id))
        return rows[endpoint_id]

    def _event(self, kind, ticket=None, **values):
        if self.hass is None:
            return
        from .queue_activity import emit_queue_event
        queue_id = ticket.queue_id if ticket is not None else values.pop("queue_id")
        definition = self.resolve(queue_id)
        payload = {"event_type": kind, "config_entry_id": self.entry.entry_id,
            "queue_id": queue_id, "queue_name": definition.name if definition else queue_id}
        if ticket is not None:
            payload.update(entry_id=ticket.entry_id, call_id=ticket.call_id, generation=ticket.generation)
        emit_queue_event(self.hass, {**payload, **values})

    def begin_offer(self, ticket, endpoint_id):
        if ticket.closed or self.reservations.get(endpoint_id) is not ticket:
            return ""
        for identity, (phone, _started) in ticket.offers.items():
            if phone == endpoint_id:
                return identity
        identity = uuid4().hex
        ticket.offers[identity] = (endpoint_id, self.clock())
        ticket.tried.add(endpoint_id)
        self.round_robin[ticket.queue_id] = endpoint_id
        self._event("offer_started", ticket, endpoint_id=endpoint_id, attempt_id=identity)
        self._changed(persist=True)
        return identity

    def finish_offer(self, ticket, endpoint_id, outcome, rang, attempt_id, *, notify=True):
        offer = ticket.offers.get(attempt_id)
        if offer is None or offer[0] != endpoint_id:
            return
        ticket.offers.pop(attempt_id)
        stats = self._stats(ticket.queue_id, endpoint_id)
        if outcome == "in_call":
            stats["consecutive_misses"] = 0
        elif outcome == "timeout" and rang:
            stats["consecutive_misses"] += 1
            threshold = self.policies.get(ticket.queue_id, ticket.policy).auto_pause_after
            if (threshold and stats["consecutive_misses"] >= threshold
                    and self.agent_states.get(ticket.queue_id, {}).get(endpoint_id) == "available"):
                self.agent_states[ticket.queue_id][endpoint_id] = "paused"
                stats["pause_reason"] = "no_answer"
                self._event("agent_state_changed", queue_id=ticket.queue_id, endpoint_id=endpoint_id,
                    state="paused", previous_state="available", reason="no_answer")
        self._event("offer_ended", ticket, endpoint_id=endpoint_id, attempt_id=attempt_id,
                    outcome=outcome, reason="no_answer" if outcome == "timeout" and rang else outcome)
        if notify:
            self._changed(persist=True)

    def release_attempt(self, ticket, *, notify=True):
        for identity, (member, _started) in tuple(ticket.offers.items()):
            self.finish_offer(ticket, member, "cancelled", False, identity, notify=False)
        for member in ticket.reserved:
            if self.reservations.get(member) is ticket:
                self.reservations.pop(member)
        ticket.reserved = ()
        if notify:
            self._changed()

    def finish(self, ticket, *, connected_endpoint="", reason="abandoned", notify=True):
        if ticket.closed:
            return
        ticket.closed = True
        self.staged_admissions.pop(ticket.entry_id, None)
        pending = self.waiting.get(ticket.queue_id)
        if pending is not None and ticket in pending:
            pending.remove(ticket)
            if not pending:
                self.waiting.pop(ticket.queue_id, None)
        self.release_attempt(ticket, notify=False)
        if ticket.admitted and ticket.connected_at is None:
            self._event("exited", ticket, reason=reason, outcome=reason.removeprefix("queue_"),
                        wait_seconds=max(0, self.clock() - ticket.enqueued))
        if connected_endpoint:
            self.start_wrap_up(ticket.queue_id, connected_endpoint, duration=ticket.policy.wrap_up, notify=False)
        if notify:
            self._changed()

    def connected(self, ticket, endpoint_id):
        if ticket.closed or ticket.connected_at is not None:
            return
        ticket.connected_at = self.clock()
        self.conversations[endpoint_id] = ticket
        stats = self._stats(ticket.queue_id, endpoint_id)
        stats["last_answered"] = time.time()
        stats["consecutive_misses"] = 0
        self._event("answered", ticket, endpoint_id=endpoint_id,
                    wait_seconds=max(0, ticket.connected_at - ticket.enqueued))
        self.finish(ticket, notify=False)
        self._changed(persist=True)

    def complete_conversation(self, ticket, endpoint_id, reason):
        if ticket.completed or ticket.connected_at is None:
            return
        ticket.completed = True
        if self.conversations.get(endpoint_id) is ticket:
            self.conversations.pop(endpoint_id)
        self.last_ended[endpoint_id] = time.time()
        self._stats(ticket.queue_id, endpoint_id)["last_ended"] = self.last_ended[endpoint_id]
        self.start_wrap_up(ticket.queue_id, endpoint_id, duration=ticket.policy.wrap_up, notify=False)
        self._event("exited", ticket, endpoint_id=endpoint_id, outcome="completed", reason=reason,
                    wait_seconds=max(0, ticket.connected_at - ticket.enqueued),
                    talk_seconds=max(0, self.clock() - ticket.connected_at))
        self._changed(persist=True)

    def endpoint_changed(self, event):
        previous = event.previous
        endpoint = event.endpoint
        if previous is not None and previous.active_call_id and previous.active_call_id != endpoint.active_call_id:
            conversation = self.conversations.get(endpoint.endpoint_id)
            if conversation is not None and conversation.call_id == previous.active_call_id:
                self.complete_conversation(conversation, endpoint.endpoint_id, "agent_leg_ended")
            sip = getattr(self.entry.runtime_data, "sip", None)
            session = sip.get_session(previous.active_call_id) if sip is not None else None
            connected_leg = session is not None and any(
                leg.endpoint_id == endpoint.endpoint_id and (leg.state == "in_call" or leg.phase in {"answered", "held"})
                for leg in session.legs.values())
            # An answered caller does not make an unanswered fork candidate a
            # completed conversation. Use the released endpoint's actual leg.
            if conversation is not None or connected_leg:
                self.last_ended[endpoint.endpoint_id] = time.time()
                for queue in self.definitions.values():
                    if endpoint.endpoint_id in queue.members:
                        self._stats(queue.queue_id, endpoint.endpoint_id)["last_ended"] = self.last_ended[endpoint.endpoint_id]
        self.refresh() if self.hass is not None else None
        self._changed(persist=True)

    def start_wrap_up(self, queue_id, endpoint_id, *, duration=None, notify=True):
        if duration is None:
            duration = self.policies.get(queue_id, QueuePolicy()).wrap_up
        if duration > 0 and not self.closed:
            self.wrap_until[endpoint_id] = max(self.wrap_until.get(endpoint_id, 0), self.clock() + duration)
            if notify:
                self._changed()

    def subscribe(self, callback):
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    def _changed(self, *, persist=False):
        for tickets in self.waiting.values():
            for ticket in tickets:
                ticket.changed.set()
        if persist and self.store is not None:
            self.store.async_delay_save(self._snapshot, 0.2)
        if self.hass is not None:
            self.hass.bus.async_fire(EVENT_QUEUE_CHANGED)
        for callback in tuple(self._listeners):
            callback()

    def _snapshot(self):
        return {"policies": {key: asdict(value) for key, value in self.policies.items()},
                "agent_states": {key: dict(value) for key, value in self.agent_states.items()},
                "agent_stats": self.agent_stats, "round_robin": self.round_robin, "last_ended": self.last_ended}

    def get_state(self, ref, phone=None):
        queue = self.resolve(ref)
        if queue is None:
            raise ValueError("Unknown queue")
        endpoints = self.endpoints
        eligible = frozenset(self.eligible(queue.queue_id))
        members = []
        for member in queue.members:
            if phone and member != phone:
                continue
            endpoint = endpoints.get(member)
            state = self.agent_states[queue.queue_id][member]
            members.append({"phone": member, "state": state, "logged_in": state != "logged_out",
                "paused": state == "paused", "busy": bool(endpoint and endpoint.active_call_id),
                "available": member in eligible,
                "online": bool(endpoint and endpoint.availability is EndpointAvailability.AVAILABLE),
                "dnd": bool(endpoint and endpoint.dnd),
                **self._stats(queue.queue_id, member),
                "reserved_queue_id": self.reservations[member].queue_id if member in self.reservations else None,
                "reserved_attempt_id": next((identity for identity, (phone, _) in self.reservations[member].offers.items()
                    if phone == member), None) if member in self.reservations else None,
                "wrap_up_remaining": max(0, self.wrap_until.get(member, 0) - self.clock())})
            row = members[-1]
            reasons = []
            if not self.policies[queue.queue_id].enabled:
                reasons.append("queue_disabled")
            if state != "available":
                reasons.append(state)
            if not row["online"]:
                reasons.append("offline")
            if row["dnd"]:
                reasons.append("dnd")
            if row["busy"]:
                reasons.append("busy")
            if row["reserved_queue_id"]:
                reasons.append("reserved")
            if row["wrap_up_remaining"]:
                reasons.append("wrap_up")
            row["unavailable_reasons"] = reasons
            row["unavailable_reason"] = reasons[0] if reasons else None
        return {"queue_id": queue.queue_id, "name": queue.name,
                "policy": asdict(self.policies[queue.queue_id]),
                "waiting": len(self.waiting.get(queue.queue_id, ())), "members": members,
                "oldest_wait_seconds": max((max(0, self.clock() - ticket.enqueued)
                    for ticket in self.waiting.get(queue.queue_id, ())), default=0)}

    def phone_summary(self, endpoint_id):
        rows = []
        for queue in self.definitions.values():
            if endpoint_id not in queue.members:
                continue
            snapshot = self.get_state(queue.queue_id, endpoint_id)
            rows.append({"queue_id": queue.queue_id, "name": queue.name,
                         "waiting": snapshot["waiting"], **snapshot["members"][0]})
        return rows

    def roster_entries(self):
        return [RosterEntry(id=f"queue:{queue.queue_id}", name=queue.name,
            extension=queue.extension, ha_bridge=True,
            enabled=self.policies[queue.queue_id].enabled, metadata={"virtual_endpoint": "queue", "queue_id": queue.queue_id,
                "endpoint_id": f"service:queue:{queue.queue_id}", "endpoint_kind": "service"})
            for queue in self.definitions.values()]

    async def close(self):
        self.closed = True
        if self._unsub is not None:
            self._unsub()
        for tickets in self.waiting.values():
            for ticket in tickets:
                ticket.changed.set()
        self._listeners.clear()
        if self.store is not None:
            await self.store.async_save(self._snapshot())


def get_queue_manager(hass):
    from .runtime_data import runtime_data
    runtime = runtime_data(hass)
    return runtime.queues if runtime is not None else None


async def async_setup_queues(hass, entry):
    from homeassistant.helpers.storage import Store
    from .group_config import async_apply_group_updates, group_records, group_subentries
    store = Store(hass, 1, f"voip_stack.queues.{entry.entry_id}")
    data = _storage_mapping(await store.async_load())
    policies = _storage_mapping(data.get("policies"))
    previous_ids = set(group_records(entry))
    # A saved form command can survive a restart before the update listener ran.
    await async_apply_group_updates(hass, entry)
    for item in group_subentries(entry):
        identity = item.data["group_id"]
        legacy = normalize_roster_key(item.data["name"])
        if (item.data["kind"] == "queue" and identity not in previous_ids
                and legacy in policies):
            try:
                policy = QueuePolicy.from_data(policies[legacy])
            except (ValueError, TypeError):
                continue
            hass.config_entries.async_update_subentry(entry, item,
                data={**item.data, "policy": asdict(policy), "enabled": policy.enabled})
    manager = QueueManager(hass, entry, store=store)
    for key, value in policies.items():
        try:
            manager.policies[key] = QueuePolicy.from_data(value)
        except (ValueError, TypeError):
            continue
    operations = normalize_queue_operations(data)
    manager.agent_states = operations["agent_states"]
    manager.agent_stats = operations["agent_stats"]
    manager.round_robin = operations["round_robin"]
    manager.last_ended = operations["last_ended"]
    for identity, record in group_records(entry, "queue").items():
        legacy = normalize_roster_key(record["name"])
        if identity not in manager.agent_states and legacy in manager.agent_states:
            manager.agent_states[identity] = dict(manager.agent_states[legacy])
    entry.runtime_data.queues = manager
    manager.refresh()
    manager._unsub = entry.runtime_data.endpoints.subscribe(manager.endpoint_changed)
    return manager


async def async_set_queue_agent_state(call):
    from homeassistant.exceptions import ServiceValidationError
    from .phone_state import resolve_phone
    from .service_endpoints import async_require_phone_service_control
    endpoint = resolve_phone(call.hass, call.data["phone"])
    await async_require_phone_service_control(call.hass, call, endpoint=endpoint)
    manager = get_queue_manager(call.hass)
    if manager is None:
        raise ServiceValidationError("Queues are unavailable")
    try:
        manager.set_agent_state(call.data["queue"], endpoint.endpoint_id, call.data["state"], call.data.get("reason", ""))
    except ValueError as error:
        raise ServiceValidationError(str(error)) from error


async def async_get_queue_state(call):
    from .authorization import async_require_service_admin
    from .phone_state import resolve_phone
    from homeassistant.exceptions import ServiceValidationError
    await async_require_service_admin(call.hass, call)
    manager = get_queue_manager(call.hass)
    if manager is None:
        raise ServiceValidationError("Queues are unavailable")
    phone = resolve_phone(call.hass, call.data["phone"]).endpoint_id if call.data.get("phone") else None
    try:
        return manager.get_state(call.data["queue"], phone)
    except ValueError as error:
        raise ServiceValidationError(str(error)) from error


async def async_configure_queue(call):
    from .authorization import async_require_service_admin
    from homeassistant.exceptions import ServiceValidationError
    await async_require_service_admin(call.hass, call)
    manager = get_queue_manager(call.hass)
    if manager is None:
        raise ServiceValidationError("Queues are unavailable")
    try:
        manager.configure(call.data["queue"], {key: value for key, value in call.data.items() if key != "queue"})
    except (ValueError, TypeError) as error:
        raise ServiceValidationError(str(error)) from error
