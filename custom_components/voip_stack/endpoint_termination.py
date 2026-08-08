"""Remote SIP endpoint termination with one teardown owner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from homeassistant.core import HomeAssistant

from .call_scope import take_pending_route
from .endpoint_lifecycle import call_registry, project_session_termination
from .endpoint_session import (
    SipTerminationDisposition,
    TerminationInitiator,
    TerminationIntent,
)
from .runtime_data import (
    call_runtime_artifacts,
    conference_component,
)
_LOGGER = logging.getLogger(__name__)

__all__ = ["EndpointTerminationHandler", "project_session_termination"]


@dataclass(slots=True)
class EndpointTerminationHandler:
    """Terminate one call generation through its sole cleanup owner."""

    hass: HomeAssistant

    async def handle(
        self,
        call_id: str,
        reason: str = "remote_hangup",
    ) -> None:
        """Translate transport termination into the shared terminal path."""

        await self.terminate(
            call_id,
            TerminationIntent(
                reason,
                initiator=TerminationInitiator.REMOTE_PEER,
                sip_disposition=SipTerminationDisposition.NONE,
            ),
        )

    async def terminate(
        self,
        call_id: str,
        intent: TerminationIntent,
    ) -> bool:
        """Claim, signal, drain and project one call exactly once."""

        artifacts = call_runtime_artifacts(self.hass)
        registry = call_registry(self.hass)
        source_call_id, _ = registry.bridge_for(call_id)
        call_id = source_call_id or registry.resolve_session_id(call_id) or call_id
        call_artifacts = artifacts.artifacts_for(call_id)
        if call_artifacts is not None and call_artifacts.trunk_info_queue is not None:
            call_artifacts.trunk_closed = True
        if not registry.begin_termination(call_id, intent):
            _LOGGER.debug(
                "Ignoring duplicate SIP termination call_id=%s reason=%s",
                call_id,
                intent.reason,
            )
            return False
        forward_task = artifacts.task_for(call_id, "forward")
        if forward_task is not None and forward_task is not asyncio.current_task():
            forward_task.cancel()
            await asyncio.gather(forward_task, return_exceptions=True)
        if call_artifacts is not None:
            call_artifacts.trunk_info_queue = None
        route = take_pending_route(self.hass, call_id)
        if route is not None:
            future = route.get("future")
            if future is not None and not future.done():
                future.set_result(
                    {
                        "action": "cancel",
                        "reason": "Request Terminated",
                        "decline_reason": intent.reason,
                    }
                )
        manager = conference_component(self.hass)
        if manager is not None:
            resolved = manager.resolve_ha_call(call_id)
            if resolved is not None:
                await manager.leave_ha_softphone(
                    resolved[0],
                    call_id=call_id,
                    reason=intent.reason,
                )
            else:
                await manager.leave_call(call_id, reason=intent.reason)
        await registry.terminate_call_wait(call_id, intent=intent)
        return True

    async def terminate_reason(
        self,
        call_id: str,
        reason: str,
        initiator: TerminationInitiator = TerminationInitiator.INTERNAL,
    ) -> bool:
        """Terminate from a reason when no special SIP disposition is needed."""

        return await self.terminate(
            call_id,
            TerminationIntent(reason, initiator=initiator),
        )
