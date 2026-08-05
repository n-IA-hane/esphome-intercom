"""Home Assistant authorization boundaries for VoIP control surfaces."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from homeassistant.exceptions import Unauthorized, UnknownUser


POLICY_READ = "read"
POLICY_CONTROL = "control"
VOIP_CONTROL_ENTITY_ID = "event.voip_stack_call"
_MEDIA_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{15,127}\Z", re.ASCII)
_DOMAIN = "voip_stack"


def _require_entity_permission(user: Any, policy: str, *, context: Any = None) -> str:
    """Require one HA entity policy and return the authenticated user id."""

    user_id = str(getattr(user, "id", "") or "")
    permissions = getattr(user, "permissions", None)
    check_entity = getattr(permissions, "check_entity", None)
    if not user_id or not callable(check_entity) or not check_entity(
        VOIP_CONTROL_ENTITY_ID, policy
    ):
        raise Unauthorized(
            context=context,
            entity_id=VOIP_CONTROL_ENTITY_ID,
            permission=policy,
            user_id=user_id or None,
        )
    return user_id


def _endpoint_permission_entity_ids(hass: Any, endpoint: Any) -> tuple[str, ...]:
    """Return live HA entities which represent one logical phone."""
    endpoint_id = str(getattr(endpoint, "endpoint_id", "") or "").strip()
    entity_ids = {
        str(entity_id or "").strip()
        for entity_id in (getattr(endpoint, "entity_ids", ()) or ())
        if str(entity_id or "").strip()
    }
    device_id = str(getattr(endpoint, "device_id", "") or "").strip()
    endpoint_kind = str(
        getattr(getattr(endpoint, "kind", ""), "value", getattr(endpoint, "kind", ""))
        or ""
    ).strip().lower()
    if device_id and endpoint_kind != "esphome":
        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(hass)
            entity_ids.update(
                str(item.entity_id)
                for item in registry.entities.values()
                if str(getattr(item, "device_id", "") or "") == device_id
            )
        except (AttributeError, ImportError):
            pass
    if endpoint_id == "default":
        # Preserve the original singleton permission contract for the master
        # phone while still accepting its newer per-device entities.
        entity_ids.add(VOIP_CONTROL_ENTITY_ID)
    return tuple(sorted(entity_ids))


def _require_endpoint_permission(
    hass: Any,
    user: Any,
    endpoint: Any,
    policy: str,
    *,
    context: Any = None,
) -> str:
    """Require permission on at least one entity owned by a logical phone."""
    user_id = str(getattr(user, "id", "") or "")
    permissions = getattr(user, "permissions", None)
    check_entity = getattr(permissions, "check_entity", None)
    entity_ids = _endpoint_permission_entity_ids(hass, endpoint)
    if (
        not user_id
        or not callable(check_entity)
        or not entity_ids
        or not any(check_entity(entity_id, policy) for entity_id in entity_ids)
    ):
        raise Unauthorized(
            context=context,
            entity_id=entity_ids[0] if entity_ids else None,
            permission=policy,
            user_id=user_id or None,
        )
    return user_id


def _require_explicit_entity_permission(
    user: Any,
    entity_ids: tuple[str, ...],
    policy: str,
    *,
    context: Any = None,
) -> str:
    """Require permission on one entity from an action-specific allow-list."""
    user_id = str(getattr(user, "id", "") or "")
    permissions = getattr(user, "permissions", None)
    check_entity = getattr(permissions, "check_entity", None)
    normalized = tuple(
        sorted(
            {
                str(entity_id or "").strip()
                for entity_id in entity_ids
                if str(entity_id or "").strip()
            }
        )
    )
    if (
        not user_id
        or not callable(check_entity)
        or not normalized
        or not any(check_entity(entity_id, policy) for entity_id in normalized)
    ):
        raise Unauthorized(
            context=context,
            entity_id=normalized[0] if normalized else None,
            permission=policy,
            user_id=user_id or None,
        )
    return user_id


async def _service_user(hass: Any, call: Any) -> Any | None:
    """Resolve a service caller; an empty user is an internal HA automation."""

    context = getattr(call, "context", None)
    user_id = str(getattr(context, "user_id", "") or "")
    if not user_id:
        return None
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=context, user_id=user_id)
    return user


async def async_require_service_control(hass: Any, call: Any) -> None:
    """Allow internal automations or users allowed to control VoIP Stack."""

    user = await _service_user(hass, call)
    if user is not None:
        _require_entity_permission(
            user,
            POLICY_CONTROL,
            context=getattr(call, "context", None),
        )


async def async_require_service_admin(hass: Any, call: Any) -> None:
    """Allow internal automations or authenticated HA administrators."""

    user = await _service_user(hass, call)
    if user is not None and not bool(getattr(user, "is_admin", False)):
        raise Unauthorized(
            context=getattr(call, "context", None),
            user_id=str(getattr(user, "id", "") or "") or None,
        )


async def async_require_service_endpoint_control(
    hass: Any,
    call: Any,
    endpoint: Any,
) -> None:
    """Allow internal automations or users allowed to control this phone."""
    user = await _service_user(hass, call)
    if user is not None:
        _require_endpoint_permission(
            hass,
            user,
            endpoint,
            POLICY_CONTROL,
            context=getattr(call, "context", None),
        )


async def async_require_service_entity_control(
    hass: Any,
    call: Any,
    entity_ids: tuple[str, ...],
) -> None:
    """Allow internal automations or users controlling an exact action entity."""
    user = await _service_user(hass, call)
    if user is not None:
        _require_explicit_entity_permission(
            user,
            entity_ids,
            POLICY_CONTROL,
            context=getattr(call, "context", None),
        )


def require_websocket_read(connection: Any) -> str:
    """Require read access before exposing call/device topology."""

    return _require_entity_permission(getattr(connection, "user", None), POLICY_READ)


def require_websocket_control(connection: Any) -> str:
    """Require control access before starting or mutating a call."""

    return _require_entity_permission(
        getattr(connection, "user", None), POLICY_CONTROL
    )


def require_websocket_endpoint_read(
    hass: Any,
    connection: Any,
    endpoint: Any,
) -> str:
    """Require read access to the selected logical phone."""
    return _require_endpoint_permission(
        hass,
        getattr(connection, "user", None),
        endpoint,
        POLICY_READ,
    )


def require_websocket_endpoint_control(
    hass: Any,
    connection: Any,
    endpoint: Any,
) -> str:
    """Require control access to the selected logical phone."""
    return _require_endpoint_permission(
        hass,
        getattr(connection, "user", None),
        endpoint,
        POLICY_CONTROL,
    )


def websocket_can_control_endpoint(
    hass: Any,
    connection: Any,
    endpoint: Any | None,
) -> bool:
    """Return whether a state subscriber can make this phone reachable.

    Read-only dashboards may observe a phone, but routing presence must be
    driven only by a browser that can actually answer it.  Legacy singleton
    setups have no endpoint object and retain the integration-wide control
    boundary.
    """
    try:
        require_websocket_control(connection)
        if endpoint is not None:
            require_websocket_endpoint_control(hass, connection, endpoint)
    except Unauthorized:
        return False
    return True


def require_http_control(request: Any) -> str:
    """Require control access before attaching private browser media."""

    getter = getattr(request, "get", None)
    user = getter("hass_user") if callable(getter) else None
    return _require_entity_permission(user, POLICY_CONTROL)


def require_media_client_id(request: Any) -> str:
    """Return one bounded browser-instance token covered by HA's signed path."""

    query = getattr(request, "query", None)
    getter = getattr(query, "get", None)
    client_id = str(getter("client_id") if callable(getter) else "")
    if _MEDIA_CLIENT_ID.fullmatch(client_id) is None:
        raise ValueError("a valid signed media client_id is required")
    return client_id


