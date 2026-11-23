"""Project native hardware associations from configured SIP accounts and Contacts."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
)
from .browser_phone_data import _endpoint_registry


def device_is_phonebook_available(hass: HomeAssistant, device: dict) -> bool:
    """Project SIP registration; API entity state cannot make a phone routable."""
    return bool(device.get('endpoint_type') == EndpointKind.SIP_ACCOUNT.value and device.get('registered'))


def device_transport(device: dict) -> str:
    """Read the endpoint-declared SIP signaling transport."""
    value = str(device.get("sip_transport") or "").lower()
    return value if value in ("udp", "tcp", "tls") else ""


async def _get_voip_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Read the canonical account without creating or changing an endpoint."""
    from .core import sip
    from .device_resolver import get_resolver
    from .mailbox import mailbox_snapshot
    from .parking_config import parking_settings
    from .phone_config import phone_endpoint_id, phone_subentries
    from .runtime_data import sip_registrar
    from .store import config_entry

    registry = _endpoint_registry(hass)
    entry = config_entry(hass)
    if registry is None or entry is None:
        return []
    hardware = {row["device_id"]: row for row in await get_resolver(hass).list_devices()}
    registrar = sip_registrar(hass)
    registered = {row.id.casefold(): row for row in registrar.registered_roster_entries()} if registrar is not None and registrar.enabled else {}
    parking = parking_settings(hass)
    result = []
    for row in phone_subentries(entry):
        endpoint = registry.get(phone_endpoint_id(row))
        device = hardware.get(str(row.data.get("device_id") or ""))
        if (endpoint is None or endpoint.kind is not EndpointKind.SIP_ACCOUNT or device is None
                or endpoint.device_id != device["device_id"]):
            continue
        contact = registered.get(endpoint.username.casefold())
        uri = sip.parse_sip_uri(contact.sip_uri) if contact is not None else None
        wire = dict(contact.metadata) if contact is not None else {}
        available = bool(contact is not None and endpoint.availability is not EndpointAvailability.UNAVAILABLE)
        result.append({
            **device, **wire, "hardware_name": device["name"], "name": endpoint.name,
            "endpoint_id": endpoint.endpoint_id, "endpoint_username": endpoint.username,
            "endpoint_type": EndpointKind.SIP_ACCOUNT.value, "sip_uri_user": endpoint.username,
            "account_device_id": endpoint.account_device_id,
            "availability": EndpointAvailability.AVAILABLE.value if available else EndpointAvailability.OFFLINE.value,
            "registered": available, "managed": bool(row.data.get("managed_esphome")),
            "host": uri.host if uri is not None else "", "sip_port": (uri.port or 5060) if uri is not None else None,
            "sip_transport": wire.get("sip_transport", ""),
            "extension": endpoint.extension, "ring_group": endpoint.ring_group, "queues": list(endpoint.queues),
            "conference_group": endpoint.conference_group, "conference_ring": endpoint.conference_ring,
            "capabilities": sorted(endpoint.capabilities), "voicemail": mailbox_snapshot(hass, endpoint.endpoint_id),
            "parking": {"enabled": bool(parking.get("enabled")), "unavailable_reason": parking.get("unavailable_reason", "")},
        })
    return result
