"""Home Assistant browser-phone termination orchestration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .call_scope import endpoint_call_ids, pending_routes, take_pending_route
from .endpoint_lifecycle import call_registry
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationInitiator, TerminationIntent
from .fsm import TerminalReason
from .route_decisions import set_pending_route_decision
from .softphone_commands import BrowserCallCommand
from .websocket_api import _ha_softphone_store


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
    registry = call_registry(hass)
    source_call_id, dest_call_id = registry.bridge_for(call_id)
    if not source_call_id:
        return False, "", "", False, False
    client_present = bool(dest_call_id and registry.sip_clients.get(dest_call_id))
    terminated = await EndpointTerminationHandler(hass).terminate(
        source_call_id,
        TerminationIntent.bye(terminal_reason),
    )
    return True, source_call_id, dest_call_id, client_present, terminated


async def async_hangup_browser_call(
    hass: HomeAssistant,
    command: BrowserCallCommand,
) -> None:
    """Hang up exactly one browser phone's call and release its resources."""

    endpoint_id = command.endpoint_id
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

    pending_ids = (
        [call_id]
        if call_id and call_id in pending
        else ([] if call_id else endpoint_pending)
    )
    terminated_ids: set[str] = set()
    terminator = EndpointTerminationHandler(hass)
    for pending_call_id in pending_ids:
        invite = pending.get(pending_call_id)
        if invite is None:
            continue
        preanswered_item = registry.preanswered.get(pending_call_id)
        await terminator.terminate(
            pending_call_id,
            (
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
        await terminator.terminate(
            call_id,
            TerminationIntent(
                TerminalReason.LOCAL_HANGUP.value,
                initiator=TerminationInitiator.LOCAL_USER,
            ),
        )
