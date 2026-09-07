"""Stateless Home Assistant projections of authoritative PBX sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from types import MappingProxyType
from typing import Any, Literal, Mapping

from homeassistant.core import HomeAssistant

from .endpoint_session import (
    CallToken,
    EndpointCallSession,
    SessionPhase,
    TerminationIntent,
)
from .runtime_data import require_runtime_data, runtime_data


@dataclass(frozen=True, slots=True)
class CallProjectionEvent:
    """One immutable request to project an accepted session mutation."""

    token: CallToken
    scope: Literal["phone", "sip_bridge"]
    endpoint_id: str = ""
    leg_id: str = ""
    intent: TerminationIntent | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


_RESERVED_FIELDS = {"state", "sip_state", "call_id", "caller", "callee", "endpoint_id"}
_TERMINAL_PHONE_PROJECTIONS = "terminal_phone_projections"
_LOGGER = logging.getLogger(__name__)


def publish_esp_media_route(hass: HomeAssistant, call_id: str, route: str) -> None:
    """Project a committed media owner's decision onto its physical call legs."""
    if route not in {"direct", "ha_transcoding"}:
        raise ValueError("invalid committed media route")
    data = runtime_data(hass)
    if data is None or data.sip is None:
        return
    session = data.sip.get_session(call_id)
    if session is None or not session.live or session.state != "in_call":
        return
    # Local media endpoints such as Assist share relay lifecycle ownership,
    # but do not expose a relay's media-route decision.
    if any(
        resource.name.startswith("relay:")
        and getattr(resource.value, "media_route", None) == "ha_transcoding"
        for resource in session.resources
    ):
        route = "ha_transcoding"
    session.update_metadata(committed_media_route=route)
    token = session.token
    targets: dict[str, str] = {}
    destination_id = str(session.metadata.get("dest_endpoint_id") or "")
    destination_call_id = str(session.metadata.get("bridge_dest_call_id") or "")
    endpoint_ids = set(session.endpoint_claims)
    endpoint_ids.update(
        str(session.metadata.get(key) or "")
        for key in ("source_endpoint_id", "dest_endpoint_id", "target_endpoint_id")
    )
    for endpoint_id in endpoint_ids - {""}:
        endpoint = data.endpoints.get(endpoint_id)
        if endpoint is None or not endpoint.device_id:
            continue
        leg_call_id = next(
            (leg.sip_call_id for leg in session.legs.values()
             if leg.endpoint_id == endpoint_id and leg.sip_call_id),
            destination_call_id if endpoint_id == destination_id and destination_call_id else session.call_id,
        )
        targets[endpoint.device_id] = leg_call_id
    if not targets:
        return

    async def notify() -> None:
        from .device_resolver import get_resolver
        from .esphome_actions import async_call_action, has_action

        devices = await get_resolver(hass).list_devices()
        for device in devices:
            device_id = str(device.get("device_id") or "")
            leg_call_id = targets.get(device_id)
            if not leg_call_id or not has_action(hass, device, "set_media_route"):
                continue
            if (not session.owns(token) or session.state != "in_call"
                    or session.metadata.get("committed_media_route") != route):
                return
            delivered = session.metadata.setdefault("esp_media_routes", {})
            value = (leg_call_id, route)
            if delivered.get(device_id) == value:
                continue
            try:
                await async_call_action(hass, device, "set_media_route", {
                    "call_id": leg_call_id, "route": route,
                })
            except Exception:
                # Presentation must never fail an established media session.
                _LOGGER.debug("ESP media route projection failed", exc_info=True)
            else:
                if session.owns(token) and session.metadata.get("committed_media_route") == route:
                    delivered[device_id] = value

    session.create_task(notify(), name=f"voip-media-route-{session.call_id}")


def stage_phone_termination_projection(
    session: EndpointCallSession,
    endpoint_id: str,
    **details: Any,
) -> bool:
    """Store endpoint-specific terminal presentation on its owning session."""

    endpoint_id = str(endpoint_id or "").strip()
    if not endpoint_id or not session.live:
        return False
    staged = dict(session.metadata.get(_TERMINAL_PHONE_PROJECTIONS) or {})
    staged[endpoint_id] = dict(details)
    session.update_metadata(**{_TERMINAL_PHONE_PROJECTIONS: staged})
    return True


