"""Native physical-phone editor, binding existing logical SIP account owners."""

from __future__ import annotations

from copy import deepcopy
import re
import time

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigSubentry, ConfigSubentryFlow, SOURCE_USER
from homeassistant.data_entry_flow import Optional as visible_optional, section
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.selector import (
    AreaSelector, BooleanSelector, DeviceSelector, EntitySelector, ObjectSelector,
    SelectSelector, TextSelector, TextSelectorConfig, TextSelectorType,
)

from .const import DOMAIN
from .endpoint_device import endpoint_device_identifier
from .group_config import group_records
from .phone_config import (
    _add_phone_subentry, endpoint_from_data, phone_subentries,
    phone_subentry_data, validate_sip_account_namespace,
)
from .phone_directory import contact_key_reference
from .phone_endpoint import EndpointKind
from .phone_profile import resolve_phone_language
from .phone_profile_catalog import PhoneProfileCatalog
from .physical_phone import (
    KEY_FUNCTIONS, PHYSICAL_LINE_TRANSPORTS, PHYSICAL_PHONE_SUBENTRY_TYPE, physical_phone_records,
    provisioning_enabled, discovery_enabled, draft_physical_subentry, physical_phone_data, physical_phone_id, physical_phone_model,
    matches_announced_model, phone_candidate_name, reorder_physical_keys,
)
from .physical_phone_validation import physical_slot, physical_validation_runtime, validate_physical_inventory

CREATE_ACCOUNT = "__create_account__"
_IDENTITY_FIELDS = frozenset({
    "name", "mac", "profile_id", "host", "area_id", "person",
    "follow_ha_language", "enabled",
})


def _slot(value):
    return physical_slot(value)


def _readable_refs(names, *, reserved=None):
    """HA's editable select prints its wire value. Scope readable choices to this form."""
    refs = dict(reserved or {})
    for ref, name in sorted(names.items()):
        label = name
        # A number remains available to type verbatim, even when a contact has that name.
        count = 1
        while label in refs or re.fullmatch(r"[+*#0-9]+", label):
            label = f"{name} ({count})"
            count += 1
        refs[label] = ref
    return refs


