"""Configure known ESPHome phones through their existing native API owner."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from homeassistant.components import network
from homeassistant.config_entries import ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import EVENT_SERVICE_REGISTERED, EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .config import transport_config
from .const import CONF_REGISTRAR_ENABLED
from .device_resolver import entity_role, slugify_route_id
from .endpoint_lifecycle import create_runtime_task
from .managed_phones import (
    AUTO_REGISTER_ACTION, CONF_MANAGED_PHONES, MANAGED_ACCOUNT_ID, ManagedPhone, managed_records,
)
from .phone_endpoint import EndpointAvailability, EndpointKind, PhoneEndpoint
from .runtime_data import require_runtime_data, sip_registrar
from .sip_registrar import generate_password, normalize_username

_LOGGER = logging.getLogger(__name__)
_OFFLINE = {"unknown", "unavailable", ""}


def _mac(value: str) -> str:
    return value.replace(":", "").replace("-", "").casefold()


class EspHomeProvisioner:
    """One entry-owned coordinator; account state stays in the config entry."""

    def __init__(self, hass, entry, *, ldap_port=1389, base_dn="ou=phonebook,dc=voip", imap_port=1143):
        self.hass, self.entry = hass, entry
        self.ldap_port, self.base_dn, self.imap_port = ldap_port, base_dn, imap_port
        self._lock = asyncio.Lock()
        self._unsubscribers = []
        self._delivered = {}
        self._task = None
        self._closed = False
        self._removed_origins = set()
        self._removed_devices = set()
        self._dirty = False
        self.advertise_host = ""
        self.tls = False
        self.directory_enabled = True
        self.imap_enabled = True

    @callback
    def start(self):
        self._unsubscribers = [
            self.hass.bus.async_listen(EVENT_SERVICE_REGISTERED, self._service_changed),
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed),
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_changed),
            self.hass.bus.async_listen("voip_stack_voicemail_changed", lambda _: self.schedule()),
            async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._entry_changed),
        ]
        self.schedule()

    @callback
    def schedule(self):
        self._dirty = True
        if not self._closed and (self._task is None or self._task.done()):
            self._task = create_runtime_task(self.hass, self._run_pending())

    async def _run_pending(self):
        while self._dirty and not self._closed:
            self._dirty = False
            await self.async_sync()

    @callback
    def _service_changed(self, event):
        if event.data.get("domain") == "esphome":
            name = str(event.data.get("service") or "")
            if name.endswith(("_" + AUTO_REGISTER_ACTION, "_set_account", "_configure_directory", "_configure_voicemail")):
                self.schedule()

    @callback
    def _state_changed(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if new is None or new.state in _OFFLINE or (old is not None and old.state not in _OFFLINE):
            return
        entity = er.async_get(self.hass).async_get(event.data["entity_id"])
        if entity is not None and entity_role(entity) == "voip_state":
            self._delivered.pop(entity.device_id, None)
            self.schedule()

    @callback
    def _device_changed(self, event):
        if event.data.get("action") == "remove":
            self._removed_devices.add(event.data["device_id"])
            self._revoke(lambda phone: phone.device_id == event.data["device_id"])
        if event.data.get("action") in {"create", "update", "remove"}:
            self.schedule()

    @callback
    def _entry_changed(self, change, entry):
        if entry.domain != "esphome":
            return
        if change is ConfigEntryChange.REMOVED:
            # Revoke before awaiting the lock; an in-flight RPC cannot recreate
            # the account whose owning integration was just removed.
            self._removed_origins.add(entry.entry_id)
            self._revoke(lambda phone: phone.origin_entry_id == entry.entry_id)
        self.schedule()

    def _revoke(self, predicate):
        registrar = sip_registrar(self.hass)
        if registrar is not None:
            revoked = {phone.username.casefold() for phone in managed_records(self.entry).values() if predicate(phone)}
            registrar.update_accounts([account for account in registrar.accounts.values()
                                       if account.username.casefold() not in revoked])

    def _save(self, records):
        data = {key: phone.record() for key, phone in records.items()}
        if data != self.entry.data.get(CONF_MANAGED_PHONES, {}):
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_MANAGED_PHONES: data})

    def _refresh_accounts(self):
        from .store import sip_accounts

        registrar = sip_registrar(self.hass)
        if registrar is not None:
            registrar.update_accounts(sip_accounts(self.hass))
            registrar.enabled = bool(transport_config(self.hass).get(CONF_REGISTRAR_ENABLED) or managed_records(self.entry))

    def _current(self, phone):
        return (not self._closed and phone.origin_entry_id not in self._removed_origins
                and phone.device_id not in self._removed_devices
                and self.hass.config_entries.async_get_entry(phone.origin_entry_id) is not None)

    def _principal(self, username):
        if self._closed:
            return None
        registrar = sip_registrar(self.hass)
        account = registrar.accounts.get(username.casefold()) if registrar is not None else None
        if account is None or not account.enabled:
            return None
        endpoint = require_runtime_data(self.hass).endpoints.by_username(account.username)
        if endpoint is None:
            return None
        if account.endpoint_id:
            phone = managed_records(self.entry).get(endpoint.device_id)
            if phone is None or not self._current(phone):
                return None
        return account, endpoint.endpoint_id

    async def authenticate(self, username, password):
        """The same canonical credential authenticates LDAP and IMAP."""
        principal = self._principal(username)
        return principal[1] if principal is not None and hmac.compare_digest(principal[0].password.encode(), password) else None

    async def authenticate_verifier(self, username, verifier):
        principal = self._principal(username)
        return principal[1] if principal is not None and verifier(principal[0].password) else None

    async def async_sync(self):
        async with self._lock:
            if self._closed:
                return
            directory_changed = False
            records = managed_records(self.entry)
            for phone in tuple(records.values()):
                origin = self.hass.config_entries.async_get_entry(phone.origin_entry_id)
                if (origin is None or phone.origin_entry_id in self._removed_origins
                        or phone.device_id in self._removed_devices
                        or dr.async_get(self.hass).async_get(phone.device_id) is None):
                    await self._purge(phone)
                    directory_changed = True
                    records.pop(phone.device_id, None)
            self._save(records)
            for device_id in self._removed_devices:
                endpoint = require_runtime_data(self.hass).endpoints.by_device_id(device_id)
                if endpoint is not None and endpoint.kind is EndpointKind.ESPHOME:
                    await self._purge_endpoint(endpoint.endpoint_id, endpoint.username)
                    directory_changed = True
            devices = dr.async_get(self.hass)
            candidates = [(origin, device) for origin in self.hass.config_entries.async_entries("esphome")
                          for device in dr.async_entries_for_config_entry(devices, origin.entry_id)]
            for origin, device in candidates:
                if origin.entry_id in self._removed_origins or device.id in self._removed_devices:
                    continue
                native = getattr(origin, "runtime_data", None)
                client = getattr(native, "client", None)
                services = {service.name: service for service in getattr(native, "services", {}).values()}
                if (AUTO_REGISTER_ACTION not in services or "set_account" not in services
                        or not getattr(client, "is_connected", False) or not getattr(native, "available", True)):
                    continue
                info = getattr(native, "device_info", None)
                if info is None or not any(kind == dr.CONNECTION_NETWORK_MAC and _mac(value) == _mac(info.mac_address)
                                           for kind, value in device.connections):
                    continue
                try:
                    username = normalize_username(info.name)
                    if len(username) > 64:
                        raise ValueError("The firmware device name exceeds the SIP username bound")
                    previous = records.get(device.id)
                    registrar = sip_registrar(self.hass)
                    collision = registrar.accounts.get(username.casefold()) if registrar is not None else None
                    if collision is not None and collision.endpoint_id != f"esphome:{device.id}":
                        raise ValueError("The SIP username is already owned by another phone")
                    extension = previous.extension if previous else ""
                    for entity in er.async_entries_for_device(er.async_get(self.hass), device.id):
                        if entity_role(entity) == "voip_extension":
                            state = self.hass.states.get(entity.entity_id)
                            if state is not None and state.state not in _OFFLINE:
                                extension = state.state.strip()
                    phone = ManagedPhone(
                        device_id=device.id, origin_entry_id=origin.entry_id, mac=info.mac_address,
                        username=username, password=previous.password if previous else generate_password(),
                        display_name=device.name_by_user or device.name or info.friendly_name or info.name,
                        route_id=slugify_route_id(info.name), api_host=origin.data.get("host", ""),
                        extension=extension,
                    )
                    records[device.id] = phone
                    self._save(records)
                    self._refresh_accounts()
                    registry = require_runtime_data(self.hass).endpoints
                    old = registry.get(phone.endpoint_id)
                    directory_changed |= previous != phone or old is None or (
                        old.name, old.username, old.extension
                    ) != (phone.display_name, phone.username, phone.extension)
                    if old is None:
                        registry.register(PhoneEndpoint(endpoint_id=phone.endpoint_id, name=phone.display_name,
                            username=phone.username, device_id=device.id, kind=EndpointKind.ESPHOME, extension=phone.extension,
                            capabilities=frozenset({"audio", "dtmf"}), availability=EndpointAvailability.OFFLINE))
                    else:
                        registry.update(phone.endpoint_id, name=phone.display_name, username=phone.username, extension=phone.extension)
                    await self._deliver(phone, native, services)
                except Exception as error:
                    # Exception types are sufficient; configuration RPC errors
                    # can contain secret parameters in third-party messages.
                    _LOGGER.warning("Phone provisioning incomplete for %s (%s)", device.id, type(error).__name__)
            if directory_changed:
                from .config_entry_runtime import async_refresh_and_push_phonebook
                await async_refresh_and_push_phonebook(self.hass)

    async def _deliver(self, phone, native, services):
        from .mailbox import mailbox_settings

        cfg = transport_config(self.hass)
        host = cfg.get("advertise_host") or await network.async_get_source_ip(self.hass, target_ip=phone.api_host)
        self.advertise_host = host
        mailbox_enabled = bool(mailbox_settings(self.hass, phone.endpoint_id).get("enabled"))
        account = dict(account_id=MANAGED_ACCOUNT_ID, registrar=host, port=int(cfg["sip_port"]), domain=host,
            username=phone.username, password=phone.password, auth_username=phone.username, proxy="",
            proxy_port=5060, transport="auto", expires=3600, persist=True)
        actions = [
            (AUTO_REGISTER_ACTION, {"name": phone.display_name}),
            ("set_account", account),
            ("register_account", {"account_id": MANAGED_ACCOUNT_ID}),
            ("configure_directory", {"server": host, "port": self.ldap_port, "base_dn": self.base_dn,
                "bind_dn": f"uid={{username}},ou=accounts,{self.base_dn}",
                "account_id": MANAGED_ACCOUNT_ID, "tls": self.tls, "enabled": self.directory_enabled}),
            ("configure_voicemail", {"server": host, "port": self.imap_port, "folder": "INBOX",
                "account_id": MANAGED_ACCOUNT_ID, "tls": self.tls,
                "enabled": mailbox_enabled and self.imap_enabled}),
        ]
        # Reuse ESPHome's existing API connection and native UserService RPC.
        # A secret is never copied into a HA ServiceCall/event or our logs.
        delivered = self._delivered.setdefault(phone.device_id, {})
        for action, data in actions:
            if not self._current(phone):
                return
            if action not in services:
                continue
            signature = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).digest()
            if delivered.get(action) == signature:
                continue
            await native.client.execute_service(services[action], data)
            if self._current(phone):
                delivered[action] = signature

    async def _purge(self, phone):
        await self._purge_endpoint(phone.endpoint_id, phone.username)
        self._delivered.pop(phone.device_id, None)

    async def _purge_endpoint(self, endpoint_id, username):
        from .mailbox import async_remove_mailbox
        from .endpoint_termination import EndpointTerminationHandler
        from .sip_capabilities import async_forget_peer

        runtime = require_runtime_data(self.hass)
        await async_forget_peer(self.hass, endpoint_id)
        endpoint = runtime.endpoints.get(endpoint_id)
        # Claims include early/ringing destinations which do not yet own the
        # public active_call_id. Terminate through the existing call owner.
        calls = {endpoint.active_call_id} if endpoint is not None and endpoint.active_call_id else set()
        if runtime.sip is not None:
            for session in tuple(runtime.sip.sessions.values()):
                if (endpoint_id in session.endpoint_claims
                        or any(leg.endpoint_id == endpoint_id for leg in session.legs.values())):
                    calls.add(session.call_id)
        for call_id in calls:
            await EndpointTerminationHandler(self.hass).terminate_reason(call_id, "phone_removed")
        methods = runtime.sip.component("sip_application") if runtime.sip is not None else None
        if methods is not None:
            await methods.async_revoke_account(username)
        for name in ("ldap_server", "imap_server"):
            server = getattr(runtime, name, None)
            if server is not None:
                await server.async_revoke_endpoint(endpoint_id, username=username)
        await async_remove_mailbox(self.hass, endpoint_id)
        archive = runtime.sip.component("media_archive") if runtime.sip is not None else None
        if archive is not None:
            for message in tuple(archive.list(endpoint_id, kind="recording")):
                await archive.delete(message["id"], endpoint_id)
        if runtime.endpoints.get(endpoint_id) is not None:
            runtime.endpoints.remove(endpoint_id)
        if runtime.directory is not None:
            runtime.directory.remove(endpoint_id)

    async def async_close(self):
        self._closed = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        async with self._lock:
            self._delivered.clear()
