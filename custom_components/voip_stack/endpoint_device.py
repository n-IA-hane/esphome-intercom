"""Home Assistant device helpers for logical phone endpoints.

VoIP Stack owns virtual browser and registrar-account phones, so those are
represented by ``DeviceEntryType.SERVICE`` devices.  ESPHome phones already
have a physical device owned by the ESPHome config entry; this module only
resolves that device and deliberately never adopts or mutates it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry, DeviceEntryType, DeviceInfo

from .const import DOMAIN, INTEGRATION_VERSION
from .runtime_data import endpoint_directory

if TYPE_CHECKING:
    from .endpoint_registry import EndpointRegistry
    from .phone_endpoint import PhoneEndpoint


MANAGED_ENDPOINT_KINDS = frozenset({"browser", "sip_account"})
ENDPOINT_DEVICE_PREFIX = "phone_endpoint:"
BROWSER_PHONE_DEVICE_MODEL = "Home Assistant softphone"
SIP_ACCOUNT_DEVICE_MODEL = "SIP account"


def enum_value(value: object) -> str:
    """Return a stable lower-case value from a string-backed enum."""
    return str(getattr(value, "value", value) or "").strip().lower()


def is_managed_endpoint(endpoint: PhoneEndpoint) -> bool:
    """Return whether VoIP Stack owns the endpoint's HA device."""
    return enum_value(endpoint.kind) in MANAGED_ENDPOINT_KINDS


def endpoint_device_identifier(endpoint_id: str) -> str:
    """Return the stable Device Registry identifier for a logical phone."""
    return f"{ENDPOINT_DEVICE_PREFIX}{str(endpoint_id).strip()}"


def endpoint_config_subentry_id(
    hass: HomeAssistant,
    endpoint_id: str,
    registry: EndpointRegistry | None = None,
) -> str | None:
    """Return the config subentry currently backing an endpoint."""
    if registry is None:
        registry = endpoint_directory(hass)
    return registry.subentry_ids.get(str(endpoint_id or "").strip())


def endpoint_device_info(endpoint: PhoneEndpoint) -> DeviceInfo | None:
    """Return DeviceInfo for integration-owned phones only.

    Returning ``None`` for ESPHome endpoints is intentional.  Supplying its
    identifiers from this integration would make Home Assistant add the VoIP
    Stack config entry to the ESPHome-owned device.
    """
    if not is_managed_endpoint(endpoint):
        return None
    model = BROWSER_PHONE_DEVICE_MODEL
    if enum_value(endpoint.kind) == "sip_account":
        model = SIP_ACCOUNT_DEVICE_MODEL
    return DeviceInfo(
        identifiers={(DOMAIN, endpoint_device_identifier(endpoint.endpoint_id))},
        name=endpoint.name,
        manufacturer="VoIP Stack",
        model=model,
        sw_version=INTEGRATION_VERSION,
        entry_type=DeviceEntryType.SERVICE,
    )


@callback
def async_ensure_endpoint_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    endpoint: PhoneEndpoint,
    registry: EndpointRegistry | None = None,
) -> DeviceEntry | None:
    """Resolve or create the HA device for ``endpoint``.

    ESPHome devices are returned by their existing Device Registry ID and are
    never passed to ``async_get_or_create``.  This keeps ownership with the
    ESPHome config entry and avoids unsupported cross-integration merging.
    """
    device_registry = dr.async_get(hass)
    if not is_managed_endpoint(endpoint):
        device_id = str(getattr(endpoint, "device_id", "") or "")
        return device_registry.async_get(device_id) if device_id else None

    info = endpoint_device_info(endpoint)
    assert info is not None
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=endpoint_config_subentry_id(
            hass, endpoint.endpoint_id, registry
        ),
        **info,
    )
    if registry is not None and str(getattr(endpoint, "device_id", "") or "") != device.id:
        registry.update(endpoint.endpoint_id, device_id=device.id)
    return device


def endpoint_public_attributes(endpoint: PhoneEndpoint) -> dict[str, Any]:
    """Return low-churn, recorder-safe attributes common to phone entities."""
    return {
        "endpoint_id": endpoint.endpoint_id,
        "endpoint_kind": enum_value(endpoint.kind),
        "extension": endpoint.extension,
        "capabilities": sorted(str(item) for item in endpoint.capabilities),
    }


def endpoint_call_state_attributes(
    endpoint: PhoneEndpoint | None,
    payload: dict[str, object],
    *,
    extra_keys: tuple[str, ...] = (),
    include_empty: bool = False,
) -> dict[str, Any]:
    """Return stable endpoint and active-call attributes for one phone sensor."""
    attributes = endpoint_public_attributes(endpoint) if endpoint is not None else {}
    keys = (
        "call_id",
        "direction",
        "ingress",
        "peer_name",
        "terminal_reason",
        *extra_keys,
    )
    attributes.update(
        {
            key: payload.get(key, "")
            for key in keys
            if include_empty or payload.get(key) not in (None, "")
        }
    )
    return attributes


@callback
def async_link_endpoint_entity(
    registry: EndpointRegistry | None,
    endpoint_id: str,
    entity_id: str | None,
) -> None:
    """Index an entity under its endpoint without mutating HA device ownership."""
    if registry is None or not entity_id:
        return
    endpoint = registry.get(endpoint_id)
    if endpoint is None or entity_id in endpoint.entity_ids:
        return
    registry.update(
        endpoint_id,
        entity_ids=frozenset((*endpoint.entity_ids, entity_id)),
    )
