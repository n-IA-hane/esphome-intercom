"""Native physical-phone editor, binding existing logical SIP account owners."""

from __future__ import annotations

from copy import deepcopy
import re
from types import SimpleNamespace

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigSubentry, ConfigSubentryFlow, SOURCE_USER
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    AreaSelector, BooleanSelector, EntitySelector, ObjectSelector,
    SelectSelector, TextSelector,
)

from .phone_config import (
    _add_phone_subentry, endpoint_from_data, phone_subentries,
    validate_sip_account_namespace,
)
from .phone_endpoint import EndpointKind
from .phone_profile import resolve_phone_keys, resolve_phone_language
from .phone_profile_catalog import PhoneProfileCatalog
from .physical_phone import (
    KEY_FUNCTIONS, PHYSICAL_LINE_TRANSPORTS, PHYSICAL_PHONE_SUBENTRY_TYPE, normalize_physical_phone, physical_phone_records,
    provisioning_enabled,
)

CREATE_ACCOUNT = "__create_account__"
_IDENTITY_FIELDS = frozenset({
    "name", "mac", "profile_id", "host", "area_id", "person",
    "follow_ha_language", "enabled",
})


def _slot(value):
    text = str(value or "")
    if not text.isascii() or not text.isdigit() or int(text) < 1:
        raise ValueError("invalid_phone_bindings")
    return str(int(text))


