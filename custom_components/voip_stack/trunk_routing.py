"""Inbound trunk routing decision primitives."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .routing_automation import request_route_window
from .const import CONF_TRUNK_INBOUND_DEFAULT_TARGET
from .fsm import CallState
from .pbx_trace import trace_call

if TYPE_CHECKING:
    from .endpoint_session import EndpointCallSession


_LOGGER = logging.getLogger(__name__)


def trunk_default_target(trunk_config: dict) -> str:
    """Return the explicit trunk fallback, preserving the HA compatibility alias."""

    return (
        str(trunk_config.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA").strip() or "HA"
    )


async def async_request_inbound_destination(
    hass: HomeAssistant,
    invite,
    *,
    registry,
    session: EndpointCallSession,
    trunk_config: dict,
    timeout: float,
) -> dict:
    """Expose one bounded automation decision and always release its future."""

    fallback = trunk_default_target(trunk_config)
    session = registry.transition(invite.call_id, state=CallState.CONNECTING.value,
        callee=fallback, expected_generation=session.generation)
    if session is None:
        return {}
    trace_call(_LOGGER, invite.call_id,
        "Trunk %s waits for a routing action for %ss; fallback is %s",
        trunk_config.get("name") or getattr(invite, "trunk_id", "") or "default", float(timeout), fallback)
    decision = await request_route_window(hass, session, invite=invite, timeout=timeout, stage="dtmf_fallback",
        peer_name=invite.caller, direction="incoming", ingress="trunk", origin="trunk",
        route_kind="trunk", scope="sip_trunk", default_destination=fallback,
        fallback_destination=fallback, source_host=invite.source_host)
    action = str(decision.get("action") or "default").strip().lower()
    if action == "default":
        if decision:
            trace_call(_LOGGER, invite.call_id, "Routing action keeps trunk fallback %s", fallback)
        else:
            trace_call(_LOGGER, invite.call_id, "No routing action before the deadline; using trunk fallback %s", fallback)
        if decision.get("trunk_id"):
            return {"action": "default", "trunk_id": decision["trunk_id"]}
        return {}
    return decision
