"""Home Assistant service adapter for physical ESPHome phones."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .device_resolver import get_resolver
from .phone_endpoint import EndpointKind
from .websocket_api import _get_voip_devices

_LOGGER = logging.getLogger(__name__)


def _registry_endpoint(hass: HomeAssistant, selector: str):
    """Resolve a selected logical phone without assuming its transport."""
    registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    by_device_id = getattr(registry, "by_device_id", None)
    endpoint = by_device_id(selector) if callable(by_device_id) else None
    resolve = getattr(registry, "resolve", None)
    if endpoint is None and callable(resolve):
        try:
            endpoint = resolve(selector)
        except (KeyError, ValueError):
            return None
    return endpoint


async def async_press_device_button(
    hass: HomeAssistant,
    device: dict,
    key: str,
    label: str,
    *,
    context=None,
) -> bool:
    """Press a physical phone button exposed by ESPHome."""
    button_eid = (device.get("entities") or {}).get(key)
    if not button_eid:
        _LOGGER.warning(
            "Cannot press %s for %s: entity not found",
            label,
            device.get("name"),
        )
        return False
    try:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": button_eid},
            blocking=True,
            context=context,
        )
        _LOGGER.info(
            "Pressed %s for %s via voip_stack service",
            button_eid,
            device.get("name"),
        )
        return True
    except Exception:
        _LOGGER.exception(
            "Failed pressing %s for %s",
            button_eid,
            device.get("name"),
        )
        return False


async def async_call_action(
    hass: HomeAssistant,
    device: dict,
    action: str,
    data: dict | None = None,
    *,
    context=None,
) -> None:
    """Invoke a native ESPHome action exposed by the selected SIP phone."""
    route_id = str(device.get("route_id") or "").strip()
    if not route_id:
        raise ServiceValidationError(
            f"{device.get('name') or 'ESP phone'} has no ESPHome service route"
        )
    service = f"{route_id}_{action}"
    if not hass.services.has_service("esphome", service):
        raise ServiceValidationError(
            f"ESPHome service esphome.{service} is not available; "
            "include packages/voip/ha_phone.yaml in the device firmware"
        )
    await hass.services.async_call(
        "esphome",
        service,
        data or {},
        blocking=True,
        context=context,
    )
    _LOGGER.info(
        "ESP SIP phone %s action=%s data=%s",
        device.get("name"),
        action,
        data or {},
    )


def has_action(hass: HomeAssistant, device: dict, action: str) -> bool:
    """Return whether an ESP phone exposes one native action."""
    route_id = str(device.get("route_id") or "").strip()
    return bool(
        route_id
        and hass.services.has_service("esphome", f"{route_id}_{action}")
    )


async def async_resolve_target_device(
    hass: HomeAssistant,
    call: ServiceCall,
) -> dict | None:
    """Resolve a Home Assistant service target to an ESP phone."""
    return await get_resolver(hass).resolve_target(call)


async def async_resolve_source_device(
    hass: HomeAssistant,
    call: ServiceCall,
) -> dict | None:
    """Resolve the explicit physical source of a phone action."""
    source = str(call.data.get("device_id") or "").strip()
    if not source:
        return None
    devices = await _get_voip_devices(hass)
    wanted = source.lower()
    for device in devices:
        if (
            str(device.get("device_id") or "").lower() == wanted
            or str(device.get("name") or "").lower() == wanted
            or str(device.get("route_id") or "").lower() == wanted
            or str(device.get("host") or "").lower() == wanted
        ):
            return device

    # The registry keeps the ESP identity while its live endpoint is missing.
    # Do not pass that physical phone to the browser adapter.
    endpoint = _registry_endpoint(hass, source)
    if endpoint is not None and getattr(endpoint, "kind", None) is EndpointKind.ESPHOME:
        name = str(getattr(endpoint, "name", "") or "ESPHome phone")
        raise ServiceValidationError(
            f"{name} is an ESPHome phone, but its live VoIP endpoint is unavailable"
        )
    return None


async def async_resolve_command_phone(
    hass: HomeAssistant,
    call: ServiceCall,
) -> dict | None:
    """Resolve an optional ESP selector for public phone-control services.

    No physical selector means the command targets a browser softphone. A
    logical browser Device must never be mistaken for an ESP target.
    """
    selector = str(call.data.get("device_id") or "").strip()
    if selector:
        endpoint = _registry_endpoint(hass, selector)
        if (
            endpoint is not None
            and getattr(endpoint, "kind", None) is EndpointKind.BROWSER
        ):
            return None
    source = await async_resolve_source_device(hass, call)
    if source is not None:
        return source
    return await async_resolve_target_device(hass, call)
