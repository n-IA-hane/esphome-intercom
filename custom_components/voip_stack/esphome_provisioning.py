"""Manage ESP phone identities through ESPHome's existing private API owner."""

import asyncio
from dataclasses import replace
import hashlib
import json
import logging

from homeassistant.components import network
from homeassistant.config_entries import ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED, EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .config import transport_config
from .const import CONF_REGISTRAR_ENABLED
from .device_resolver import entity_role, slugify_route_id
from .endpoint_lifecycle import create_runtime_task
from .managed_phones import (CONF_MANAGED_PHONES, MANAGED_ACCOUNT_ID,
    ManagedPhone, active_managed_accounts, managed_phone_mode, managed_records, normalize_mac)
from .phone_endpoint import EndpointKind, PhoneEndpoint
from .phone_queue_membership import (CONF_ESPHOME_PHONE_MEMBERSHIPS, membership_text,
    normalize_membership_tracking, observe_native_membership)
from .runtime_data import runtime_data, sip_registrar
from .sip_registrar import generate_password

_LOGGER = logging.getLogger(__name__)
_ROLES = {"voip_extension": "extension", "voip_ring_groups": "ring_group",
          "voip_conference_groups": "conference_group", "voip_conference_ring": "conference_ring",
          "voip_queues": "queues"}


@callback
def setup_provisioning(hass, entry):
    """Share one coordinator across entry and mailbox composition hooks."""
    runtime = entry.runtime_data
    if runtime.phone_provisioning is None:
        runtime.phone_provisioning = EspHomeProvisioner(hass, entry)
    runtime.phone_provisioning.start()
    return runtime.phone_provisioning


