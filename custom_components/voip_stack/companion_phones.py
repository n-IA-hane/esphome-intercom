"""Discover native mobile phones and deliver addressed call invitations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .companion_protocol import CAPABILITY_KEY, COMMAND, CONF_MOBILE_APP_ENTRY_ID, CompanionCallToken, companion_endpoint_id
from .const import DOMAIN
from .endpoint_lifecycle import call_registry, create_runtime_task
from .phone_config import _add_phone_subentry, browser_phone_data, phone_subentries, sync_registry_from_entry
from .phone_endpoint import EndpointAvailability, EndpointKind
from .runtime_data import require_runtime_data
from .websocket_api import HA_SOFTPHONE_STATE_EVENT

_LOGGER = logging.getLogger(__name__)
_INVITATION_LIFETIME = 120.0


@dataclass(frozen=True, slots=True)
class CompanionBinding:
    registration_id: str
    mobile_device_id: str
    endpoint_id: str
    user_id: str
    webhook_id: str


@dataclass(frozen=True, slots=True)
class CompanionInvitation:
    token: CompanionCallToken
    expires: float
    caller: str


class CompanionPhones:
    """Own discovery and notification delivery, never routing or media resources."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.bindings: dict[str, CompanionBinding] = {}
        self.invitations: dict[str, CompanionInvitation] = {}
        self._entry_unsubs: dict[str, object] = {}
        self._sync_lock = asyncio.Lock()
        self._unsubs: list = []

    async def setup(self) -> None:
        await self.sync()
        self._unsubs.extend((
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_changed),
            self.hass.bus.async_listen(HA_SOFTPHONE_STATE_EVENT, self._call_changed),
            self.entry.add_update_listener(self._entry_changed),
        ))
        self.entry.async_on_unload(self.close)

    @callback
    def close(self) -> None:
        for unsubscribe in [*self._unsubs, *self._entry_unsubs.values()]:
            unsubscribe()
        self._unsubs.clear()
        self._entry_unsubs.clear()
        self.bindings.clear()
        self.invitations.clear()

    @callback
    def _device_changed(self, event) -> None:
        create_runtime_task(self.hass, self.sync())

    async def _entry_changed(self, hass, entry) -> None:
        await self.sync()

    async def sync(self) -> None:
        async with self._sync_lock:
            devices = dr.async_get(self.hass)
            bindings = {}
            existing = {str(p.data.get("endpoint_id")): p for p in phone_subentries(self.entry)}
            names = {str(p.data.get("name", p.title)).casefold() for p in existing.values()}
            for mobile in self.hass.config_entries.async_entries("mobile_app"):
                if mobile.entry_id not in self._entry_unsubs:
                    self._entry_unsubs[mobile.entry_id] = mobile.add_update_listener(self._entry_changed)
                if mobile.data.get("app_data", {}).get(CAPABILITY_KEY) != 1:
                    continue
                registered_devices = dr.async_entries_for_config_entry(devices, mobile.entry_id)
                if len(registered_devices) != 1:
                    continue
                device = registered_devices[0]
                endpoint_id = companion_endpoint_id(mobile.entry_id)
                binding = CompanionBinding(mobile.entry_id, device.id, endpoint_id,
                    str(mobile.data.get("user_id", "")), str(mobile.data.get("webhook_id", "")))
                if not binding.user_id or not binding.webhook_id:
                    continue
                bindings[endpoint_id] = binding
                if endpoint_id not in existing:
                    name = f"{device.name_by_user or device.name or mobile.title} (Companion)"
                    if name.casefold() in names:
                        name = f"{name} {mobile.entry_id[-6:]}"
                    names.add(name.casefold())
                    data = browser_phone_data(self.hass, self.entry, endpoint_id=endpoint_id)
                    data.update(kind=EndpointKind.COMPANION.value, name=name, enabled=False,
                        extension="", ring_group="", conference_group="", video_enabled=False,
                        send_video=False, **{CONF_MOBILE_APP_ENTRY_ID: mobile.entry_id})
                    _add_phone_subentry(self.hass, self.entry, data=data, title=name)
            self.bindings = bindings
            sync_registry_from_entry(self.hass, self.entry)
            directory = require_runtime_data(self.hass).endpoints
            for phone in phone_subentries(self.entry):
                endpoint_id = str(phone.data.get("endpoint_id", ""))
                endpoint = directory.get(endpoint_id)
                if endpoint is None or endpoint.kind is not EndpointKind.COMPANION:
                    continue
                available = endpoint_id in bindings and bool(phone.data.get("enabled", True))
                directory.update(endpoint_id, availability=EndpointAvailability.AVAILABLE if available else EndpointAvailability.UNAVAILABLE)

    @callback
    def _call_changed(self, event) -> None:
        endpoint_id = str(event.data.get("endpoint_id", ""))
        binding = self.bindings.get(endpoint_id)
        if binding is None:
            return
        phase = event.data.get("state")
        previous = self.invitations.get(endpoint_id)
        if phase == "ringing":
            call_id = str(event.data.get("call_id", ""))
            generation = call_registry(self.hass).current_generation(call_id)
            if generation is None:
                return
            token = CompanionCallToken(binding.registration_id, call_id, generation)
            if previous is not None and previous.token == token:
                return
            invitation = CompanionInvitation(token, time.monotonic() + _INVITATION_LIFETIME,
                str(event.data.get("caller") or event.data.get("peer_name") or "Home Assistant"))
            self.invitations[endpoint_id] = invitation
            create_runtime_task(self.hass, self._notify(binding, invitation, "ring"))
        elif phase not in {"connecting", "in_call", "ringing"} and previous is not None:
            self.invitations.pop(endpoint_id, None)
            create_runtime_task(self.hass, self._notify(binding, previous, "cancel"))

    async def _notify(self, binding: CompanionBinding, invitation: CompanionInvitation, action: str) -> None:
        from homeassistant.components.mobile_app.util import get_notify_service
        from homeassistant.exceptions import HomeAssistantError

        service = get_notify_service(self.hass, binding.webhook_id)
        if service is None:
            _LOGGER.warning("Companion notification service unavailable for device %s", binding.mobile_device_id)
            return
        try:
            await self.hass.services.async_call("notify", service, {
                "message": COMMAND,
                "data": {"call_action": action, "call_id": invitation.token.call_id,
                    "call_path": invitation.token.path(), "priority": "high", "ttl": 0},
            }, blocking=True)
        except HomeAssistantError:
            # Provider errors can include secret webhook IDs; report only the device.
            _LOGGER.warning("Failed to deliver Companion call to device %s", binding.mobile_device_id)
            current = self.invitations.get(binding.endpoint_id)
            registry = call_registry(self.hass)
            if action == "ring" and current == invitation and registry.is_generation_current(
                invitation.token.call_id, invitation.token.generation
            ):
                endpoint = require_runtime_data(self.hass).endpoints.get(binding.endpoint_id)
                if endpoint is not None:
                    await self.hass.services.async_call("voip_stack", "decline", {
                        "device_id": endpoint.device_id, "call_id": invitation.token.call_id,
                        "status": 480, "reason": "Companion unavailable",
                    }, blocking=True)
