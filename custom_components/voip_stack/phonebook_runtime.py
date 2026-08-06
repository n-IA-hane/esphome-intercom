"""Runtime helpers for publishing the HA-managed SIP phonebook."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .device_resolver import get_resolver
from .peer import Peer
from .runtime_data import runtime_data, sip_registrar
from .websocket_api import _get_voip_devices

_LOGGER = logging.getLogger(__name__)


def format_entry_unified(peer: Peer) -> str:
    """Return the compact ESP phonebook row for a peer."""
    name = peer.name
    peer_ip = peer.host or ""
    if not peer_ip:
        return name
    tx = ";".join(peer.tx_formats or [])
    rx = ";".join(peer.rx_formats or [])
    sip_transport = str(
        (peer.device or {}).get("sip_transport") or ("tcp" if peer.is_ha else "")
    ).lower()
    if sip_transport not in {"tcp", "udp"}:
        return name
    sip_transport_token = "sip_tcp" if sip_transport == "tcp" else "sip_udp"
    return (
        f"{name}|{peer_ip}|{peer.sip_port or 5060}|"
        f"{peer.rtp_port or 40000}|{peer.audio_mode}|{tx}|{rx}|{sip_transport_token}"
    )


def registered_roster_entries(hass: HomeAssistant):
    """Return local SIP account roster entries with active Contact bindings."""

    registrar = sip_registrar(hass)
    entries = getattr(registrar, "registered_roster_entries", None)
    return list(entries()) if callable(entries) else []


def available_esphome_services(hass: HomeAssistant) -> set[str]:
    """Return currently registered ESPHome service names."""
    try:
        services = hass.services.async_services().get("esphome", {})
    except Exception:
        return set()
    if isinstance(services, dict):
        return set(services)
    return set(services or [])


async def push_roster_json_to_esps(
    hass: HomeAssistant,
    roster_json: str,
    *,
    target_services: set[str] | None = None,
) -> None:
    """Push the canonical JSON roster to every online ESP endpoint."""
    if not roster_json:
        return
    runtime = runtime_data(hass)
    if runtime is None:
        return
    async with runtime.phonebook_push_lock:
        devices = await _get_voip_devices(hass)
        services = available_esphome_services(hass)
        resolver = get_resolver(hass)
        for device in devices:
            if not device.get("host"):
                continue
            slug = resolver.route_id_for_host(device["host"])
            if not slug:
                _LOGGER.debug(
                    "Phonebook push skipped for %s: no ESPHome route id",
                    device.get("name"),
                )
                continue
            service_name = f"{slug}_set_roster_json"
            if target_services is not None and service_name not in target_services:
                continue
            if service_name not in services:
                _LOGGER.debug(
                    "Phonebook push skipped for %s: missing esphome.%s",
                    device.get("name"),
                    service_name,
                )
                continue
            if runtime.phonebook_delivered_roster.get(service_name) == roster_json:
                continue
            try:
                await hass.services.async_call(
                    "esphome",
                    service_name,
                    {"roster_json": roster_json},
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.error(
                    "Phonebook JSON push to %s failed: %s", device.get("name"), err
                )
                continue
            runtime.phonebook_delivered_roster[service_name] = roster_json
            _LOGGER.info(
                "Phonebook JSON pushed to %s via esphome.%s",
                device.get("name"),
                service_name,
            )