def _media_controller_user_id(
    registry: Any,
    call_id: str,
    endpoint_id: str = "",
) -> str:
    """Return the sticky HA user controlling one call or local phone leg."""

    session_id = registry.resolve_session_id(str(call_id or "").strip())
    session = registry.sessions.get(session_id)
    if session is None:
        return ""
    requested_endpoint_id = str(endpoint_id or "").strip()
    if requested_endpoint_id and session.metadata.get("local_bridge"):
        return str(
            (session.metadata.get("controller_user_ids") or {}).get(
                requested_endpoint_id
            )
            or ""
        ).strip()
    return str(session.metadata.get("controller_user_id") or "").strip()


def media_controller_status(
    registry: Any,
    call_id: str,
    endpoint_id: str,
    user_id: str,
) -> str:
    """Return whether this HA user controls or may observe one media leg."""

    controller_user_id = _media_controller_user_id(
        registry,
        call_id,
        endpoint_id,
    )
    if not controller_user_id:
        return "available"
    return "self" if controller_user_id == str(user_id or "").strip() else "other"


async def async_require_media_controller(
    hass: Any,
    registry: Any,
    call_id: str,
    user: Any,
    *,
    endpoint_id: str = "",
) -> str:
    """Authorize a media attachment against the call's sticky controller.

    Calls started or answered by an authenticated user remain private to that
    user, including administrators.  Calls created by internal automations do
    not have a user identity; an administrator may bind the first browser
    atomically, after which the same rule applies for reconnects.
    """

    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        raise Unauthorized(user_id=None)
    lock = getattr(registry, "media_controller_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        registry.media_controller_lock = lock
    async with lock:
        session_id = registry.resolve_session_id(str(call_id or "").strip())
        session = registry.sessions.get(session_id)
        if session is None:
            raise Unauthorized(user_id=user_id)
        requested_endpoint_id = str(endpoint_id or "").strip()
        scoped = bool(
            requested_endpoint_id and session.metadata.get("local_bridge")
        )
        controller_user_id = _media_controller_user_id(
            registry,
            session_id,
            requested_endpoint_id,
        )
        if controller_user_id:
            if controller_user_id != user_id:
                raise Unauthorized(user_id=user_id)
            return user_id
        if not bool(getattr(user, "is_admin", False)):
            raise Unauthorized(user_id=user_id)
        try:
            registry.bind_controller(
                session_id,
                user_id=user_id,
                endpoint_id=requested_endpoint_id if scoped else "",
            )
        except ValueError as err:
            raise Unauthorized(user_id=user_id) from err
        return user_id
