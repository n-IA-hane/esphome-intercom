"""Push HA-owned mailbox snapshots over the authenticated ESPHome API."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import network
from homeassistant.components.esphome.const import CLIENT_INFO
from homeassistant.const import EVENT_SERVICE_REGISTERED, EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .device_resolver import entity_role, slugify_route_id
from .endpoint_lifecycle import create_runtime_task
from .esphome_actions import async_call_action, has_action
from .mailbox import EVENT_VOICEMAIL_CHANGED, mailbox_snapshot
from .websocket_api import _get_voip_devices

_LOGGER = logging.getLogger(__name__)
_ACTION = "set_message_waiting"
_OFFLINE = {STATE_UNAVAILABLE, STATE_UNKNOWN}


class EspHomeMailboxBridge:
    """One entry-owned delivery cache, with no polling or additional mailbox."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._lock = asyncio.Lock()
        self._delivered: dict[str, tuple[dict, int]] = {}
        self._epochs: dict[str, int] = {}
        self._unsubscribers = []
        self._closed = False

    @callback
    def start(self) -> None:
        self._unsubscribers = [
            self.hass.bus.async_listen(EVENT_VOICEMAIL_CHANGED, self._mailbox_changed),
            self.hass.bus.async_listen(EVENT_SERVICE_REGISTERED, self._service_registered),
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed),
        ]

    @callback
    def unsubscribe(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    def _native_entry(self, device_id: str):
        device = dr.async_get(self.hass).async_get(device_id)
        entry = self.hass.config_entries.async_get_entry(device.config_entry_id) if device else None
        return entry if entry is not None and entry.domain == "esphome" else None

    @callback
    def _invalidate(self, service: str) -> None:
        # An in-flight write belongs to the earlier API connection. Its return
        # cannot suppress the snapshot needed by the replacement connection.
        self._epochs[service] = self._epochs.get(service, 0) + 1

    @callback
    def _mailbox_changed(self, event) -> None:
        endpoint_id = str(event.data.get("endpoint_id") or "")
        if endpoint_id and not self._closed:
            create_runtime_task(self.hass, self.async_push(endpoint_id=endpoint_id))

    @callback
    def _service_registered(self, event) -> None:
        service = str(event.data.get("service") or "")
        if self._closed or event.data.get("domain") != "esphome" or not service.endswith(f"_{_ACTION}"):
            return
        self._invalidate(service)
        create_runtime_task(self.hass, self.async_push(service=service))

    @callback
    def _state_changed(self, event) -> None:
        if self._closed:
            return
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or (old is not None and old.state not in _OFFLINE and new.state not in _OFFLINE):
            return
        entity = er.async_get(self.hass).async_get(event.data["entity_id"])
        if entity is None or entity_role(entity) not in {"voip_state", "voip_endpoint"}:
            return
        entry = self._native_entry(entity.device_id)
        if entry is None:
            return
        service = f"{slugify_route_id(entry.data.get('device_name') or entry.title)}_{_ACTION}"
        self._invalidate(service)
        if new.state not in _OFFLINE:
            create_runtime_task(self.hass, self.async_push(service=service))

    async def async_push(self, *, endpoint_id: str = "", service: str = "") -> None:
        async with self._lock:
            if not self._closed:
                await self._push(endpoint_id=endpoint_id, service=service)

    async def _push(self, *, endpoint_id: str = "", service: str = "", unavailable: bool = False) -> None:
        for device in await _get_voip_devices(self.hass):
            identity = str(device.get("endpoint_id") or f"esphome:{device['device_id']}")
            action_service = f"{device.get('route_id') or ''}_{_ACTION}"
            if (endpoint_id and endpoint_id != identity) or (service and service != action_service):
                continue
            if not has_action(self.hass, device, _ACTION):
                continue
            entry = self._native_entry(device["device_id"])
            api_host = entry.data.get("host") if entry is not None else None
            if not api_host:
                continue
            snapshot = mailbox_snapshot(self.hass, identity)
            available = bool(not unavailable and snapshot["enabled"])
            try:
                # The API and SIP can use different hosts, including proxies.
                source_address = await network.async_get_source_ip(self.hass, target_ip=api_host)
                data = {
                    "available": available,
                    "waiting": available and snapshot["new"] > 0,
                    "new_messages": snapshot["new"] if available else 0,
                    "old_messages": snapshot["old"] if available else 0,
                    "source_client_info": CLIENT_INFO,
                    "source_client_address": source_address,
                }
                epoch = self._epochs.get(action_service, 0)
                if self._delivered.get(action_service) == (data, epoch):
                    continue
                await async_call_action(self.hass, device, _ACTION, data)
            except (HomeAssistantError, OSError) as error:
                _LOGGER.debug("Mailbox snapshot to %s could not be delivered: %s", device.get("name"), error)
                continue
            self._delivered[action_service] = (data, epoch)

    async def async_close(self) -> None:
        first = not self._closed
        self._closed = True
        self.unsubscribe()
        async with self._lock:
            # Concurrent shutdown callers share this existing delivery barrier.
            if first:
                await self._push(unavailable=True)
                self._delivered.clear()
                self._epochs.clear()
