"""Home Assistant service boundary for forwarding one owned SIP call."""

from __future__ import annotations

import asyncio

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .automation_routing import resolve_forward_call_id
from .call_scope import call_belongs_to_endpoint, pending_routes
from .runtime_data import call_runtime_artifacts
from .endpoint_lifecycle import call_registry
from .route_decisions import set_pending_route_decision
from .service_endpoints import (
    async_require_phone_service_control,
    service_browser_endpoint,
)


async def async_forward_browser_call(call: ServiceCall) -> None:
    """Forward one browser-owned call through the canonical PBX dispatcher."""

    hass = call.hass
    data = dict(call.data)
    registry = call_registry(hass)
    endpoint_id, endpoint = service_browser_endpoint(hass, call, strict=True)
    await async_require_phone_service_control(hass, call, endpoint=endpoint)
    owned_routes = {
        call_id: route
        for call_id, route in registry.pending_routes.items()
        if call_belongs_to_endpoint(registry, call_id, endpoint_id)
    }
    owned_invites = {
        call_id: invite
        for call_id, invite in registry.pending_invites.items()
        if call_belongs_to_endpoint(registry, call_id, endpoint_id)
    }
    try:
        selected_call_id = resolve_forward_call_id(
            str(data.get("call_id") or ""),
            owned_routes,
            owned_invites,
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    if not call_belongs_to_endpoint(registry, selected_call_id, endpoint_id):
        raise ServiceValidationError(
            f"call_id {selected_call_id} belongs to another phone endpoint"
        )
    if not data.get("call_id"):
        context = registry.event_context(selected_call_id)
        data["call_id"] = selected_call_id
        if context is not None:
            data.setdefault("expected_state", context.state)
            data.setdefault("expected_sequence", context.sequence)

    routes = pending_routes(hass)
    if selected_call_id in routes:
        route = routes[selected_call_id]
        data["action"] = "forward"
        if not route.get("ring_group_endpoint_ids"):
            set_pending_route_decision(hass, data)
            return

        # The ring-group coordinator owns all candidate legs until it observes
        # this reroute and completes their teardown. Do not let the next route
        # claim the same logical endpoints before that barrier commits.
        handoff = asyncio.get_running_loop().create_future()
        route["forward_handoff"] = handoff
        set_pending_route_decision(hass, data)
        try:
            await asyncio.wait_for(handoff, timeout=5.0)
        except TimeoutError as err:
            raise ServiceValidationError(
                f"ring-group route for call_id {selected_call_id} "
                "did not release ownership"
            ) from err
        previous = call_runtime_artifacts(hass).forward_tasks.get(selected_call_id)
        if previous is not None and not previous.done():
            await asyncio.gather(previous, return_exceptions=True)

    callback = call_runtime_artifacts(hass).forward_call
    if callback is None:
        raise ServiceValidationError("SIP endpoint is not running")
    destination = str(data.get("destination") or "").strip()
    await callback(
        call_id=selected_call_id,
        destination=destination,
        on_failure=str(data.get("on_failure") or "resume"),
        expected_state=str(data.get("expected_state") or ""),
        expected_sequence=int(data.get("expected_sequence") or 0),
    )
