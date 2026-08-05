"""Typed config-entry runtime for VoIP Stack."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .endpoint_registry import EndpointRegistry

if TYPE_CHECKING:
    from .call_registry import CallRegistry
    from .pbx_runtime import SipEndpointRuntime
    from .phone_control import PhoneAdapterRegistry


@dataclass(slots=True)
class VoipStackRuntime:
    """Entry-scoped configuration used by the live SIP runtime."""

    transport_config: dict[str, Any]
    assist_config: dict[str, Any]
    trunk_config: dict[str, Any]
    endpoints: EndpointRegistry
    phones: PhoneAdapterRegistry
    preferred_phone_device_id: str = ""
    debug_mode: bool = False
    media_capture: bool = False
    calls: CallRegistry | None = None
    sip: SipEndpointRuntime | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    rtp_port_pool: dict[str, Any] = field(default_factory=dict)
    next_rtp_port: int = 0


type VoipStackConfigEntry = ConfigEntry[VoipStackRuntime]


def runtime_data(hass: HomeAssistant) -> VoipStackRuntime | None:
    """Return the single loaded entry runtime, if setup has reached it."""

    config_entries = getattr(hass, "config_entries", None)
    async_entries = getattr(config_entries, "async_entries", None)
    if not callable(async_entries):
        return None
    for entry in async_entries(DOMAIN):
        value = getattr(entry, "runtime_data", None)
        if isinstance(value, VoipStackRuntime):
            return value
    return None


def call_projection(hass: HomeAssistant) -> CallRegistry | None:
    """Return the entry-owned observable call index, if initialized."""

    runtime = runtime_data(hass)
    return runtime.calls if runtime is not None else None


def sip_endpoint_runtime(hass: HomeAssistant) -> SipEndpointRuntime | None:
    """Return the entry-owned authoritative SIP runtime, if active."""

    runtime = runtime_data(hass)
    return runtime.sip if runtime is not None else None
