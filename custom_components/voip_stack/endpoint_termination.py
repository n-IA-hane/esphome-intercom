"""Remote SIP endpoint termination with one teardown owner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.core import HomeAssistant

from .call_scope import pending_routes
from .const import DOMAIN, HA_SOFTPHONE_DEVICE_ID
from .endpoint_lifecycle import call_registry
from .fsm import CallState, TerminalReason
from .phone_endpoint import DEFAULT_ENDPOINT_ID
from .runtime_data import conference_component, endpoint_directory
from .websocket_api import (
    _ha_softphone_store,
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class EndpointTerminationHandler:
    """Terminate a transport-reported call exactly once."""

    hass: HomeAssistant
    ha_peer_name: Callable[[HomeAssistant], str]

    async def handle(
        self,
        call_id: str,
        reason: str = "remote_hangup",
    ) -> None:
        """Release call media and publish its final state."""

        bucket = self.hass.data.setdefault(DOMAIN, {})
        registry = call_registry(self.hass)
        if not registry.begin_termination(call_id, reason):
            _LOGGER.debug(
                "Ignoring duplicate SIP termination call_id=%s reason=%s",
                call_id,
                reason,
            )
            return
        forward_task = bucket.setdefault("forward_tasks", {}).get(call_id)
        if forward_task is not None and forward_task is not asyncio.current_task():
            forward_task.cancel()
            await asyncio.gather(forward_task, return_exceptions=True)
        bucket.setdefault("trunk_info_queues", {}).pop(call_id, None)
        route = pending_routes(self.hass).pop(call_id, None)
        closed_calls = bucket.setdefault("trunk_closed_calls", set())
        if len(closed_calls) >= 256:
            closed_calls.pop()
        closed_calls.add(call_id)
        if route is not None:
            future = route.get("future")
            if future is not None and not future.done():
                future.set_result(
                    {
                        "action": "cancel",
                        "reason": "Request Terminated",
                        "decline_reason": (reason or TerminalReason.CANCELLED.value),
                    }
                )
        invite = registry.take_pending_invite(call_id)
        active_media = registry.softphone_media.get(call_id, {})
        active_media_invite = active_media.get("invite")
        if invite is None:
            invite = active_media_invite
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        source_call_id, dest_call_id = registry.bridge_for(call_id)
        relay = registry.relays.get(source_call_id) if source_call_id else None
        client = registry.sip_clients.get(dest_call_id) if dest_call_id else None
        if source_call_id:
            call_id = source_call_id
        event_caller = (
            invite.caller
            if invite is not None
            else (session.caller if session is not None else "")
        )
        event_callee = (
            session.callee
            if session is not None and session.callee
            else invite.target
            if invite is not None
            else ""
        )
        session_metadata = session.metadata if session is not None else {}
        session_endpoint_id = (
            str(session_metadata.get("endpoint_id") or DEFAULT_ENDPOINT_ID).strip()
            or DEFAULT_ENDPOINT_ID
        )
        session_endpoint = endpoint_directory(self.hass).get(session_endpoint_id)
        session_device_id = str(
            session_metadata.get("session_device_id")
            or getattr(session_endpoint, "device_id", "")
            or HA_SOFTPHONE_DEVICE_ID
        )
        softphone_store = _ha_softphone_store(self.hass, session_endpoint_id)
        softphone_call_id = str(softphone_store.get("call_id") or "")
        terminal_reason = reason or "remote_hangup"
        terminal_state = (
            CallState.CANCELLED.value
            if terminal_reason == TerminalReason.CANCELLED.value
            else CallState.IDLE.value
        )
        manager = conference_component(self.hass)
        if manager is not None and await manager.leave_call(
            call_id,
            reason=terminal_reason,
        ):
            await registry.finish_and_pop_wait(
                call_id,
                reason=terminal_reason,
                state=terminal_state,
            )
            return
        if relay is not None or client is not None:
            _set_sip_bridge_call_state(
                self.hass,
                terminal_state,
                call_id=call_id,
                dest_call_id=dest_call_id,
                caller=event_caller,
                callee=event_callee,
                peer_name=event_callee,
                target=event_callee,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                last_sip_event="BYE",
            )
        elif (
            relay is None
            and client is None
            and (invite is not None or (call_id and softphone_call_id == call_id))
        ):
            _set_ha_softphone_call_state(
                self.hass,
                terminal_state,
                endpoint_id=session_endpoint_id,
                session_device_id=session_device_id,
                caller=(invite.caller if invite is not None else ""),
                callee=(
                    invite.target
                    if invite is not None
                    else self.ha_peer_name(self.hass)
                ),
                peer_name=(invite.caller if invite is not None else ""),
                direction="incoming",
                call_id=call_id,
                reason=terminal_reason,
                origin="remote",
            )
        elif session is not None:
            # A caller can cancel while a router-owned fork has only early
            # outbound legs. There is then no bridge or browser media object,
            # but the logical session still owes observers one terminal event.
            _set_sip_bridge_call_state(
                self.hass,
                terminal_state,
                call_id=call_id,
                caller=event_caller,
                callee=event_callee,
                peer_name=event_callee,
                target=event_callee,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                last_sip_event=(
                    "CANCEL"
                    if terminal_reason == TerminalReason.CANCELLED.value
                    else "BYE"
                ),
                route_kind=session.route_kind,
            )
        # begin_termination makes this callback the sole teardown owner.
        # Finalize exactly once even when transport reports a call without a
        # relay, client, pending INVITE or matching browser store.
        await registry.finish_and_pop_wait(
            call_id,
            reason=terminal_reason,
            state=terminal_state,
        )
        if relay is not None or client is not None:
            _LOGGER.info(
                "SIP bridge terminated call_id=%s reason=%s relay=%s dest_client=%s",
                call_id,
                terminal_reason,
                relay is not None,
                client is not None,
            )
