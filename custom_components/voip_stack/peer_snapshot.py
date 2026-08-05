"""Build the canonical runtime snapshot of SIP-capable peers."""

from __future__ import annotations

import logging

from homeassistant.components import network
from homeassistant.core import HomeAssistant

from .audio_format import HA_SIP_PCM_FORMATS
from .config import transport_config
from .const import DOMAIN, HA_SOFTPHONE_ENDPOINT_ENTITY_ID
from .device_resolver import parse_voip_endpoint
from .peer import Peer
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointAvailability,
    EndpointKind,
)
from .runtime_data import endpoint_directory
from .websocket_api import _get_voip_devices

_LOGGER = logging.getLogger(__name__)


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


def device_is_phonebook_available(hass: HomeAssistant, device: dict) -> bool:
    """Return whether an ESP should be advertised to other VoIP peers."""
    entities = device.get("entities") or {}
    endpoint_entity = entities.get("voip_endpoint")
    if not endpoint_entity:
        return False
    endpoint_state = hass.states.get(endpoint_entity)
    if endpoint_state is None or str(endpoint_state.state).strip().lower() in (
        "",
        "unknown",
        "unavailable",
    ):
        return False

    state_entity = entities.get("voip_state")
    if state_entity:
        state = hass.states.get(state_entity)
        if state is None or str(state.state).strip().lower() in (
            "unknown",
            "unavailable",
        ):
            return False
    return True


def device_transport(device: dict) -> str:
    """Read the endpoint-declared SIP signaling transport."""
    value = str(device.get("sip_transport") or "").lower()
    return value if value in ("udp", "tcp") else ""


async def async_build_peer_snapshot(hass: HomeAssistant) -> list[Peer]:
    """Snapshot every online peer published through endpoint sensors."""
    devices = await _get_voip_devices(hass)
    cfg = transport_config(hass)
    out: list[Peer] = []
    for device in devices:
        name = device.get("name") or ""
        host = device.get("host") or ""
        if not name or not host:
            continue
        if not device_is_phonebook_available(hass, device):
            _LOGGER.debug("Skipping offline VoIP peer from phonebook: %s", name or host)
            continue
        sip_transport = device_transport(device)
        if not sip_transport:
            _LOGGER.warning(
                "Skipping SIP peer %s from phonebook: endpoint did not publish sip_transport",
                name or host,
            )
            continue
        out.append(
            Peer(
                device=device,
                name=name,
                host=host,
                endpoint_id=str(device.get("endpoint_id") or ""),
                endpoint_kind=EndpointKind.ESPHOME.value,
                capabilities=tuple(device.get("capabilities") or ("audio", "dtmf")),
                sip_port=int(device.get("sip_port") or cfg["sip_port"]),
                rtp_port=int(device.get("rtp_port") or cfg["rtp_port"]),
                extension=str(device.get("extension") or ""),
                sip_uri_user=str(device.get("sip_uri_user") or ""),
                conference_group=str(device.get("conference_group") or ""),
                conference_ring=bool(device.get("conference_ring", False)),
                ring_group=str(device.get("ring_group") or ""),
                audio_mode=device.get("audio_mode", "full_duplex"),
                tx_formats=list(device.get("tx_formats") or []),
                rx_formats=list(device.get("rx_formats") or []),
            )
        )
        device["sip_transport"] = sip_transport

    endpoint_registry = endpoint_directory(hass)
    local_ip = await async_advertise_host(hass)
    browser_endpoints = [
        endpoint
        for endpoint in endpoint_registry.endpoints
        if endpoint.kind is EndpointKind.BROWSER
        and endpoint.availability is not EndpointAvailability.UNAVAILABLE
    ]
    for endpoint in browser_endpoints:
        formats = [fmt.wire_token() for fmt in HA_SIP_PCM_FORMATS[:8]]
        out.append(
            Peer(
                device={
                    "device_id": endpoint.device_id,
                    "endpoint_id": endpoint.endpoint_id,
                    "endpoint_type": EndpointKind.BROWSER.value,
                    "sip_transport": "tcp",
                    "capabilities": sorted(endpoint.capabilities),
                },
                name=endpoint.name,
                host=local_ip or "",
                endpoint_id=endpoint.endpoint_id,
                endpoint_kind=EndpointKind.BROWSER.value,
                capabilities=tuple(sorted(endpoint.capabilities)),
                local_ha=True,
                sip_port=int(cfg["sip_port"]),
                rtp_port=int(cfg["rtp_port"]),
                extension=endpoint.extension,
                conference_group=endpoint.conference_group,
                conference_ring=endpoint.conference_ring,
                ring_group=endpoint.ring_group,
                audio_mode="full_duplex",
                tx_formats=formats,
                rx_formats=formats,
            )
        )

    # Compatibility fallback for YAML-only or partially migrated setups where
    # no logical endpoint registry exists yet.
    ha_endpoint_state = hass.states.get(HA_SOFTPHONE_ENDPOINT_ENTITY_ID)
    ha_endpoint_payload = ""
    ha_endpoint_attrs = {}
    if ha_endpoint_state is not None:
        ha_endpoint_attrs = ha_endpoint_state.attributes or {}
        ha_endpoint_payload = str(
            ha_endpoint_attrs.get("endpoint") or ha_endpoint_state.state or ""
        )
    ha_endpoint = parse_voip_endpoint(ha_endpoint_payload)
    if not browser_endpoints and ha_endpoint is not None:
        out.append(
            Peer(
                device=None,
                name=ha_endpoint["name"],
                host=ha_endpoint["host"],
                endpoint_id=DEFAULT_ENDPOINT_ID,
                endpoint_kind=EndpointKind.BROWSER.value,
                capabilities=("audio", "dtmf"),
                local_ha=True,
                sip_port=int(ha_endpoint.get("sip_port") or cfg["sip_port"]),
                rtp_port=int(ha_endpoint.get("rtp_port") or cfg["rtp_port"]),
                extension=str(ha_endpoint.get("extension") or ""),
                conference_group=str(ha_endpoint_attrs.get("conference_group") or ""),
                conference_ring=bool(
                    ha_endpoint_attrs.get("conference_ring", False)
                ),
                ring_group=str(ha_endpoint_attrs.get("ring_group") or ""),
                audio_mode=ha_endpoint.get("audio_mode", "full_duplex"),
                tx_formats=[
                    fmt.wire_token() for fmt in ha_endpoint.get("tx_formats") or []
                ],
                rx_formats=[
                    fmt.wire_token() for fmt in ha_endpoint.get("rx_formats") or []
                ],
            )
        )
    elif not browser_endpoints:
        # The SIP endpoint starts before config-entry platforms. The deferred
        # phonebook sync discovers the sensor as soon as the platform is ready.
        if hass.data.get(DOMAIN, {}).get("ha_softphone_endpoint_sensor") is None:
            _LOGGER.debug(
                "HA softphone endpoint sensor is not ready; phonebook sync is deferred"
            )
        else:
            _LOGGER.warning(
                "HA softphone endpoint sensor is unavailable; HA will not appear in the SIP phonebook"
            )
    return out
