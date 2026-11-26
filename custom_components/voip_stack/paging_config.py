"""Persistent paging groups projected into the existing phone directory."""

from __future__ import annotations

from uuid import uuid4

from .roster import RosterEntry
from .ringing_policy import validate_ringing_timeout

MAX_PAGING_TARGETS = 16


def normalize_paging_group(data, *, identity=""):
    name = str(data.get("name") or "").strip()
    extension = str(data.get("extension") or "").strip()
    members = tuple(dict.fromkeys(str(value).strip() for value in data.get("members", ()) if str(value).strip()))
    if not name or len(name) > 128 or (extension and (not extension.isdecimal() or len(extension) > 32)):
        raise ValueError("A paging group needs a name and an optional numeric extension")
    if len(members) > MAX_PAGING_TARGETS:
        raise ValueError("Select at most sixteen paging phones")
    answer_timeout = validate_ringing_timeout(data.get("answer_timeout"), inherit=True)
    timeout = float(data.get("timeout", 120))
    if not 1 <= timeout <= 600:
        raise ValueError("Paging timeouts exceed the supported limits")
    return {"paging_group_id": str(identity or data.get("paging_group_id") or uuid4().hex),
            "name": name, "extension": extension, "members": list(members),
            "answer_timeout": answer_timeout, "timeout": timeout,
            "enabled": bool(data.get("enabled", True))}


def paging_group_configs(entry):
    return {str(item.data.get("paging_group_id") or item.subentry_id): dict(item.data)
            for item in entry.subentries.values() if item.subentry_type == "paging"}


def paging_group_entries(entry):
    return [RosterEntry(id=f"paging:{identity}", name=data["name"],
                        extension=str(data.get("extension") or ""), ha_bridge=True,
                        enabled=bool(data.get("enabled", True) and data.get("members")),
                        metadata={"virtual_endpoint": "paging", "paging_group_id": identity,
                                  "endpoint_id": f"service:paging:{identity}", "endpoint_kind": "service"})
            for identity, data in paging_group_configs(entry).items()]


def paging_group_memberships(entry):
    """Derive phone memberships from the saved paging groups, never a shadow field."""
    members = {}
    for identity, data in paging_group_configs(entry).items():
        for phone in data.get("members", ()):
            members.setdefault(phone, []).append(identity)
    return members


def update_phone_paging_groups(hass, entry, endpoint_id, selected):
    """Validate the complete membership change before updating canonical groups."""
    if isinstance(selected, str):
        selected = [value.strip() for value in selected.split(",") if value.strip()]
    if not isinstance(selected, (list, tuple)) or any(not isinstance(value, str) for value in selected):
        raise ValueError("Select existing paging groups")
    selected = {value.strip() for value in selected if value.strip()}
    groups = paging_group_configs(entry)
    if selected - groups.keys():
        raise ValueError("Select existing paging groups")
    updates = []
    for record in entry.subentries.values():
        if record.subentry_type != "paging":
            continue
        identity = str(record.data.get("paging_group_id") or record.subentry_id)
        previous = list(record.data.get("members", ()))
        members = [member for member in previous if member != endpoint_id]
        if identity in selected:
            members = previous if endpoint_id in previous else [*previous, endpoint_id]
        if members != previous:
            proposal = {**record.data, "members": members}
            validated = normalize_paging_group(proposal, identity=identity)
            updates.append((record, {**record.data, "members": validated["members"]}))
    # The service holds the existing group reconciliation lock across this batch.
    for record, data in updates:
        hass.config_entries.async_update_subentry(entry, record, data=data)
    return bool(updates)


def sync_paging_devices(hass, entry):
    """Keep each paging service on its existing native config subentry."""
    from homeassistant.helpers import device_registry as dr
    from .const import DOMAIN

    devices = dr.async_get(hass)
    for row in entry.subentries.values():
        if row.subentry_type != "paging":
            continue
        identity = str(row.data.get("paging_group_id") or row.subentry_id)
        devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=row.subentry_id,
            identifiers={(DOMAIN, f"paging:{identity}")}, name=row.title,
            manufacturer="VoIP Stack", model="Paging group", model_id="paging_group",
            entry_type=dr.DeviceEntryType.SERVICE)
