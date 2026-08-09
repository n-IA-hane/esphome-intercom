"""Shared command boundary for Home Assistant phone call services."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .call_scope import (
    call_belongs_to_endpoint,
    endpoint_call_ids,
    pending_routes,
    single_pending_route_call_id,
)
from .endpoint_lifecycle import call_registry
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationInitiator, TerminationIntent
from .service_endpoints import (
    async_require_phone_service_control,
    browser_endpoint_name,
    service_browser_endpoint,
)
from .fsm import TerminalReason
from .route_decisions import set_pending_route_decision
from .runtime_data import conference_component
from .websocket_api import _ha_softphone_store


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BrowserCallCommand:
    """Resolved browser-phone command and its authoritative call scope."""

    endpoint_id: str
    endpoint: Any
    endpoint_name: str
    device_id: str
    call_id: str
    registry: Any


def bind_service_call_controller(
    registry: Any,
    call_id: str,
    call: ServiceCall,
    *,
    endpoint_id: str = "",
) -> None:
    """Bind the initiating HA context before any call events are published."""

    try:
        registry.bind_controller(
            call_id,
            context=getattr(call, "context", None),
            endpoint_id=endpoint_id,
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err


async def async_resolve_browser_call_command(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    endpoint_id: str = "",
    endpoint=None,
) -> BrowserCallCommand:
    """Resolve, authorize and scope one browser-phone call command."""

    if not endpoint_id:
        endpoint_id, endpoint = service_browser_endpoint(hass, call, strict=True)
    await async_require_phone_service_control(hass, call, endpoint=endpoint)
    call_id = str(call.data.get("call_id") or "").strip()
    if not call_id:
        call_id = (
            single_pending_route_call_id(hass, endpoint_id)
            or str(_ha_softphone_store(hass, endpoint_id).get("call_id") or "").strip()
        )
    registry = call_registry(hass)
    if call_id and not call_belongs_to_endpoint(registry, call_id, endpoint_id):
        raise ServiceValidationError(
            f"call_id {call_id} belongs to another phone endpoint"
        )
    return BrowserCallCommand(
        endpoint_id=endpoint_id,
        endpoint=endpoint,
        endpoint_name=browser_endpoint_name(hass, endpoint_id, endpoint),
        device_id=str(getattr(endpoint, "device_id", "")),
        call_id=call_id,
        registry=registry,
    )


async def async_decline_browser_call(
    hass: HomeAssistant,
    call: ServiceCall,
    command: BrowserCallCommand,
) -> None:
    """Decline one browser-phone leg without affecting sibling fork legs."""

    endpoint_id = command.endpoint_id
    call_id = command.call_id
    registry = command.registry
    status = int(call.data.get("status") or 486)
    reason = str(call.data.get("reason") or "Busy Here").strip() or "Busy Here"
    app_reason = str(call.data.get("decline_reason") or "").strip()
    if not app_reason:
        if status == 486:
            app_reason = TerminalReason.BUSY.value
        elif status == 487:
            app_reason = TerminalReason.CANCELLED.value
        elif status == 603:
            app_reason = TerminalReason.DECLINED.value
        else:
            app_reason = reason or TerminalReason.DECLINED.value

    from .local_softphone_bridge import LocalBridgeError
    from .local_softphone_runtime import local_softphone_bridge

    local_bridge = local_softphone_bridge(hass)
    if local_bridge is not None and local_bridge.get_call(call_id) is not None:
        try:
            local_bridge.decline(call_id, endpoint_id)
        except LocalBridgeError as err:
            raise ServiceValidationError(str(err)) from err
        return
    if call_id.startswith("conference:"):
        manager = conference_component(hass)
        if manager is not None and await manager.decline_ha_softphone(
            call_id,
            endpoint_id,
            reason=app_reason,
        ):
            return
        raise ServiceValidationError(
            f"conference call {call_id} is no longer ringing on phone {endpoint_id}"
        )

    # A browser member declining a ring group rejects only that B-leg. The
    # fork controller remains authoritative until another leg wins or every
    # candidate has completed.
    if call_id and call_id in pending_routes(hass):
        set_pending_route_decision(
            hass,
            {
                "call_id": call_id,
                "action": (
                    "busy"
                    if status == 486
                    else "cancel"
                    if status == 487
                    else "decline"
                ),
                "status": status,
                "reason": reason,
                "decline_reason": app_reason,
                "endpoint_id": endpoint_id,
            },
        )
        return

    pending = dict(registry.artifact_items("pending_invite"))
    endpoint_pending = endpoint_call_ids(registry, pending, endpoint_id)
    if not call_id and len(endpoint_pending) == 1:
        call_id = endpoint_pending[0]
    preanswered_item = (
        registry.resource_for(call_id, "preanswered") if call_id else None
    )
    if preanswered_item is not None:
        final_response_sent = bool(preanswered_item.get("final_response_sent", True))
        _LOGGER.info(
            "SIP declined %s trunk call_id=%s reason=%s",
            "answered" if final_response_sent else "early-media",
            call_id,
            app_reason,
        )
        await EndpointTerminationHandler(hass).terminate(
            call_id,
            TerminationIntent.bye(app_reason)
            if final_response_sent
            else TerminationIntent.final_response(
                app_reason, status, TerminationInitiator.LOCAL_USER
            ),
        )
        return
    if (
        not call_id
        or registry.sessions.get(registry.resolve_session_id(call_id)) is None
    ):
        _LOGGER.warning("sip_decline: no pending SIP call %s", call_id or "(current)")
        return

    _LOGGER.info(
        "SIP declined call_id=%s status=%s reason=%s app_reason=%s",
        call_id,
        status,
        reason,
        app_reason,
    )
    await EndpointTerminationHandler(hass).terminate(
        call_id,
        TerminationIntent.final_response(
            app_reason, status, TerminationInitiator.LOCAL_USER
        ),
    )