class PhysicalPhoneSubentryFlowHandler(ConfigSubentryFlow):
    """Keep configuration drafts private until the complete phone is valid."""

    def __init__(self):
        self._current = None
        self._seed = {}
        self._draft = {}
        self._accounts = {}
        self._account_records = {}
        self._inventory_draft = None
        self._inventory_mac = ""
        self._account_flow = None
        self._catalog = None
        self._manifest = None
        self._manifest_commit = ""
        self._profile = None
        self._language_override = False
        self._account_devices = {}
        self._target_devices = {}
        self._other_accounts = {}
        self._other_targets = {}
        self._functions = {}
        self._selector_labels = {}

    def _previous(self):
        return physical_phone_data(self._current) if self._current is not None else self._seed

    def _phone_accounts(self):
        return {str(phone_subentry_data(item)["endpoint_id"]): phone_subentry_data(item)
                for item in phone_subentries(self._get_entry())
                if item.data.get("kind") == EndpointKind.SIP_ACCOUNT.value}

    def _selection_devices(self, identities, *, groups=()):
        """Project existing HA registry devices onto this entry's allowed references."""
        devices = dr.async_get(self.hass)
        entry = self._get_entry()
        endpoints = getattr(getattr(entry, "runtime_data", None), "endpoints", None)
        result = {}
        for identity in identities:
            endpoint = endpoints.get(identity) if endpoints is not None else None
            device_id = (endpoint.account_device_id or endpoint.device_id) if endpoint else ""
            device = devices.async_get(device_id) if device_id else None
            if device is None:
                device = devices.async_get_device_by_identifier((DOMAIN, endpoint_device_identifier(identity)), entry.entry_id)
            if device is not None and not device.disabled_by:
                result[device.id] = identity
        for identity in groups:
            device = devices.async_get_device_by_identifier((DOMAIN, f"group:{identity}"), entry.entry_id)
            if device is not None and not device.disabled_by:
                result[device.id] = identity
        return result

    def _device_selector(self, refs):
        devices = dr.async_get(self.hass)
        filters = []
        for identity in refs:
            device = devices.async_get(identity)
            # HA filters the picker by product. Only this entry's mapped device IDs are accepted.
            config = self.hass.config_entries.async_get_entry(device.config_entry_id)
            match = {key: value for key, value in (
                ("integration", config.domain if config else None),
                ("manufacturer", device.manufacturer), ("model", device.model), ("model_id", device.model_id),
            ) if value}
            if match not in filters:
                filters.append(match)
        return DeviceSelector({"filter": filters})

    async def _load_selector_labels(self):
        language = self.context.get("language") or self.hass.config.language
        self._selector_labels = await async_get_translations(self.hass, language, "selector", [DOMAIN])

    def _option_label(self, category, value):
        return self._selector_labels.get(f"component.{DOMAIN}.selector.{category}.options.{value}", value)

    async def _manifest_for_form(self):
        if self._catalog is None:
            owner = getattr(getattr(self._get_entry(), "runtime_data", None), "physical_phones", None)
            self._catalog = getattr(owner, "catalog", None) or PhoneProfileCatalog(self.hass)
        if self._manifest is None:
            pin = self._previous().get("catalog_commit", "")
            self._manifest = await self._catalog.async_manifest(commit=pin)
            self._manifest_commit = pin or self._catalog.commit
        return self._manifest

    async def async_step_reconfigure(self, user_input=None):
        self._current = self._get_reconfigure_subentry()
        self._draft = deepcopy(physical_phone_data(self._current))
        if not provisioning_enabled(self._get_entry()):
            return self.async_abort(reason="phone_provisioning_disabled")
        options = ["user", "bindings"]
        if len(self._draft.get("keys", [])) > 1:
            options.append("key_order")
        return self.async_show_menu(step_id="reconfigure", menu_options=options)

    def _new_inventory_draft(self):
        if self._current is not None:
            return
        mac = re.sub(r"[:-]", "", str(self._draft.get("mac") or "")).lower()
        if not mac:
            return
        if self._inventory_mac and self._inventory_mac != mac:
            self._inventory_draft = None
            self._seed = {key: value for key, value in self._seed.items() if key not in {"phone_id", "provisioning_token"}}
        discovery = getattr(getattr(self._get_entry(), "runtime_data", None), "phone_discovery", None)
        candidate = getattr(discovery, "candidates", {}).get(mac)
        if (candidate is not None and discovery_enabled(self._get_entry())
                and not getattr(discovery, "_closed", False) and time.monotonic() - candidate.seen < 86400):
            self._seed.update(candidate.setup_data())
            self._inventory_draft = candidate.native_subentry
        if self._inventory_draft is None:
            self._inventory_draft = draft_physical_subentry({key: value for key, value in self._seed.items() if key != "phone_id"})
        self._inventory_mac = mac
        self._seed = {**self._seed, **physical_phone_data(self._inventory_draft)}

    def _store_inventory(self, data):
        if (self._current is not None and "pnp_enrollment" in data
                and any(data[key] != self._current.data[key] for key in ("mac", "profile_id"))):
            data["pnp_enrollment"] = {}
        record = draft_physical_subentry(data, previous=self._current or self._inventory_draft, title=data["name"])
        if self._current is not None:
            self.hass.config_entries.async_update_subentry(self._get_entry(), self._current,
                data=record.data, title=record.title, unique_id=record.unique_id)
            return self.async_abort(reason="reconfigure_successful")
        self.hass.config_entries.async_add_subentry(self._get_entry(), record)
        return self.async_abort(reason="phone_configured")

    def _finish_identity(self):
        if self._current is None:
            identity = str(self._draft.pop("account_id", "") or "")
            if identity and identity not in self._phone_accounts():
                raise ValueError("phone_sip_account_required")
            self._draft["lines"] = [{"slot": "1", "endpoint_id": identity, "transport": "udp"}] if identity else []
            self._draft["keys"] = []
        return self._store_inventory(self._validate_phone())

    async def async_step_user(self, user_input=None):
        if not provisioning_enabled(self._get_entry()):
            return self.async_abort(reason="phone_provisioning_disabled")
        if (user_input is not None and "discovery_candidate" in user_input
                and self.context.get("source") == SOURCE_USER and not self._draft):
            candidate = user_input["discovery_candidate"]
            if not isinstance(candidate, dict):
                return self.async_abort(reason="invalid_phone_discovery")
            self._seed = {key: candidate[key] for key in
                          ("phone_id", "mac", "provisioning_token") if candidate.get(key)}
            self._draft = {"mac": candidate["mac"]} if candidate.get("mac") else {}
            self._draft["name"] = phone_candidate_name(candidate.get("vendor"), candidate.get("model"))
            self._discovery_initial_name = self._draft["name"]
            self._discovery_model = (str(candidate.get("vendor") or "").lower(),
                                     re.sub(r"[^a-z0-9]", "", str(candidate.get("model") or "").lower()))
            user_input = None
        self._new_inventory_draft()
        errors = {}
        try:
            manifest = await self._manifest_for_form()
        except (OSError, ValueError, TimeoutError, ClientError):
            return self.async_abort(reason="phone_catalog_unavailable")
        profiles = [item for item in manifest["profiles"] if item.get("status") in {
            "software_validated", "protocol_verified", "hardware_qualified"}]
        choices = [{"value": item["id"],
                    "label": f"{item['vendor'][:1].upper()}{item['vendor'][1:]} {physical_phone_model(item['id'])}"}
                   for item in profiles]
        if not choices:
            return self.async_abort(reason="phone_catalog_unavailable")
        if user_input is not None:
            self._draft.update({key: value for key, value in user_input.items() if key in _IDENTITY_FIELDS})
            if self._current is None:
                device = user_input.get("account_device")
                account = user_input.get("account_id")
                if device and (device not in self._account_devices or account):
                    errors["base"] = "phone_sip_account_required"
                else:
                    self._draft["account_id"] = self._account_devices.get(device) or account or ""
            if "advanced" in user_input:
                advanced = user_input["advanced"]
                if not isinstance(advanced, dict):
                    return self.async_show_form(step_id="user", errors={"base": "invalid_phone_host"})
                self._draft["host"] = advanced.get("host", self._draft.get("host", ""))
                self._draft.update({key: advanced[key] for key in ("area_id", "person", "follow_ha_language", "enabled") if key in advanced})
                if "admin_username" in advanced:
                    self._draft["admin_username"] = advanced["admin_username"]
                if advanced.get("admin_password"):
                    self._draft["admin_password"] = advanced["admin_password"]
            try:
                if errors:
                    raise ValueError(errors["base"])
                name = str(self._draft.get("name") or "").strip()
                mac = re.sub(r"[:-]", "", str(self._draft.get("mac") or "")).lower()
                if not re.fullmatch(r"[0-9a-f]{12}", mac):
                    raise ValueError("invalid_phone_mac")
                if not name or len(name) > 100:
                    raise ValueError("invalid_phone_profile")
                self._draft.update(name=name, mac=mac)
                self._check_mac(mac)
                self._new_inventory_draft()
                identity = str(self._draft.get("profile_id") or "")
                changed = bool(self._current is not None and identity != self._current.data.get("profile_id"))
                if user_input.get("update_profile") or changed:
                    self._manifest = await self._catalog.async_manifest(refresh=True)
                    self._manifest_commit = self._catalog.commit
                if not re.fullmatch(r"[0-9a-f]{40}", self._manifest_commit):
                    raise ValueError("phone_catalog_unavailable")
                self._profile, _templates = await self._catalog.async_profile(identity, commit=self._manifest_commit)
                self._draft["catalog_commit"] = self._manifest_commit
                caps = self._profile["capabilities"]
                if not 1 <= int(caps["max_lines"]) <= 64:
                    raise ValueError("invalid_phone_profile")
                host = str(self._draft.get("host") or "")
                if len(host) > 253 or any(ord(char) <= 32 or char in "/\\@?#" for char in host):
                    raise ValueError("invalid_phone_host")
                follow = bool(self._draft.get("follow_ha_language", True))
                if not follow:
                    if not caps.get("languages"):
                        raise ValueError("phone_language_unsupported")
                    return await self.async_step_language()
                if caps.get("languages"):
                    try:
                        canonical, _vendor = resolve_phone_language(self._profile, self.hass.config.language)
                    except ValueError:
                        if self._current is not None and self._previous().get("language"):
                            try:
                                canonical, _vendor = resolve_phone_language(self._profile, self._previous()["language"])
                            except ValueError:
                                pass
                            else:
                                self._draft["language"] = canonical
                                return self._finish_identity()
                        self._language_override = True
                        return await self.async_step_language()
                    self._draft["language"] = canonical
                else:
                    self._draft["language"] = ""
                return self._finish_identity()
            except (ValueError, KeyError, TypeError, OSError, TimeoutError, ClientError) as error:
                errors["base"] = self._error(error)
        values = self._draft
        default_profile = values.get("profile_id", "")
        if not default_profile and hasattr(self, "_discovery_model"):
            vendor, model = self._discovery_model
            matches = [item for item in profiles if matches_announced_model(item["vendor"], item["model"], vendor, model)]
            if len(matches) == 1:
                default_profile = matches[0]["id"]
                if values.get("name") == self._discovery_initial_name:
                    values["name"] = (f"{matches[0]['vendor'][:1].upper()}{matches[0]['vendor'][1:]} "
                                      + physical_phone_model(default_profile))
        fields = {
            vol.Required("name", default=values.get("name", "")): TextSelector(),
            vol.Required("mac", default=values.get("mac", "")): TextSelector(),
            vol.Required("profile_id", **({"default": default_profile} if default_profile else {})):
                SelectSelector({"options": choices}),
            vol.Optional("advanced"): section(vol.Schema({
                vol.Optional("host", default=values.get("host", "")): TextSelector(),
                vol.Optional("admin_username", default=values.get("admin_username", "")): TextSelector(),
                visible_optional("admin_password", visible={"field": "admin_username", "operator": "not_eq", "value": ""}):
                    TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional("area_id", **({"default": values["area_id"]} if values.get("area_id") else {})): AreaSelector(),
                vol.Optional("person", **({"default": values["person"]} if values.get("person") else {})): EntitySelector({"domain": "person"}),
                vol.Required("follow_ha_language", default=values.get("follow_ha_language", True)): BooleanSelector(),
                vol.Required("enabled", default=values.get("enabled", True)): BooleanSelector(),
            }), {"collapsed": True}),
        }
        if self._current is not None:
            fields[vol.Optional("update_profile", default=False)] = BooleanSelector()
        else:
            accounts = self._phone_accounts()
            self._account_devices = self._selection_devices(accounts)
            selected = values.get("account_id")
            if self._account_devices:
                device = next((key for key, ref in self._account_devices.items() if ref == selected), None)
                fields[vol.Optional("account_device", **({"default": device} if device else {}))] = (
                    self._device_selector(self._account_devices))
            others = [{"value": ref, "label": row["name"]} for ref, row in accounts.items()
                      if ref not in self._account_devices.values()]
            if others:
                fields[visible_optional("account_id", visible={"field": "account_device", "operator": "not_exists"},
                    **({"default": selected} if selected in {row["value"] for row in others} else {}))] = SelectSelector({"options": others, "mode": "dropdown"})
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema(fields))

    async def async_step_language(self, user_input=None):
        if not provisioning_enabled(self._get_entry()):
            return self.async_abort(reason="phone_provisioning_disabled")
        languages = self._profile["capabilities"].get("languages", {})
        if not languages:
            return self.async_abort(reason="phone_language_unsupported")
        errors = {"base": "phone_language_unsupported"} if self._language_override else {}
        if user_input is not None:
            try:
                canonical, _vendor = resolve_phone_language(self._profile, user_input.get("language"))
                self._draft.update(language=canonical, follow_ha_language=False)
            except ValueError:
                errors["base"] = "phone_language_unsupported"
            else:
                return self._finish_identity()
        return self.async_show_form(step_id="language", errors=errors, data_schema=vol.Schema({
            vol.Required("language", default=self._draft.get("language") or next(iter(languages))):
                SelectSelector({"options": list(languages)})}))

    def _check_mac(self, mac):
        own = physical_phone_id(self._current) if self._current is not None else None
        if any(phone["mac"] == mac and phone["phone_id"] != own
               for phone in physical_phone_records(self._get_entry()).values()):
            raise ValueError("phone_mac_exists")

    @staticmethod
    def _error(error):
        code = str(error)
        return code if re.fullmatch(r"(?:phone_|invalid_phone_)[a-z_]+", code) else "invalid_phone_configuration"

    def _canonical_bindings(self, user_input):
        if "line" in user_input or "key" in user_input:
            line, key = user_input.get("line", {}), dict(user_input.get("key", {}))
            if queue := key.pop("queue_device", None):
                key["target_device"] = queue
            for name in ("other_phone", "other_queue", "parking_target"):
                if value := key.pop(name, None):
                    key["target"] = value
            slot = str(self._profile["capabilities"]["key_slots"][0]["slot"])
            user_input = {
                "lines": [{**line, "slot": "1"}] if line.get("account_device") or line.get("endpoint_id") else [],
                "keys": [{**key, "slot": slot, "line": "1"}] if key.get("function") else [],
            }
        raw_lines, raw_keys = user_input.get("lines", []), user_input.get("keys", [])
        if (not isinstance(raw_lines, list) or not isinstance(raw_keys, list)
                or any(not isinstance(row, dict) for row in [*raw_lines, *raw_keys])):
            raise ValueError("invalid_phone_bindings")
        lines, keys = [], []
        for raw in raw_lines:
            line = {"slot": _slot(raw.get("slot")), "transport": str(raw.get("transport") or "udp").lower()}
            if line["transport"] not in PHYSICAL_LINE_TRANSPORTS:
                raise ValueError("invalid_phone_transport")
            device = raw.get("account_device")
            other = raw.get("endpoint_id")
            if device and other:
                raise ValueError("invalid_phone_bindings")
            reference = self._account_devices.get(device) if device else self._other_accounts.get(other)
            if reference is None:
                raise ValueError("invalid_phone_bindings")
            line["endpoint_id"] = reference
            lines.append(line)
        for raw in raw_keys:
            key = {name: raw[name] for name in ("function", "target", "label") if name in raw}
            key["slot"], key["line"] = _slot(raw.get("slot")), _slot(raw.get("line", "1"))
            key["function"] = self._functions.get(key.get("function"), key.get("function"))
            device, other = raw.get("target_device"), key.get("target")
            if device:
                if device not in self._target_devices or other:
                    raise ValueError("invalid_phone_key_target")
                key["target"] = self._target_devices[device]
            elif other and key["function"] in {"blf", "speed_dial", "queue_login", "queue_pause"}:
                key["target"] = self._other_targets.get(other, other)
            keys.append(key)
        return lines, keys

    def _validation_runtime(self):
        return physical_validation_runtime(self.hass, self._get_entry(), self._accounts)

    def _validate_phone(self):
        return validate_physical_inventory(self.hass, self._get_entry(), self._draft, self._profile,
            previous=self._previous(), current_subentry_id=self._current.subentry_id if self._current else None,
            account_drafts=self._accounts)

    def _binding_schema(self):
        caps = self._profile["capabilities"]
        accounts = {**self._phone_accounts(), **self._accounts}
        self._account_devices = self._selection_devices(accounts)
        registry = getattr(getattr(self._get_entry(), "runtime_data", None), "endpoints", None)
        targets = {endpoint.endpoint_id: endpoint.name for endpoint in getattr(registry, "endpoints", ())}
        targets.update({ref: item["name"] for ref, item in accounts.items() if ref not in targets})
        queues = group_records(self._get_entry(), "queue")
        self._target_devices = self._selection_devices(targets, groups=queues)
        targets.update({ref: row["name"] for ref, row in queues.items()})
        targets.update({contact_key_reference(contact): contact.display_name
                        for contact in self._validation_runtime().directory.programmable_contacts()})

        account_names = {ref: item["name"] for ref, item in accounts.items()}
        unavailable = self._option_label("physical_phone_accounts", "unavailable")
        for line in self._draft.get("lines", []):
            ref = line.get("endpoint_id")
            if ref and ref != CREATE_ACCOUNT:
                account_names.setdefault(ref, unavailable)
        for key in self._draft.get("keys", []):
            ref = key.get("target")
            if ref and (key["function"] in {"blf", "queue_login", "queue_pause"} or (
                    key["function"] == "speed_dial" and not re.fullmatch(r"[+*#0-9]+", ref))):
                targets.setdefault(ref, unavailable)
        account_device_refs = set(self._account_devices.values())
        target_device_refs = set(self._target_devices.values())
        self._other_accounts = _readable_refs(
            {ref: name for ref, name in account_names.items() if ref not in account_device_refs},
            reserved={self._option_label("physical_phone_accounts", CREATE_ACCOUNT): CREATE_ACCOUNT})
        self._other_targets = _readable_refs(
            {ref: name for ref, name in targets.items() if ref not in target_device_refs})
        line_slots = [str(slot) for slot in range(1, int(caps["max_lines"]) + 1)]
        functions = sorted(KEY_FUNCTIONS & {function for slot in caps.get("key_slots", [])
                                          for function in slot.get("functions", caps.get("functions", []))})
        self._functions = {self._option_label("physical_phone_key_function", key): key for key in functions}

        def displayed_rows(rows, field, devices, device_field, others):
            display = {ref: (field, name) for name, ref in others.items()}
            display.update({ref: (device_field, device) for device, ref in devices.items()})
            rendered = []
            for row in rows:
                data = dict(row)
                if ref := data.pop(field, None):
                    name, value = display.get(ref, (field, ref))
                    data[name] = value
                rendered.append(data)
            return rendered

        lines = displayed_rows(self._draft.get("lines", []), "endpoint_id", self._account_devices,
                               "account_device", self._other_accounts)
        keys = displayed_rows(self._draft.get("keys", []), "target", self._target_devices,
                              "target_device", self._other_targets)
        account_fields = {}
        if self._account_devices:
            account_fields["account_device"] = {"selector": self._device_selector(self._account_devices)}
        account_fields["endpoint_id"] = {"selector": SelectSelector({"options": list(self._other_accounts), "mode": "dropdown"})}
        target_fields = {}
        if self._target_devices:
            target_fields["target_device"] = {"selector": self._device_selector(self._target_devices)}
        target_fields["target"] = {"selector": SelectSelector({"options": list(self._other_targets), "custom_value": True})}
        if len(line_slots) == 1 and len(caps.get("key_slots", [])) == 1 and len(lines) <= 1 and len(keys) <= 1:
            return self._single_binding_schema(lines, keys, functions, queues, account_fields)
        for key in keys:
            key["function"] = self._option_label("physical_phone_key_function", key["function"])
        return vol.Schema({
            vol.Required("lines", default=lines): ObjectSelector({
                "multiple": True,
                "translation_key": "physical_phone_lines", "fields": {
                    "slot": {"required": True, "selector": SelectSelector({"options": line_slots})},
                    **account_fields,
                    "transport": {"selector": SelectSelector({"options": list(PHYSICAL_LINE_TRANSPORTS)})},
                }}),
            vol.Required("keys", default=keys): ObjectSelector({
                "multiple": True,
                "translation_key": "physical_phone_keys", "fields": {
                    "slot": {"required": True, "selector": SelectSelector({"options": [str(item["slot"]) for item in caps.get("key_slots", [])]})},
                    "function": {"required": True, "selector": SelectSelector({"options": list(self._functions)})},
                    "line": {"required": True, "selector": SelectSelector({"options": line_slots})},
                    **target_fields,
                    "label": {"selector": TextSelector()},
                }}),
        })

    def _single_binding_schema(self, lines, keys, functions, queues, account_fields):
        # A single physical position can be edited inline with HA's normal device field.
        # Suggested values display the existing assignments and allow the user to clear them.
        line_fields = {}
        if "account_device" in account_fields:
            line_fields[vol.Optional("account_device")] = account_fields["account_device"]["selector"]
        line_fields[visible_optional("endpoint_id", visible={"field": "account_device", "operator": "not_exists"})] = (
            account_fields["endpoint_id"]["selector"])
        line_fields[vol.Optional("transport")] = SelectSelector({"options": list(PHYSICAL_LINE_TRANSPORTS)})

        key_fields = {vol.Optional("function"): SelectSelector({"options": functions, "mode": "dropdown",
                                                                "translation_key": "physical_phone_key_function"})}
        grouped_devices = {}
        for name, kinds, is_queue in (
            ("target_device", ("speed_dial", "blf"), False),
            ("queue_device", ("queue_login", "queue_pause"), True),
        ):
            refs = {device: ref for device, ref in self._target_devices.items() if (ref in queues) == is_queue}
            active = [kind for kind in kinds if kind in functions]
            if refs and active:
                grouped_devices[name] = refs
                key_fields[visible_optional(name, visible={"field": "function", "operator": "in", "value": active})] = (
                    self._device_selector(refs))
        queue_refs = set(queues)
        for row in self._draft.get("keys", []):
            if row["function"] in {"queue_login", "queue_pause"}:
                queue_refs.add(row.get("target"))
        other_queues = [name for name, ref in self._other_targets.items() if ref in queue_refs]
        speed_dials = [name for name, ref in self._other_targets.items() if ref not in queue_refs]
        other_phones = [name for name in speed_dials if not self._other_targets[name].startswith("contact:")]
        alternate_fields = {}
        for name, kinds, choices, custom, device_field in (
            ("target", ("speed_dial",), speed_dials, True, "target_device"),
            ("other_phone", ("blf",), other_phones, False, "target_device"),
            ("other_queue", ("queue_login", "queue_pause"), other_queues, False, "queue_device"),
            ("parking_target", ("park", "park_call"), None, True, None),
        ):
            active = [kind for kind in kinds if kind in functions]
            if not active or not (custom or choices):
                continue
            conditions = [{"field": "function", "operator": "in", "value": active}]
            if device_field in grouped_devices:
                conditions.append({"field": device_field, "operator": "not_exists"})
            key_fields[visible_optional(name, visible=conditions)] = (
                TextSelector() if choices is None else SelectSelector(
                    {"options": choices, "custom_value": custom, "mode": "dropdown"}))
            alternate_fields.update(dict.fromkeys(active, name))
        key_fields[visible_optional("label", visible={"field": "function", "operator": "exists"})] = TextSelector()
        schema = vol.Schema({
            vol.Optional("line"): section(vol.Schema(line_fields), {"collapsed": False}),
            vol.Optional("key"): section(vol.Schema(key_fields), {"collapsed": False}),
        })
        line = lines[0] if lines else {}
        key = dict(keys[0]) if keys else {}
        if "target" in key and key["function"] in alternate_fields:
            key[alternate_fields[key["function"]]] = key.pop("target")
        if key.get("target_device") in grouped_devices.get("queue_device", {}):
            key["queue_device"] = key.pop("target_device")
        return self.add_suggested_values_to_schema(schema, {"line": line, "key": key})

    async def async_step_bindings(self, user_input=None):
        if not provisioning_enabled(self._get_entry()):
            return self.async_abort(reason="phone_provisioning_disabled")
        if self._profile is None:
            await self._manifest_for_form()
            self._profile, _templates = await self._catalog.async_profile(self._draft["profile_id"], commit=self._draft["catalog_commit"])
        errors = {}
        if user_input is not None:
            try:
                lines, keys = self._canonical_bindings(user_input)
                self._draft.update(lines=lines, keys=keys)
                if len(lines) > int(self._profile["capabilities"]["max_lines"]):
                    raise ValueError("phone_line_unsupported")
                slots = [line["slot"] for line in lines]
                if (len(slots) != len(set(slots)) or any(int(slot) > int(self._profile["capabilities"]["max_lines"])
                                                       for slot in slots)):
                    raise ValueError("phone_line_unsupported")
                if any(line.get("endpoint_id") == CREATE_ACCOUNT for line in lines):
                    return await self.async_step_account()
                data = self._validate_phone()
                selected = {line["endpoint_id"] for line in data["lines"]}
                drafts = [value for identity, value in self._accounts.items() if identity in selected]
                validate_sip_account_namespace(self._get_entry(), [*self._phone_accounts().values(), *drafts])
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                errors["base"] = self._error(error)
            else:
                for account in drafts:
                    _add_phone_subentry(self.hass, self._get_entry(), data=account, title=account["name"],
                        draft=self._account_records.get(account["endpoint_id"]))
                return self._store_inventory(data)
        try:
            await self._load_selector_labels()
            schema = self._binding_schema()
        except ValueError as error:
            errors["base"] = self._error(error)
            schema = vol.Schema({})
        return self.async_show_form(step_id="bindings", errors=errors, data_schema=schema,
            description_placeholders={"max_lines": str(self._profile["capabilities"]["max_lines"]),
                                      "max_keys": str(len(self._profile["capabilities"].get("key_slots", [])))})

    async def async_step_key_order(self, user_input=None):
        if not provisioning_enabled(self._get_entry()):
            return self.async_abort(reason="phone_provisioning_disabled")
        if self._profile is None:
            await self._manifest_for_form()
            self._profile, _templates = await self._catalog.async_profile(self._draft["profile_id"], commit=self._draft["catalog_commit"])
        keys = self._draft.get("keys", [])
        positions = sorted((key["slot"] for key in keys), key=int)
        errors = {}
        if user_input is not None:
            try:
                proposal = {**self._draft, "keys": reorder_physical_keys(keys, [user_input.get(slot) for slot in positions])}
                data = validate_physical_inventory(self.hass, self._get_entry(), proposal, self._profile,
                    previous=self._previous(), current_subentry_id=self._current.subentry_id)
            except ValueError as error:
                errors["base"] = self._error(error)
            else:
                return self._store_inventory(data)
        await self._load_selector_labels()
        options = [{"value": key["slot"], "label": f"{key.get('label') or self._option_label('physical_phone_key_function', key['function'])} ({key['slot']})"}
                   for key in keys]
        return self.async_show_form(step_id="key_order", errors=errors,
            data_schema=vol.Schema({vol.Required(slot, default=slot): SelectSelector({"options": options}) for slot in positions}))

    async def async_step_account(self, user_input=None):
        if not provisioning_enabled(self._get_entry()):
            return self.async_abort(reason="phone_provisioning_disabled")
        if self._account_flow is None:
            from .config_flow import PhoneSubentryFlowHandler
            self._account_flow = PhoneSubentryFlowHandler()
            self._account_flow.hass = self.hass
            self._account_flow._get_entry = self._get_entry
        errors = {}
        if user_input is not None:
            data, errors = self._account_flow._normalized_common(user_input, kind=EndpointKind.SIP_ACCOUNT)
            if not errors:
                try:
                    endpoint_from_data(self._get_entry(), data)
                    validate_sip_account_namespace(self._get_entry(), [
                        *self._phone_accounts().values(), *self._accounts.values(), data])
                except ValueError:
                    errors["base"] = "phone_alias_conflict"
            if not errors:
                self._accounts[data["endpoint_id"]] = data
                self._account_records[data["endpoint_id"]] = self._account_flow.account_draft
                pending = next(line for line in self._draft["lines"] if line.get("endpoint_id") == CREATE_ACCOUNT)
                pending["endpoint_id"] = data["endpoint_id"]
                self._account_flow = None
                if any(line.get("endpoint_id") == CREATE_ACCOUNT for line in self._draft["lines"]):
                    return await self.async_step_account()
                return await self.async_step_bindings()
        return self.async_show_form(step_id="account", errors=errors,
            data_schema=self._account_flow._common_schema(sip_account=True))


