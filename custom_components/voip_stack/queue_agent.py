"""Authenticated self-service queue keys and their existing BLF projection."""

from dataclasses import dataclass
from typing import ClassVar

from .local_application import LocalCallApplication, attach_local_application
from .roster import RosterEntry


def queue_agent_address(hass, target, endpoint_id):
    from .call_queues import get_queue_manager
    manager = get_queue_manager(hass)
    if manager is None:
        return None
    for prefix, control in (("queue-login+", "login"), ("queue-pause+", "pause")):
        if not str(target).startswith(prefix):
            continue
        queue = manager.resolve(str(target)[len(prefix):])
        if queue is None or endpoint_id not in queue.members:
            return None
        return queue.queue_id, endpoint_id, control
    return None


def queue_agent_resource(address):
    queue_id, endpoint_id, control = address
    return f"queue-agent:{queue_id}:{control}:{endpoint_id}"


def queue_agent_snapshot(manager, address):
    from .dialog_info import DialogState
    queue_id, endpoint_id, control = address
    queue = manager.resolve(queue_id) if manager is not None else None
    if queue is None or endpoint_id not in queue.members:
        return None
    state = manager.agent_states[queue_id][endpoint_id]
    active = state != "logged_out" if control == "login" else state == "paused"
    return (DialogState(queue_agent_resource(address), "confirmed", "recipient"),) if active else ()


def parse_queue_agent_resource(value):
    if not str(value).startswith("queue-agent:"):
        return None
    parts = str(value).split(":", 3)
    if len(parts) != 4 or parts[2] not in {"login", "pause"}:
        return None
    return parts[1], parts[3], parts[2]


@dataclass(slots=True)
class QueueAgentCall(LocalCallApplication):
    owner: ClassVar[str] = "queue_agent"
    address: tuple = ()

    async def close(self, _reason):
        return None

    async def run(self):
        from .call_queues import get_queue_manager
        from .endpoint_lifecycle import call_registry
        from .endpoint_session import TerminationIntent
        registry = call_registry(self.hass)
        try:
            await self.answer()
            manager = get_queue_manager(self.hass)
            queue_id, endpoint_id, control = self.address
            previous = manager.agent_states[queue_id][endpoint_id]
            state = ("available" if previous == "logged_out" else "logged_out") if control == "login" else (
                "available" if previous == "paused" else "paused")
            manager.set_agent_state(queue_id, endpoint_id, state)
        finally:
            registry.request_termination(self.session.call_id, TerminationIntent("queue_agent_complete"),
                                         generation=self.session.generation)


async def route_queue_agent_call(runtime, invite, registry, source_endpoint):
    """Use the authenticated phone, never a caller-supplied operator identity."""
    from .sip_listener import SipInviteResult
    from .inbound_routing.local import _claim_source
    if source_endpoint is None:
        return SipInviteResult(403, "Forbidden")
    address = queue_agent_address(runtime.hass, invite.routing_target, source_endpoint.endpoint_id)
    if address is None:
        return SipInviteResult(404, "Not Found")
    if busy := await _claim_source(hass=runtime.hass, registry=registry, invite=invite,
                                  source_endpoint=source_endpoint, state="ringing", route_kind="queue_agent"):
        return busy
    session = registry.upsert(invite.call_id, state="ringing", owner="queue_agent",
                              caller=invite.caller, callee="Queue membership", route_kind="queue_agent")
    registry.set_pending_invite(invite.call_id, invite)
    application = attach_local_application(QueueAgentCall, runtime.hass, session, invite,
        RosterEntry("queue_agent", name="Queue membership"), runtime.local_ip, address=address)
    session.create_task(application.run(), name="queue-agent-control")
    return SipInviteResult(180, "Ringing", defer_final=True)
