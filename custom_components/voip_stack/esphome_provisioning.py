"""Manage ESP phone identities through ESPHome's existing private API owner."""

import asyncio
from dataclasses import replace
import hashlib
import json
import logging

from homeassistant.components import network
from homeassistant.config_entries import ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event

from .config import transport_config
from .const import CONF_REGISTRAR_ENABLED
from .device_resolver import entity_role, slugify_route_id
from .endpoint_device import account_device_info
from .esphome_native import (
    ProvisioningActions, connected, connection_is_current, connection_scope, device_info, runtime_for_entry,
)
from .endpoint_lifecycle import create_runtime_task
from .endpoint_registry import EndpointCollisionError
from .managed_phones import (CONF_ESPHOME_ENROLLMENTS, MANAGED_ACCOUNT_ID,
    ManagedNameError, ManagedPhone, REGISTER_HA_ACCOUNT, managed_device_username,
    managed_account_subentries, managed_phone_mode, managed_records,
    normalize_mac, save_managed_account)
from .phone_endpoint import EndpointKind
from .esphome_phone_inventory import managed_phone_endpoint, sync_managed_phone_endpoint
from .phone_queue_membership import (CONF_ESPHOME_PHONE_MEMBERSHIPS, membership_text,
    normalize_membership_tracking, observe_native_membership)
