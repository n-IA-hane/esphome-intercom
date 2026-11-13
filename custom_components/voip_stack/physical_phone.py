"""Physical SIP inventory and line bindings, independent of logical endpoints."""

from __future__ import annotations

from copy import deepcopy
import re
import secrets
from uuid import uuid4

PHYSICAL_PHONE_SUBENTRY_TYPE = "physical_phone"
PHYSICAL_LINE_TRANSPORTS = ("udp", "tcp")
CONF_PHONE_PROVISIONING = "phone_provisioning"
PHONE_PROVISIONING_CHANGED = "voip_stack.physical_phone_changed"
KEY_FUNCTIONS = frozenset({"line", "speed_dial", "blf", "queue_login", "queue_pause", "park", "park_call", "directory"})


def provisioning_enabled(entry) -> bool:
    return bool(entry and entry.data.get(CONF_PHONE_PROVISIONING, {}).get("enabled", False))


def physical_phone_subentries(entry):
    return [item for item in entry.subentries.values() if item.subentry_type == PHYSICAL_PHONE_SUBENTRY_TYPE]


def physical_phone_records(entry):
    return {str(item.data["phone_id"]): dict(item.data) for item in physical_phone_subentries(entry)}


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
        if (not key["slot"] or key["slot"] in seen_keys or key.get("function") not in KEY_FUNCTIONS
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
    return {
        "schema_version": 1,
        "phone_id": str(old.get("phone_id") or data.get("phone_id") or uuid4().hex),
        "mac": mac, "name": name, "profile_id": profile,
        "host": str(data.get("host") or old.get("host") or ""),
        "area_id": data.get("area_id") or None, "person": person,
        "lines": lines, "keys": keys,
        "follow_ha_language": bool(data.get("follow_ha_language", True)),
        "language": str(data.get("language") or old.get("language") or ""),
        "catalog_commit": str(data.get("catalog_commit") or old.get("catalog_commit") or ""),
        "provisioning_token": str(old.get("provisioning_token") or secrets.token_urlsafe(32)),
        "enabled": bool(data.get("enabled", True)),
    }


def sync_physical_phone_devices(hass, entry):
    from homeassistant.helpers import device_registry as dr
    from .const import DOMAIN

    from homeassistant.helpers import area_registry as ar

    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    for subentry in physical_phone_subentries(entry):
        data = subentry.data
        vendor, model = str(data["profile_id"]).split("/", 1)
        mac = ":".join(data["mac"][i:i + 2] for i in range(0, 12, 2))
        devices.async_get_or_create(
            config_entry_id=entry.entry_id, config_subentry_id=subentry.subentry_id,
            identifiers={(DOMAIN, "physical_phone:" + data["phone_id"])},
            connections={(dr.CONNECTION_NETWORK_MAC, mac)}, name=data["name"],
            manufacturer=vendor.title(), model=model,
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


def account_physical_bindings_selector(entry):
    from homeassistant.helpers.selector import ObjectSelector, SelectSelector

    return ObjectSelector({"multiple": True, "label_field": "phone_id",
        "translation_key": "account_physical_bindings", "fields": {
            "phone_id": {"required": True, "selector": SelectSelector({"options": [
                {"value": identity, "label": phone["name"]}
                for identity, phone in physical_phone_records(entry).items()]})},
            "slot": {"required": True, "selector": SelectSelector({"options": [str(i) for i in range(1, 65)]})},
            "transport": {"selector": SelectSelector({"options": list(PHYSICAL_LINE_TRANSPORTS)})},
        }})


async def async_prepare_account_bindings(hass, entry, endpoint_id, rows, account_data=None):
    """Validate every affected inventory draft before the account editor writes."""
    from .physical_phone_flow import PhysicalPhoneSubentryFlowHandler, _slot
    from .phone_profile_catalog import PhoneProfileCatalog

    if not isinstance(rows, list) or len(rows) > 512:
        raise ValueError("invalid_phone_bindings")
    records, selected, seen = physical_phone_records(entry), {}, set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"phone_id", "slot", "transport"}:
            raise ValueError("invalid_phone_bindings")
        identity, slot = str(row.get("phone_id") or ""), _slot(row.get("slot"))
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
        phone = dict(subentry.data)
        if phone["phone_id"] not in selected and not any(line["endpoint_id"] == endpoint_id for line in phone["lines"]):
            continue
        retained = [dict(line) for line in phone["lines"] if line["endpoint_id"] != endpoint_id]
        additions = selected.get(phone["phone_id"], [])
        if {line["slot"] for line in retained} & {line["slot"] for line in additions}:
            raise ValueError("phone_line_occupied")
        lines = sorted([*retained, *additions], key=lambda line: int(line["slot"]))
        if any(key["line"] not in {line["slot"] for line in lines} for key in phone["keys"]):
            raise ValueError("phone_line_has_keys")
        editor = PhysicalPhoneSubentryFlowHandler()
        editor.hass, editor._current = hass, subentry
        editor._get_entry = lambda: entry
        editor._draft = {**phone, "lines": lines}
        if account_data is not None:
            editor._accounts[endpoint_id] = dict(account_data)
        editor._profile, _templates = await catalog.async_profile(phone["profile_id"], commit=phone.get("catalog_commit", ""))
        prepared.append((subentry, editor._validate_phone()))
    return tuple(prepared)


def apply_account_bindings(hass, entry, prepared):
    """Publish prevalidated physical drafts without an intervening await."""
    for subentry, data in prepared:
        hass.config_entries.async_update_subentry(entry, subentry, data=data, title=data["name"])
