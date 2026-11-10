"""Queue waiting policy attached to the shared call/media/forward owners."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from typing import ClassVar

from .call_projection import publish_bridge_projection
from .call_queues import get_queue_manager
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage, TerminationIntent
from .local_application import LocalCallApplication, attach_local_application
from .roster import RosterEntry
from .router import RouteAction, RouteDecision


@dataclass(slots=True)
class QueueCall(LocalCallApplication):
    owner: ClassVar[str] = "queue"
    ticket: object = None
    manager: object = None
    waiter: asyncio.Task | None = None
    music: asyncio.Task | None = None
    detached: bool = False
    policy_snapshot: object = None

    def policy(self):
        try:
            return self.manager.policy(self.ticket.queue_id)
        except ValueError:
            return getattr(self.ticket, "policy", None) or self.policy_snapshot

    async def stop_music(self):
        if self.music is not None:
            task, self.music = self.music, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.media is not None:
            self.media.discard_output()

    def start_music(self):
        policy = self.policy()
        if not policy.hold_media or self.music is not None or self.media is None:
            return

        async def play():
            from .announcement_media import async_audio_stream
            while self.session.live and not self.detached:
                async with aclosing(async_audio_stream(self.hass, policy.hold_media)) as stream:
                    await self.media.play_pcm_stream(stream)
                await self.media.tx_queue.join()
        self.music = self.session.create_task(play(), name="queue-hold-media")

    async def release_media_for_forward(self):
        await self.stop_music()
        await super().release_media_for_forward()

    async def resume(self):
        if not self.session.live or self.detached:
            return
        if self.answered and self.media is None:
            await self.answer()
        self.start_music()
        self.ticket.changed.set()
        publish_bridge_projection(self.hass, self.session, direction="incoming",
                                  event_type="state_changed", route_kind="queue", queue_id=self.ticket.queue_id)

    async def close(self, reason):
        await self.stop_music()
        if reason == "forwarded":
            self.detached = True
            winner = str(self.session.metadata.get("dest_endpoint_id") or self.session.metadata.get("endpoint_id") or "")
            if winner not in self.ticket.reserved:
                winner = ""
            queue_id = self.ticket.queue_id
            wrap_duration = (getattr(self.ticket, "policy", None) or self.policy_snapshot).wrap_up
            self.manager.finish(self.ticket)
            if winner:
                def begin_wrap_up(_reason):
                    self.manager.start_wrap_up(queue_id, winner, duration=wrap_duration)
                call_registry(self.hass).own_resource(
                    self.session.call_id, f"queue_wrap_up:{self.session.call_id}", winner, begin_wrap_up,
                    stage=CleanupStage.OBSERVER, generation=self.session.generation)
            self.ticket.changed.set()
            return
        self.manager.finish(self.ticket)

    async def _fallback(self, policy, reason):
        self.manager.finish(self.ticket)
        if policy.fallback and self.session.live:
            await call_registry(self.hass).forward_call(call_id=self.session.call_id,
                destination=policy.fallback, on_failure="terminate")
        elif self.session.live:
            await call_registry(self.hass).terminate_call_wait(self.session.call_id, reason=reason)

    async def run(self):
        registry = call_registry(self.hass)
        no_agent_since = None
        try:
            from .runtime_data import call_runtime_artifacts
            installing = call_runtime_artifacts(self.hass).task_for(self.session.call_id, "forward")
            if installing is not None and installing is not asyncio.current_task():
                await asyncio.shield(installing)
            await self.answer()
            self.start_music()
            while self.session.live and not self.detached:
                definition = self.manager.resolve(self.ticket.queue_id)
                policy = self.policy()
                if self.manager.closed:
                    await registry.terminate_call_wait(self.session.call_id,
                        intent=TerminationIntent("runtime_shutdown", public_state="idle"))
                    return
                if definition is None:
                    await self._fallback(policy, "queue_removed")
                    return
                now = self.manager.clock()
                if not policy.enabled or now >= self.ticket.deadline:
                    await self._fallback(policy, "queue_disabled" if not policy.enabled else "queue_timeout")
                    return
                state = self.manager.get_state(definition.queue_id)
                online_agents = any(member["state"] == "available" and member["online"] for member in state["members"])
                if online_agents:
                    no_agent_since = None
                elif no_agent_since is None:
                    no_agent_since = now
                if no_agent_since is not None and now - no_agent_since >= policy.no_agent_wait:
                    await self._fallback(policy, "queue_no_agents")
                    return
                members = self.manager.claim_turn(self.ticket)
                if members:
                    endpoints = [self.manager.endpoints.get(identity) for identity in members]
                    entry = RosterEntry(id=f"queue-attempt:{definition.queue_id}", name=definition.name,
                        ha_bridge=True, metadata={"group_type": "ring", "members": [endpoint.name for endpoint in endpoints if endpoint]})
                    decision = RouteDecision(RouteAction.GROUP, target=definition.name, entry=entry)
                    try:
                        await registry.forward_call(call_id=self.session.call_id, destination=definition.name,
                            on_failure="resume", _resolved_decision=decision,
                            _ring_timeout=min(policy.ring_timeout, self.ticket.deadline - now))
                        from .runtime_data import call_runtime_artifacts
                        task = call_runtime_artifacts(self.hass).task_for(self.session.call_id, "forward")
                        if task is not None:
                            await asyncio.shield(task)
                    finally:
                        self.manager.release_attempt(self.ticket)
                    if self.detached or not self.session.live:
                        return
                self.ticket.changed.clear()
                remaining = self.ticket.deadline - self.manager.clock()
                if no_agent_since is not None:
                    remaining = min(remaining, policy.no_agent_wait - (self.manager.clock() - no_agent_since))
                try:
                    await asyncio.wait_for(self.ticket.changed.wait(), timeout=max(0.01, min(policy.retry_delay, remaining)))
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.session.live:
                await registry.terminate_call_wait(self.session.call_id, reason="queue_failed")
            raise
        finally:
            if not self.detached:
                self.manager.finish(self.ticket)


async def route_queue_call(runtime, invite, decision, registry, source_endpoint):
    """Admit a queue ticket before answering through the existing local owner."""
    from .inbound_routing.local import _claim_source
    from .sip_listener import SipInviteResult
    manager = get_queue_manager(runtime.hass)
    queue = manager.resolve(decision.entry.metadata["queue_id"]) if manager is not None else None
    if queue is None:
        return SipInviteResult(404, "Not Found", to_tag="")
    if busy := await _claim_source(hass=runtime.hass, registry=registry, invite=invite,
                                   source_endpoint=source_endpoint, state="ringing", route_kind="queue"):
        return busy
    session = registry.upsert(invite.call_id, state="ringing", owner="queue", caller=invite.caller,
                             callee=queue.name, route_kind="queue")
    try:
        start_queue_application(runtime.hass, session, invite, decision.entry, runtime.local_ip)
    except ValueError:
        return SipInviteResult(486, "Busy Here", to_tag="")
    return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)


def start_queue_application(hass, session, invite, contact, local_ip, *, previous=None, local_source=None):
    """Enter a queue through inbound routing or the same forwarding dispatcher."""
    registry = call_registry(hass)
    manager = get_queue_manager(hass)
    queue_id = contact.metadata["queue_id"]
    ticket = manager.enqueue(queue_id, session.call_id, session.generation)
    session = registry.transition(session.call_id, owner="queue", state="in_call" if previous and previous.answered else "ringing",
                                  callee=contact.display_name, route_kind="queue")
    registry.set_pending_invite(invite.call_id, invite)
    application = attach_local_application(QueueCall, hass, session, invite, contact, local_ip,
                                           previous=previous, local_source=local_source, ticket=ticket, manager=manager,
                                           policy_snapshot=manager.policy(queue_id))
    application.waiter = session.create_task(application.run(), name="queue-waiter")
    return application
