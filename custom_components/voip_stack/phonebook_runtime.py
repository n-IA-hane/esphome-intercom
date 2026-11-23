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
        raise ValueError("A phonebook peer requires its current SIP host")
    transport = str((peer.device or {}).get("sip_transport") or ("tcp" if peer.is_ha else "")).lower()
    if transport not in {"tcp", "udp", "tls"}:
        raise ValueError("A phonebook peer requires its current SIP transport")
    if not peer.sip_uri_user or peer.sip_port is None:
        raise ValueError("A phonebook peer requires its canonical SIP username and port")
    return f"{name}|{peer.sip_uri_user}|{peer_ip}|{peer.sip_port}|{peer.rtp_port or 0}|{transport}"


def registered_roster_entries(hass: HomeAssistant):
    """Return local SIP account roster entries with active Contact bindings."""

    registrar = sip_registrar(hass)
    return list(registrar.registered_roster_entries()) if registrar is not None and registrar.enabled else []


def available_esphome_services(hass: HomeAssistant) -> set[str]:
    """Return currently registered ESPHome service names."""
    try:
        services = hass.services.async_services().get("esphome", {})
    except Exception:
        return set()
    if isinstance(services, dict):
        return set(services)
    return set(services or [])
