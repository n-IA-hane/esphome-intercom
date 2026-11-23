"""Build the canonical runtime snapshot of SIP-capable peers."""

from __future__ import annotations

from homeassistant.components import network
from homeassistant.core import HomeAssistant

from .config import transport_config
from .peer import Peer
from .phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
)
from .runtime_data import endpoint_directory
from .esphome_phone_inventory import device_is_phonebook_available as device_is_phonebook_available
from .esphome_phone_inventory import (
    _get_voip_devices, device_transport,
)


async def async_advertise_host(hass: HomeAssistant) -> str:
    """Return the IP/host Home Assistant should publish to SIP peers."""
    cfg = transport_config(hass)
    configured = (cfg.get("advertise_host") or "").strip()
    if configured:
        return configured
    addresses = await network.async_get_announce_addresses(hass)
    return addresses[0] if addresses else ""


def device_entity_state(hass: HomeAssistant, device: dict, key: str) -> str:
    """Read one usable state from an ESP phone descriptor."""
    entity_id = (device.get("entities") or {}).get(key)
    if not entity_id:
        return ""
    state = hass.states.get(entity_id)
    value = (state.state if state is not None else "").strip()
    return "" if value.lower() in ("unknown", "unavailable") else value


async def async_build_peer_snapshot(hass: HomeAssistant) -> list[Peer]:
    """Project existing accounts over current Contact bindings or their HA client."""
    from .account_runtime import account_has_ha_binding
    from .core import sip
    from .phonebook_runtime import registered_roster_entries

    physical = {row["endpoint_id"]: row for row in await _get_voip_devices(hass)}
    registry = endpoint_directory(hass)
    cfg = transport_config(hass)
    result, registered = [], set()
    for row in registered_roster_entries(hass):
        endpoint = registry.by_username(row.id)
        if endpoint is None or endpoint.kind is not EndpointKind.SIP_ACCOUNT:
            continue
        uri = sip.parse_sip_uri(row.sip_uri)
        device = {**physical.get(endpoint.endpoint_id, {}), **row.metadata,
                  "device_id": endpoint.device_id, "endpoint_id": endpoint.endpoint_id,
                  "endpoint_type": EndpointKind.SIP_ACCOUNT.value}
        transport = device_transport(device)
        if not transport:
            continue
        result.append(Peer(device=device, name=endpoint.name, host=uri.host,
            endpoint_id=endpoint.endpoint_id, endpoint_kind=EndpointKind.SIP_ACCOUNT.value,
            capabilities=tuple(sorted(endpoint.capabilities)), sip_port=uri.port or 5060,
            rtp_port=device.get("rtp_port"), sip_uri_user=endpoint.username, contact_uri=row.sip_uri,
            extension=endpoint.extension, conference_group=endpoint.conference_group,
            conference_ring=endpoint.conference_ring, ring_group=endpoint.ring_group))
        registered.add(endpoint.endpoint_id)
    local_ip = await async_advertise_host(hass)
    for endpoint in registry.endpoints:
        if (endpoint.kind is not EndpointKind.SIP_ACCOUNT or endpoint.endpoint_id in registered
                or endpoint.availability is EndpointAvailability.UNAVAILABLE
                or not account_has_ha_binding(hass, endpoint.endpoint_id)):
            continue
        result.append(Peer(device={"device_id": endpoint.device_id, "endpoint_id": endpoint.endpoint_id,
                "endpoint_type": EndpointKind.SIP_ACCOUNT.value, "sip_transport": "tcp"},
            name=endpoint.name, host=local_ip, endpoint_id=endpoint.endpoint_id,
            endpoint_kind=EndpointKind.SIP_ACCOUNT.value, sip_uri_user=endpoint.username,
            capabilities=tuple(sorted(endpoint.capabilities)), local_ha=True,
            sip_port=int(cfg["sip_port"]), rtp_port=int(cfg["rtp_port"]),
            extension=endpoint.extension, conference_group=endpoint.conference_group,
            conference_ring=endpoint.conference_ring, ring_group=endpoint.ring_group))
    return result