from .runtime_data import runtime_data, sip_registrar
from .repairs import async_sync_esphome_account_name_issue, async_sync_esphome_endpoint_collision_issue
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
        self._state_unsubscribe = None
        self._state_entities = frozenset()
        self._delivered = {}
        self._task = None
        self._closed = False
        self._removed_origins, self._removed_devices = set(), set()
        self._deleted_accounts = set()
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
        return (origin_entry_id in self._removed_origins or device_id in self._removed_devices
                or device_id in self._deleted_accounts
                or self.entry.data.get(CONF_ESPHOME_ENROLLMENTS, {}).get(device_id) == {"origin_entry_id": origin_entry_id})

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
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_changed),
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._entity_changed),
            async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._entry_changed),
        ]
        if self.runtime.sip is not None:
            self._unsubscribers.append(self.runtime.sip.subscribe_state(self._call_changed))
        self._refresh_state_entities()
        self.schedule()

    @callback
    def _call_changed(self, snapshot):
        if str(snapshot.phase) == 'terminated':
            self.schedule()

    async def _async_reconcile_account_names(self):
        """Commit a deferred automatic name only through the account migration owner."""
        from .managed_phones import CONF_MANAGED_PHONES
        from .phone_config import PhoneRouteConflict
        from .repairs import async_sync_account_route_issue, async_sync_phone_identity_issue
        from .sip_account_identity import async_migrate_account_identities, managed_account_name_updates, legacy_esphome_references

        legacy = CONF_MANAGED_PHONES in self.entry.data or any(legacy_esphome_references(self.hass, self.entry))
        try:
            updates = managed_account_name_updates(self.hass, self.entry)
            renamed = any(data.get('username') != self.entry.subentries[identity].data.get('username')
                          for identity, data in updates.items())
            if not legacy and not updates:
                async_sync_phone_identity_issue(self.hass, self.entry)
                return True
            if self.runtime.sip is not None and any(session.live for session in self.runtime.sip.sessions.values()):
                return not legacy
            await async_migrate_account_identities(self.hass, self.entry, account_updates=updates)
        except PhoneRouteConflict as error:
            async_sync_account_route_issue(self.hass, self.entry, error)
            return not legacy
        except (ValueError, TypeError, RuntimeError, OSError) as error:
            async_sync_phone_identity_issue(self.hass, self.entry, error)
            return not legacy
        if not self._owned():
            return False
        async_sync_account_route_issue(self.hass, self.entry)
        async_sync_phone_identity_issue(self.hass, self.entry)
        self._refresh_accounts()
        from homeassistant.config_entries import ConfigEntryState
        if renamed and self.entry.state is ConfigEntryState.LOADED:
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            return False
        return True

    @callback
    def _refresh_state_entities(self):
        """Rebind HA's entity index only when registry membership changes."""
        if not self._owned():
            return
        entities = frozenset(entity.entity_id for entity in er.async_get(self.hass).entities.values()
                             if entity.platform == "esphome" and entity_role(entity) in {*_ROLES, "voip_state"})
        if entities == self._state_entities:
            return
        if self._state_unsubscribe is not None:
            self._state_unsubscribe()
            self._state_unsubscribe = None
        self._state_entities = entities
        if entities:
            self._state_unsubscribe = async_track_state_change_event(self.hass, entities, self._state_changed)

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
        if not self._owned():
            return
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
        identity = str(event.data.get("entity_id") or "")
        entity = er.async_get(self.hass).async_get(identity)
        if (identity in self._state_entities or event.data.get("old_entity_id") in self._state_entities
                or (entity is not None and entity.platform == "esphome" and entity_role(entity) in {*_ROLES, "voip_state"})):
            self._refresh_state_entities()
            self.schedule()

    @callback
    def _device_changed(self, event):
        device_id = event.data["device_id"]
        if event.data["action"] == "remove":
            if event.data["device"]["config_entry_id"] == self.entry.entry_id:
                return
            async_sync_esphome_endpoint_collision_issue(self.hass, device_id)
            async_sync_esphome_account_name_issue(self.hass, device_id)
            self._removed_devices.add(device_id)
            self._refresh_accounts()
        else:
            device = dr.async_get(self.hass).async_get(device_id)
            if device is not None and device.config_entry_id == self.entry.entry_id:
                # Publishing a PBX service device must not interrupt ESPHome enrollment.
                return
        self.schedule()

    @callback
    def _entry_changed(self, change, entry):
        if entry is self.entry and change is ConfigEntryChange.UPDATED:
            rows = managed_account_subentries(entry)
            deleted = {device_id for device_id, receipt in entry.data.get(CONF_ESPHOME_ENROLLMENTS, {}).items()
                       if "endpoint_id" in receipt and device_id not in rows}
            if deleted != self._deleted_accounts:
                self._deleted_accounts = deleted
                if deleted:
                    self._refresh_accounts()
                    self.schedule()
            return
        if entry.domain != "esphome":
            return
        if change is ConfigEntryChange.REMOVED:
            self._removed_origins.add(entry.entry_id)
            self._removed_devices.update(device.id for device in dr.async_entries_for_config_entry(
                dr.async_get(self.hass), entry.entry_id))
            self._refresh_accounts()
        self.schedule()

    def _refresh_accounts(self):
        if not self._owned():
            return
        from .store import sip_accounts

        registrar = sip_registrar(self.hass)
        if registrar is not None:
            rows = managed_account_subentries(self.entry)
            revoked = {phone.username.casefold() for phone in managed_records(self.entry).values()
                       if (phone.device_id in self._removed_devices or phone.origin_entry_id in self._removed_origins)
                       and self._exclusive_account(phone, rows.get(phone.device_id))}
            registrar.update_accounts([account for account in sip_accounts(self.hass)
                                       if account.username.casefold() not in revoked])
            registrar.enabled = bool(transport_config(self.hass).get(CONF_REGISTRAR_ENABLED))

    def _owned_contacts(self, phone):
        """Identify only an unambiguous SIP flow at the verified native hardware address."""
        registrar = sip_registrar(self.hass)
        candidates = tuple(contact for contact in registrar.registered_contacts(phone.username)
                           if contact.source_host == phone.api_host) if registrar is not None and phone.api_host else ()
        return candidates if len(candidates) == 1 else ()

    def _exclusive_account(self, phone, row):
        if row is None or not row.data.get('managed_exclusive', True) or row.data.get('ha_bindings'):
            return False
        from .account_runtime import account_has_ha_binding
        if account_has_ha_binding(self.hass, phone.endpoint_id):
            return False
        from .physical_phone import account_physical_bindings

        if account_physical_bindings(self.entry, phone.endpoint_id):
            return False
        registrar = sip_registrar(self.hass)
        contacts = registrar.registered_contacts(phone.username) if registrar is not None else ()
        owned = {contact.flow_key for contact in self._owned_contacts(phone)}
        return all(contact.flow_key in owned for contact in contacts)

    def _unbind_hardware(self, phone, row):
        """Remove the native provision association while retaining a shared account."""
        registrar = sip_registrar(self.hass)
        if registrar is not None:
            for contact in self._owned_contacts(phone):
                registrar.remove_contact(contact.flow_key)
        fields = {'device_id', 'origin_entry_id', 'mac', 'route_id', 'api_host', 'active',
                  'membership_pending', 'membership_observed', 'managed_esphome', 'managed_exclusive'}
        data = {key: value for key, value in row.data.items() if key not in fields}
        self.hass.config_entries.async_update_subentry(self.entry, row, data=data)
        endpoint = self.runtime.endpoints.get(phone.endpoint_id)
        if endpoint is not None:
            account_device = endpoint.account_device_id
            entities = er.async_get(self.hass)
            kept = frozenset(identity for identity in endpoint.entity_ids
                             if (entity := entities.async_get(identity)) is None or entity.device_id != phone.device_id)
            self.runtime.endpoints.update(phone.endpoint_id, device_id=account_device,
                account_device_id='', entity_ids=kept)
        self._delivered.pop(phone.device_id, None)

    def _current(self, phone, actions, revision):
        origin = self.hass.config_entries.async_get_entry(phone.origin_entry_id)
        device = dr.async_get(self.hass).async_get(phone.device_id)
        return (self._owned() and revision == self._revision
                and not self.is_removed(phone.origin_entry_id, phone.device_id)
                and origin is not None and actions.current(origin)
                and device is not None and origin.entry_id == device.config_entry_id
                and managed_phone_mode(self.hass, origin, phone.device_id, phone.mac) is True)

    def _new_username(self, native, endpoint_id):
        from .config_validation import entry_route_mappings, route_namespace_conflicts

        username = managed_device_username(native)
        existing = entry_route_mappings(self.entry, self.entry.data, exclude_endpoint_ids=(endpoint_id,))
        if route_namespace_conflicts(candidate_username=username, existing=existing):
            raise ManagedNameError("esphome_account_name_collision", value=username)
        return username

    def _phone(self, origin, device, native, previous):
        info = device_info(native)
        if previous is not None and (previous.mac != normalize_mac(info.mac_address)
                                     or previous.origin_entry_id != origin.entry_id):
            raise ValueError("Managed physical ownership changed")
        name = (previous.display_name if previous else managed_device_username(native)).strip()
        endpoint_id = previous.username if previous else managed_device_username(native)
        if previous is None:
            username = self._new_username(native, endpoint_id)
        else:
            username = previous.username
        if not name or len(name.encode()) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError("Display name exceeds the native phone contract")
        account = managed_account_subentries(self.entry).get(device.id)
        phone = ManagedPhone(device_id=device.id, origin_entry_id=origin.entry_id,
            mac=normalize_mac(info.mac_address), password=previous.password if previous else generate_password(),
            username=username, endpoint_id=endpoint_id,
            display_name=name, route_id=slugify_route_id(info.name), api_host=str(origin.data.get("host") or ""),
            extension=previous.extension if previous else "", ring_group=previous.ring_group if previous else "",
            queues=previous.queues if previous else (),
            membership_pending=previous.membership_pending if previous else {},
            membership_observed=previous.membership_observed if previous else {},
            conference_group=previous.conference_group if previous else "",
            conference_ring=previous.conference_ring if previous else False,
            active=bool(account.data.get("enabled", True)) if account is not None else True)
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
                    self._membership_changes.pop((device.id, field), None)
                    record["membership_pending"][field] = membership_text(field, record.get(field))
                    updates.update(observe_native_membership(record, field, value))
        self._membership_scopes[device.id] = connection_scope(native)
        return replace(phone, **updates)

    def _sync_account_device(self, phone, row):
        """Attach the SIP service to its subentry and preserve the transport device ID."""
        registry = self.runtime.endpoints
        registry.subentry_ids[phone.endpoint_id] = row.subentry_id
        devices = dr.async_get(self.hass)
        existing = devices.async_get_device_by_identifier(('voip_stack', 'phone_endpoint:' + phone.endpoint_id), self.entry.entry_id)
        if existing is not None and existing.config_subentry_id != row.subentry_id:
            devices.async_update_device(existing.id, new_config_subentry_id=row.subentry_id)
        device = devices.async_get_or_create(
            config_entry_id=self.entry.entry_id, config_subentry_id=row.subentry_id,
            **account_device_info(phone.endpoint_id, phone.display_name or phone.username,
                                  hardware_device_id=phone.device_id),
        )
        endpoint = registry.get(phone.endpoint_id)
        if (endpoint is not None and endpoint.kind is EndpointKind.SIP_ACCOUNT
                and endpoint.device_id == phone.device_id and endpoint.account_device_id != device.id):
            registry.update(phone.endpoint_id, account_device_id=device.id)

    async def async_sync(self):
        async with self._lock:
            if not self._owned():
                return
            revision = self._revision
            if not await self._async_reconcile_account_names():
                return
            if not self._owned() or revision != self._revision:
                return
            records = managed_records(self.entry)
            rows = managed_account_subentries(self.entry)
            devices = dr.async_get(self.hass)
            for device_id in rows.keys() - self.entry.data.get(CONF_ESPHOME_ENROLLMENTS, {}).keys():
                if phone := records.get(device_id):
                    save_managed_account(self.hass, self.entry, phone)
            saved_receipts = self.entry.data.get(CONF_ESPHOME_ENROLLMENTS, {})
            receipts = dict(saved_receipts)
            changed = False
            for device_id, receipt in saved_receipts.items():
                origin_id = receipt["origin_entry_id"]
                device = devices.async_get(device_id)
                origin = self.hass.config_entries.async_get_entry(origin_id)
                owner_gone = (origin is None or origin_id in self._removed_origins or device is None
                              or device.config_entry_id != origin_id or device_id in self._removed_devices)
                phone = records.get(device_id)
                if owner_gone or device_id not in rows:
                    if owner_gone and phone is not None and device_id in rows and not self._exclusive_account(phone, rows[device_id]):
                        self._unbind_hardware(phone, rows.pop(device_id))
                        records.pop(device_id, None)
                        receipts.pop(device_id, None)
                        changed = True
                        continue
                    if endpoint_id := receipt.get("endpoint_id"):
                        self.runtime.endpoints.subentry_ids.pop(endpoint_id, None)
                        endpoint = self.runtime.endpoints.get(endpoint_id)
                        username = getattr(phone or endpoint, "username", "")
                        await self._purge_endpoint(endpoint_id, username)
                        if not self._owned() or revision != self._revision:
                            return
                        async_sync_esphome_account_name_issue(self.hass, device_id)
                        self._delivered.pop(device_id, None)
                        changed = True
                    if owner_gone:
                        receipts.pop(device_id)
                        if row := rows.get(device_id):
                            self.hass.config_entries.async_remove_subentry(self.entry, row.subentry_id)
                        records.pop(device_id, None)
                    else:
                        # The native account was deleted. Block only this ESPHome ownership.
                        receipts[device_id] = {"origin_entry_id": origin_id}
                elif phone is not None:
                    self._sync_account_device(phone, rows[device_id])
                    mode = managed_phone_mode(self.hass, origin, device_id, phone.mac)
                    from .repairs import async_sync_esphome_account_setup_issue
                    async_sync_esphome_account_setup_issue(self.hass, self.entry, device_id, pending=mode is not True)
                    if phone.active and mode is False:
                        async_sync_esphome_account_name_issue(self.hass, device_id)
                        records[device_id] = replace(phone, active=False)
                        save_managed_account(self.hass, self.entry, records[device_id])
                        self._delivered.pop(device_id, None)
                        changed = True
            if receipts != saved_receipts:
                self.hass.config_entries.async_update_entry(self.entry,
                    data={**self.entry.data, CONF_ESPHOME_ENROLLMENTS: receipts})
            self._refresh_accounts()
            # Legacy direct phones also use the same permanent-removal owner.
            for device_id in self._removed_devices:
                endpoint = self.runtime.endpoints.by_device_id(device_id)
                if endpoint is not None and endpoint.kind is EndpointKind.ESPHOME and endpoint.device_id == device_id:
                    await self._purge_endpoint(endpoint.endpoint_id, endpoint.username)
                    if not self._owned() or revision != self._revision:
                        return
                    changed = True
            for origin in self.hass.config_entries.async_entries("esphome"):
                if origin.entry_id in self._removed_origins:
                    continue
                native = runtime_for_entry(origin)
                if not connected(native):
                    continue
                for device in dr.async_entries_for_config_entry(devices, origin.entry_id):
                    if self.is_removed(origin.entry_id, device.id):
                        continue
                    modes = {managed_phone_mode(self.hass, origin, device.id, mac)
                             for kind, mac in device.connections if kind == dr.CONNECTION_NETWORK_MAC}
                    if True not in modes:
                        if False in modes:
                            async_sync_esphome_account_name_issue(self.hass, device.id)
                        continue
                    try:
                        previous = records.get(device.id)
                        phone = self._phone(origin, device, native, previous)
                        async_sync_esphome_account_name_issue(self.hass, device.id)
                        if any(other.device_id != phone.device_id and other.mac == phone.mac for other in records.values()):
                            raise ValueError("MAC is already owned")
                        from .store import sip_account_dicts
                        if previous is None and any(str(account.get("username") or account.get("id") or "").casefold() == phone.username.casefold()
                               for account in sip_account_dicts(self.hass)):
                            raise EndpointCollisionError(field="username", value=phone.username,
                                endpoint_id=phone.endpoint_id, conflicting_endpoint_id="configured SIP account")
                        registry = self.runtime.endpoints
                        old = registry.get(phone.endpoint_id)
                        if old is not None and (old.username != phone.username or old.device_id != phone.device_id):
                            raise EndpointCollisionError(field='endpoint_id', value=phone.endpoint_id,
                                endpoint_id=phone.endpoint_id, conflicting_endpoint_id=old.endpoint_id)
                        registry.validate(managed_phone_endpoint(registry, phone), replacing_endpoint_id=phone.endpoint_id)
                        changed |= previous != phone or old is None
                        records[device.id] = phone
                        if phone != previous:
                            rows[device.id] = save_managed_account(self.hass, self.entry, phone,
                                username_source='esphome' if previous is None else None)
                        registry.subentry_ids[phone.endpoint_id] = rows[device.id].subentry_id
                        sync_managed_phone_endpoint(registry, phone)
                        if phone != previous or old is None:
                            self._sync_account_device(phone, rows[device.id])
                        async_sync_esphome_endpoint_collision_issue(self.hass, device.id)
                        self._refresh_accounts()
                        if not await self._deliver(phone, native):
                            return
                        if not self._owned() or revision != self._revision:
                            return
                        await self._deliver_memberships(phone.device_id, phone.record(), native)
                        if not self._owned() or revision != self._revision:
                            return
                    except ManagedNameError as error:
                        async_sync_esphome_account_name_issue(self.hass, device.id,
                            name=device.name_by_user or device.name or origin.title, error=error)
                    except EndpointCollisionError as error:
                        async_sync_esphome_endpoint_collision_issue(self.hass, device.id,
                            name=device.name_by_user or device.name or origin.title, error=error)
                    except Exception as error:
                        # Third-party RPC errors may contain secret parameters.
                        _LOGGER.warning("Phone provisioning incomplete for %s (%s)", device.id, type(error).__name__)
            await self._sync_legacy_memberships()
            if not self._owned() or revision != self._revision:
                return
            if self._removed_devices:
                # HA restores the device ID when the same ESP is added again.
                # Keep it fenced until the old account, endpoint and memberships
                # have been purged. The removed config entry stays revoked.
                restored = any(
                    (device := devices.async_get(device_id)) is not None
                    and device.config_entry_id not in self._removed_origins
                    for device_id in self._removed_devices
                )
                self._removed_devices.clear()
                if restored:
                    self.schedule()
            if changed and self._owned():
                from .config_entry_runtime import async_refresh_directory
                await async_refresh_directory(self.hass)
            # Refresh only after the canonical composition has committed. The
            # action phase above may have enrolled or renamed the first phone.
            for phone in records.values():
                origin = self.hass.config_entries.async_get_entry(phone.origin_entry_id)
                native = runtime_for_entry(origin)
                if phone.active and native is not None:
                    await self._deliver_directory_refresh(phone, native)

    async def _sync_legacy_memberships(self):
        """Project saved membership settings while native account enrollment is pending."""
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
            native = runtime_for_entry(origin)
            if not connected(native):
                continue
            from .phone_queue_membership import GROUP_MEMBERSHIP_VERSION, project_phone_memberships
            record = project_phone_memberships(self.entry, endpoint_id, saved)
            canonical_membership = self.entry.data.get(GROUP_MEMBERSHIP_VERSION) == 1
            for entity in er.async_entries_for_device(er.async_get(self.hass), device_id):
                field = _ROLES.get(entity_role(entity))
                state = self.hass.states.get(entity.entity_id)
                if (entity.platform != "esphome" or field not in {"queues", "ring_group"}
                        or state is None or state.state in {"unknown", "unavailable"}):
                    continue
                change = self._membership_changes.pop((device_id, field), None)
                scope = connection_scope(native)
                live = (change is not None and all(left is right for left, right in zip(change[0], scope))
                        and change[1] == record.get("membership_observed", {}).get(field)
                        and change[2] == membership_text(field, state.state))
                if canonical_membership:
                    record["membership_pending"] = {**record.get("membership_pending", {}),
                                                     field: membership_text(field, record.get(field))}
                record.update(observe_native_membership(record, field, state.state,
                                                       allow_change=live and not canonical_membership))
            if isinstance(record.get("queues"), tuple):
                record["queues"] = list(record["queues"])
            legacy[endpoint_id] = record
            self._membership_scopes[device_id] = connection_scope(native)
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
        revision, connection = self._revision, connection_scope(native)
        for entity in er.async_entries_for_device(er.async_get(self.hass), device_id):
            field = _ROLES.get(entity_role(entity))
            pending = normalize_membership_tracking(record.get("membership_pending"))
            if entity.platform != "esphome" or field not in pending:
                continue
            state = self.hass.states.get(entity.entity_id)
            if state is None or state.state in {"unknown", "unavailable"}:
                continue
            if (not self._owned() or revision != self._revision or not connection_is_current(connection)
                    or not connected(native)):
                return
            signature = (*map(id, connection), pending[field], record.get("membership_observed", {}).get(field))
            key = (device_id, field)
            if self._membership_delivered.get(key) == signature:
                continue
            try:
                await self.hass.services.async_call("text", "set_value", {
                    "entity_id": entity.entity_id, "value": pending[field]}, blocking=True)
            except Exception as error:
                _LOGGER.warning("Phone membership delivery pending for %s (%s)", device_id, type(error).__name__)
                return
            if not self._owned() or revision != self._revision or not connection_is_current(connection):
                return
            self._membership_delivered[key] = signature

    async def _deliver(self, phone, native):
        """Return whether the captured API owner still owns this sync operation."""
        revision, session = self._revision, ProvisioningActions.capture(native)
        if not self._current(phone, session, revision):
            return False
        cfg = transport_config(self.hass)
        host = cfg.get("advertise_host") or await network.async_get_source_ip(self.hass, target_ip=phone.api_host)
        if not self._current(phone, session, revision):
            return False
        register = REGISTER_HA_ACCOUNT
        actions = [("set_ha_display_name", {"name": phone.display_name.encode()[:64].decode('utf-8', 'ignore')}),
            ("set_account", dict(account_id=MANAGED_ACCOUNT_ID, registrar=host, port=int(cfg["sip_port"]),
                domain=host, username=phone.username, password=phone.password, auth_username=phone.username,
                proxy="", proxy_port=5060, transport="auto", expires=3600, persist=True)),
            (register, {})]
        directory_data = self._directory_data(host)
        if directory_data is not None:
            actions.append(("configure_directory", directory_data))
        previous_scope, delivered = self._delivered.get(phone.device_id, (None, {}))
        if not session.same_owner(previous_scope):
            delivered = {}
        by_name = session.by_name()
        for action, data in actions:
            if not self._current(phone, session, revision):
                return False
            if action not in by_name:
                continue
            signature = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).digest()
            if delivered.get(action) == signature:
                continue
            if action == "set_account":
                delivered.pop(register, None)
            await session.execute(by_name[action], data)
            if not self._current(phone, session, revision):
                return False
            delivered[action] = signature
            self._delivered[phone.device_id] = (session, delivered)
        return True

    def _directory_data(self, host):
        config = self._directory_config
        if config is None:
            return None
        return dict(server=host, port=config["ldap_port"], base_dn=config["base_dn"],
            bind_dn=f"uid={{username}},ou=accounts,{config['base_dn']}",
            account_id=MANAGED_ACCOUNT_ID, tls=config["tls"], enabled=config["enabled"])

    async def _deliver_directory_refresh(self, phone, native):
        revision, session = self._revision, ProvisioningActions.capture(native)
        if not self._current(phone, session, revision):
            return
        by_name = session.by_name()
        if not {"configure_directory", "refresh_ldap_directory"} <= by_name.keys():
            return  # A manually configured provider is not managed by HA.
        previous_scope, delivered = self._delivered.get(phone.device_id, (None, {}))
        if not session.same_owner(previous_scope):
            return  # The configuration phase must reach this API owner first.
        cfg = transport_config(self.hass)
        host = cfg.get("advertise_host") or await network.async_get_source_ip(self.hass, target_ip=phone.api_host)
        if not self._current(phone, session, revision):
            return
        data = self._directory_data(host)
        if data is None or delivered.get("configure_directory") != hashlib.sha256(json.dumps(data, sort_keys=True).encode()).digest():
            return
        directory = self.runtime.directory
        if (self._directory_config is not None and self._directory_config["enabled"]
                and directory is not None and not directory.closed and "refresh_ldap_directory" in by_name):
            generation = directory.generation
            signature = hashlib.sha256(f"{self._directory_epoch}:{generation}".encode()).digest()
            if delivered.get("refresh_ldap_directory") != signature and self._current(phone, session, revision):
                await session.execute(by_name["refresh_ldap_directory"], {})
                if (self._current(phone, session, revision)
                        and self.runtime.directory is directory and directory.generation == generation and not directory.closed):
                    delivered["refresh_ldap_directory"] = signature
                    self._delivered[phone.device_id] = (session, delivered)

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
        if self._state_unsubscribe is not None:
            self._state_unsubscribe()
            self._state_unsubscribe = None
        self._state_entities = frozenset()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._task is not None and self._task is not asyncio.current_task() and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        async with self._lock:
            self._delivered.clear()
