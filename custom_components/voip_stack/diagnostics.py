"""Privacy-safe diagnostics for VoIP Stack."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .config import trunk_config
from .const import DOMAIN, INTEGRATION_VERSION
from .runtime_diagnostics import runtime_resource_snapshot
from .runtime_data import (
    call_projection,
    endpoint_directory,
    runtime_data,
    sip_endpoint_manager,
    sip_trunk,
)


# SIP identities, routing aliases and network topology are private even when
# they are not authentication secrets. Keep public diagnostics useful through
# allowlisted runtime summaries and redact all configured identity fields.
TO_REDACT = {
    "address",
    "assist_pipeline",
    "auth_username",
    "callee",
    "caller",
    "contact",
    "contacts",
    "device_id",
    "discovery_keys",
    "display_name",
    "endpoint_id",
    "entity_id",
    "entry_id",
    "extension",
    "ha_softphone_conference_group",
    "ha_softphone_extension",
    "ha_softphone_ring_group",
    "host",
    "name",
    "number",
    "offline_forward_target",
    "outbound_proxy",
    "password",
    "peer",
    "peer_name",
    "phonebook_contacts",
    "ring_group",
    "conference_group",
    "sip_accounts",
    "sip_uri",
    "subentry_id",
    "title",
    "trunk_auth_username",
    "trunk_domain",
    "trunk_inbound_default_target",
    "trunk_outbound_proxy",
    "trunk_password",
    "trunk_server",
    "trunk_username",
    "unique_id",
    "uri",
    "username",
}


def _enum_value(value: object) -> str:
    """Return a stable value for string-backed enums."""

    return str(getattr(value, "value", value) or "").strip().lower()


def _endpoints(hass: HomeAssistant) -> tuple[Any, ...]:
    registry = endpoint_directory(hass)
    values = getattr(registry, "endpoints", ())
    return tuple(values) if values is not None else ()


def _endpoint_summary(hass: HomeAssistant) -> dict[str, Any]:
    """Aggregate endpoints without exposing routable identities."""

    kinds: Counter[str] = Counter()
    availability: Counter[str] = Counter()
    capabilities: Counter[str] = Counter()
    active = 0
    for endpoint in _endpoints(hass):
        kinds[_enum_value(getattr(endpoint, "kind", "unknown")) or "unknown"] += 1
        availability[
            _enum_value(getattr(endpoint, "availability", "unknown")) or "unknown"
        ] += 1
        for capability in getattr(endpoint, "capabilities", ()) or ():
            token = str(capability or "").strip().lower()
            if token:
                capabilities[token] += 1
        if bool(getattr(endpoint, "active_call_id", "")):
            active += 1
    return {
        "total": sum(kinds.values()),
        "active": active,
        "by_kind": dict(sorted(kinds.items())),
        "by_availability": dict(sorted(availability.items())),
        "by_capability": dict(sorted(capabilities.items())),
    }


def _signaling_summary(hass: HomeAssistant) -> dict[str, Any]:
    """Return bounded listener state without Call-IDs or peer addresses."""

    endpoint = sip_endpoint_manager(hass)
    snapshot = getattr(endpoint, "snapshot", None)
    if not callable(snapshot):
        return {"configured": False}
    data = snapshot()
    return {
        "configured": True,
        "udp_ready": bool(getattr(data, "udp_ready", False)),
        "tcp_ready": bool(getattr(data, "tcp_ready", False)),
        "pending_transactions": int(
            getattr(data, "pending_transactions", 0) or 0
        ),
        "active_dialogs": int(getattr(data, "active_dialogs", 0) or 0),
        "last_status_code": int(getattr(data, "last_sip_status_code", 0) or 0),
    }


def _trunk_summary(hass: HomeAssistant, bucket: dict[str, Any]) -> dict[str, Any]:
    """Return registration health without the provider identity."""

    trunk = sip_trunk(hass)
    snapshot = getattr(trunk, "snapshot", None)
    if not callable(snapshot):
        configured = bool(trunk_config(hass).get("trunk_enabled"))
        return {"enabled": configured, "registered": False}
    data = dict(snapshot())
    return {
        "enabled": bool(data.get("trunk_enabled")),
        "registered": bool(data.get("trunk_registered")),
        "status_code": int(data.get("trunk_status_code") or 0),
        "transport": str(data.get("trunk_transport") or ""),
        "expires_at": float(data.get("trunk_expires_at") or 0.0),
    }


def _runtime_summary(hass: HomeAssistant, bucket: dict[str, Any]) -> dict[str, Any]:
    runtime = runtime_data(hass)
    return {
        "endpoints": _endpoint_summary(hass),
        "signaling": _signaling_summary(hass),
        "trunk": _trunk_summary(hass, bucket),
        "resources": runtime_resource_snapshot(
            bucket,
            call_projection(hass),
            detailed=False,
            rtp_port_pool=runtime.rtp_port_pool if runtime is not None else None,
            call_artifacts=runtime.sip if runtime is not None else None,
        ),
    }


def _endpoint_for_device(hass: HomeAssistant, device: DeviceEntry) -> Any | None:
    registry = endpoint_directory(hass)
    by_device_id = getattr(registry, "by_device_id", None)
    if callable(by_device_id) and (endpoint := by_device_id(device.id)) is not None:
        return endpoint

    for domain, identifier in getattr(device, "identifiers", ()) or ():
        if domain != DOMAIN:
            continue
        prefix = "phone_endpoint:"
        if str(identifier).startswith(prefix):
            get_endpoint = getattr(registry, "get", None)
            if callable(get_endpoint):
                return get_endpoint(str(identifier)[len(prefix) :])
    return None


def _device_summary(hass: HomeAssistant, device: DeviceEntry) -> dict[str, Any]:
    endpoint = _endpoint_for_device(hass, device)
    if endpoint is None:
        return {"found": False}
    kind = _enum_value(getattr(endpoint, "kind", "unknown"))
    summary = {
        "found": True,
        "kind": kind,
        "availability": _enum_value(
            getattr(endpoint, "availability", "unknown")
        ),
        "capabilities": sorted(
            str(item) for item in (getattr(endpoint, "capabilities", ()) or ())
        ),
        "dnd": bool(getattr(endpoint, "dnd", False)),
        "active": bool(getattr(endpoint, "active_call_id", "")),
        "entity_count": len(getattr(endpoint, "entity_ids", ()) or ()),
        "ring_group_configured": bool(getattr(endpoint, "ring_group", "")),
        "conference_group_configured": bool(
            getattr(endpoint, "conference_group", "")
        ),
        "conference_ring": bool(getattr(endpoint, "conference_ring", False)),
    }
    if kind == "sip_account":
        summary["offline_policy"] = _enum_value(
            getattr(endpoint, "offline_policy", "unknown")
        )
    return summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return privacy-safe diagnostics for the integration entry."""

    bucket = hass.data.get(DOMAIN, {})
    return {
        "integration": {
            "version": INTEGRATION_VERSION,
            "entry_state": _enum_value(getattr(entry, "state", "unknown")),
        },
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "runtime": _runtime_summary(hass, bucket),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return privacy-safe diagnostics for one logical phone device."""

    bucket = hass.data.get(DOMAIN, {})
    runtime = runtime_data(hass)
    return {
        "integration": {
            "version": INTEGRATION_VERSION,
            "entry_state": _enum_value(getattr(entry, "state", "unknown")),
        },
        "phone": _device_summary(hass, device),
        "resources": runtime_resource_snapshot(
            bucket,
            call_projection(hass),
            detailed=False,
            rtp_port_pool=runtime.rtp_port_pool if runtime is not None else None,
            call_artifacts=runtime.sip if runtime is not None else None,
        ),
    }