def publish_call_projection(
    hass: HomeAssistant,
    session: EndpointCallSession,
    event: CallProjectionEvent,
) -> bool:
    """Project one current generation without owning or changing its state."""

    runtime_data = require_runtime_data(hass)
    runtime = runtime_data.sip
    if runtime is None:
        return False
    current = runtime.get_session(
        event.token.call_id,
        generation=event.token.generation,
    )
    live_projection = current is session and session.live and event.intent is None
    terminal_projection = bool(
        event.intent is not None
        and event.intent is session.termination_intent
        and session.phase is SessionPhase.TERMINATED
        and current is None
        and runtime.get_session(event.token.call_id) is None
        and runtime.is_terminated(
            event.token.call_id,
            generation=event.token.generation,
        )
    )
    if not live_projection and not terminal_projection:
        return False
    if live_projection and event.scope == "phone":
        endpoint = runtime_data.endpoints.get(event.endpoint_id)
        active_call_id = str(getattr(endpoint, "active_call_id", "") or "")
        if (
            active_call_id
            and runtime.resolve_session_id(active_call_id) != session.call_id
        ):
            return False
    state = event.intent.public_state if event.intent is not None else session.state
    if event.leg_id:
        leg = session.legs.get(event.leg_id)
        if leg is None:
            return False
        state = event.intent.public_state if event.intent is not None else leg.state
    details = {
        key: value
        for key, value in event.details.items()
        if key not in _RESERVED_FIELDS
    }
    metadata = session.metadata
    if event.scope == "phone":
        from .websocket_api import _set_ha_softphone_call_state

        endpoint = runtime_data.endpoints.get(event.endpoint_id)
        _set_ha_softphone_call_state(
            hass,
            state,
            endpoint_id=event.endpoint_id,
            session_device_id=str(
                getattr(endpoint, "device_id", "")
                or metadata.get("session_device_id")
                or ""
            ),
            caller=session.caller,
            callee=session.callee,
            call_id=session.call_id,
            **details,
        )
    else:
        from .websocket_api import _set_sip_bridge_call_state

        _set_sip_bridge_call_state(
            hass,
            state,
            call_id=session.call_id,
            dest_call_id=str(metadata.get("bridge_dest_call_id") or ""),
            caller=session.caller,
            callee=session.callee,
            **details,
        )
    return True


def publish_bridge_projection(
    hass: HomeAssistant,
    session: EndpointCallSession,
    *,
    intent: TerminationIntent | None = None,
    **details: Any,
) -> bool:
    return publish_call_projection(
        hass,
        session,
        CallProjectionEvent(
            session.token,
            "sip_bridge",
            intent=intent,
            details=MappingProxyType(dict(details)),
        ),
    )


def publish_phone_projection(
    hass: HomeAssistant,
    session: EndpointCallSession,
    endpoint_id: str,
    *,
    leg_id: str = "",
    intent: TerminationIntent | None = None,
    **details: Any,
) -> bool:
    return publish_call_projection(
        hass,
        session,
        CallProjectionEvent(
            session.token,
            "phone",
            endpoint_id,
            leg_id,
            intent,
            MappingProxyType(dict(details)),
        ),
    )


def observe_phone_leg_projection(
    hass: HomeAssistant,
    registry: Any,
    session: EndpointCallSession,
    endpoint_id: str,
    state: str,
    *,
    leg_id: str,
    role: str = "ha_softphone",
    **details: Any,
) -> bool:
    observed = registry.observe_leg(
        session.call_id,
        leg_id,
        role=role,
        state=state,
        endpoint_id=endpoint_id,
        generation=session.generation,
    )
    return bool(
        observed
        and publish_phone_projection(
            hass, session, endpoint_id, leg_id=leg_id, **details
        )
    )
