"""Opt-in physical phone reconciliation, without another call lifecycle."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .physical_phone import (
    CONF_PHONE_PROVISIONING,
    PHYSICAL_LINE_TRANSPORTS,
    PHONE_PROVISIONING_CHANGED,
    physical_phone_records,
    provisioning_enabled,
    sync_physical_phone_devices,
)
from .phone_profile import render_phone_bundle, resolve_phone_keys, resolve_phone_language
from .phone_profile_catalog import PhoneProfileCatalog
from .runtime_data import runtime_data


def provisioning_base_url(hass, entry):
    config = entry.data.get(CONF_PHONE_PROVISIONING, {})
    value = str(config.get("base_url") or "").rstrip("/")
    if not value:
        from homeassistant.helpers.network import get_url
        value = get_url(hass, allow_cloud=False, require_ssl=not config.get("allow_insecure_http", False)).rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and not config.get("allow_insecure_http", False))):
        raise ValueError("phone_provisioning_url_invalid")
    return value


def phone_provisioning_url(hass, entry, phone):
    return (provisioning_base_url(hass, entry) + "/api/voip_stack/phone_provisioning/"
            + phone["phone_id"] + "/" + phone["provisioning_token"] + "/")


@dataclass(frozen=True, slots=True, repr=False)
class RenderDependencies:
    """Immutable inputs, with account generations in place of credentials."""

    phone: tuple
    authentication: tuple
    profile: str
    context: tuple
    shared_services: tuple
    footprint: tuple


def _freeze(value):
    if isinstance(value, dict):
        return tuple((str(key), _freeze(item)) for key, item in sorted(value.items())
                     if str(key).casefold() not in {"password", "auth_password"})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _declared_slots(phone):
    return {"keys": sorted({str(key["slot"]) for key in phone.get("keys", ())}),
            "lines": {str(line["slot"]): [str(line["endpoint_id"])] for line in phone.get("lines", ())}}


def _stored_footprint(path, identity):
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    return saved.get("data", {}).get(identity, {}).get("managed_slots")


def validate_slot_removal(previous, proposed, profile, footprint=None):
    """Validate administrative removal against only the slots HA already owns."""
    owned = footprint if footprint is not None else _declared_slots(previous or proposed)
    active = _declared_slots(proposed)
    retired = {"keys": sorted(set(owned.get("keys", ())) - set(active["keys"])),
               "lines": sorted(set(owned.get("lines", {})) - set(active["lines"]))}
    caps = profile["capabilities"]
    if ((previous and previous.get("mac") != proposed.get("mac") and any(owned.values()))
            or owned.get("mac", proposed.get("mac")) != proposed.get("mac")):
        raise ValueError("phone_owned_mac_change_unsupported")
    declared_keys = {str(item["slot"]) for item in caps.get("key_slots", ())}
    if (set(owned.get("keys", ())) - declared_keys
            or any(not str(slot).isdigit() or not 1 <= int(slot) <= int(caps["max_lines"])
                   for slot in owned.get("lines", {}))):
        raise ValueError("phone_retired_slot_unsupported")
    for kind, capability in (("keys", "clear_keys"), ("lines", "disable_lines")):
        if retired[kind] and caps.get(capability) is not True:
            raise ValueError("phone_" + capability + "_unsupported")
    return retired


@dataclass(frozen=True, slots=True, repr=False)
class PreparedPhoneFiles:
    """Rendered bytes and the configuration/account lifetimes that own them."""

    dependencies: RenderDependencies
    profile: dict
    files: dict[str, bytes]


class PhysicalPhoneProvisioner:
    """Resolve one inventory revision using the existing directory and registrar."""

    def __init__(self, hass, entry):
        self.hass, self.entry, self.runtime = hass, entry, entry.runtime_data
        self.catalog = PhoneProfileCatalog(hass)
        self.states = {}
        self.files = {}
        self._pending = set()
        self._task = None
        self._closed = False
        self._unsubscribers = []
        self.discovery = None
        self._records = {}
        self.discovery_error = ""

    def owned(self):
        return (not self._closed and runtime_data(self.hass) is self.runtime
                and self.runtime.physical_phones is self and provisioning_enabled(self.entry))

    async def async_start(self):
        from homeassistant.helpers.storage import Store
        self.store = Store(self.hass, 1, f"voip_stack.physical_phones.{self.entry.entry_id}", atomic_writes=True)
        saved = await self.store.async_load()
        if not self.owned():
            return
        if isinstance(saved, dict):
            self.states = {key: value for key, value in saved.items() if isinstance(value, dict)}
        from .websocket_api import CALL_EVENT
        self._unsubscribers.append(self.hass.bus.async_listen(
            CALL_EVENT, lambda _event: self._wake_idle()))
        self._unsubscribers.append(self.hass.bus.async_listen(
            "core_config_updated", self._language_changed))
        self._unsubscribers.append(self.runtime.endpoints.subscribe(lambda _event: self.schedule()))
        if self.runtime.directory is not None:
            self._unsubscribers.append(self.runtime.directory.subscribe(lambda: self.schedule()))
        from .physical_phone_discovery import PhoneDiscovery
        discovery = self.discovery = PhoneDiscovery(self)
        try:
            await discovery.async_start()
        except (OSError, HomeAssistantError):
            self.discovery_error = "phone_discovery_unavailable"
            await discovery.async_close()
            self.discovery = None
        if not self.owned():
            await discovery.async_close()
            if self.discovery is discovery:
                self.discovery = None
            return
        registrar = self.runtime.sip.component("registrar") if self.runtime.sip else None
        if registrar is not None:
            self._unsubscribers.append(registrar.subscribe_account_changes(self._account_changed))
        await self.async_reconcile()

    @callback
    def _account_changed(self, usernames):
        self.schedule({identity for identity, phone in physical_phone_records(self.entry).items()
                       if any(endpoint is not None and endpoint.username.casefold() in {str(name).casefold() for name in usernames}
                              for line in phone["lines"]
                              if (endpoint := self.runtime.endpoints.get(line["endpoint_id"])) is not None)})

    @callback
    def _language_changed(self, event):
        # The same HA event also changes automatic internal/external URL choices.
        self.schedule()

    @callback
    def _wake_idle(self):
        self.schedule({identity for identity, state in self.states.items()
                       if state.get("state") == "waiting_idle"})

    @callback
    def schedule(self, identities=None):
        if not self.owned():
            return
        self._pending.update(physical_phone_records(self.entry) if identities is None else identities)
        if self._pending and (self._task is None or self._task.done()):
            self._task = self.hass.async_create_task(self._run(), "voip-physical-phone-reconcile")

    async def _run(self):
        while self._pending and self.owned():
            identity = self._pending.pop()
            phone = physical_phone_records(self.entry).get(identity)
            if phone is None:
                continue
            try:
                await self.async_prepare(phone)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A template/transport error must not disclose rendered credentials.
                state = self.states.setdefault(identity, {})
                state.update(state="error", error=type(error).__name__)
                self._changed(identity)

    async def async_reconcile(self):
        records = physical_phone_records(self.entry)
        sync_physical_phone_devices(self.hass, self.entry)
        for identity in self.states.keys() - records.keys():
            self.states.pop(identity, None)
            self.files.pop(identity, None)
        changed = {identity for identity, phone in records.items() if self._records.get(identity) != phone}
        self._records = records
        for identity, phone in records.items():
            prepared = self.files.get(identity)
            if identity in changed or prepared is None:
                continue
            try:
                current, _context = await self._capture(phone, prepared.profile,
                    profile_digest=prepared.dependencies.profile)
            except Exception:
                changed.add(identity)
            else:
                if current != prepared.dependencies:
                    changed.add(identity)
        self.schedule(changed)
        self._changed("")

    def busy(self, phone, *, previous=None):
        identities = {line["endpoint_id"] for line in phone["lines"]}
        for footprint in (self._footprint(phone), previous or {}):
            for references in footprint.get("lines", {}).values():
                identities.update(references)
        return any(endpoint is not None and endpoint.active_call_id for identity in identities
                   if (endpoint := self.runtime.endpoints.get(identity)) is not None)

    def _footprint(self, phone):
        saved = self.states.get(phone["phone_id"], {}).get("managed_slots", {})
        if not isinstance(saved, dict):
            saved = {}
        keys = saved.get("keys", ())
        lines = saved.get("lines", {})
        result = {"mac": saved.get("mac", phone["mac"]),
                  "keys": sorted({slot for slot in keys if isinstance(slot, str) and slot.isascii()
                                  and slot.isdigit() and str(int(slot)) == slot and 0 < int(slot) <= 256})
                  if isinstance(keys, (list, tuple)) else [], "lines": {}}
        if isinstance(lines, dict):
            for slot, references in lines.items():
                if (isinstance(slot, str) and slot.isascii() and slot.isdigit()
                        and str(int(slot)) == slot and 0 < int(slot) <= 64
                        and isinstance(references, (list, tuple))):
                    result["lines"][slot] = sorted({ref for ref in references
                        if isinstance(ref, str) and ref and len(ref) <= 512})
        return result

    def _proposed_footprint(self, phone):
        saved, declared = self._footprint(phone), _declared_slots(phone)
        saved["keys"] = sorted(set(saved["keys"]) | set(declared["keys"]))
        for slot, references in declared["lines"].items():
            # A slot deliberately reused by HA belongs to its new binding.
            saved["lines"][slot] = references
        return saved

    def validate_inventory_change(self, proposed, profile):
        current = physical_phone_records(self.entry).get(proposed["phone_id"])
        state = self.states.get(proposed["phone_id"], {})
        return validate_slot_removal(current, proposed, profile,
            self._footprint(proposed) if "managed_slots" in state else None)

    async def _capture(self, phone, profile, *, profile_digest=None):
        authentication = self.authentication_owner(phone)
        if authentication is None:
            raise ValueError("phone_registrar_or_account_unavailable")
        footprint = self._proposed_footprint(phone)
        context = await self.async_context(phone, profile)
        context["retired"] = validate_slot_removal(phone, phone, profile, footprint)
        requested, applied, epoch = self._directory_service()
        # Server certificate paths and account passwords are not snapshot data.
        fields = ("enabled", "ldap_port", "base_dn", "tls", "allow_anonymous")
        shared = ({"requested": {key: requested[key] for key in fields},
                   "applied": {key: applied[key] for key in fields} if applied is not None else None, "epoch": epoch}
                  if "ldap" in profile["capabilities"].get("functions", ()) else {})
        digest = profile_digest or hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        return RenderDependencies(_freeze(phone), authentication, digest, _freeze(context),
                                  _freeze(shared), _freeze(footprint)), context

    def _directory_service(self):
        from .phone_services import settings
        requested = settings(self.entry)
        controller = self.runtime.phone_provisioning
        applied = getattr(controller, "_directory_config", None) if controller is not None else None
        epoch = getattr(controller, "_directory_epoch", "") if controller is not None else ""
        return requested, applied if controller is not None else requested, epoch

    async def _persist_footprint(self, identity):
        """Commit on our one Store; HA logs write failures without raising them."""
        snapshot = deepcopy(self.states)
        expected = snapshot.get(identity, {}).get("managed_slots")

        async def commit():
            await self.store.async_save(snapshot)
            actual = await self.hass.async_add_executor_job(_stored_footprint, self.store.path, identity)
            if actual != expected:
                raise ValueError("phone_footprint_not_persisted")

        # The scoped commit settles before rollback, including its executor
        # write. Cancellation cannot release Store's lock over a running write.
        task = self.hass.async_create_task(commit(), "voip-physical-phone-store-commit")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def authentication_owner(self, phone):
        # An unassigned physical inventory owns only credential-free hardware
        # settings and its recorded retirement slots, through the same secret URL.
        if not phone["lines"]:
            return ()
        registrar = self.runtime.sip.component("registrar") if self.runtime.sip is not None else None
        if registrar is None:
            return None
        owners = []
        for binding in phone["lines"]:
            endpoint = self.runtime.endpoints.get(binding["endpoint_id"])
            if endpoint is None or str(endpoint.kind) != "sip_account":
                return None
            generation = registrar.authentication_generation(endpoint.username)
            if generation is None:
                return None
            owners.append((endpoint.endpoint_id, endpoint.username.casefold(), generation))
        return tuple(owners) if owners else None

    async def async_context(self, phone, profile):
        from homeassistant.components import network
        from ldap3.utils.dn import escape_rdn

        cfg = self.runtime.transport_config
        host = cfg.get("advertise_host") or (await network.async_get_source_ip(
            self.hass, target_ip=phone.get("host") or "224.0.1.75") if phone["lines"] else "")
        registrar = self.runtime.sip.component("registrar") if self.runtime.sip is not None else None
        if registrar is None and phone["lines"]:
            raise ValueError("phone_registrar_unavailable")
        lines = []
        caps = profile["capabilities"]
        for binding in phone["lines"]:
            endpoint = self.runtime.endpoints.get(binding["endpoint_id"])
            if endpoint is None or str(endpoint.kind) != "sip_account":
                raise ValueError("phone_sip_account_required")
            account = registrar.accounts.get(endpoint.username.casefold())
            if account is None or not account.enabled or not 1 <= int(binding["slot"]) <= int(caps["max_lines"]):
                raise ValueError("phone_line_unsupported")
            transport = str(binding.get("transport") or "udp").lower()
            if transport not in PHYSICAL_LINE_TRANSPORTS:
                raise ValueError("invalid_phone_transport")
            lines.append({"slot": int(binding["slot"]), "username": account.username,
                "auth_username": account.username, "password": account.password,
                "display_name": endpoint.name, "server": host, "domain": host,
                "port": int(cfg["sip_port"]), "transport": transport,
                "expires": 300, "outbound_proxy": ""})
        requested = getattr(self.hass.config, "language", "en") if phone["follow_ha_language"] else phone["language"]
        language_code, language = "", ""
        if caps.get("languages"):
            try:
                language_code, language = resolve_phone_language(profile, requested)
            except ValueError:
                # An unsupported new HA locale must not block line/key updates.
                # The current supported language is retained in the desired data.
                previous = self.states.get(phone["phone_id"], {}).get("language_code", "") or phone.get("language", "")
                if previous:
                    language_code, language = resolve_phone_language(profile, previous)
                elif not phone["follow_ha_language"]:
                    raise
        time_zone = ""
        time_zones = caps.get("time_zones", {})
        if isinstance(time_zones, dict) and time_zones:
            requested_time_zone = getattr(self.hass.config, "time_zone", "")
            if requested_time_zone in time_zones:
                time_zone = requested_time_zone
        directory = {}
        requested, config, _epoch = self._directory_service()
        ldap_requested = bool(lines) and requested["enabled"] and "ldap" in caps.get("functions", ()) and phone.get("directory_enabled", True)
        if ldap_requested and config is None:
            raise ValueError("phone_directory_service_unavailable")
        if ldap_requested and config["enabled"]:
            first = lines[0]
            directory["ldap"] = {"host": host, "port": config["ldap_port"],
                "base_dn": config["base_dn"], "bind_dn": f"uid={escape_rdn(first['username'])},ou=accounts,{config['base_dn']}",
                "password": first["password"], "display_name": "VoIP Stack",
                "name_filter": "(|(cn=%)(givenName=%)(sn=%))",
                "number_filter": "(|(telephonenumber=%)(mobile=%))",
                "name_attributes": "cn", "number_attributes": "telephonenumber mobile"}
        return {"physical": {"name": phone["name"], "mac": phone["mac"],
                             "language": language, "language_code": language_code,
                             "time_zone": time_zone},
                "lines": lines, "keys": resolve_phone_keys(self.runtime, phone, profile),
                "directory": directory, "provisioning": {"url": phone_provisioning_url(self.hass, self.entry, phone)
                    if "provision" in caps.get("functions", ()) else ""}}

    async def async_prepare(self, phone):
        identity = phone["phone_id"]
        state = self.states.setdefault(identity, {})
        if not phone.get("enabled", True):
            state.update(state="disabled")
            self.files.pop(identity, None)
            self._changed(identity)
            return
        if self.busy(phone):
            state.update(state="waiting_idle")
            self._changed(identity)
            return
        authentication = self.authentication_owner(phone)
        if authentication is None:
            raise ValueError("phone_registrar_or_account_unavailable")
        prepared = self.files.get(identity)
        if (prepared is not None and _freeze(phone) == prepared.dependencies.phone
                and not state.get("error")):
            current, _context = await self._capture(phone, prepared.profile,
                profile_digest=prepared.dependencies.profile)
            if current == prepared.dependencies:
                available = "unassigned" if not phone["lines"] else "downloaded" if state.get("downloaded_revision") == state.get("desired_revision") else "ready"
                if state.get("state") != available:
                    state["state"] = available
                    self._changed(identity)
                return
        profile, templates = await self.catalog.async_profile(phone["profile_id"], commit=phone.get("catalog_commit", ""))
        profile_digest = hashlib.sha256(json.dumps({"profile": profile, "templates": templates},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        dependencies, context = await self._capture(phone, profile, profile_digest=profile_digest)
        prepared = self.files.get(identity)
        if prepared is not None and prepared.dependencies == dependencies and not state.get("error"):
            available = "unassigned" if not phone["lines"] else "downloaded" if state.get("downloaded_revision") == state.get("desired_revision") else "ready"
            if state.get("state") != available:
                state["state"] = available
                self._changed(identity)
            return
        files, revision = await self.hass.async_add_executor_job(render_phone_bundle, profile, templates, context)
        current, _current_context = await self._capture(phone, profile, profile_digest=profile_digest)
        if (not self.owned() or physical_phone_records(self.entry).get(identity) != phone
                or self.authentication_owner(phone) != authentication or current != dependencies):
            return
        if self.busy(phone):
            state.update(state="waiting_idle")
            self._changed(identity)
            return
        old_footprint = self._footprint(phone)
        footprint = self._proposed_footprint(phone)
        previous_footprint = state.get("managed_slots")
        footprint_attempted, published = False, False
        try:
            if previous_footprint != footprint:
                footprint_attempted = True
                state["managed_slots"] = footprint
                await self._persist_footprint(identity)
                current, _current_context = await self._capture(phone, profile, profile_digest=profile_digest)
                if (not self.owned() or physical_phone_records(self.entry).get(identity) != phone
                        or current != dependencies):
                    return
            if self.busy(phone, previous=old_footprint):
                state.update(state="waiting_idle")
                self._changed(identity)
                return
            self.files[identity] = PreparedPhoneFiles(dependencies, deepcopy(profile), files)
            state.update(state="unassigned" if not phone["lines"] else "downloaded" if state.get("downloaded_revision") == revision else "ready",
                         desired_revision=revision, error="", profile_id=phone["profile_id"],
                         language_code=context["physical"]["language_code"],
                         file_formats={output["path"].replace("{mac}", phone["mac"]): output["format"]
                                       for output in profile["outputs"]})
            published = True
            self._changed(identity)
        finally:
            if footprint_attempted and not published:
                # A cancelled/stale candidate must not acquire slots it never
                # made available. Store serializes this rollback with its write.
                if previous_footprint is None:
                    state.pop("managed_slots", None)
                else:
                    state["managed_slots"] = previous_footprint
                await self._persist_footprint(identity)

    async def async_file(self, identity, token, filename):
        if not self.owned():
            return None
        phone = physical_phone_records(self.entry).get(identity)
        if (phone is None or not phone.get("enabled", True)
                or not secrets.compare_digest(phone["provisioning_token"].encode(), token.encode())):
            return None
        if self.busy(phone):
            return None
        prepared = self.files.get(identity)
        if prepared is None:
            return None
        try:
            current, _context = await self._capture(phone, prepared.profile,
                                                   profile_digest=prepared.dependencies.profile)
        except Exception:
            self.schedule({identity})
            return None
        if (not self.owned() or physical_phone_records(self.entry).get(identity) != phone
                or current != prepared.dependencies or self.busy(phone)):
            self.schedule({identity})
            return None
        data = prepared.files.get(filename)
        if data is None:
            return None
        state = self.states.setdefault(identity, {})
        state.update(state="error" if state.get("error") else "unassigned" if not phone["lines"] else "downloaded",
                     downloaded_revision=state.get("desired_revision"), last_download=time.time())
        self.store.async_delay_save(lambda: self.states, 1)
        self._changed(identity)
        return data

    def public_state(self, identity):
        phone = physical_phone_records(self.entry).get(identity)
        value = dict(self.states.get(identity, {}))
        value.pop("managed_slots", None)
        if phone is not None:
            registrar = self.runtime.sip.component("registrar") if self.runtime.sip else None
            value["lines"] = [{"slot": line["slot"], "endpoint_id": line["endpoint_id"],
                "account_registered": bool(registrar and (endpoint := self.runtime.endpoints.get(line["endpoint_id"]))
                                           and registrar.registered_contacts(endpoint.username)),
                "device_registration_verified": False} for line in phone["lines"]]
        return value

    @callback
    def _changed(self, identity):
        self.hass.bus.async_fire(PHONE_PROVISIONING_CHANGED, {"phone_id": identity})

    async def async_close(self, *, clear_assets=False):
        self._closed = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self.discovery is not None:
            await self.discovery.async_close()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.files.clear()
        from .physical_phone_services import unregister_physical_phone_services
        unregister_physical_phone_services(self.hass)
        if clear_assets:
            await self.catalog.async_clear()


async def async_reconcile_physical_phones(hass, entry):
    runtime = entry.runtime_data
    manager = runtime.physical_phones
    if not provisioning_enabled(entry):
        if manager is not None:
            await manager.async_close(clear_assets=True)
            runtime.physical_phones = None
            hass.bus.async_fire(PHONE_PROVISIONING_CHANGED, {"phone_id": ""})
        return
    if manager is None:
        from .physical_phone_http import async_register_physical_phone_http
        async_register_physical_phone_http(hass)
        runtime.physical_phones = manager = PhysicalPhoneProvisioner(hass, entry)
        from .physical_phone_services import register_physical_phone_services
        register_physical_phone_services(hass)
        await manager.async_start()
    else:
        await manager.async_reconcile()
