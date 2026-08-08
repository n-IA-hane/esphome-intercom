"""Home Assistant browser-phone termination orchestration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .bridge_manager import async_terminate_sip_bridge
from .call_scope import endpoint_call_ids, pending_routes, take_pending_route
from .fsm import CallState, TerminalReason, sip_public_state
from .media_ports import release_media_reservation
from .route_decisions import set_pending_route_decision
from .runtime_data import call_runtime_artifacts, conference_component
from .session_cleanup import async_cleanup_sip_runtime
from .sip_runtime import send_bye, send_final_response, sip_servers
from .softphone_commands import BrowserCallCommand
from .websocket_api import (
    _ha_softphone_store,
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
    _sip_bridge_store,
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
    """Terminate one B2BUA bridge and publish its terminal projections."""

    softphone = _ha_softphone_store(hass, endpoint_id) if endpoint_id else {}
    softphone_call_id = str(softphone.get("call_id") or "")
    bridge = dict(_sip_bridge_store(hass))
    result = await async_terminate_sip_bridge(
        hass,
        call_id,
        terminal_reason=terminal_reason,
        send_bye=lambda source_call_id: send_bye(hass, source_call_id),
    )
    handled, source_call_id, dest_call_id, _client_closed, _source_bye = result
    projected_call_ids = {
        str(bridge.get("call_id") or ""),
        str(bridge.get("dest_call_id") or ""),
    }
    if handled and projected_call_ids.intersection({source_call_id, dest_call_id}):
        reason = terminal_reason or TerminalReason.LOCAL_HANGUP.value
        reserved = {
            "state",
            "sip_state",
            "call_id",
            "dest_call_id",
            "caller",
            "callee",
            "peer_name",
            "target",
            "reason",
            "terminal_reason",
            "origin",
            "last_sip_event",
            "last_terminal_call_id",
            "last_terminal_dest_call_id",
        }
        extra = {
            key: value
            for key, value in bridge.items()
            if key not in reserved and value not in (None, "")
        }
        _set_sip_bridge_call_state(
            hass,
            sip_public_state(reason),
            call_id=source_call_id,
            dest_call_id=dest_call_id,
            caller=str(bridge.get("caller") or ""),
            callee=str(bridge.get("callee") or ""),
            peer_name=str(bridge.get("peer_name") or ""),
            target=str(bridge.get("target") or ""),
            reason=reason,
            terminal_reason=reason,
            origin=(
                "self"
                if reason == TerminalReason.LOCAL_HANGUP.value
                else "remote"
            ),
            last_sip_event=(
                "SIP_BYE"
                if reason
                in {
                    TerminalReason.LOCAL_HANGUP.value,
                    TerminalReason.REMOTE_HANGUP.value,
                }
                else str(bridge.get("last_sip_event") or "SIP_TERMINATED")
            ),
            **extra,
        )
    if handled and source_call_id == softphone_call_id:
        reason = terminal_reason or TerminalReason.LOCAL_HANGUP.value
        _set_ha_softphone_call_state(
            hass,
            CallState.IDLE.value,
            endpoint_id=endpoint_id,
            session_device_id=session_device_id,
            caller=str(softphone.get("caller") or ""),
            callee=str(softphone.get("callee") or ""),
            peer_name=str(softphone.get("peer_name") or ""),
            direction=str(softphone.get("direction") or ""),
            call_id=source_call_id,
            reason=reason,
            terminal_reason=reason,
            origin="self" if reason == TerminalReason.LOCAL_HANGUP.value else "remote",
            last_sip_event="SIP_BYE",
        )
    return result


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

    client, watcher = registry.detach_client(call_id) if call_id else (None, None)
    if client is not None and watcher is None and client.dialog is None:
        # The initial INVITE coroutine is the only reader for its transaction.
        # Ask it to emit CANCEL when RFC 3261 permits instead of racing it.
        client.request_cancel()
        client = None
    relay = registry.take_relay(call_id) if call_id else None
    media_session = media_sessions.pop(call_id, None) if call_id else None
    release_media_reservation(media_session)
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
    await async_cleanup_sip_runtime(
        relay=relay,
        client=client,
        watcher=watcher,
        terminate_client=True,
        relay_first=False,
    )
    for pending_call_id in pending_ids:
        invite = registry.take_pending_invite(pending_call_id)
        if invite is None:
            continue
        preanswered_item = registry.take_media(pending_call_id, provisional=True)
        if preanswered_item is not None:
            release_media_reservation(preanswered_item)
            if send_bye(hass, pending_call_id):
                pending_closed += 1
        elif send_final_response(
            hass,
            pending_call_id,
            487,
            "Request Terminated",
            decline_reason=TerminalReason.LOCAL_HANGUP.value,
        ):
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
        registry.terminate_call(
            pending_call_id,
            reason=TerminalReason.LOCAL_HANGUP.value,
        )
    if client is None and relay is None:
        for server in sip_servers(hass):
            server_send_bye = getattr(server, "send_bye", None)
            if callable(server_send_bye) and server_send_bye(call_id):
                server_bye = True
                if not call_id:
                    call_id = "(active)"
                break

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
    if call_id:
        registry.terminate_call(
            call_id,
            reason=TerminalReason.LOCAL_HANGUP.value,
        )
    _LOGGER.info(
        "SIP hangup call_id=%s client=%s relay=%s pending_closed=%d server_bye=%s",
        call_id,
        client is not None,
        relay is not None,
        pending_closed,
        server_bye,
    )
