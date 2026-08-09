"""Lifecycle ownership for outbound Home Assistant SIP calls."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import HA_PEER_FALLBACK_NAME
from .endpoint_lifecycle import call_registry
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationInitiator, TerminationIntent
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state,
    sip_terminal_reason,
)
from .runtime_data import call_runtime_artifacts
from .websocket_api import _ha_softphone_store, _set_ha_softphone_call_state

_LOGGER = logging.getLogger(__name__)

HA_SOFTPHONE_ACTIVE_STATES = {
    CallState.CALLING.value,
    CallState.REMOTE_RINGING.value,
    CallState.RINGING.value,
    CallState.CONNECTING.value,
    CallState.IN_CALL.value,
    CallState.TERMINATING.value,
}


def attach_outbound_connected_identity_state(
    hass: HomeAssistant,
    client,
    *,
    endpoint_id: str,
) -> None:
    """Project RFC 4916 identity updates onto the owning HA softphone."""

    call_id = str(client.dialog_ids.call_id)

    def _update(connected_party: str, connected_uri: str) -> None:
        registry = call_registry(hass)
        if registry.sip_client_for(call_id) is not client:
            return
        store = _ha_softphone_store(hass, endpoint_id)
        if (
            str(store.get("call_id") or "") != call_id
            or str(store.get("state") or "") != CallState.IN_CALL.value
        ):
            return
        session_id = registry.resolve_session_id(call_id)
        session = registry.sessions.get(session_id)
        if session is not None:
            registry.upsert(
                session_id,
                state=session.state,
                owner=session.owner,
                caller=session.caller,
                callee=session.callee,
                route_kind=session.route_kind,
                connected_party=connected_party,
                connected_uri=connected_uri,
            )
        _set_ha_softphone_call_state(
            hass,
            CallState.IN_CALL.value,
            endpoint_id=endpoint_id,
            session_device_id=str(store.get("session_device_id") or ""),
            caller=str(store.get("caller") or ""),
            callee=str(store.get("callee") or ""),
            peer_name=connected_party,
            connected_party=connected_party,
            direction=str(store.get("direction") or "outgoing"),
            call_id=call_id,
            target_device_id=str(store.get("target_device_id") or ""),
            remote_uri=connected_uri,
            last_sip_event="UPDATE",
        )

    client.on_connected_identity = _update


def _ha_peer_name(hass: HomeAssistant) -> str:
    """Return the configured Home Assistant peer name."""
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


async def async_track_outbound_sip_client(
    hass: HomeAssistant,
    *,
    client,
    result: str,
    target: str,
    sip_uri: str = "",
    endpoint_id: str,
    local_name: str = "",
    session_device_id: str = "",
    target_device_id: str = "",
    video_requested: bool = False,
    video_failure_reason: str = "",
) -> None:
    """Keep an outbound SIP client alive and complete early-dialog INVITEs."""
    registry = call_registry(hass)
    terminator = EndpointTerminationHandler(hass)
    attach_outbound_connected_identity_state(
        hass,
        client,
        endpoint_id=endpoint_id,
    )
    local_name = local_name or _ha_peer_name(hass)
    if result not in {"ringing", "in_call"}:
        public_result = sip_public_state(result)
        await terminator.terminate(
            client.dialog_ids.call_id,
            TerminationIntent(sip_terminal_reason(result, public_result)),
        )
        return

    registry.upsert(
        client.dialog_ids.call_id,
        state=CallState.REMOTE_RINGING.value
        if result == "ringing"
        else CallState.IN_CALL.value,
        owner="ha_softphone",
        caller=local_name,
        callee=target,
        route_kind="direct",
        endpoint_id=endpoint_id,
        session_device_id=session_device_id,
        target_device_id=target_device_id,
        sip_uri=sip_uri,
    )
    registry.add_leg(
        client.dialog_ids.call_id,
        client.dialog_ids.call_id,
        role="ha_softphone",
        state=result,
    )
    registry.attach_sip_client(
        client.dialog_ids.call_id,
        client.dialog_ids.call_id,
        client,
        role="ha_softphone",
        state=result,
    )

    async def _watch_sip_lifecycle() -> None:
        try:
            final = "in_call" if result == "in_call" else await client.wait_for_final()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            final = "timeout"
        except Exception as err:  # noqa: BLE001 - keep detached watcher failures contained.
            _LOGGER.warning(
                "SIP final watcher failed for call_id=%s target=%s: %s",
                client.dialog_ids.call_id,
                target,
                err,
            )
            final = "error"
        public_final = sip_public_state(final)
        connected_party = str(
            getattr(client, "connected_party", "") or target
        ).strip()
        if registry.sip_client_for(client.dialog_ids.call_id) is not client:
            # Hangup/replacement already revoked this watcher. A queued final
            # response must never resurrect a detached call in the HA store.
            return
        if public_final == CallState.IN_CALL.value and client.dialog is not None:
            video_active = bool(
                client.dialog.video_format is not None
                and client.dialog.local_video_direction != "inactive"
            )
            video_status = (
                "degraded"
                if video_failure_reason
                else "active"
                if video_active
                else "rejected"
                if video_requested
                else "inactive"
            )
            final_video_failure_reason = video_failure_reason or (
                "remote_video_rejected"
                if video_requested and not video_active
                else ""
            )
            registry.upsert(
                client.dialog_ids.call_id,
                state=CallState.IN_CALL.value,
                owner="ha_softphone",
                caller=local_name,
                callee=target,
                route_kind="direct",
                endpoint_id=endpoint_id,
            )
            registry.add_leg(
                client.dialog_ids.call_id,
                client.dialog_ids.call_id,
                role="ha_softphone",
                state=CallState.IN_CALL.value,
            )
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                endpoint_id=endpoint_id,
                session_device_id=session_device_id,
                caller=local_name,
                callee=target,
                peer_name=connected_party,
                connected_party=connected_party,
                direction="outgoing",
                call_id=client.dialog_ids.call_id,
                target_device_id=target_device_id,
                selected_tx_format=client.dialog.send_format.audio_format.wire_token(),
                selected_rx_format=client.dialog.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=client.dialog.send_format.wire_token(),
                selected_rx_rtp_format=client.dialog.recv_format.wire_token(),
                audio_direction=client.dialog.local_audio_direction,
                audio_connection_held=client.dialog.remote_audio_connection_held,
                video_active=video_active,
                video_requested=video_requested,
                video_negotiated=video_active,
                video_status=video_status,
                video_failure_reason=final_video_failure_reason,
                video_format=(
                    client.dialog.video_format.wire_token()
                    if client.dialog.video_format is not None
                    else ""
                ),
                video_send_format=(
                    client.dialog.send_video_format.wire_token()
                    if client.dialog.send_video_format is not None
                    else ""
                ),
                video_receive_format=(
                    client.dialog.recv_video_format.wire_token()
                    if client.dialog.recv_video_format is not None
                    else ""
                ),
                video_direction=client.dialog.local_video_direction,
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                sip_uri=sip_uri,
            )
        elif public_final not in {
            CallState.RINGING.value,
            CallState.IN_CALL.value,
        }:
            terminal_reason = sip_terminal_reason(final, public_final)
            await terminator.terminate(
                client.dialog_ids.call_id,
                TerminationIntent(
                    terminal_reason,
                    response_status=client.last_sip_status_code,
                ),
            )
            return
        if final == "in_call":
            try:
                terminal = await client.wait_for_dialog_termination()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep detached watcher failures contained.
                _LOGGER.warning(
                    "SIP dialog watcher failed for call_id=%s target=%s: %s",
                    client.dialog_ids.call_id,
                    target,
                    err,
                )
                terminal = "error"
            terminal_reason = (
                TerminalReason.REMOTE_HANGUP.value
                if terminal == "remote_hangup"
                else sip_terminal_reason(terminal, sip_public_state(terminal))
            )
            await terminator.terminate(
                client.dialog_ids.call_id,
                TerminationIntent(
                    terminal_reason,
                    initiator=(
                        TerminationInitiator.REMOTE_PEER
                        if terminal == "remote_hangup"
                        else TerminationInitiator.INTERNAL
                    ),
                    response_status=client.last_sip_status_code,
                ),
            )

    task = hass.async_create_task(_watch_sip_lifecycle())
    registry.attach_client_watcher(client.dialog_ids.call_id, task)


async def async_prepare_ha_outbound_call(
    hass: HomeAssistant,
    endpoint_id: str,
) -> None:
    """Close stale HA softphone SIP clients before creating a new dialog."""
    artifacts = call_runtime_artifacts(hass)
    start_lock = artifacts.softphone_start_locks.setdefault(
        endpoint_id, asyncio.Lock()
    )
    async with start_lock:
        registry = call_registry(hass)
        store = _ha_softphone_store(hass, endpoint_id)
        if str(store.get("state") or "").strip().lower() in HA_SOFTPHONE_ACTIVE_STATES:
            raise ServiceValidationError("HA softphone already has an active SIP call")

        for call_id, _client in list(registry.sip_client_items()):
            session = registry.sessions.get(registry.resolve_session_id(call_id))
            session_endpoint_id = str(
                (session.metadata if session is not None else {}).get("endpoint_id")
                or ""
            )
            if session_endpoint_id != endpoint_id:
                continue
            try:
                await EndpointTerminationHandler(hass).terminate_reason(
                    call_id,
                    TerminalReason.LOCAL_HANGUP.value,
                    TerminationInitiator.RUNTIME,
                )
            except Exception:
                _LOGGER.debug(
                    "Ignoring stale HA SIP client cleanup error",
                    exc_info=True,
                )
