"""Privacy-safe System Health data for VoIP Stack."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .phone_endpoint import EndpointAvailability, EndpointKind
from .runtime_data import (
    runtime_data,
    sip_endpoint_manager,
    sip_registrar,
    sip_trunk,
)


@callback
def async_register(
    hass: HomeAssistant,
    register: system_health.SystemHealthRegistration,
) -> None:
    """Register the VoIP Stack health callback."""

    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Return bounded operational counters without SIP identities."""

    runtime = runtime_data(hass)
    endpoint = sip_endpoint_manager(hass)
    phones = tuple(runtime.endpoints.endpoints) if runtime is not None else ()
    registrar = sip_registrar(hass)
    registrar_snapshot = registrar.snapshot() if registrar is not None else {}
    trunk = sip_trunk(hass)
    calls = runtime.sip if runtime is not None else None
    port_pool = runtime.rtp_port_pool if runtime is not None else {}
    used_ports = port_pool.get("used") if isinstance(port_pool, dict) else None
    media = runtime.media if runtime is not None else None
    return {
        "sip_udp_ready": bool(getattr(endpoint, "udp_server", None)),
        "sip_tcp_ready": bool(getattr(endpoint, "tcp_server", None)),
        "advertise_host": (
            "configured"
            if runtime is not None
            and str(runtime.transport_config.get("advertise_host") or "").strip()
            else "automatic"
        ),
        "configured_phones": len(phones),
        "online_esphome_phones": sum(
            phone.kind is EndpointKind.ESPHOME
            and phone.availability is EndpointAvailability.AVAILABLE
            for phone in phones
        ),
        "registered_sip_accounts": int(
            registrar_snapshot.get("registrar_registered", 0)
        ),
        "trunk_registered": bool(getattr(trunk, "registered", False)),
        "active_calls": calls.active_count() if calls is not None else 0,
        "reserved_rtp_ports": len(used_ports) if isinstance(used_ports, set) else 0,
        "runtime_tasks": len(runtime.tasks) if runtime is not None else 0,
        "active_media_owners": sum(
            len(media.owners_for(channel))
            for channel in ("audio", "video")
        ) if media is not None else 0,
        "media_workers": (
            len(media.debug_capture_tasks) + int(media.transcoder is not None)
            if media is not None
            else 0
        ),
    }
