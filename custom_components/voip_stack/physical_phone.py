"""Physical SIP inventory and line bindings, independent of logical endpoints."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import re
import secrets
from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry

PHYSICAL_PHONE_SUBENTRY_TYPE = "physical_phone"
PHYSICAL_LINE_TRANSPORTS = ("udp", "tcp")
CONF_PHONE_PROVISIONING = "phone_provisioning"
PHONE_PROVISIONING_CHANGED = "voip_stack.physical_phone_changed"
KEY_FUNCTIONS = frozenset({"line", "speed_dial", "blf", "queue_login", "queue_pause", "park", "park_call", "directory"})
# Exact model label from the X3U Pro manual and its verified native UI.
_MODEL_LABELS = {"fanvil/x3u-pro": "X3U Pro"}


def phone_candidate_name(vendor, model):
    """Format an advertised model for HA without changing the identity on the wire."""
    brand = str(vendor or "").strip()
    brand = {"snom": "Snom", "fanvil": "Fanvil", "yealink": "Yealink",
             "grandstream": "Grandstream"}.get(brand.casefold(), brand)
    model = str(model or "").strip()
    if brand and model.casefold().startswith(brand.casefold()):
        model = model[len(brand):].lstrip(" -_")
    return " ".join(part for part in (brand, model) if part)


def physical_phone_model(profile_id):
    """Display verified model labels without guessing other model names."""
    return _MODEL_LABELS.get(profile_id, profile_id.partition("/")[2])


def discovery_enabled(entry) -> bool:
    """Listen for phone announcements independently of file provisioning."""
    return bool(entry and entry.data.get(CONF_PHONE_PROVISIONING, {}).get("discovery_enabled", True))


def provisioning_enabled(entry) -> bool:
    return bool(entry and entry.data.get(CONF_PHONE_PROVISIONING, {}).get("enabled", False))


def physical_phone_subentries(entry):
    return [item for item in entry.subentries.values() if item.subentry_type == PHYSICAL_PHONE_SUBENTRY_TYPE]


def physical_phone_records(entry):
    return {physical_phone_id(item): physical_phone_data(item) for item in physical_phone_subentries(entry)}


def physical_phone_id(subentry):
    unique = str(subentry.unique_id or "")
    return unique.removeprefix("physical_phone:") if unique.startswith("physical_phone:") else unique or str(subentry.data.get("phone_id") or "")


def physical_phone_data(subentry):
    return {**subentry.data, "phone_id": physical_phone_id(subentry)}


def draft_physical_subentry(data=None, *, previous=None, title=""):
    """Allocate one native ID and retain it throughout an inventory draft."""
    data = dict(data or {})
    record = previous or ConfigSubentry(subentry_type=PHYSICAL_PHONE_SUBENTRY_TYPE,
        title=title, unique_id=None, data=MappingProxyType({}))
    identity = physical_phone_id(record) or record.subentry_id
    data.pop("phone_id", None)
    data.setdefault("provisioning_token", record.data.get("provisioning_token") or secrets.token_urlsafe(32))
    return replace(record, data=MappingProxyType(data), title=title or str(data.get("name") or record.title),
        unique_id=record.unique_id or "physical_phone:" + identity)


def physical_phone_identity_updates(entry):
    """Preflight every native identity before the entry migration writes any."""
    updates, seen = [], set()
    for record in physical_phone_subentries(entry):
        identity = physical_phone_id(record)
        legacy = str(record.data.get("phone_id") or "")
        if not identity or identity in seen or (legacy and identity != legacy):
            from .phone_config import PhoneIdentityConflict
            raise PhoneIdentityConflict(record.title, identity, legacy, subentry_id=record.subentry_id)
        seen.add(identity)
        data = {key: value for key, value in record.data.items() if key != "phone_id"}
        unique = record.unique_id or "physical_phone:" + identity
        if data != record.data or unique != record.unique_id:
            updates.append((record, unique, data))
    return tuple(updates)


def canonical_physical_keys(keys):
    return sorted(deepcopy(keys), key=lambda key: int(key["slot"]))


def reorder_physical_keys(keys, order):
    by_slot = {key["slot"]: key for key in keys}
    positions = sorted(by_slot, key=int)
    if (not isinstance(order, list) or len(order) != len(positions)
            or any(not isinstance(slot, str) for slot in order)
            or set(order) != set(positions) or len(set(order)) != len(order)):
        raise ValueError("invalid_phone_key_order")
    return canonical_physical_keys([{**by_slot[source], "slot": destination}
        for destination, source in zip(positions, order, strict=True)])


def normalize_physical_phone(data, *, previous=None):
    """Validate a configuration draft without adopting endpoint device IDs."""
    old = previous or {}
    mac = re.sub(r"[:-]", "", str(data.get("mac") or old.get("mac") or "")).lower()
    if not re.fullmatch(r"[0-9a-f]{12}", mac):
        raise ValueError("invalid_phone_mac")
    name = str(data.get("name") or "").strip()
    profile = str(data.get("profile_id") or "").strip()
    if not name or len(name) > 100 or not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", profile):
        raise ValueError("invalid_phone_profile")
    lines = deepcopy(data.get("lines") or [])
    keys = deepcopy(data.get("keys") or [])
    if not isinstance(lines, list) or len(lines) > 64 or not isinstance(keys, list) or len(keys) > 256:
        raise ValueError("invalid_phone_bindings")
    slots = set()
    for line in lines:
        slot = str(line.get("slot") or "")
        if not slot.isascii() or not slot.isdigit() or int(slot) < 1 or slot in slots or not line.get("endpoint_id"):
            raise ValueError("invalid_phone_line")
        slots.add(slot)
        line["slot"] = slot
        line["endpoint_id"] = str(line["endpoint_id"])
    seen_keys = set()
    for key in keys:
        key["slot"] = str(key.get("slot") or "")
        key["line"] = str(key.get("line") or "1")
        if (not key["slot"].isascii() or not key["slot"].isdigit() or int(key["slot"]) < 1
                or key["slot"] in seen_keys or key.get("function") not in KEY_FUNCTIONS
                or key["line"] not in slots):
            raise ValueError("invalid_phone_key")
        seen_keys.add(key["slot"])
        key["target"] = str(key.get("target") or "").strip()
        key["label"] = str(key.get("label") or "").strip()
        if len(key["target"]) > 512 or len(key["label"]) > 100:
            raise ValueError("invalid_phone_key")
    person = str(data.get("person") or "")
    if person and not re.fullmatch(r"person\.[a-z0-9_]+", person):
        raise ValueError("invalid_phone_person")
    admin_username = str(data.get("admin_username", old.get("admin_username", "")) or "").strip()
    admin_password = str(data.get("admin_password") or old.get("admin_password") or "") if admin_username else ""
    if (len(admin_username) > 128 or ":" in admin_username or len(admin_password) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in admin_username + admin_password)):
        raise ValueError("invalid_phone_admin_credentials")
    return {
        "schema_version": 1,
        "phone_id": str(old.get("phone_id") or data.get("phone_id") or physical_phone_id(draft_physical_subentry())),
        "mac": mac, "name": name, "profile_id": profile,
        "host": str(data.get("host") or old.get("host") or ""),
        "area_id": data.get("area_id") or None, "person": person,
        "lines": lines, "keys": canonical_physical_keys(keys),
        "follow_ha_language": bool(data.get("follow_ha_language", True)),
        "language": str(data.get("language") or old.get("language") or ""),
        "catalog_commit": str(data.get("catalog_commit") or old.get("catalog_commit") or ""),
        "provisioning_token": str(old.get("provisioning_token") or secrets.token_urlsafe(32)),
        "enabled": bool(data.get("enabled", True)),
        "admin_username": admin_username, "admin_password": admin_password,
    }


def sync_physical_phone_devices(hass, entry):
    from homeassistant.helpers import device_registry as dr
    from .const import DOMAIN

    from homeassistant.helpers import area_registry as ar

    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    for subentry in physical_phone_subentries(entry):
        data = physical_phone_data(subentry)
        vendor = str(data["profile_id"]).split("/", 1)[0]
        mac = ":".join(data["mac"][i:i + 2] for i in range(0, 12, 2))
        devices.async_get_or_create(
            config_entry_id=entry.entry_id, config_subentry_id=subentry.subentry_id,
            identifiers={(DOMAIN, "physical_phone:" + data["phone_id"])},
            connections={(dr.CONNECTION_NETWORK_MAC, mac)}, name=data["name"],
            manufacturer=vendor.title(), model=physical_phone_model(data["profile_id"]),
            suggested_area=area.name if (area := areas.async_get_area(data.get("area_id"))) else None,
        )


def physical_phone_for_device(hass, entry, device_id):
    from homeassistant.helpers import device_registry as dr
    from .const import DOMAIN

    device = dr.async_get(hass).async_get(str(device_id))
    if device is None:
        return None
    records = physical_phone_records(entry)
    return next((records[value.removeprefix("physical_phone:")] for domain, value in device.identifiers
                 if domain == DOMAIN and value.startswith("physical_phone:")
                 and value.removeprefix("physical_phone:") in records), None)


def physical_line_endpoint(runtime, phone, *, line_id="", call_id=""):
    """Choose only an unambiguous existing logical account binding."""
    lines = phone["lines"]
    if line_id:
        lines = [line for line in lines if line["slot"] == str(line_id)]
    endpoints = [runtime.endpoints.get(line["endpoint_id"]) for line in lines]
    endpoints = list({endpoint.endpoint_id: endpoint for endpoint in endpoints if endpoint is not None}.values())
    if call_id:
        endpoints = [endpoint for endpoint in endpoints if endpoint.active_call_id == call_id
                     or (runtime.sip is not None
                         and runtime.sip.is_terminated_for_endpoint(call_id, endpoint.endpoint_id))]
    if len(endpoints) != 1:
        raise ValueError("phone_line_selection_required")
    return endpoints[0]


def account_physical_bindings(entry, endpoint_id):
    """Return the inverse view of canonical physical lines, never another store."""
    return [{"phone_id": phone["phone_id"], "slot": line["slot"],
             "transport": str(line.get("transport") or "udp")}
            for phone in physical_phone_records(entry).values() for line in phone["lines"]
            if line["endpoint_id"] == endpoint_id]


def account_physical_bindings_form_rows(rows):
    return [{"binding": json.dumps([str(row["phone_id"]), str(row["slot"])], separators=(",", ":")),
             "transport": str(row.get("transport") or "udp")} for row in rows]


def account_physical_bindings_from_form(rows):
    if not isinstance(rows, list):
        raise ValueError("invalid_phone_bindings")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid_phone_bindings")
        if "binding" in row:
            if set(row) - {"binding", "transport"}:
                raise ValueError("invalid_phone_bindings")
            try:
                pair = json.loads(row["binding"])
            except (TypeError, ValueError) as error:
                raise ValueError("invalid_phone_bindings") from error
            if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(value, str) for value in pair):
                raise ValueError("invalid_phone_bindings")
            result.append({"phone_id": pair[0], "slot": pair[1], "transport": str(row.get("transport") or "udp")})
        elif set(row) <= {"phone_id", "slot", "transport"} and {"phone_id", "slot"} <= set(row):
            result.append(dict(row))
        else:
            raise ValueError("invalid_phone_bindings")
    return result


async def async_account_physical_binding_options(hass, entry, endpoint_id):
    """Describe only generated, unoccupied positions of each pinned profile."""
    records = physical_phone_records(entry)
    choices = []
    if not provisioning_enabled(entry):
        return [{"value": row["binding"], "label": f"{records[raw['phone_id']]['name']} ({raw['slot']})"}
                for raw, row in zip(account_physical_bindings(entry, endpoint_id),
                    account_physical_bindings_form_rows(account_physical_bindings(entry, endpoint_id)), strict=True)]
    if not records:
        return choices
    from .phone_profile_catalog import PhoneProfileCatalog
    owner = getattr(getattr(entry, "runtime_data", None), "physical_phones", None)
    catalog = getattr(owner, "catalog", None) or PhoneProfileCatalog(hass)
    for phone in records.values():
        profile, _templates = await catalog.async_profile(phone["profile_id"], commit=phone.get("catalog_commit", ""))
        occupied = {line["slot"] for line in phone["lines"] if line["endpoint_id"] != endpoint_id}
        for slot in range(1, int(profile["capabilities"]["max_lines"]) + 1):
            if str(slot) not in occupied:
                row = account_physical_bindings_form_rows([{"phone_id": phone["phone_id"], "slot": str(slot)}])[0]
                choices.append({"value": row["binding"], "label": f"{phone['name']} ({slot})"})
    return choices


def account_physical_bindings_selector(entry, *, options=None):
    from homeassistant.helpers.selector import ObjectSelector, SelectSelector

    class CompatibleBindingsSelector(ObjectSelector):
        def __call__(self, data):
            from voluptuous import Invalid
            try:
                form = account_physical_bindings_form_rows(account_physical_bindings_from_form(data))
            except ValueError as error:
                raise Invalid("invalid_phone_bindings") from error
            return super().__call__(form)

    if options is None:
        options = [{"value": row["binding"], "label": phone["name"] + " (" + line["slot"] + ")"}
            for phone in physical_phone_records(entry).values() for line in phone["lines"]
            for row in account_physical_bindings_form_rows([{"phone_id": phone["phone_id"], "slot": line["slot"]}])]
    return CompatibleBindingsSelector({"multiple": True, "label_field": "binding",
        "translation_key": "account_physical_bindings", "fields": {
            "binding": {"required": True, "selector": SelectSelector({"options": options})},
            "transport": {"selector": SelectSelector({"options": list(PHYSICAL_LINE_TRANSPORTS)})},
        }})


async def async_prepare_account_bindings(hass, entry, endpoint_id, rows, account_data=None):
    """Validate every affected inventory draft before the account editor writes."""
    from .physical_phone_validation import physical_slot, validate_physical_inventory
    from .phone_profile_catalog import PhoneProfileCatalog

    if not isinstance(rows, list) or len(rows) > 512:
        raise ValueError("invalid_phone_bindings")
    if not provisioning_enabled(entry):
        if rows == account_physical_bindings(entry, endpoint_id):
            return ()
        raise ValueError("phone_provisioning_disabled")
    records, selected, seen = physical_phone_records(entry), {}, set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"phone_id", "slot", "transport"}:
            raise ValueError("invalid_phone_bindings")
        identity, slot = str(row.get("phone_id") or ""), physical_slot(row.get("slot"))
        transport = str(row.get("transport") or "udp").lower()
        if identity not in records:
            raise ValueError("phone_not_found")
        if transport not in PHYSICAL_LINE_TRANSPORTS or (identity, slot) in seen:
            raise ValueError("invalid_phone_bindings")
        seen.add((identity, slot))
        selected.setdefault(identity, []).append({"slot": slot, "endpoint_id": endpoint_id, "transport": transport})
    owner = getattr(getattr(entry, "runtime_data", None), "physical_phones", None)
    catalog = getattr(owner, "catalog", None) or PhoneProfileCatalog(hass)
    prepared = []
    for subentry in physical_phone_subentries(entry):
        phone = physical_phone_data(subentry)
        if phone["phone_id"] not in selected and not any(line["endpoint_id"] == endpoint_id for line in phone["lines"]):
            continue
        retained = [dict(line) for line in phone["lines"] if line["endpoint_id"] != endpoint_id]
        additions = selected.get(phone["phone_id"], [])
        if {line["slot"] for line in retained} & {line["slot"] for line in additions}:
            raise ValueError("phone_line_occupied")
        lines = sorted([*retained, *additions], key=lambda line: int(line["slot"]))
        if any(key["line"] not in {line["slot"] for line in lines} for key in phone["keys"]):
            raise ValueError("phone_line_has_keys")
        profile, _templates = await catalog.async_profile(phone["profile_id"], commit=phone.get("catalog_commit", ""))
        proposed = validate_physical_inventory(hass, entry, {**phone, "lines": lines}, profile,
            previous=phone, current_subentry_id=subentry.subentry_id,
            account_drafts={endpoint_id: dict(account_data)} if account_data is not None else {})
        prepared.append((subentry, proposed))
    return tuple(prepared)


def apply_account_bindings(hass, entry, prepared):
    """Publish prevalidated physical drafts without an intervening await."""
    for subentry, data in prepared:
        hass.config_entries.async_update_subentry(entry, subentry, data=data, title=data["name"])
