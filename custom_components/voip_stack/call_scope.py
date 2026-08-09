"""Logical endpoint scope for PBX calls and pending routes."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant

from .endpoint_lifecycle import call_registry


def pending_routes(hass: HomeAssistant) -> dict:
    """Return a detached read projection of pending routes."""
    return dict(call_registry(hass).artifact_items("pending_route"))


def set_pending_route(hass: HomeAssistant, call_id: str, route: dict) -> None:
    """Attach route state to the authoritative call generation."""
    call_registry(hass).set_pending_route(call_id, route)


def take_pending_route(hass: HomeAssistant, call_id: str) -> dict | None:
    """Detach route state for explicit completion or cancellation."""
    return call_registry(hass).take_pending_route(call_id)


def call_endpoint_id(registry, call_id: str) -> str:
    """Return the primary browser endpoint owning a logical call."""
    session_id = registry.resolve_session_id(str(call_id or "").strip())
    session = registry.sessions.get(session_id)
    return str(
        ((session.metadata if session is not None else {}) or {}).get("endpoint_id")
        or ""
    ).strip()


def call_endpoint_ids(registry, call_id: str) -> frozenset[str]:
    """Return every logical phone participating in one call.

    Ordinary SIP calls retain the singular ``endpoint_id``. Local browser
    calls and ring groups add the participating endpoint identities so every
    legitimate leg can control and attach media to the same logical call.
    """
    session_id = registry.resolve_session_id(str(call_id or "").strip())
    session = registry.sessions.get(session_id)
    metadata = (session.metadata if session is not None else {}) or {}
    endpoint_ids = {
        str(metadata.get(key) or "").strip()
        for key in (
            "endpoint_id",
            "source_endpoint_id",
            "dest_endpoint_id",
            "target_endpoint_id",
        )
    }
    endpoint_ids.update(
        str(value or "").strip() for value in (metadata.get("ring_endpoint_ids") or ())
    )
    endpoint_ids.discard("")
    return frozenset(endpoint_ids)


def call_belongs_to_endpoint(registry, call_id: str, endpoint_id: str) -> bool:
    """Return whether an endpoint participates in a logical call."""
    return str(endpoint_id or "").strip() in call_endpoint_ids(registry, call_id)


def endpoint_call_ids(
    registry,
    call_ids: Iterable[object],
    endpoint_id: str,
) -> list[str]:
    """Filter call IDs to those controllable by one logical endpoint."""
    return [
        str(call_id)
        for call_id in call_ids
        if call_belongs_to_endpoint(registry, str(call_id), endpoint_id)
    ]


def single_pending_route_call_id(
    hass: HomeAssistant,
    endpoint_id: str,
) -> str:
    """Return the only pending route visible to one endpoint, if unambiguous."""
    registry = call_registry(hass)
    routes = endpoint_call_ids(
        registry,
        (call_id for call_id, _route in registry.artifact_items("pending_route")),
        endpoint_id,
    )
    return routes[0] if len(routes) == 1 else ""
