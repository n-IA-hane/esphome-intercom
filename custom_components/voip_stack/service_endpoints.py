"""Resolve and authorize phone endpoints selected by Home Assistant services."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import (
    async_require_service_endpoint_control,
    async_require_service_entity_control,
)
from .const import DOMAIN, HA_PEER_FALLBACK_NAME, HA_SOFTPHONE_DEVICE_ID
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointKind,
    PhoneEndpoint,
)


def _service_error(message: str, key: str) -> ServiceValidationError:
    """Build one translated service error while preserving its log message."""

    return ServiceValidationError(
        message,
        translation_domain=DOMAIN,
        translation_key=key,
    )


def service_browser_endpoint(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    strict: bool = False,
):
    """Resolve the logical HA/browser phone originating a service action."""
    registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    device_id = str(call.data.get("device_id") or "").strip()
    endpoint = None
    if registry is not None:
        endpoint = (
            registry.get(DEFAULT_ENDPOINT_ID)
            if not device_id or device_id == HA_SOFTPHONE_DEVICE_ID
            else registry.by_device_id(device_id)
        )
    if endpoint is None:
        if device_id and device_id != HA_SOFTPHONE_DEVICE_ID:
            raise _service_error(
                "Unknown Home Assistant phone device",
                "unknown_phone_device",
            )
        endpoint_id = DEFAULT_ENDPOINT_ID
    else:
        endpoint_id = endpoint.endpoint_id
    if endpoint is not None and endpoint.kind is not EndpointKind.BROWSER:
        if strict or device_id:
            raise _service_error(
                "The selected Device is not a Home Assistant browser phone",
                "phone_not_browser",
            )
        endpoint = None
    return endpoint_id, endpoint


def service_configured_endpoint(hass: HomeAssistant, call: ServiceCall):
    """Resolve one integration-owned browser or registrar-account phone."""
    registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")

    device_id = str(call.data.get("device_id") or "").strip()
    if registry is None:
        if device_id and device_id != HA_SOFTPHONE_DEVICE_ID:
            raise _service_error(
                "Unknown Home Assistant phone device",
                "unknown_phone_device",
            )
        return DEFAULT_ENDPOINT_ID, None
    endpoint = (
        registry.get(DEFAULT_ENDPOINT_ID)
        if not device_id or device_id == HA_SOFTPHONE_DEVICE_ID
        else registry.by_device_id(device_id)
    )
    if endpoint is None:
        raise _service_error(
            (
                "Unknown Home Assistant phone device"
                if device_id
                else "The default Home Assistant phone is unavailable"
            ),
            "unknown_phone_device" if device_id else "default_phone_unavailable",
        )
    if endpoint.kind not in {EndpointKind.BROWSER, EndpointKind.SIP_ACCOUNT}:
        raise _service_error(
            "The selected Device is not an integration-owned phone",
            "phone_not_integration_owned",
        )
    return endpoint.endpoint_id, endpoint


def browser_endpoint_name(
    hass: HomeAssistant,
    endpoint_id: str,
    endpoint=None,
) -> str:
    """Return the stable display name of a browser phone."""
    del endpoint_id
    fallback = (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME
    return str(getattr(endpoint, "name", "") or fallback).strip()


async def async_require_phone_service_control(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    endpoint=None,
    device: dict | None = None,
    action_entity_ids: tuple[str, ...] | None = None,
) -> None:
    """Apply per-phone HA permissions after the integration-wide boundary."""
    if endpoint is None and device is not None:
        registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
        endpoint = (
            registry.by_device_id(str(device.get("device_id") or ""))
            if registry is not None
            else None
        )
        if endpoint is None:
            device_id = str(device.get("device_id") or "").strip()
            entities = frozenset(
                str(value)
                for value in (device.get("entities") or {}).values()
                if isinstance(value, str) and "." in value
            )
            # Resolution can precede roster discovery. Authorize against an
            # ephemeral descriptor instead of failing open on a global entity.
            endpoint = PhoneEndpoint(
                endpoint_id=str(
                    device.get("endpoint_id") or f"esphome:{device_id}"
                ),
                name=str(device.get("name") or device_id or "ESP phone"),
                kind=EndpointKind.ESPHOME,
                device_id=device_id,
                entity_ids=entities,
                capabilities=frozenset({"audio", "dtmf"}),
            )
    if endpoint is not None:
        await async_require_service_endpoint_control(hass, call, endpoint)
    if action_entity_ids is not None:
        await async_require_service_entity_control(hass, call, action_entity_ids)
