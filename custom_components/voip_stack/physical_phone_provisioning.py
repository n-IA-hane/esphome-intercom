"""Opt-in physical phone reconciliation, without another call lifecycle."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
import socket
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
from .physical_phone_apply import is_safe_phone_error, safe_phone_error
from .runtime_data import runtime_data


def provisioning_base_url(hass, entry, *, config=None):
    """Resolve the same validated URL for saved and provisional configuration."""
    if config is None:
        config = entry.data.get(CONF_PHONE_PROVISIONING, {})
    value = str(config.get("base_url") or "").strip().rstrip("/")
    if not value:
        from homeassistant.helpers.network import NoURLAvailableError, get_url
        try:
            value = get_url(hass, allow_cloud=False,
                require_ssl=not config.get("allow_insecure_http", False)).rstrip("/")
        except NoURLAvailableError:
            raise ValueError("phone_provisioning_url_unavailable") from None
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment
                 and (parsed.port is None or 1 <= parsed.port <= 65535)
                 and (parsed.scheme == "https" or config.get("allow_insecure_http", False)))
    except ValueError:
        valid = False
    if not valid:
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


def _render_inventory(phone):
    """Administrative credentials never enter renderer input or dependencies."""
    return {key: value for key, value in phone.items() if key not in {"admin_username", "admin_password"}}


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
        self._idle_retry_cancel = None
        self._active_apply = None

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
            for state in self.states.values():
                if state.get("apply_status") == "requesting":
                    state.update(apply_status="uncertain" if state.get("apply_attempted_revision") else "pending",
                        apply_error="phone_apply_result_uncertain" if state.get("apply_attempted_revision") else "")
        from .websocket_api import CALL_EVENT
        self._unsubscribers.append(self.hass.bus.async_listen(
            CALL_EVENT, self._wake_idle))
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
    def _wake_idle(self, _event=None):
        self.schedule({identity for identity, state in self.states.items()
                       if state.get("state") == "waiting_idle" or state.get("apply_status") == "waiting_idle"})

    def _idle_retry_ids(self):
        if not self.owned():
            return set()
        records = physical_phone_records(self.entry)
        return {identity for identity, state in self.states.items()
                if (phone := records.get(identity)) is not None and phone.get("enabled", True)
                and phone.get("admin_username") and phone.get("admin_password") and not state.get("error")
                and state.get("apply_status") not in {"requesting", "uncertain", "error", "manual", "not_qualified"}
                # Preparation can be pending behind an older requested revision.
                # Waking its worker cannot replay that revision's attempted POST.
                and (state.get("state") == "waiting_idle"
                     or (state.get("apply_status") == "waiting_idle"
                         and state.get("apply_attempted_revision") != state.get("desired_revision")))}

    @callback
    def _sync_idle_retry(self):
        if not self._idle_retry_ids():
            if self._idle_retry_cancel is not None:
                self._idle_retry_cancel()
                self._idle_retry_cancel = None
        elif self._idle_retry_cancel is None:
            from homeassistant.helpers.event import async_call_later
            self._idle_retry_cancel = async_call_later(self.hass, 60, self._retry_idle)

    @callback
    def _retry_idle(self, _now):
        self._idle_retry_cancel = None
        self.schedule(self._idle_retry_ids())
        self._sync_idle_retry()

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
                await self._async_apply_if_needed(phone)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A template/transport error must not disclose rendered credentials.
                state = self.states.setdefault(identity, {})
                state.update(state="error", error=safe_phone_error(error))
                self._changed(identity)

    async def _apply_target(self, phone, capability):
        """Resolve a route, never infer phone availability from an address."""
        from yarl import URL

        registrar = self.runtime.sip.component("registrar") if self.runtime.sip is not None else None
        contacts = [registration for line in phone["lines"]
                    if (endpoint := self.runtime.endpoints.get(line["endpoint_id"])) is not None and registrar is not None
                    for registration in registrar.registered_contacts(endpoint.username)]
        paired = self.states.get(phone["phone_id"], {}).get("apply_target_instance", "")
        matched = [contact for contact in contacts if paired and contact.instance_id == paired]
        hosts = {contact.source_host for contact in matched}
        if not hosts and phone.get("host"):
            configured = str(phone["host"])
            try:
                hosts = {str(ipaddress.ip_address(configured))}
            except ValueError:
                addresses = await self.hass.loop.getaddrinfo(configured, int(capability.get("port", 80)), type=socket.SOCK_STREAM)
                hosts = {address[4][0] for address in addresses}
            observed = hosts & {contact.source_host for contact in contacts}
            if observed:
                hosts = observed
        elif not hosts:
            hosts = {contact.source_host for contact in contacts}
        if len(hosts) != 1:
            from .physical_phone_apply import PhoneApplyError
            raise PhoneApplyError("phone_apply_target_unverified")
        host = next(iter(hosts))
        address = ipaddress.ip_address(host)
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
            from .physical_phone_apply import PhoneApplyError
            raise PhoneApplyError("phone_apply_target_unverified")
        instances = {contact.instance_id for contact in contacts if contact.source_host == host and contact.instance_id}
        instance = next(iter(instances)) if len(instances) == 1 else ""
        return str(URL.build(scheme=capability.get("scheme", "http"), host=host,
                             port=int(capability.get("port", 80)))), instance

    async def _async_apply_if_needed(self, phone):
        """Apply only the latest qualified revision through the existing worker."""
        from aiohttp import ClientError, ClientTimeout, CookieJar
        from homeassistant.helpers.aiohttp_client import async_create_clientsession
        from .physical_phone_apply import PhoneApplyDeferred, PhoneApplyError, async_apply_fanvil

        identity = phone["phone_id"]
        state = self.states.setdefault(identity, {})
        prepared = self.files.get(identity)
        def status(value):
            if state.get("apply_status") != value:
                state["apply_status"] = value
                self._changed(identity)
        if not self.owned() or not phone.get("enabled", True) or prepared is None or state.get("error"):
            return
        active = self._active_apply
        if (state.get("apply_status") == "requesting"
                and not (active is not None and active[0] == identity and not active[1].done())):
            attempted = bool(state.get("apply_attempted_revision"))
            state.update(apply_status="uncertain" if attempted else "pending",
                         apply_error="phone_apply_result_uncertain" if attempted else "")
            self.store.async_delay_save(lambda: self.states, 1)
            self._changed(identity)
        capability = prepared.profile["capabilities"].get("apply", {})
        if not isinstance(capability, dict) or capability.get("qualified") is not True or capability.get("method") != "fanvil_native_http":
            status("not_qualified")
            return
        if not phone.get("admin_username") or not phone.get("admin_password"):
            status("manual")
            return
        revision = state.get("desired_revision", "")
        if not revision or state.get("apply_attempted_revision") == revision:
            return
        if self.busy(phone):
            status("waiting_idle")
            return

        dispatched = False

        def same_candidate(*, closing=False):
            owner = physical_phone_records(self.entry).get(identity)
            # Credential edits revoke authority to send, not ownership of an
            # already dispatched command for the same physical/render owner.
            same_phone = (owner is not None and _render_inventory(owner) == _render_inventory(phone)) if dispatched else owner == phone
            return ((closing or self.owned()) and self._active_apply is attempt and self.states.get(identity) is state
                    and self.files.get(identity) is prepared and state.get("desired_revision") == revision
                    and same_phone)

        @callback
        def dispatch():
            nonlocal dispatched
            dispatched = True
            state["apply_attempted_revision"] = revision
            self.store.async_delay_save(lambda: self.states, 1)

        def uncertain(*, closing=False):
            if same_candidate(closing=closing):
                state.update(apply_status="uncertain", apply_error="phone_apply_result_uncertain")

        async def current():
            if (not self.owned() or self.files.get(identity) is not prepared
                    or physical_phone_records(self.entry).get(identity) != phone or self.busy(phone)):
                return False
            try:
                dependencies, _context = await self._capture(phone, prepared.profile,
                    profile_digest=prepared.dependencies.profile)
            except (ValueError, HomeAssistantError):
                return False
            return dependencies == prepared.dependencies

        attempt = (identity, asyncio.current_task())
        self._active_apply = attempt
        try:
            origin, instance = await self._apply_target(phone, capability)
            if not await current():
                return
            state.update(apply_status="requesting", apply_error="")
            self._changed(identity)
            self.store.async_delay_save(lambda: self.states, 1)
            session = async_create_clientsession(self.hass, auto_cleanup=False,
                cookie_jar=CookieJar(unsafe=True), timeout=ClientTimeout(total=15), trust_env=False)
            try:
                async with asyncio.timeout(25), session:
                    result = await async_apply_fanvil(session, origin, phone, prepared.profile,
                        phone_provisioning_url(self.hass, self.entry, phone), still_current=current,
                        on_dispatch=dispatch)
            finally:
                session.detach()
            if same_candidate():
                state.update(apply_status="requested", apply_attempted_revision=revision,
                             apply_request_time=time.time(), apply_error="",
                             apply_target_instance=instance or state.get("apply_target_instance", ""), apply_method=result["method"])
        except asyncio.CancelledError:
            if dispatched:
                uncertain(closing=True)
            elif same_candidate(closing=True):
                state.update(apply_status="pending", apply_error="")
            raise
        except PhoneApplyDeferred as error:
            if dispatched:
                uncertain()
            else:
                if self.states.get(identity) is state and state.get("apply_attempted_revision") == revision:
                    state.pop("apply_attempted_revision", None)
                if same_candidate():
                    state["apply_status"] = "waiting_idle" if str(error) == "phone_apply_waiting_idle" else "pending"
            if not dispatched and str(error) == "phone_configuration_changed" and self.owned():
                self.schedule({identity})
        except Exception as error:
            if dispatched:
                uncertain()
            elif same_candidate():
                state.update(apply_status="error", apply_attempted_revision=revision,
                             apply_error=safe_phone_error(error) if isinstance(error, PhoneApplyError)
                             else "phone_apply_http_error" if isinstance(error, (ClientError, TimeoutError, OSError))
                             else safe_phone_error(error))
        finally:
            if self._active_apply is attempt:
                self._active_apply = None
            self.store.async_delay_save(lambda: self.states, 1)
            self._changed(identity)

    async def async_reconcile(self):
        records = physical_phone_records(self.entry)
        sync_physical_phone_devices(self.hass, self.entry)
        for identity in self.states.keys() - records.keys():
            self.states.pop(identity, None)
            self.files.pop(identity, None)
        changed = {identity for identity, phone in records.items() if self._records.get(identity) != phone}
        changed.update(identity for identity in records if self.states.get(identity, {}).get("error"))
        for identity, phone in records.items():
            before = self._records.get(identity)
            if (before is not None and self.states.get(identity, {}).get("apply_status") == "error"
                    and any(before.get(key) != phone.get(key) for key in ("admin_username", "admin_password"))):
                self.states.setdefault(identity, {}).pop("apply_attempted_revision", None)
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
        return RenderDependencies(_freeze(_render_inventory(phone)), authentication, digest, _freeze(context),
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
            mode = "ldaps" if config["tls"] else "plain"
            if mode not in caps.get("ldap_modes", ["plain"]):
                raise ValueError("phone_ldap_mode_unsupported")
            first = lines[0]
            directory["ldap"] = {"mode": mode, "host": host, "port": config["ldap_port"],
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
        if (prepared is not None and _freeze(_render_inventory(phone)) == prepared.dependencies.phone
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
        for key in ("error", "apply_error"):
            if key in value and not is_safe_phone_error(value[key]):
                value.pop(key)
        if phone is not None:
            registrar = self.runtime.sip.component("registrar") if self.runtime.sip else None
            value["lines"] = [{"slot": line["slot"], "endpoint_id": line["endpoint_id"],
                "account_registered": bool(registrar and (endpoint := self.runtime.endpoints.get(line["endpoint_id"]))
                                           and registrar.registered_contacts(endpoint.username)),
                "device_registration_verified": False} for line in phone["lines"]]
        return value

    @callback
    def _changed(self, identity):
        self._sync_idle_retry()
        self.hass.bus.async_fire(PHONE_PROVISIONING_CHANGED, {"phone_id": identity})

    async def async_close(self, *, clear_assets=False):
        if self._closed:
            if clear_assets:
                await self.catalog.async_clear()
            return
        self._closed = True
        self._sync_idle_retry()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self.discovery is not None:
            await self.discovery.async_close()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if (store := getattr(self, "store", None)) is not None:
            await store.async_save(self.states)
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
