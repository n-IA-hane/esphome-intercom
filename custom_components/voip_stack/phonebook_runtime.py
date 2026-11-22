"""Runtime projections for the existing Home Assistant phonebook sensor."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .peer import Peer
from .runtime_data import sip_registrar


def format_entry_unified(peer: Peer) -> str:
    """Return the compact row retained by the existing HA phonebook sensor."""
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
        f"{peer.rtp_port or 40000}|{peer.audio_mode or ''}|{tx}|{rx}|{sip_transport_token}"
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
