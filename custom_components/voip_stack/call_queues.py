"""Queues derived from configured phones, with persistent agent intent.

Membership belongs to phones. This owner only stores queue policy, operator
availability and bounded waiting tickets; it never owns another SIP lifecycle.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field
import time
from typing import Callable

from .phone_endpoint import EndpointAvailability
from .roster import RosterEntry, normalize_roster_key

EVENT_QUEUE_CHANGED = "voip_stack.queue_changed"
AGENT_STATES = frozenset({"available", "paused", "logged_out"})


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

    @classmethod
    def from_data(cls, data):
        values = dict(data or {})
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown queue setting")
        policy = cls(**values)
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


class QueueManager:
    """One entry-owned policy and admission coordinator on the HA loop."""

    def __init__(self, hass, entry, *, store=None, clock=time.monotonic):
        self.hass, self.entry, self.store, self.clock = hass, entry, store, clock
        self.endpoints = entry.runtime_data.endpoints
        self.definitions: dict[str, QueueDefinition] = {}
        self.policies: dict[str, QueuePolicy] = {}
        self.agent_states: dict[str, dict[str, str]] = {}
        self.waiting: dict[str, deque[QueueTicket]] = {}
        self.reservations: dict[str, QueueTicket] = {}
        self.wrap_until: dict[str, float] = {}
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
        if self.hass is not None:
            from .group_config import group_subentries
            for item in group_subentries(self.entry):
                if item.data.get("group_id") == queue.queue_id:
                    self.hass.config_entries.async_update_subentry(self.entry, item,
                        data={**item.data, "policy": asdict(updated), "enabled": updated.enabled})
                    break
        self.policies[queue.queue_id] = updated
        self._changed(persist=True)

    def set_agent_state(self, ref, endpoint_id, state):
        queue = self.resolve(ref)
        if queue is None or endpoint_id not in queue.members:
            raise ValueError("The phone does not declare this queue")
        if state not in AGENT_STATES:
            raise ValueError("Agent state must be available, paused or logged_out")
        if self.agent_states[queue.queue_id][endpoint_id] == state:
            return
        self.agent_states[queue.queue_id][endpoint_id] = state
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
            and (not endpoint.active_call_id or endpoint.active_call_id == call_id))

    def enqueue(self, ref, call_id, generation) -> QueueTicket:
        queue = self.resolve(ref)
        policy = self.policy(ref)
        if self.closed or not policy.enabled:
            raise ValueError("The queue is disabled")
        pending = self.waiting.setdefault(queue.queue_id, deque())
        if len(pending) >= policy.max_waiting:
            raise ValueError("The queue is full")
        if any(ticket.call_id == call_id for rows in self.waiting.values() for ticket in rows):
            raise ValueError("The call already has a queue ticket")
        now = self.clock()
        ticket = QueueTicket(queue.queue_id, call_id, generation, now, now + policy.max_wait, policy=policy)
        pending.append(ticket)
        self._changed()
        return ticket

    def claim_turn(self, ticket) -> tuple[str, ...]:
        """The FIFO head atomically reserves agents across all queues."""
        pending = self.waiting.get(ticket.queue_id, ())
        if (ticket.closed or self.resolve(ticket.queue_id) is None or not pending or pending[0] is not ticket
                or ticket.deadline <= self.clock()):
            return ()
        if ticket.reserved:
            return ticket.reserved
        members = self.eligible(ticket.queue_id, call_id=ticket.call_id)
        for member in members:
            self.reservations[member] = ticket
        ticket.reserved = members
        return members

    def release_attempt(self, ticket):
        for member in ticket.reserved:
            if self.reservations.get(member) is ticket:
                self.reservations.pop(member)
        ticket.reserved = ()
        self._changed()

    def finish(self, ticket, *, connected_endpoint=""):
        if ticket.closed:
            return
        ticket.closed = True
        pending = self.waiting.get(ticket.queue_id)
        if pending is not None and ticket in pending:
            pending.remove(ticket)
            if not pending:
                self.waiting.pop(ticket.queue_id, None)
        self.release_attempt(ticket)
        # Wrap-up starts on call completion, through the existing session owner.
        if connected_endpoint:
            self.start_wrap_up(ticket.queue_id, connected_endpoint, duration=ticket.policy.wrap_up)
        self._changed()

    def start_wrap_up(self, queue_id, endpoint_id, *, duration=None):
        if duration is None:
            duration = self.policies.get(queue_id, QueuePolicy()).wrap_up
        if duration > 0 and not self.closed:
            self.wrap_until[endpoint_id] = max(self.wrap_until.get(endpoint_id, 0), self.clock() + duration)
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
                "agent_states": {key: dict(value) for key, value in self.agent_states.items()}}

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
                "online": bool(endpoint and endpoint.availability is EndpointAvailability.AVAILABLE)})
        return {"queue_id": queue.queue_id, "name": queue.name,
                "policy": asdict(self.policies[queue.queue_id]),
                "waiting": len(self.waiting.get(queue.queue_id, ())), "members": members}

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
    data = await store.async_load() or {}
    previous_ids = set(group_records(entry))
    # A saved form command can survive a restart before the update listener ran.
    await async_apply_group_updates(hass, entry)
    for item in group_subentries(entry):
        identity = item.data["group_id"]
        legacy = normalize_roster_key(item.data["name"])
        if (item.data["kind"] == "queue" and identity not in previous_ids
                and legacy in data.get("policies", {})):
            try:
                policy = QueuePolicy.from_data(data["policies"][legacy])
            except (ValueError, TypeError):
                continue
            hass.config_entries.async_update_subentry(entry, item,
                data={**item.data, "policy": asdict(policy), "enabled": policy.enabled})
    manager = QueueManager(hass, entry, store=store)
    for key, value in data.get("policies", {}).items():
        try:
            manager.policies[key] = QueuePolicy.from_data(value)
        except (ValueError, TypeError):
            continue
    manager.agent_states = data.get("agent_states", {})
    for identity, record in group_records(entry, "queue").items():
        legacy = normalize_roster_key(record["name"])
        if identity not in manager.agent_states and legacy in manager.agent_states:
            manager.agent_states[identity] = dict(manager.agent_states[legacy])
    entry.runtime_data.queues = manager
    manager.refresh()
    manager._unsub = entry.runtime_data.endpoints.subscribe(lambda _event: (manager.refresh(), manager._changed()))
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
        manager.set_agent_state(call.data["queue"], endpoint.endpoint_id, call.data["state"])
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
