"""Borrow ESPHome's native phone inventory and its exact API connection.

HA resolves ordinary actions by service name and emits their parameters on the
service bus. SIP provisioning must keep credentials private and address the MAC,
connection and action inventory verified for this delivery. Different devices
can register the same public action name.

The ESPHome config entry supplies all references. SDK types are imported for
static checking so that standard SIP phones can load independently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aioesphomeapi import APIClient, DeviceInfo, UserService
    from homeassistant.components.esphome.entry_data import RuntimeEntryData


def normalize_mac(value: object) -> str:
    """Accept only a complete MAC, independently of its presentation."""
    mac = str(value or "").replace(":", "").replace("-", "").casefold()
    if re.fullmatch(r"[0-9a-f]{12}", mac) is None:
        raise ValueError("Managed phones require a complete MAC")
    return mac


def runtime_for_entry(origin) -> RuntimeEntryData | None:
    return getattr(origin, "runtime_data", None)


def device_info(runtime: RuntimeEntryData | None) -> DeviceInfo | None:
    return getattr(runtime, "device_info", None)


def connected(runtime: RuntimeEntryData | None) -> bool:
    return getattr(runtime, "available", False) and getattr(
        getattr(runtime, "client", None), "is_connected", False
    )


def connection_scope(runtime: RuntimeEntryData) -> tuple[RuntimeEntryData, APIClient]:
    """Identity used by public HA text-entity membership delivery."""
    return runtime, runtime.client


def connection_is_current(scope: tuple[RuntimeEntryData, APIClient]) -> bool:
    runtime, client = scope
    return runtime.client is client


def action_names(runtime: RuntimeEntryData) -> set[str]:
    return {service.name for service in runtime.services.values()}


def verified_action_names(origin, mac: str) -> set[str] | None:
    """Retained inventories survive disconnect, but incomplete or foreign ones cannot decide firmware mode."""
    runtime = runtime_for_entry(origin)
    complete = getattr(runtime, "first_connect_done", None)
    info = device_info(runtime)
    if complete is None or not complete.is_set() or info is None:
        return None
    try:
        if normalize_mac(info.mac_address) != normalize_mac(mac):
            return None
    except ValueError:
        return None
    if not isinstance(getattr(runtime, "services", None), Mapping):
        return None
    return action_names(runtime)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ProvisioningActions:
    """References captured for one delivery. The ESPHome entry owns all three."""

    _runtime: RuntimeEntryData
    _client: APIClient
    _services: Mapping[int, UserService]

    @classmethod
    def capture(cls, runtime: RuntimeEntryData) -> ProvisioningActions:
        return cls(runtime, runtime.client, runtime.services)

    def current(self, origin) -> bool:
        return (
            runtime_for_entry(origin) is self._runtime
            and self._runtime.client is self._client
            and self._runtime.services is self._services
            and self._runtime.available
            and self._client.is_connected
        )

    def same_owner(self, previous: ProvisioningActions | None) -> bool:
        return (
            previous is not None
            and previous._runtime is self._runtime
            and previous._client is self._client
            and previous._services is self._services
        )

    def by_name(self) -> dict[str, UserService]:
        return {service.name: service for service in self._services.values()}

    async def execute(self, service: UserService, data: dict) -> None:
        """Send the captured UserService key via the same client whose inventory was checked."""
        await self._client.execute_service(service, data)
