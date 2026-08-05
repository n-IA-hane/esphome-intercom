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
    from .device_resolver import VoipDeviceResolver
    from .pbx_runtime import SipEndpointRuntime
    from .phone_control import PhoneAdapterRegistry


@dataclass(slots=True)
class BrowserMediaRuntime:
    """Entry-owned browser transports, separate from authoritative call state."""

    active_sessions: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {"audio": {}, "video": {}}
    )
    owners: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {"audio": {}, "video": {}}
    )
    owner_locks: dict[str, asyncio.Lock] = field(
        default_factory=lambda: {
            "audio": asyncio.Lock(),
            "video": asyncio.Lock(),
        }
    )
    identity_locks: dict[str, Any] = field(default_factory=dict)
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)

    def sessions_for(self, channel: str) -> dict[str, Any]:
        """Return live sessions for one supported media channel."""

        return self.active_sessions[channel]

    def owners_for(self, channel: str) -> dict[str, Any]:
        """Return live owners for one supported media channel."""

        return self.owners[channel]

    def owner_lock_for(self, channel: str) -> asyncio.Lock:
        """Return the serialization lock for one supported media channel."""

        return self.owner_locks[channel]


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
    shutdown_task: asyncio.Task[Any] | None = None
    entity_managers: dict[str, Any] = field(default_factory=dict)
    entry_runtime_signature: dict[str, Any] | None = None
    entry_phone_signature: tuple[Any, ...] | None = None
    entry_phone_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    entry_contacts_signature: tuple[dict[str, Any], ...] | None = None
    esp_state_event_generations: dict[str, int] = field(default_factory=dict)
    device_resolver: VoipDeviceResolver | None = None
    rtp_port_pool: dict[str, Any] = field(default_factory=dict)
    next_rtp_port: int = 0
    media: BrowserMediaRuntime = field(default_factory=BrowserMediaRuntime)
    local_bridge: Any | None = None
    local_bridge_unsub: Any | None = None


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


def require_runtime_data(hass: HomeAssistant) -> VoipStackRuntime:
    """Return the loaded runtime or reject use outside its lifecycle."""

    runtime = runtime_data(hass)
    if runtime is None:
        raise RuntimeError("VoIP Stack runtime is unavailable")
    return runtime


def call_projection(hass: HomeAssistant) -> CallRegistry | None:
    """Return the entry-owned observable call index, if initialized."""

    runtime = runtime_data(hass)
    return runtime.calls if runtime is not None else None


def endpoint_directory(hass: HomeAssistant) -> EndpointRegistry:
    """Return the entry-owned logical phone directory."""

    return require_runtime_data(hass).endpoints


def call_runtime_artifacts(hass: HomeAssistant) -> SipEndpointRuntime:
    """Return call coordination from the authoritative SIP runtime."""

    runtime = sip_endpoint_runtime(hass)
    if runtime is None:
        raise RuntimeError("VoIP Stack SIP runtime is unavailable")
    return runtime


def sip_endpoint_runtime(hass: HomeAssistant) -> SipEndpointRuntime | None:
    """Return the entry-owned authoritative SIP runtime, if active."""

    runtime = runtime_data(hass)
    return runtime.sip if runtime is not None else None


def sip_component(hass: HomeAssistant, name: str) -> Any | None:
    """Return one component owned by the active SIP runtime."""

    runtime = sip_endpoint_runtime(hass)
    return runtime.component(name) if runtime is not None else None


def sip_registrar(hass: HomeAssistant) -> Any | None:
    """Return the local registrar without exposing its storage location."""

    return sip_component(hass, "registrar")


def sip_trunk(hass: HomeAssistant) -> Any | None:
    """Return the trunk client owned by the active SIP runtime."""

    return sip_component(hass, "trunk")


def conference_component(hass: HomeAssistant) -> Any | None:
    """Return the conference manager owned by the active SIP runtime."""

    return sip_component(hass, "conference_manager")


def sip_endpoint_manager(hass: HomeAssistant) -> Any | None:
    """Return the shared UDP/TCP endpoint manager owned by the SIP runtime."""

    runtime = sip_endpoint_runtime(hass)
    return runtime.component("udp_listener") if runtime is not None else None