class EspHomeProvisioner:
    """One entry-owned coordinator; the config entry owns persisted credentials."""

    def __init__(self, hass, entry):
        self.hass, self.entry, self.runtime = hass, entry, entry.runtime_data
        self._lock = asyncio.Lock()
        self._unsubscribers = []
        self._delivered = {}
        self._task = None
        self._closed = False
        self._removed_origins, self._removed_devices = set(), set()
        self._dirty = False
        self._revision = 0
        self._directory_config = None
        self._directory_epoch = ""
        self._membership_scopes = {}
        self._membership_changes = {}
        self._membership_delivered = {}

    def _owned(self):
        return (not self._closed and runtime_data(self.hass) is self.runtime
                and self.runtime.phone_provisioning is self)

    def is_removed(self, origin_entry_id, device_id):
        return origin_entry_id in self._removed_origins or device_id in self._removed_devices

    @callback
    def configure_directory_service(self, config, *, epoch=""):
        """A temporary failure suppresses delivery; only admin disable sends false."""
        self._directory_config = config
        self._directory_epoch = epoch
        self.schedule()

    @callback
    def start(self):
        if self._unsubscribers or not self._owned():
            return
        self._unsubscribers = [
            self.hass.bus.async_listen(EVENT_SERVICE_REGISTERED, self._service_changed),
            self.hass.bus.async_listen(EVENT_SERVICE_REMOVED, self._service_changed),
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed),
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_changed),
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._entity_changed),
            async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._entry_changed),
        ]
        self.schedule()

    @callback
    def schedule(self):
        self._revision += 1
        self._dirty = True
        if self._owned() and (self._task is None or self._task.done()):
            self._task = create_runtime_task(self.hass, self._run_pending())

    async def _run_pending(self):
        while self._dirty and self._owned():
            self._dirty = False
            await self.async_sync()

    @callback
    def _service_changed(self, event):
        if event.data.get("domain") == "esphome":
            # ESPHome replaces the inventory before publishing service events.
            self._refresh_accounts()
            self.schedule()

    @callback
    def _state_changed(self, event):
        entity = er.async_get(self.hass).async_get(str(event.data.get("entity_id") or ""))
        if entity is not None and entity.platform == "esphome" and entity_role(entity) in {
                *_ROLES, "voip_state"}:
            old, new = event.data.get("old_state"), event.data.get("new_state")
            field = _ROLES.get(entity_role(entity))
            if field in {"queues", "ring_group"}:
                if (old is not None and new is not None
                        and old.state not in {"unknown", "unavailable"}
                        and new.state not in {"unknown", "unavailable"}
                        and entity.device_id in self._membership_scopes):
                    self._membership_changes[(entity.device_id, field)] = (
                        self._membership_scopes[entity.device_id],
                        membership_text(field, old.state), membership_text(field, new.state))
                elif new is None or new.state in {"unknown", "unavailable"}:
                    self._membership_scopes.pop(entity.device_id, None)
                    self._membership_delivered = {key: value for key, value in self._membership_delivered.items()
                                                  if key[0] != entity.device_id}
            elif entity_role(entity) == "voip_state" and (new is None or new.state in {"unknown", "unavailable"}):
                self._membership_scopes.pop(entity.device_id, None)
                self._membership_delivered = {key: value for key, value in self._membership_delivered.items()
                                              if key[0] != entity.device_id}
            if old is None or new is None or old.state != new.state:
                self.schedule()

    @callback
    def _entity_changed(self, event):
        entity = er.async_get(self.hass).async_get(str(event.data.get("entity_id") or ""))
        if entity is not None and entity.platform == "esphome" and entity_role(entity) in _ROLES:
            self.schedule()

    @callback
    def _device_changed(self, event):
        if event.data.get("action") == "remove":
            self._removed_devices.add(event.data["device_id"])
            self._refresh_accounts()
        self.schedule()

    @callback
    def _entry_changed(self, change, entry):
        if entry.domain != "esphome":
            return
        if change is ConfigEntryChange.REMOVED:
            self._removed_origins.add(entry.entry_id)
            self._removed_devices.update(device.id for device in dr.async_entries_for_config_entry(
                dr.async_get(self.hass), entry.entry_id))
            self._refresh_accounts()
        self.schedule()

    def _save(self, records):
        if not self._owned():
            return
        data = {key: phone.record() for key, phone in records.items()}
        if data != self.entry.data.get(CONF_MANAGED_PHONES, {}):
            self.hass.config_entries.async_update_entry(self.entry,
                data={**self.entry.data, CONF_MANAGED_PHONES: data})

    def _refresh_accounts(self):
        if not self._owned():
            return
        from .store import sip_accounts

        registrar = sip_registrar(self.hass)
        if registrar is not None:
            revoked = {phone.username.casefold() for phone in managed_records(self.entry).values()
                       if phone.device_id in self._removed_devices or phone.origin_entry_id in self._removed_origins}
            registrar.update_accounts([account for account in sip_accounts(self.hass)
                                       if account.username.casefold() not in revoked])
            registrar.enabled = bool(transport_config(self.hass).get(CONF_REGISTRAR_ENABLED)
                                     or active_managed_accounts(self.hass, self.entry))

    def _current(self, phone, native, client, services, revision):
        origin = self.hass.config_entries.async_get_entry(phone.origin_entry_id)
        device = dr.async_get(self.hass).async_get(phone.device_id)
        return (self._owned() and revision == self._revision
                and phone.origin_entry_id not in self._removed_origins
                and phone.device_id not in self._removed_devices
                and origin is not None and getattr(origin, "runtime_data", None) is native
                and native.client is client and native.services is services
                and native.available and client.is_connected
                and device is not None and origin.entry_id == device.config_entry_id
                and managed_phone_mode(self.hass, origin, phone.device_id, phone.mac) is True)

    def _phone(self, origin, device, native, previous):
        info = native.device_info
        if previous is not None and (previous.mac != normalize_mac(info.mac_address)
                                     or previous.origin_entry_id != origin.entry_id):
            raise ValueError("Managed physical ownership changed")
        name = (device.name_by_user or device.name or info.friendly_name or info.name).strip()
        if not name or len(name.encode()) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError("Display name exceeds the native phone contract")
        phone = ManagedPhone(device_id=device.id, origin_entry_id=origin.entry_id,
            mac=normalize_mac(info.mac_address), password=previous.password if previous else generate_password(),
            username=previous.username if previous else "", endpoint_id=previous.endpoint_id if previous else "",
            display_name=name, route_id=slugify_route_id(info.name), api_host=str(origin.data.get("host") or ""),
            extension=previous.extension if previous else "", ring_group=previous.ring_group if previous else "",
            queues=previous.queues if previous else (),
            membership_pending=previous.membership_pending if previous else {},
            membership_observed=previous.membership_observed if previous else {},
            conference_group=previous.conference_group if previous else "",
            conference_ring=previous.conference_ring if previous else False)
        updates = {}
        for entity in er.async_entries_for_device(er.async_get(self.hass), device.id):
            role = entity_role(entity)
            state = self.hass.states.get(entity.entity_id)
            if (entity.platform == "esphome" and role in _ROLES and state is not None
                    and state.state not in {"unknown", "unavailable"}):
                value = state.state.strip()
                field = _ROLES[role]
                if field in {"queues", "ring_group"}:
                    record = {**phone.record(), **updates}
                    change = self._membership_changes.pop((device.id, field), None)
                    scope = (native, native.client)
                    live = (change is not None and all(left is right for left, right in zip(change[0], scope))
                            and change[1] == record["membership_observed"].get(field)
                            and change[2] == membership_text(field, value))
                    updates.update(observe_native_membership(record, field, value, allow_change=live))
                else:
                    updates[field] = value.casefold() in {"on", "true", "1"} if role == "voip_conference_ring" else value
        self._membership_scopes[device.id] = (native, native.client)
        return replace(phone, **updates)

    async def async_sync(self):
        async with self._lock:
            if not self._owned():
                return
            revision = self._revision
            records = managed_records(self.entry)
            changed = False
            for phone in tuple(records.values()):
                origin = self.hass.config_entries.async_get_entry(phone.origin_entry_id)
                device = dr.async_get(self.hass).async_get(phone.device_id)
                if (origin is None or phone.origin_entry_id in self._removed_origins
                        or device is None or phone.device_id in self._removed_devices):
                    await self._purge_endpoint(phone.endpoint_id, phone.username)
                    if not self._owned() or revision != self._revision:
                        return
                    records.pop(phone.device_id, None)
                    self._delivered.pop(phone.device_id, None)
                    changed = True
                elif managed_phone_mode(self.hass, origin, phone.device_id, phone.mac) is False:
                    records[phone.device_id] = replace(phone, active=False)
                    self._delivered.pop(phone.device_id, None)
                    changed |= phone.active
            self._save(records)
            self._refresh_accounts()
            # Legacy direct phones also use the same permanent-removal owner.
            for device_id in self._removed_devices:
                endpoint = self.runtime.endpoints.by_device_id(device_id)
                if endpoint is not None and endpoint.kind is EndpointKind.ESPHOME:
                    await self._purge_endpoint(endpoint.endpoint_id, endpoint.username)
                    if not self._owned() or revision != self._revision:
                        return
                    changed = True
            devices = dr.async_get(self.hass)
            for origin in self.hass.config_entries.async_entries("esphome"):
                if origin.entry_id in self._removed_origins:
                    continue
                native = getattr(origin, "runtime_data", None)
                if not getattr(native, "available", False) or not getattr(getattr(native, "client", None), "is_connected", False):
                    continue
                for device in dr.async_entries_for_config_entry(devices, origin.entry_id):
                    if device.id in self._removed_devices or not any(kind == dr.CONNECTION_NETWORK_MAC
                            and managed_phone_mode(self.hass, origin, device.id, mac) is True
                            for kind, mac in device.connections):
                        continue
                    try:
                        previous = records.get(device.id)
                        phone = self._phone(origin, device, native, previous)
                        if any(other.device_id != phone.device_id and other.mac == phone.mac for other in records.values()):
                            raise ValueError("MAC is already owned")
                        from .store import sip_account_dicts
                        if any(str(account.get("username") or account.get("id") or "").casefold() == phone.username.casefold()
                               for account in sip_account_dicts(self.hass)):
                            raise ValueError("SIP username is already configured")
                        registry = self.runtime.endpoints
                        old = registry.get(phone.endpoint_id)
                        fields = dict(name=phone.display_name, username=phone.username, extension=phone.extension,
                            ring_group=phone.ring_group, conference_group=phone.conference_group,
                            conference_ring=phone.conference_ring, queues=phone.queues)
                        if old is None:
                            registry.register(PhoneEndpoint(endpoint_id=phone.endpoint_id, device_id=device.id,
                                kind=EndpointKind.ESPHOME, capabilities=frozenset({"audio", "dtmf"}), **fields))
                        else:
                            registry.update(phone.endpoint_id, **fields)
                        changed |= previous != phone or old is None
                        records[device.id] = phone
                        self._save(records)
                        self._refresh_accounts()
                        await self._deliver(phone, native)
                        if not self._owned() or revision != self._revision:
                            return
                        await self._deliver_memberships(phone.device_id, phone.record(), native)
                        if not self._owned() or revision != self._revision:
                            return
                    except Exception as error:
                        # Third-party RPC errors may contain secret parameters.
                        _LOGGER.warning("Phone provisioning incomplete for %s (%s)", device.id, type(error).__name__)
            await self._sync_legacy_memberships()
            if not self._owned() or revision != self._revision:
                return
            if changed and self._owned():
                from .config_entry_runtime import async_refresh_and_push_phonebook
                await async_refresh_and_push_phonebook(self.hass)
            # Refresh only after the canonical composition has committed. The
            # action phase above may have enrolled or renamed the first phone.
            for phone in records.values():
                origin = self.hass.config_entries.async_get_entry(phone.origin_entry_id)
                native = getattr(origin, "runtime_data", None)
                if phone.active and native is not None:
                    await self._deliver_directory_refresh(phone, native)

    async def _sync_legacy_memberships(self):
        """Retain direct-phone declarations without creating a SIP credential."""
        legacy = dict(self.entry.data.get(CONF_ESPHOME_PHONE_MEMBERSHIPS, {}))
        managed = {phone.endpoint_id for phone in managed_records(self.entry).values()}
        deliveries = []
        for endpoint_id, saved in tuple(legacy.items()):
            device_id = saved.get("device_id", "")
            device = dr.async_get(self.hass).async_get(device_id)
            origin = self.hass.config_entries.async_get_entry(device.config_entry_id) if device is not None else None
            if (endpoint_id in managed or device is None or origin is None
                    or origin.domain != "esphome" or device_id in self._removed_devices
                    or origin.entry_id in self._removed_origins):
                legacy.pop(endpoint_id, None)
                continue
            native = getattr(origin, "runtime_data", None)
            if not getattr(native, "available", False) or not getattr(getattr(native, "client", None), "is_connected", False):
                continue
            record = dict(saved)
            for entity in er.async_entries_for_device(er.async_get(self.hass), device_id):
                field = _ROLES.get(entity_role(entity))
                state = self.hass.states.get(entity.entity_id)
                if (entity.platform != "esphome" or field not in {"queues", "ring_group"}
                        or state is None or state.state in {"unknown", "unavailable"}):
                    continue
                change = self._membership_changes.pop((device_id, field), None)
                scope = (native, native.client)
                live = (change is not None and all(left is right for left, right in zip(change[0], scope))
                        and change[1] == record.get("membership_observed", {}).get(field)
                        and change[2] == membership_text(field, state.state))
                record.update(observe_native_membership(record, field, state.state, allow_change=live))
            if isinstance(record.get("queues"), tuple):
                record["queues"] = list(record["queues"])
            legacy[endpoint_id] = record
            self._membership_scopes[device_id] = (native, native.client)
            deliveries.append((device_id, record, native))
            endpoint = self.runtime.endpoints.get(endpoint_id)
            if endpoint is not None:
                self.runtime.endpoints.update(endpoint_id, queues=tuple(record.get("queues", ())),
                                              ring_group=str(record.get("ring_group") or ""))
        if legacy != self.entry.data.get(CONF_ESPHOME_PHONE_MEMBERSHIPS, {}):
            self.hass.config_entries.async_update_entry(self.entry,
                data={**self.entry.data, CONF_ESPHOME_PHONE_MEMBERSHIPS: legacy})
        for device_id, record, native in deliveries:
            revision = self._revision
            await self._deliver_memberships(device_id, record, native)
            if not self._owned() or revision != self._revision:
                return

    async def _deliver_memberships(self, device_id, record, native):
        """Deliver desired text through ESPHome's existing HA entity owner."""
        revision, client = self._revision, native.client
        for entity in er.async_entries_for_device(er.async_get(self.hass), device_id):
            field = _ROLES.get(entity_role(entity))
            pending = normalize_membership_tracking(record.get("membership_pending"))
            if entity.platform != "esphome" or field not in pending:
                continue
            state = self.hass.states.get(entity.entity_id)
            if state is None or state.state in {"unknown", "unavailable"}:
                continue
            if (not self._owned() or revision != self._revision or native.client is not client
                    or not native.available or not client.is_connected):
                return
            signature = (id(native), id(client), pending[field], record.get("membership_observed", {}).get(field))
            key = (device_id, field)
            if self._membership_delivered.get(key) == signature:
                continue
            try:
                await self.hass.services.async_call("text", "set_value", {
                    "entity_id": entity.entity_id, "value": pending[field]}, blocking=True)
            except Exception as error:
                _LOGGER.warning("Phone membership delivery pending for %s (%s)", device_id, type(error).__name__)
                return
            if not self._owned() or revision != self._revision or native.client is not client:
                return
            self._membership_delivered[key] = signature

    async def _deliver(self, phone, native):
        revision, client, services = self._revision, native.client, native.services
        if not self._current(phone, native, client, services, revision):
            return
        cfg = transport_config(self.hass)
        host = cfg.get("advertise_host") or await network.async_get_source_ip(self.hass, target_ip=phone.api_host)
        if not self._current(phone, native, client, services, revision):
            return
        actions = [("set_ha_display_name", {"name": phone.display_name}),
            ("set_account", dict(account_id=MANAGED_ACCOUNT_ID, registrar=host, port=int(cfg["sip_port"]),
                domain=host, username=phone.username, password=phone.password, auth_username=phone.username,
                proxy="", proxy_port=5060, transport="auto", expires=3600, persist=True)),
            ("register_account", {"account_id": MANAGED_ACCOUNT_ID})]
        directory_data = self._directory_data(host)
        if directory_data is not None:
            actions.append(("configure_directory", directory_data))
        scope = (native, client, services)
        previous_scope, delivered = self._delivered.get(phone.device_id, (None, {}))
        if previous_scope is None or any(left is not right for left, right in zip(previous_scope, scope)):
            delivered = {}
        by_name = {service.name: service for service in services.values()}
        for action, data in actions:
            if not self._current(phone, native, client, services, revision):
                return
            if action not in by_name:
                continue
            signature = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).digest()
            if delivered.get(action) == signature:
                continue
            if action == "set_account":
                delivered.pop("register_account", None)
            await client.execute_service(by_name[action], data)
            if not self._current(phone, native, client, services, revision):
                return
            delivered[action] = signature
            self._delivered[phone.device_id] = (scope, delivered)

    def _directory_data(self, host):
        config = self._directory_config
        if config is None:
            return None
        return dict(server=host, port=config["ldap_port"], base_dn=config["base_dn"],
            bind_dn=f"uid={{username}},ou=accounts,{config['base_dn']}",
            account_id=MANAGED_ACCOUNT_ID, tls=config["tls"], enabled=config["enabled"])

    async def _deliver_directory_refresh(self, phone, native):
        revision, client, services = self._revision, native.client, native.services
        if not self._current(phone, native, client, services, revision):
            return
        by_name = {service.name: service for service in services.values()}
        if not {"configure_directory", "refresh_ldap_directory"} <= by_name.keys():
            return  # A manually configured provider is not managed by HA.
        scope = (native, client, services)
        previous_scope, delivered = self._delivered.get(phone.device_id, (None, {}))
        if previous_scope is None or any(left is not right for left, right in zip(previous_scope, scope)):
            return  # The configuration phase must reach this API owner first.
        cfg = transport_config(self.hass)
        host = cfg.get("advertise_host") or await network.async_get_source_ip(self.hass, target_ip=phone.api_host)
        if not self._current(phone, native, client, services, revision):
            return
        data = self._directory_data(host)
        if data is None or delivered.get("configure_directory") != hashlib.sha256(json.dumps(data, sort_keys=True).encode()).digest():
            return
        directory = self.runtime.directory
        if (self._directory_config is not None and self._directory_config["enabled"]
                and directory is not None and not directory.closed and "refresh_ldap_directory" in by_name):
            generation = directory.generation
            signature = hashlib.sha256(f"{self._directory_epoch}:{generation}".encode()).digest()
            if delivered.get("refresh_ldap_directory") != signature and self._current(phone, native, client, services, revision):
                await client.execute_service(by_name["refresh_ldap_directory"], {})
                if (self._current(phone, native, client, services, revision)
                        and self.runtime.directory is directory and directory.generation == generation and not directory.closed):
                    delivered["refresh_ldap_directory"] = signature
                    self._delivered[phone.device_id] = (scope, delivered)

    async def _purge_endpoint(self, endpoint_id, username):
        from .endpoint_termination import EndpointTerminationHandler
        from .mailbox import async_remove_mailbox

        owner = self.runtime.sip
        if owner is not None:
            for session in tuple(owner.sessions.values()):
                if (endpoint_id in session.endpoint_claims
                        or any(leg.endpoint_id == endpoint_id for leg in session.legs.values())):
                    await EndpointTerminationHandler(self.hass).terminate_reason(session.call_id, "phone_removed")
                    if not self._owned():
                        return
            for key, task in tuple(self.runtime.capability_probes.items()):
                if key[0] == username.casefold():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self.runtime.capability_probes.pop(key, None)
            methods = owner.component("sip_application")
            if methods is not None:
                await methods.async_revoke_account(username)
            if self.runtime.ldap_server is not None:
                await self.runtime.ldap_server.async_revoke_endpoint(endpoint_id, username=username)
            if not self._owned():
                return
            await async_remove_mailbox(self.hass, endpoint_id)
            if not self._owned():
                return
            archive = owner.component("media_archive")
            if archive is not None:
                for message in tuple(archive.list(endpoint_id, kind="recording")):
                    await archive.delete(message["id"], endpoint_id)
        if self._owned() and self.runtime.endpoints.get(endpoint_id) is not None:
            self.runtime.endpoints.remove(endpoint_id)
        if self._owned() and self.runtime.directory is not None:
            self.runtime.directory.remove(endpoint_id)

    async def async_close(self):
        self._closed = True
        self._revision += 1
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._task is not None and self._task is not asyncio.current_task() and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        async with self._lock:
            self._delivered.clear()
