"""Resolve and authorize phone endpoints selected by Home Assistant services."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import (
    async_require_service_endpoint_control,
    async_require_service_entity_control,
)
from .const import HA_PEER_FALLBACK_NAME
from .endpoint_registry import EndpointAmbiguousError, EndpointLookup, EndpointRegistry
from .phone_endpoint import (
    EndpointKind,
    PhoneEndpoint,
    ha_client_endpoint,
)
from .runtime_data import endpoint_directory, preferred_browser_phone
from .service_errors import service_error as _service_error


def resolve_service_phone(
    registry: EndpointRegistry, selector: str, *, allow_internal: bool = False,
    namespace: EndpointLookup = EndpointLookup.DEVICE_ID,
) -> PhoneEndpoint | None:
    """Resolve a native HA Device selector, then adapt saved legacy references.

    A Device selector returns a device ID, even when another phone happens to
    publish that text as its name. Older YAML actions also accepted names,
    usernames, extensions and endpoint references; that fallback must be unique
    across those namespaces rather than depending on their lookup order.
    """
    try:
        endpoint = registry.resolve(selector, namespace=namespace)
        if endpoint is not None:
            return endpoint
        namespaces = (EndpointLookup.NAME, EndpointLookup.USERNAME,
                      EndpointLookup.EXTENSION, EndpointLookup.ENDPOINT_ID,
                      EndpointLookup.DEVICE_ID)
        if allow_internal:
            namespaces += (EndpointLookup.ENTITY_ID,)
        return registry.resolve(selector, namespace=namespaces)
    except EndpointAmbiguousError as error:
        raise ServiceValidationError(f"Ambiguous phone selector: {selector}") from error


def resolve_public_phone(registry: EndpointRegistry, selector: str, *, directory=None) -> PhoneEndpoint | None:
    """Resolve a manually entered public destination without HA/internal IDs."""
    from .phone_directory import PhoneDirectory
    from .roster import RosterEntry, RosterError

    rows = [row for row in directory.snapshot()
            if registry.get(row.metadata.get("endpoint_id", "")) is None] if directory is not None else []
    rows.extend(RosterEntry(
        id=endpoint.username, name=endpoint.name, extension=endpoint.extension,
        metadata={"endpoint_id": endpoint.endpoint_id},
    ) for endpoint in registry.endpoints)
    public_directory = PhoneDirectory(rows)
    try:
        row = public_directory.resolve(selector)
    except RosterError as error:
        raise ServiceValidationError(f"Ambiguous phone destination: {selector}") from error
    return registry.get(row.metadata.get("endpoint_id", "")) if row is not None else None


def service_browser_endpoint(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    strict: bool = False,
):
    """Resolve the logical HA/browser phone originating a service action."""
    registry = endpoint_directory(hass)
    device_id = str(call.data.get("device_id") or "").strip()
    endpoint = resolve_service_phone(registry, device_id) if device_id else preferred_browser_phone(hass)
    if endpoint is None:
        if device_id:
            raise _service_error(
                "Unknown Home Assistant phone device",
                "unknown_phone_device",
            )
        raise _service_error(
            "Select a Home Assistant phone",
            "phone_selection_required",
        )
    endpoint_id = endpoint.endpoint_id
    view = ha_client_endpoint(endpoint)
    if view is None:
        if strict or device_id:
            raise _service_error(
                "The selected Device is not a Home Assistant browser phone",
                "phone_not_browser",
            )
    return endpoint_id, view


def service_configured_endpoint(hass: HomeAssistant, call: ServiceCall):
    """Resolve one integration-owned browser or registrar-account phone."""
    registry = endpoint_directory(hass)

    device_id = str(call.data.get("device_id") or "").strip()
    endpoint = resolve_service_phone(registry, device_id) if device_id else preferred_browser_phone(hass)
    if endpoint is None:
        raise _service_error(
            (
                "Unknown Home Assistant phone device"
                if device_id
                else "Select a Home Assistant phone"
            ),
            "unknown_phone_device" if device_id else "phone_selection_required",
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
        registry = endpoint_directory(hass)
        endpoint = registry.by_device_id(str(device.get("device_id") or ""))
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
                name=str(device.get("name") or "ESP phone"),
                kind=EndpointKind.ESPHOME,
                device_id=device_id,
                entity_ids=entities,
                capabilities=frozenset({"audio", "dtmf"}),
            )
    if endpoint is not None:
        await async_require_service_endpoint_control(hass, call, endpoint)
    if action_entity_ids is not None:
        await async_require_service_entity_control(hass, call, action_entity_ids)
