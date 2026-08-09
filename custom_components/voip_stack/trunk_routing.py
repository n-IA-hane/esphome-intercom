"""Inbound trunk routing decision primitives."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .call_scope import set_pending_route, take_pending_route
from .const import CONF_TRUNK_INBOUND_DEFAULT_TARGET
from .fsm import CallState

if TYPE_CHECKING:
    from .endpoint_session import EndpointCallSession


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

    future = asyncio.get_running_loop().create_future()
    from .call_projection import publish_bridge_projection

    now = time.time()
    expires_at = now + float(timeout)
    fallback = trunk_default_target(trunk_config)
    session = registry.transition(
        invite.call_id,
        state=CallState.CONNECTING.value,
        callee=fallback,
        expected_generation=session.generation,
    )
    if session is None:
        return {}
    set_pending_route(
        hass,
        invite.call_id,
        {
            "future": future,
            "invite": invite,
            "created_at": now,
            "expires_at": expires_at,
            "decision_deadline": expires_at,
            "fallback_destination": fallback,
        },
    )
    publish_bridge_projection(
        hass,
        session,
        peer_name=invite.caller,
        direction="incoming",
        ingress="trunk",
        origin="trunk",
        route_kind="trunk",
        scope="sip_trunk",
        phase="route_decision",
        route_request=True,
        default_destination=fallback,
        fallback_destination=fallback,
        expires_at=expires_at,
        decision_deadline=expires_at,
        decision_timeout_ms=int(float(timeout) * 1000),
        source_host=invite.source_host,
    )
    try:
        decision = await asyncio.wait_for(future, timeout=float(timeout))
        action = str((decision or {}).get("action") or "default").strip().lower()
        return dict(decision or {}) if action != "default" else {}
    except TimeoutError:
        return {}
    finally:
        take_pending_route(hass, invite.call_id)