class DiscoveredPhoneEditor(PhysicalPhoneSubentryFlowHandler):
    """Reuse the editor in a discovery card without a second pending flow."""

    def __init__(self, parent, entry, candidate):
        super().__init__()
        self._parent = parent
        self._entry = entry
        self._candidate = candidate
        self.hass = parent.hass
        self.context = {**parent.context, "source": SOURCE_USER}

    def _get_entry(self):
        return self._entry

    def _candidate_error(self, mac=None):
        entry, candidate, error = self._parent._observed_phone_candidate()
        if error:
            return error
        if entry is not self._entry or candidate is not self._candidate:
            return "phone_discovery_expired"
        if not provisioning_enabled(entry):
            return "phone_provisioning_disabled"
        if mac is not None and re.sub(r"[:-]", "", str(mac)).lower() != candidate.mac:
            return "invalid_phone_discovery"
        return None

    async def async_step_user(self, user_input=None):
        mac = user_input.get("mac") if isinstance(user_input, dict) else None
        if error := self._candidate_error(mac):
            return self.async_abort(reason=error)
        result = await super().async_step_user(user_input)
        if result.get("type") == "form" and (error := self._candidate_error()):
            return self.async_abort(reason=error)
        return result

    def _store_inventory(self, data):
        if error := self._candidate_error(data.get("mac")):
            return self.async_abort(reason=error)
        from .physical_phone_enrollment import arm_dhcp_enrollment

        arm_dhcp_enrollment(self.hass, data)
        return super()._store_inventory(data)

    def async_show_form(self, *, step_id, **kwargs):
        return self._parent.async_show_form(step_id="phone_" + step_id, **kwargs)

    def async_abort(self, **kwargs):
        return self._parent.async_abort(**kwargs)

    def async_create_entry(self, *, title, data, unique_id=None, **kwargs):
        self.hass.config_entries.async_add_subentry(self._entry, ConfigSubentry(
            subentry_type=PHYSICAL_PHONE_SUBENTRY_TYPE, title=title, data=data, unique_id=unique_id))
        return self._parent.async_abort(reason="phone_configured")