class PhysicalPhoneSubentryFlowHandler(ConfigSubentryFlow):
    """Keep configuration drafts private until the complete phone is valid."""

    def __init__(self):
        self._current = None
        self._seed = {}
        self._draft = {}
        self._accounts = {}
        self._account_flow = None
        self._catalog = None
        self._manifest = None
        self._manifest_commit = ""
        self._profile = None
        self._language_override = False

    def _previous(self):
        return dict(self._current.data) if self._current is not None else self._seed

    def _phone_accounts(self):
        return {str(item.data["endpoint_id"]): dict(item.data)
                for item in phone_subentries(self._get_entry())
                if item.data.get("kind") == EndpointKind.SIP_ACCOUNT.value}

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
        self._draft = deepcopy(dict(self._current.data))
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        if not provisioning_enabled(self._get_entry()):
            return self.async_abort(reason="phone_provisioning_disabled")
        if (user_input is not None and "discovery_candidate" in user_input
                and self.context.get("source") == SOURCE_USER and not self._draft):
            candidate = user_input["discovery_candidate"]
            if not isinstance(candidate, dict):
                return self.async_abort(reason="invalid_phone_discovery")
            self._seed = {key: candidate[key] for key in
                          ("phone_id", "mac", "host", "provisioning_token") if candidate.get(key)}
            self._draft = {key: candidate[key] for key in ("mac", "host") if candidate.get(key)}
            self._draft["name"] = " ".join(str(candidate.get(key) or "") for key in ("vendor", "model")).strip()
            self._discovery_model = (str(candidate.get("vendor") or "").lower(),
                                     re.sub(r"[^a-z0-9]", "", str(candidate.get("model") or "").lower()))
            user_input = None
        errors = {}
        try:
            manifest = await self._manifest_for_form()
        except (OSError, ValueError, TimeoutError, ClientError):
            return self.async_abort(reason="phone_catalog_unavailable")
        profiles = [item for item in manifest["profiles"] if item.get("status") in {
            "software_validated", "protocol_verified", "hardware_qualified"}]
        choices = [{"value": item["id"], "label": f"{item['vendor']} {item['model']}"} for item in profiles]
        if not choices:
            return self.async_abort(reason="phone_catalog_unavailable")
        if user_input is not None:
            self._draft.update({key: value for key, value in user_input.items() if key in _IDENTITY_FIELDS})
            if "advanced" in user_input:
                advanced = user_input["advanced"]
                if not isinstance(advanced, dict):
                    return self.async_show_form(step_id="user", errors={"base": "invalid_phone_host"})
                self._draft["host"] = advanced.get("host", self._draft.get("host", ""))
            try:
                name = str(self._draft.get("name") or "").strip()
                mac = re.sub(r"[:-]", "", str(self._draft.get("mac") or "")).lower()
                if not re.fullmatch(r"[0-9a-f]{12}", mac):
                    raise ValueError("invalid_phone_mac")
                if not name or len(name) > 100:
                    raise ValueError("invalid_phone_profile")
                self._draft.update(name=name, mac=mac)
                self._check_mac(mac)
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
                                return await self.async_step_bindings()
                        self._language_override = True
                        return await self.async_step_language()
                    self._draft["language"] = canonical
                else:
                    self._draft["language"] = ""
                return await self.async_step_bindings()
            except (ValueError, KeyError, TypeError, OSError, TimeoutError, ClientError) as error:
                errors["base"] = self._error(error)
        values = self._draft
        default_profile = values.get("profile_id", "")
        if not default_profile and hasattr(self, "_discovery_model"):
            vendor, model = self._discovery_model
            default_profile = next((item["id"] for item in profiles
                if str(item["vendor"]).lower() == vendor
                and re.sub(r"[^a-z0-9]", "", str(item["model"]).lower()) == model), "")
        fields = {
            vol.Required("name", default=values.get("name", "")): TextSelector(),
            vol.Required("mac", default=values.get("mac", "")): TextSelector(),
            vol.Required("profile_id", **({"default": default_profile} if default_profile else {})):
                SelectSelector({"options": choices}),
            vol.Optional("advanced"): section(vol.Schema({
                vol.Optional("host", default=values.get("host", "")): TextSelector(),
            }), {"collapsed": True}),
            vol.Optional("area_id", **({"default": values["area_id"]} if values.get("area_id") else {})): AreaSelector(),
            vol.Optional("person", **({"default": values["person"]} if values.get("person") else {})): EntitySelector({"domain": "person"}),
            vol.Required("follow_ha_language", default=values.get("follow_ha_language", True)): BooleanSelector(),
            vol.Required("enabled", default=values.get("enabled", True)): BooleanSelector(),
        }
        if self._current is not None:
            fields[vol.Optional("update_profile", default=False)] = BooleanSelector()
        selected_profile = next((item for item in profiles if item["id"] == values.get("profile_id")), {})
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema(fields),
            description_placeholders={"profile_pin": self._manifest_commit,
                "profile_status": str(selected_profile.get("status", "")),
                "profile_source": str(manifest.get("upstream", {}).get("repository", "fusionpbx/fusionpbx"))})

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
                return await self.async_step_bindings()
        return self.async_show_form(step_id="language", errors=errors, data_schema=vol.Schema({
            vol.Required("language", default=self._draft.get("language") or next(iter(languages))):
                SelectSelector({"options": list(languages)})}))

    def _check_mac(self, mac):
        own = self._current.data.get("phone_id") if self._current is not None else None
        if any(phone["mac"] == mac and phone["phone_id"] != own
               for phone in physical_phone_records(self._get_entry()).values()):
            raise ValueError("phone_mac_exists")

    @staticmethod
    def _error(error):
        code = str(error)
        return code if re.fullmatch(r"(?:phone_|invalid_phone_)[a-z_]+", code) else "invalid_phone_configuration"

    def _canonical_bindings(self, user_input):
        raw_lines, raw_keys = user_input.get("lines", []), user_input.get("keys", [])
        if (not isinstance(raw_lines, list) or not isinstance(raw_keys, list)
                or any(not isinstance(row, dict) for row in [*raw_lines, *raw_keys])):
            raise ValueError("invalid_phone_bindings")
        lines, keys = [], []
        for raw in raw_lines:
            line = {"slot": _slot(raw.get("slot")), "endpoint_id": str(raw.get("endpoint_id") or ""),
                    "transport": str(raw.get("transport") or "udp").lower()}
            if line["transport"] not in PHYSICAL_LINE_TRANSPORTS:
                raise ValueError("invalid_phone_transport")
            lines.append(line)
        for raw in raw_keys:
            key = {name: raw[name] for name in ("function", "target", "label") if name in raw}
            key["slot"], key["line"] = _slot(raw.get("slot")), _slot(raw.get("line", "1"))
            keys.append(key)
        return lines, keys

    def _validation_runtime(self):
        from .endpoint_registry import EndpointRegistry
        from .group_config import group_members, group_records
        from .contact_config import contact_dicts
        from .phone_directory import PhoneDirectory
        from .roster import parse_roster_json

        runtime = getattr(self._get_entry(), "runtime_data", None)
        configured = EndpointRegistry()
        for item in phone_subentries(self._get_entry()):
            configured.register(endpoint_from_data(self._get_entry(), item.data))
        for data in self._accounts.values():
            configured.register(endpoint_from_data(self._get_entry(), data))
        actual = getattr(runtime, "endpoints", None)
        endpoints = SimpleNamespace(
            resolve=lambda ref: configured.resolve(ref) or (actual.resolve(ref) if actual is not None else None))
        queues = getattr(runtime, "queues", None)
        if queues is None:
            rows = {identity: SimpleNamespace(**record, queue_id=identity,
                    members=frozenset(group_members(self.hass, self._get_entry(), record)))
                    for identity, record in group_records(self._get_entry(), "queue").items()}
            queues = SimpleNamespace(resolve=lambda ref: rows.get(str(ref)))
        directory = PhoneDirectory(parse_roster_json(contact_dicts(self._get_entry())))
        return SimpleNamespace(endpoints=endpoints, queues=queues, directory=directory)

    def _validate_phone(self):
        data = {**self._previous(), **normalize_physical_phone(self._draft, previous=self._previous())}
        self._check_mac(data["mac"])
        caps = self._profile["capabilities"]
        accounts = {**self._phone_accounts(), **self._accounts}
        for line in data["lines"]:
            if int(line["slot"]) > int(caps["max_lines"]):
                raise ValueError("phone_line_unsupported")
            if line["endpoint_id"] not in accounts:
                raise ValueError("phone_sip_account_required")
        runtime = self._validation_runtime()
        for key in data["keys"]:
            function, target = key["function"], key.get("target", "")
            if function in {"queue_login", "queue_pause"}:
                queue = runtime.queues.resolve(target)
                if queue is not None:
                    key["target"] = queue.queue_id
            elif function in {"speed_dial", "blf"}:
                if str(target).casefold().startswith("contact:"):
                    from .phone_directory import contact_key_reference
                    contact = runtime.directory.key_contact(target)
                    if function != "speed_dial" or contact is None:
                        raise ValueError("invalid_phone_key_target")
                    key["target"] = contact_key_reference(contact)
                else:
                    endpoint = runtime.endpoints.resolve(target)
                    if endpoint is not None:
                        key["target"] = endpoint.endpoint_id
        resolve_phone_keys(runtime, data, self._profile)
        from .physical_phone_provisioning import validate_slot_removal
        owner = getattr(getattr(self._get_entry(), "runtime_data", None), "physical_phones", None)
        validator = getattr(owner, "validate_inventory_change", None)
        if validator is not None:
            validator(data, self._profile)
        else:
            validate_slot_removal(self._previous(), data, self._profile)
        return data

    def _binding_schema(self):
        caps = self._profile["capabilities"]
        accounts = {**self._phone_accounts(), **self._accounts}
        account_choices = [{"value": identity, "label": item["name"]} for identity, item in accounts.items()]
        account_choices.append({"value": CREATE_ACCOUNT, "label": "Create SIP account"})
        line_slots = [str(slot) for slot in range(1, int(caps["max_lines"]) + 1)]
        functions = sorted(KEY_FUNCTIONS & {function for slot in caps.get("key_slots", [])
                                          for function in slot.get("functions", caps.get("functions", []))})
        registry = getattr(getattr(self._get_entry(), "runtime_data", None), "endpoints", None)
        targets = [{"value": endpoint.endpoint_id, "label": endpoint.name}
                   for endpoint in getattr(registry, "endpoints", ())]
        targets.extend({"value": identity, "label": item["name"]} for identity, item in accounts.items()
                       if identity not in {row["value"] for row in targets})
        from .group_config import group_records
        targets.extend({"value": identity, "label": row["name"]}
                       for identity, row in group_records(self._get_entry(), "queue").items())
        from .phone_directory import contact_key_reference
        targets.extend({"value": contact_key_reference(contact), "label": contact.display_name}
                       for contact in self._validation_runtime().directory.programmable_contacts())
        return vol.Schema({
            vol.Required("lines", default=self._draft.get("lines", [])): ObjectSelector({
                "multiple": True, "label_field": "slot", "description_field": "endpoint_id",
                "translation_key": "physical_phone_lines", "fields": {
                    "slot": {"required": True, "selector": SelectSelector({"options": line_slots})},
                    "endpoint_id": {"required": True, "selector": SelectSelector({"options": account_choices,
                        "translation_key": "physical_phone_accounts"})},
                    "transport": {"selector": SelectSelector({"options": list(PHYSICAL_LINE_TRANSPORTS)})},
                }}),
            vol.Required("keys", default=self._draft.get("keys", [])): ObjectSelector({
                "multiple": True, "label_field": "label", "description_field": "function",
                "translation_key": "physical_phone_keys", "fields": {
                    "slot": {"required": True, "selector": SelectSelector({"options": [str(item["slot"]) for item in caps.get("key_slots", [])]})},
                    "function": {"required": True, "selector": SelectSelector({"options": functions,
                        "translation_key": "physical_phone_key_function"})},
                    "line": {"required": True, "selector": SelectSelector({"options": line_slots})},
                    "target": {"selector": SelectSelector({"options": targets, "custom_value": True})},
                    "label": {"selector": TextSelector()},
                }}),
        })

    async def async_step_bindings(self, user_input=None):
        if not provisioning_enabled(self._get_entry()):
            return self.async_abort(reason="phone_provisioning_disabled")
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
                    _add_phone_subentry(self.hass, self._get_entry(), data=account, title=account["name"])
                if self._current is not None:
                    return self.async_update_and_abort(self._get_entry(), self._current, data=data, title=data["name"])
                return self.async_create_entry(title=data["name"], data=data,
                    unique_id="physical_phone:" + data["phone_id"])
        return self.async_show_form(step_id="bindings", errors=errors, data_schema=self._binding_schema(),
            description_placeholders={"max_lines": str(self._profile["capabilities"]["max_lines"]),
                                      "max_keys": str(len(self._profile["capabilities"].get("key_slots", [])))})

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

    def __init__(self, parent, entry):
        super().__init__()
        self._parent = parent
        self._entry = entry
        self.hass = parent.hass
        self.context = {**parent.context, "source": SOURCE_USER}

    def _get_entry(self):
        return self._entry

    def async_show_form(self, *, step_id, **kwargs):
        return self._parent.async_show_form(step_id="phone_" + step_id, **kwargs)

    def async_abort(self, **kwargs):
        return self._parent.async_abort(**kwargs)

    def async_create_entry(self, *, title, data, unique_id=None, **kwargs):
        self.hass.config_entries.async_add_subentry(self._entry, ConfigSubentry(
            subentry_type=PHYSICAL_PHONE_SUBENTRY_TYPE, title=title, data=data, unique_id=unique_id))
        return self._parent.async_abort(reason="phone_configured")
