"""Home Assistant browser-phone termination orchestration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .call_scope import endpoint_call_ids, pending_routes, take_pending_route
from .endpoint_lifecycle import call_registry
from .endpoint_session import TerminationInitiator, TerminationIntent
from .fsm import CallState, TerminalReason
from .route_decisions import set_pending_route_decision
from .runtime_data import call_runtime_artifacts, conference_component
from .softphone_commands import BrowserCallCommand
from .websocket_api import (
    _ha_softphone_store,
    _set_ha_softphone_call_state,
)


_LOGGER = logging.getLogger(__name__)


async def async_terminate_sip_bridge_session(
    hass: HomeAssistant,
    call_id: str,
    *,
    endpoint_id: str = "",
    session_device_id: str = "",
    terminal_reason: str = TerminalReason.LOCAL_HANGUP.value,
) -> tuple[bool, str, str, bool, bool]:
    """Terminate one B2BUA bridge through the authoritative session owner."""

    del endpoint_id, session_device_id
    return await call_registry(hass).terminate_bridge_wait(
        call_id,
        TerminationIntent.bye(terminal_reason),
    )


async def async_hangup_browser_call(
    hass: HomeAssistant,
    command: BrowserCallCommand,
) -> None:
    """Hang up exactly one browser phone's call and release its resources."""

    endpoint_id = command.endpoint_id
    endpoint_device_id = command.device_id
    call_id = command.call_id
    registry = command.registry

    from .local_softphone_bridge import LocalBridgeError
    from .local_softphone_runtime import local_softphone_bridge

    local_bridge = local_softphone_bridge(hass)
    if local_bridge is not None and local_bridge.get_call(call_id) is not None:
        try:
            local_bridge.hangup(call_id, endpoint_id)
        except LocalBridgeError as err:
            raise ServiceValidationError(str(err)) from err
        return

    forward_task = call_runtime_artifacts(hass).task_for(call_id, "forward")
    if forward_task is not None and not forward_task.done():
        forward_task.cancel()
        await asyncio.gather(forward_task, return_exceptions=True)
    routes = pending_routes(hass)
    if call_id and call_id in routes:
        future = routes[call_id].get("future")
        if future is not None and future.done():
            take_pending_route(hass, call_id)
        else:
            set_pending_route_decision(
                hass,
                {
                    "call_id": call_id,
                    "action": "cancel",
                    "reason": "Request Terminated",
                    "decline_reason": TerminalReason.LOCAL_HANGUP.value,
                    "endpoint_id": endpoint_id,
                },
            )
            return

    clients = registry.sip_clients
    pending = registry.pending_invites
    media_sessions = registry.softphone_media
    softphone_store = _ha_softphone_store(hass, endpoint_id)
    endpoint_bridge_calls = endpoint_call_ids(
        registry,
        registry.bridge_clients,
        endpoint_id,
    )
    endpoint_clients = endpoint_call_ids(registry, clients, endpoint_id)
    endpoint_pending = endpoint_call_ids(registry, pending, endpoint_id)
    endpoint_media = endpoint_call_ids(registry, media_sessions, endpoint_id)
    if not call_id and len(endpoint_bridge_calls) == 1:
        call_id = endpoint_bridge_calls[0]
    if not call_id and len(endpoint_clients) == 1:
        call_id = endpoint_clients[0]
    if not call_id and len(endpoint_pending) == 1:
        call_id = endpoint_pending[0]
    if not call_id and len(endpoint_media) == 1:
        call_id = endpoint_media[0]
    if not call_id:
        call_id = str(softphone_store.get("call_id") or "").strip()

    active_session = (
        registry.sessions.get(registry.resolve_session_id(call_id)) if call_id else None
    )
    caller = str(
        (active_session.caller if active_session is not None else "")
        or softphone_store.get("caller")
        or softphone_store.get("last_terminal_caller")
        or ""
    )
    callee = str(
        (active_session.callee if active_session is not None else "")
        or softphone_store.get("callee")
        or softphone_store.get("last_terminal_callee")
        or ""
    )
    peer_name = str(
        callee
        or softphone_store.get("peer_name")
        or softphone_store.get("last_terminal_peer_name")
        or ""
    )
    direction = str(
        softphone_store.get("direction")
        or softphone_store.get("last_terminal_direction")
        or ("incoming" if active_session is not None else "")
        or ""
    )

    (
        bridge_handled,
        bridge_source_call_id,
        bridge_dest_call_id,
        bridge_client,
        bridge_server_bye,
    ) = await async_terminate_sip_bridge_session(
        hass,
        call_id,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
    )
    if bridge_handled:
        _LOGGER.info(
            "SIP bridge hangup call_id=%s dest_call_id=%s client=%s server_bye=%s",
            bridge_source_call_id,
            bridge_dest_call_id,
            bridge_client,
            bridge_server_bye,
        )
        return

    client = clients.get(call_id) if call_id else None
    relay = registry.relays.get(call_id) if call_id else None
    media_session = media_sessions.get(call_id) if call_id else None
    conference_room = str((media_session or {}).get("conference_room") or "")
    if conference_room:
        manager = conference_component(hass)
        if manager is not None:
            await manager.leave_ha_softphone(
                conference_room,
                call_id=call_id,
                reason=TerminalReason.LOCAL_HANGUP.value,
            )

    pending_ids = (
        [call_id]
        if call_id and call_id in pending
        else ([] if call_id else endpoint_pending)
    )
    server_bye = False
    pending_closed = 0
    terminated_ids: set[str] = set()
    for pending_call_id in pending_ids:
        invite = pending.get(pending_call_id)
        if invite is None:
            continue
        preanswered_item = registry.preanswered.get(pending_call_id)
        pending_closed += 1
        _set_ha_softphone_call_state(
            hass,
            CallState.IDLE.value,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            caller=invite.caller,
            callee=invite.target,
            peer_name=invite.caller,
            direction="incoming",
            call_id=pending_call_id,
            reason=TerminalReason.LOCAL_HANGUP.value,
            origin="self",
            sip_status_code=487,
            last_sip_event="SIP_RESPONSE",
        )
        await registry.terminate_call_wait(
            pending_call_id,
            intent=(
                TerminationIntent.bye(TerminalReason.LOCAL_HANGUP.value)
                if preanswered_item is not None
                else TerminationIntent.final_response(
                    TerminalReason.LOCAL_HANGUP.value,
                    487,
                    TerminationInitiator.LOCAL_USER,
                )
            ),
        )
        terminated_ids.add(pending_call_id)

    if call_id and call_id not in terminated_ids:
        server_bye = client is None and relay is None
        await registry.terminate_call_wait(
            call_id,
            intent=TerminationIntent(
                TerminalReason.LOCAL_HANGUP.value,
                initiator=TerminationInitiator.LOCAL_USER,
            ),
        )
    _set_ha_softphone_call_state(
        hass,
        CallState.IDLE.value,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
        caller=caller,
        callee=callee,
        peer_name=peer_name,
        direction=direction,
        call_id=call_id,
        reason=TerminalReason.LOCAL_HANGUP.value,
        origin="self",
        last_sip_event=(
            "SIP_BYE"
            if client is not None or relay is not None or server_bye
            else "SIP_HANGUP"
        ),
        pending_closed=pending_closed,
    )
    _LOGGER.info(
        "SIP hangup call_id=%s client=%s relay=%s pending_closed=%d server_bye=%s",
        call_id,
        client is not None,
        relay is not None,
        pending_closed,
        server_bye,
    )
