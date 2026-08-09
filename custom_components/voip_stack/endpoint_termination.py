"""Remote SIP endpoint termination with one teardown owner."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.core import HomeAssistant

from .endpoint_lifecycle import (
    call_registry,
    create_runtime_task,
    project_session_termination,
)
from .endpoint_session import (
    SipTerminationDisposition,
    TerminationInitiator,
    TerminationIntent,
)
from .runtime_data import call_runtime_artifacts
_LOGGER = logging.getLogger(__name__)

__all__ = ["EndpointTerminationHandler", "project_session_termination"]


@dataclass(slots=True)
class _EndpointTerminationHandler:
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

        claimed = self._claim(call_id, intent)
        if claimed is None:
            return False
        await self._drain(*claimed, intent)
        return True

    def request(self, call_id: str, intent: TerminationIntent) -> bool:
        """Claim immediately and let the runtime drain from a sync callback."""

        claimed = self._claim(call_id, intent)
        if claimed is None:
            return False
        create_runtime_task(self.hass, self._drain(*claimed, intent))
        return True

    def request_reason(
        self,
        call_id: str,
        reason: str,
        initiator: TerminationInitiator = TerminationInitiator.INTERNAL,
    ) -> bool:
        """Request termination without exposing intent construction to callers."""

        return self.request(
            call_id,
            TerminationIntent(reason, initiator=initiator),
        )

    def _claim(self, call_id: str, intent: TerminationIntent):
        registry = call_registry(self.hass)
        source_call_id, _ = registry.bridge_for(call_id)
        call_id = source_call_id or registry.resolve_session_id(call_id) or call_id
        if not registry.begin_termination(call_id, intent):
            _LOGGER.debug(
                "Ignoring duplicate SIP termination call_id=%s reason=%s",
                call_id,
                intent.reason,
            )
            return None
        return call_id, registry

    async def _drain(
        self,
        call_id,
        registry,
        intent: TerminationIntent,
    ) -> None:
        await registry.terminate_call_wait(call_id, intent=intent)

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


def EndpointTerminationHandler(hass: HomeAssistant) -> _EndpointTerminationHandler:
    """Return the sole termination service owned by the active PBX runtime."""

    runtime = call_runtime_artifacts(hass)
    service = getattr(runtime, "_termination_service", None)
    if not isinstance(service, _EndpointTerminationHandler):
        service = _EndpointTerminationHandler(hass)
        runtime._termination_service = service
    return service
