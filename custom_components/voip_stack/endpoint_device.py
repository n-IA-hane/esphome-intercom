"""Native Home Assistant service devices for VoIP Stack phone accounts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

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
    return enum_value(endpoint.kind) in MANAGED_ENDPOINT_KINDS or bool(endpoint.account_device_id)


def endpoint_device_identifier(endpoint_id: str) -> str:
    """Return the stable Device Registry identifier for a logical phone."""
    return f"{ENDPOINT_DEVICE_PREFIX}{str(endpoint_id).strip()}"


def account_device_id_for_endpoint(hass: HomeAssistant, entry: ConfigEntry, endpoint_id: str) -> str:
    """Recover an account's native service device when rebuilding an offline ESP endpoint."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, endpoint_device_identifier(endpoint_id)), entry.entry_id
    )
    return device.id if device is not None else ""


def endpoint_config_subentry_id(
    hass: HomeAssistant,
    endpoint_id: str,
    registry: EndpointRegistry | None = None,
) -> str | None:
    """Return the config subentry currently backing an endpoint."""
    if registry is None:
        registry = endpoint_directory(hass)
    return registry.subentry_ids.get(str(endpoint_id or "").strip())


def account_device_info(endpoint_id: str, name: str, *, hardware_device_id: str = "") -> DeviceInfo:
    """Use the same stable service identity for every SIP line."""
    info = DeviceInfo(
        identifiers={(DOMAIN, endpoint_device_identifier(endpoint_id))},
        name=name,
        manufacturer="VoIP Stack",
        model=SIP_ACCOUNT_DEVICE_MODEL,
        model_id="esphome_sip_account" if hardware_device_id else "sip_account",
        sw_version=INTEGRATION_VERSION,
        entry_type=DeviceEntryType.SERVICE,
    )
    if hardware_device_id:
        info["configuration_url"] = f"homeassistant://config/devices/device/{quote(hardware_device_id, safe='')}"
    else:
        info["configuration_url"] = (
            "homeassistant://voip-stack-popup?endpoint_id="
            f"{quote(endpoint_id, safe='')}"
        )
    return info


def endpoint_device_info(endpoint: PhoneEndpoint) -> DeviceInfo | None:
    """Associate PBX entities with the SIP account service device, when present."""
    if not is_managed_endpoint(endpoint):
        return None
    return account_device_info(endpoint.endpoint_id, endpoint.name,
                               hardware_device_id=endpoint.device_id if endpoint.account_device_id else "")


@callback
def async_ensure_endpoint_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    endpoint: PhoneEndpoint,
    registry: EndpointRegistry | None = None,
) -> DeviceEntry | None:
    """Resolve or create the HA device for ``endpoint``.

    A direct ESPHome phone has only its hardware device. For a managed phone
    the PBX service device and the ESPHome hardware retain separate owners.
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
    if registry is not None and not endpoint.account_device_id and endpoint.device_id != device.id:
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
    old_entity_id: str | None = None,
) -> None:
    """Index an entity under its endpoint without mutating HA device ownership."""
    if registry is None or not entity_id:
        return
    endpoint = registry.get(endpoint_id)
    if endpoint is None:
        return
    entity_ids = (endpoint.entity_ids - {old_entity_id}) | {entity_id}
    if entity_ids != endpoint.entity_ids:
        registry.update(endpoint_id, entity_ids=frozenset(entity_ids))
