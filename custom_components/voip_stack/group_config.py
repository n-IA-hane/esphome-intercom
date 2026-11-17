"""Stable service devices for groups whose membership belongs to phones."""

from uuid import uuid4
import logging

from .roster import RosterEntry, normalize_roster_key

GROUP_SUBENTRY_TYPE = "group"
GROUP_KINDS = frozenset({"queue", "ring"})
_LOGGER = logging.getLogger(__name__)


def group_subentries(entry):
    return [item for item in entry.subentries.values() if item.subentry_type == GROUP_SUBENTRY_TYPE]


def group_records(entry, kind=None):
    return {str(item.data.get("group_id") or item.subentry_id): dict(item.data)
            for item in group_subentries(entry) if kind is None or item.data.get("kind") == kind}


def normalize_group_data(data, *, identity=""):
    name = str(data.get("name") or "").strip()
    extension = str(data.get("extension") or "").strip()
    kind = data.get("kind")
    limit = 255 if kind == "ring" else 32
    if kind not in GROUP_KINDS or not name or len(name.encode()) > limit or not normalize_roster_key(name):
        raise ValueError(f"Choose a queue or ring group with a name of at most {limit} bytes")
    if any(ord(char) < 32 or ord(char) == 127 or char in ",;|" for char in name):
        raise ValueError("Group names cannot contain control characters or delimiters")
    if extension and (not extension.isascii() or not extension.isdigit() or len(extension) > 32):
        raise ValueError("The extension must contain at most 32 digits")
    policy = dict(data.get("policy") or {})
    if kind == "queue":
        from .call_queues import QueuePolicy
        from dataclasses import asdict
        policy = asdict(QueuePolicy.from_data(policy))
    elif policy:
        raise ValueError("Queue settings cannot be applied to a ring group")
    return {"group_id": identity or str(data.get("group_id") or uuid4().hex),
            "kind": kind, "name": name, "extension": extension,
            "origin": "auto" if data.get("origin") == "auto" else "manual",
            "enabled": bool(data.get("enabled", True)), "policy": policy}


def iter_memberships(hass, entry, kind):
    from .phone_queue_membership import iter_phone_memberships
    yield from iter_phone_memberships(hass, entry, kind)


def group_members(hass, entry, record):
    key = normalize_roster_key(record["name"])
    return tuple(endpoint_id for endpoint_id, names in iter_memberships(hass, entry, record["kind"])
                 if any(normalize_roster_key(name) == key for name in names))


def membership_signature(hass, entry):
    return tuple((kind, endpoint_id, tuple(names)) for kind in ("queue", "ring")
                 for endpoint_id, names in iter_memberships(hass, entry, kind))


def validate_group_namespace(hass, entry, data, *, exclude=""):
    from .config_validation import entry_route_mappings, mapping_route_aliases, normalized_aliases
    wanted = normalized_aliases((data["name"], data.get("extension")))
    from .mailbox_address import parse_mailbox_address
    from .parking_config import normalize_parking_config, parse_parking_address
    parking = normalize_parking_config(entry.data.get("parking", {}))
    for alias in (data["name"], data.get("extension", "")):
        if alias and (parse_mailbox_address(alias) is not None or parse_parking_address(parking, alias) is not None
                      or alias.startswith(("queue-login+", "queue-pause+"))):
            raise ValueError("The name or extension is reserved for a telephone service")
    for identity, other in group_records(entry).items():
        if identity != exclude and wanted & normalized_aliases((other["name"], other.get("extension"))):
            raise ValueError("The name or extension already belongs to another group")
    mappings = entry_route_mappings(entry, entry.data)
    for item in mappings:
        if item.get("group_id") == exclude or item.get("group_id"):
            continue
        if wanted & mapping_route_aliases(item):
            raise ValueError("The name or extension already belongs to another destination")
        conference = str(item.get("conference_group") or (item.get("metadata") or {}).get("conference_group") or "")
        if wanted & normalized_aliases(conference.split(",")):
            raise ValueError("The group conflicts with an existing conference")


def reconcile_group_records(hass, entry):
    """Materialize declarations without using online registration as membership."""
    from homeassistant.config_entries import ConfigSubentry
    records = group_records(entry)
    declared = {}
    for kind in ("queue", "ring"):
        for _endpoint_id, names in iter_memberships(hass, entry, kind):
            for name in names:
                declared.setdefault((kind, normalize_roster_key(name)), name)
    occupied = {(record["kind"], normalize_roster_key(record["name"])) for record in records.values()}
    for (kind, key), name in declared.items():
        if not key or (kind, key) in occupied:
            continue
        try:
            data = normalize_group_data({"kind": kind, "name": name, "origin": "auto",
                                         "extension": name if name.isascii() and name.isdigit() else ""})
            validate_group_namespace(hass, entry, data)
        except ValueError as error:
            _LOGGER.warning("Cannot create automatic %s group %s: %s", kind, name, error)
            continue
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type=GROUP_SUBENTRY_TYPE, title=name, data=data, unique_id=f"group:{data['group_id']}"))
        occupied.add((kind, key))
    for item in tuple(group_subentries(entry)):
        data = item.data
        if (data.get("origin") == "auto" and "member_update" not in data
                and (data["kind"], normalize_roster_key(data["name"])) not in declared):
            hass.config_entries.async_remove_subentry(entry, item.subentry_id)
    sync_group_devices(hass, entry)


def sync_group_devices(hass, entry):
    from homeassistant.helpers import device_registry as dr
    from .const import DOMAIN
    devices = dr.async_get(hass)
    for item in group_subentries(entry):
        devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=item.subentry_id,
            identifiers={(DOMAIN, f"group:{item.data['group_id']}")}, name=item.title,
            manufacturer="VoIP Stack", model="Call queue" if item.data["kind"] == "queue" else "Ring group",
            entry_type=dr.DeviceEntryType.SERVICE)


def group_roster_entries(hass, entry):
    runtime = entry.runtime_data
    for identity, data in group_records(entry).items():
        if data["kind"] != "ring":
            continue
        members = []
        for endpoint_id in group_members(hass, entry, data):
            endpoint = runtime.endpoints.get(endpoint_id)
            if endpoint is not None:
                members.append(endpoint.username or endpoint.name)
        yield RosterEntry(id=data["name"], name=data["name"], extension=data.get("extension", ""),
            ha_bridge=True, enabled=bool(data.get("enabled", True)),
            metadata={"endpoint_id": f"group:ring:{identity}", "endpoint_kind": "group", "group_type": "ring",
                      "group_id": identity, "members": members, "ring_members": [], "auto": data.get("origin") == "auto"})


async def async_apply_group_updates(hass, entry, previous=None):
    """Consume an editor's membership command, never keep a parallel member list."""
    from .phone_queue_membership import async_update_group_members
    current = group_records(entry)
    for identity, old in (previous or {}).items():
        if identity not in current:
            await async_update_group_members(hass, entry, old["kind"], old["name"], ())
    for item in tuple(group_subentries(entry)):
        if "member_update" not in item.data:
            continue
        data = dict(item.data)
        await async_update_group_members(hass, entry, data["kind"], data["name"],
                                        data.pop("member_update"), old_name=data.pop("previous_name", ""))
        hass.config_entries.async_update_subentry(entry, item, data=data)
    reconcile_group_records(hass, entry)
