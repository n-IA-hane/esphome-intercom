"""Home Assistant projections and delivery bookkeeping for PBX sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from types import MappingProxyType
from typing import Any, Literal, Mapping

from homeassistant.core import HomeAssistant

from .endpoint_session import (
    CallToken,
    EndpointCallSession,
    LegPhase,
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


class CallStatePublisher:
    """Deliver committed state through the existing HA projection adapters.

    A single loop callback per live generation lets the operation attach its
    presentation details synchronously. Missing explicit projections are filled
    from the committed state, never by changing the call to match a card.
    """

    def __init__(self, hass, registry):
        self.hass = hass
        self.registry = registry
        self.pending = {}
        self.delivered = {}
        self.unsubscribe = registry.subscribe_state(self.changed)

    def changed(self, snapshot):
        token = (snapshot.call_id, snapshot.generation)
        if snapshot.phase == SessionPhase.TERMINATED.value:
            handle = self.pending.pop(token, None)
            if handle is not None:
                handle.cancel()
            self.delivered.pop(token, None)
            return
        if token not in self.pending:
            self.pending[token] = self.hass.loop.call_soon(self.flush, token)

    def acknowledge(self, session, scope, endpoint_id=""):
        if session.committed_state is not None and session.live:
            token = (session.call_id, session.generation)
            self.delivered.setdefault(token, {})[(scope, endpoint_id)] = session.committed_state

    def flush(self, token):
        self.pending.pop(token, None)
        session = self.registry.get_session(token[0], generation=token[1])
        if session is None or not session.live or session.committed_state is None:
            self.delivered.pop(token, None)
            return
        snapshot = session.committed_state
        if not session.owner or session.metadata.get("event_only"):
            return
        runtime = require_runtime_data(self.hass)
        deliveries = self.delivered.setdefault(token, {})
        endpoint_ids = set(snapshot.participants)
        endpoint_ids.update(leg.endpoint_id for leg in snapshot.legs if leg.endpoint_id)
        endpoint_ids.update(endpoint for scope, endpoint in deliveries if scope == "phone")
        for endpoint_id in endpoint_ids:
            endpoint = runtime.endpoints.get(endpoint_id)
            if endpoint is None or not endpoint.supports_ha_client:
                continue
            if not endpoint.kind.is_softphone and not any(
                leg.endpoint_id == endpoint_id and leg.role == "ha_softphone"
                and leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}
                for leg in session.legs.values()
            ):
                # Native SIP accounts are projected by PhoneStateObserver.
                # Only an actual HA transport needs the browser media store.
                continue
            if deliveries.get(("phone", endpoint_id)) == snapshot:
                continue
            if endpoint.active_call_id and endpoint.active_call_id != session.call_id:
                continue
            leg_id = f"browser:{endpoint_id}"
            leg = session.legs.get(leg_id)
            if endpoint_id not in session.endpoint_claims:
                store = runtime.softphones.get(endpoint_id, {})
                if store.get("call_id") == session.call_id:
                    from .websocket_api import _set_ha_softphone_call_state
                    _set_ha_softphone_call_state(
                        self.hass, "cancelled", endpoint_id=endpoint_id,
                        call_id=session.call_id, caller=session.caller, callee=session.callee,
                        reason="forwarded", event_type="state_changed",
                    )
                continue
            if leg is not None and leg.phase in {LegPhase.CLOSING, LegPhase.CLOSED}:
                continue
            source = endpoint_id == session.metadata.get("source_endpoint_id")
            publish_phone_projection(
                self.hass, session, endpoint_id,
                leg_id=leg_id if leg is not None else "",
                direction="outgoing" if source else "incoming",
                peer_name=session.callee if source else session.caller,
                event_type="state_changed",
            )
        if snapshot not in deliveries.values():
            publish_bridge_projection(self.hass, session, route_kind=session.route_kind,
                                      event_type="state_changed")

    async def close(self):
        self.unsubscribe()
        for handle in self.pending.values():
            handle.cancel()
        self.pending.clear()
        self.delivered.clear()


def bind_state_publisher(hass, registry):
    if registry.component("state_publisher") is None:
        publisher = CallStatePublisher(hass, registry)
        registry.adopt_component("state_publisher", publisher, closer=publisher.close)


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
    leg_id = event.leg_id
    if not leg_id and event.scope == "phone" and f"browser:{event.endpoint_id}" in session.legs:
        leg_id = f"browser:{event.endpoint_id}"
    if leg_id:
        leg = session.legs.get(leg_id)
        if leg is None:
            return False
        state = event.intent.public_state if event.intent is not None else leg.state
    details = {
        key: value
        for key, value in event.details.items()
        if key not in _RESERVED_FIELDS
    }
    publisher = runtime.component("state_publisher")
    if publisher is not None:
        publisher.acknowledge(session, event.scope, event.endpoint_id)
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
