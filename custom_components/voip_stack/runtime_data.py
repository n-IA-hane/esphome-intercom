"""Typed config-entry runtime for VoIP Stack."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .endpoint_registry import EndpointRegistry

if TYPE_CHECKING:
    from .phone_control import PhoneAdapterRegistry


@dataclass(slots=True)
class VoipStackRuntime:
    """Entry-scoped configuration used by the live SIP runtime."""

    transport_config: dict[str, Any]
    assist_config: dict[str, Any]
    trunk_config: dict[str, Any]
    endpoints: EndpointRegistry
    phones: PhoneAdapterRegistry
    debug_mode: bool = False
    media_capture: bool = False


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
