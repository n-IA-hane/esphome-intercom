"""Native group devices and their ordered canonical membership."""

from uuid import uuid4
import logging

from .roster import RosterEntry, normalize_roster_key

GROUP_SUBENTRY_TYPE = "group"
GROUP_KINDS = frozenset({"queue", "ring"})
INITIAL_RING_STEP_TIMEOUT = 15.0
_LOGGER = logging.getLogger(__name__)


def materialize_ring_step(policy, *, legacy=False):
    """Persist the initial step at creation or during the explicit legacy upgrade.

    Old policies treated a missing or false-valued step as the initial value,
    capped by an explicitly shorter group limit. Only migration accepts that
    legacy spelling; new configuration and runtime reject an explicit zero.
    """
    values = dict(policy or {})
    if "step_timeout" not in values or (legacy and not values["step_timeout"]):
        overall = values.get("overall_timeout")
        if overall is None:
            overall = values.get("ring_timeout")
        values["step_timeout"] = (min(INITIAL_RING_STEP_TIMEOUT, overall)
                                  if isinstance(overall, (int, float)) and not isinstance(overall, bool)
                                  and overall > 0 else INITIAL_RING_STEP_TIMEOUT)
    return values


def group_subentries(entry):
    return [item for item in entry.subentries.values() if item.subentry_type == GROUP_SUBENTRY_TYPE]


def group_records(entry, kind=None):
    records = {}
    for item in group_subentries(entry):
        if kind is not None and item.data.get("kind") != kind:
            continue
        data = dict(item.data)
        if data.get("kind") == "queue" and "members" in data:
            data["policy"] = {**data.get("policy", {}), 'enabled': bool(data.get('enabled', True)),
                              "agent_order": list(data["members"])}
        records[str(data.get("group_id") or item.subentry_id)] = data
    return records


def normalize_group_data(data, *, identity="", legacy_policy=False):
    """Validate a configuration write, materializing only absent duration choices."""
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
    raw_members = data.get("members", ())
    if not isinstance(raw_members, (list, tuple)) or any(not isinstance(value, str) for value in raw_members):
        raise ValueError("Group members must be an ordered list of destinations")
    members = list(dict.fromkeys(value.strip() for value in raw_members if value.strip()))
    if len(members) > 128 or any(len(value.encode()) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in value) for value in members):
        raise ValueError("Group destinations exceed the supported limits")
    policy = dict(data.get("policy") or {})
    enabled = bool(data.get('enabled', policy.get('enabled', True)))
    if kind == "queue":
        from .queue_policy import QueuePolicy
        from .queue_config import initial_queue_policy
        from dataclasses import asdict
        policy = initial_queue_policy(policy)
        policy = asdict(QueuePolicy.from_data({**policy, 'enabled': enabled, "agent_order": members}))
        policy.pop("agent_order", None)
        policy.pop('enabled', None)
    else:
        from .dial_plan import RingPolicy
        from dataclasses import asdict
        policy = materialize_ring_step(policy, legacy=legacy_policy)
        inherited = policy.get("overall_timeout") is None and policy.get("ring_timeout") is None
        configured = policy.get("overall_timeout")
        if configured is None:
            configured = policy.get("ring_timeout")
        policy = asdict(RingPolicy.from_metadata(policy))
        # Persist configuration, not the fork's infinity representation or a
        # snapshot of the global value. Existing explicit values stay explicit.
        policy["overall_timeout"] = None if inherited else configured
    fallback = str(data.get("fallback") or "").strip()
    if len(fallback.encode()) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in fallback):
        raise ValueError("Group fallback exceeds the supported limits")
    return {"group_id": identity or str(data.get("group_id") or uuid4().hex),
            "kind": kind, "name": name, "extension": extension,
            "origin": "auto" if data.get("origin") == "auto" else "manual",
            "enabled": enabled, "policy": policy, "members": members,
            **({"fallback": fallback} if kind == "ring" else {})}


def iter_memberships(hass, entry, kind):
    from .phone_queue_membership import iter_phone_memberships
    yield from iter_phone_memberships(hass, entry, kind)


def group_members(hass, entry, record):
    """Read canonical membership, or the one-time upgrade input."""
    if "members" in record:
        return tuple(record["members"])
    from .phone_queue_membership import _legacy_iter_phone_memberships
    key = normalize_roster_key(record["name"])
    return tuple(endpoint_id for endpoint_id, names in _legacy_iter_phone_memberships(hass, entry, record["kind"])
                 if any(normalize_roster_key(name) == key for name in names))


def membership_signature(hass, entry):
    return tuple((kind, endpoint_id, tuple(names)) for kind in ("queue", "ring")
                 for endpoint_id, names in iter_memberships(hass, entry, kind))


def contact_group_destination(data):
    """Use a contact's existing public dial string, never its internal record ID."""
    return str(data.get('number') or data.get('sip_uri') or data.get('name') or '').strip()


def prepare_contact_group_commands(hass, entry, commands, *, contacts):
    """Preflight administrator import commands against the same native group owner."""
    from homeassistant.config_entries import ConfigSubentry
    from types import SimpleNamespace
    from .phone_queue_membership import membership_names
    from .const import CONF_PHONEBOOK_CONTACTS

    records = group_records(entry, 'ring')
    future = {identity: {**data, 'members': list(group_members(hass, entry, data))} for identity, data in records.items()}
    prospective = SimpleNamespace(data={**entry.data, CONF_PHONEBOOK_CONTACTS: contacts},
        options=entry.options, subentries=entry.subentries, runtime_data=getattr(entry, 'runtime_data', None))
    for target, names in commands:
        if not target:
            raise ValueError('A group contact needs a public number, SIP address or name')
        wanted = {normalize_roster_key(name): name for name in membership_names('ring_group', names)}
        occupied = {normalize_roster_key(data['name']) for data in future.values()}
        for identity, data in tuple(future.items()):
            members = list(data.get('members', ()))
            if normalize_roster_key(data['name']) in wanted:
                if target not in members:
                    members.append(target)
            else:
                members = [member for member in members if member != target]
            future[identity] = normalize_group_data({**data, 'members': members}, identity=identity)
        for key, name in wanted.items():
            if key in occupied:
                continue
            data = normalize_group_data({'kind': 'ring', 'name': name, 'origin': 'auto', 'members': [target]})
            validate_group_namespace(hass, prospective, data)
            future[data['group_id']] = data
    rows = {row.data['group_id']: row for row in group_subentries(entry) if row.data.get('kind') == 'ring'}
    updates, creations = [], []
    for identity, data in future.items():
        if identity in rows:
            if data != dict(rows[identity].data):
                updates.append((rows[identity], data))
        else:
            creations.append(ConfigSubentry(subentry_type='group', title=data['name'],
                unique_id='group:' + identity, data=data))

    def commit():
        for row, data in updates:
            hass.config_entries.async_update_subentry(entry, row, data=data)
        for row in creations:
            hass.config_entries.async_add_subentry(entry, row)
        reconcile_group_records(hass, entry)
        from .phone_queue_membership import sync_group_membership_projection
        sync_group_membership_projection(hass, entry)
    return commit


def validate_group_namespace(hass, entry, data, *, exclude=""):
    from .config_validation import entry_route_mappings, mapping_route_aliases, normalized_aliases
    wanted = normalized_aliases((data["name"], data.get("extension")))
    from .mailbox_address import parse_mailbox_address
    from .parking_config import parking_addresses, parse_parking_address
    parking = parking_addresses(entry.data.get("parking", {}))
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
        if item.get("username") and str(item["username"]).casefold() in {
                str(data.get("name") or "").casefold(), str(data.get("extension") or "").casefold()}:
            raise ValueError("The name or extension already belongs to another destination")
        if wanted & mapping_route_aliases(item):
            raise ValueError("The name or extension already belongs to another destination")
        conference = str(item.get("conference_group") or (item.get("metadata") or {}).get("conference_group") or "")
        if wanted & normalized_aliases(conference.split(",")):
            raise ValueError("The group conflicts with an existing conference")


def reconcile_group_records(hass, entry, *, replaced=()):
    """Convert phone declarations once, then reconcile the existing group owner."""
    from homeassistant.config_entries import ConfigSubentry
    from .phone_queue_membership import GROUP_MEMBERSHIP_VERSION, _legacy_iter_phone_memberships

    migrating = entry.data.get(GROUP_MEMBERSHIP_VERSION) != 1
    memberships = _legacy_iter_phone_memberships if migrating else iter_memberships
    declared = {}
    for kind in ("queue", "ring"):
        for endpoint_id, names in memberships(hass, entry, kind):
            for name in names:
                if (kind, normalize_roster_key(name)) in replaced:
                    continue
                group = declared.setdefault((kind, normalize_roster_key(name)), [name, []])
                if endpoint_id not in group[1]:
                    group[1].append(endpoint_id)
    if migrating:
        from .contact_config import contact_dicts
        from .phone_queue_membership import membership_names
        for contact in contact_dicts(entry):
            metadata = contact.get('metadata') or {}
            target = contact_group_destination(contact)
            if not target:
                continue
            for name in membership_names('ring_group', metadata.get('ring_group')):
                key = ('ring', normalize_roster_key(name))
                if key in replaced:
                    continue
                group = declared.setdefault(key, [name, []])
                if target not in group[1]:
                    group[1].append(target)
    records = group_records(entry)
    conflicts = []
    for item in tuple(group_subentries(entry)):
        if "members" in item.data:
            extra = declared.get((item.data['kind'], normalize_roster_key(item.data['name'])), ('', []))[1] if migrating else ()
            members = list(dict.fromkeys((*item.data['members'], *extra)))
            if members != item.data['members']:
                hass.config_entries.async_update_subentry(entry, item, data={**item.data, 'members': members})
            continue
        data = dict(item.data)
        legacy = declared.get((data["kind"], normalize_roster_key(data["name"])), ("", []))[1]
        preferred = data.get("policy", {}).get("agent_order", ())
        data["members"] = list(dict.fromkeys((*preferred, *legacy)))
        data = normalize_group_data(data, identity=data.get("group_id", item.subentry_id),
                                    legacy_policy=entry.version < 7 or entry.minor_version < 7)
        hass.config_entries.async_update_subentry(entry, item, data=data)
    occupied = {(record["kind"], normalize_roster_key(record["name"])) for record in records.values()}
    for (kind, key), (name, members) in declared.items():
        if not key or (kind, key) in occupied:
            continue
        try:
            data = normalize_group_data({"kind": kind, "name": name, "origin": "auto", "members": members,
                                         "extension": name if name.isascii() and name.isdigit() else ""})
            validate_group_namespace(hass, entry, data)
        except ValueError as error:
            _LOGGER.warning("Cannot create automatic %s group %s: %s", kind, name, error)
            conflicts.append(name)
            continue
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type=GROUP_SUBENTRY_TYPE, title=name, data=data, unique_id=f"group:{data['group_id']}"))
        occupied.add((kind, key))
    for item in tuple(group_subentries(entry)):
        if item.data.get("origin") == "auto" and not item.data.get("members") and "member_update" not in item.data:
            hass.config_entries.async_remove_subentry(entry, item.subentry_id)
    if migrating and not conflicts:
        for row in tuple(entry.subentries.values()):
            if row.subentry_type != "phone" or not {"queues", "ring_group"} & row.data.keys():
                continue
            data = {field: value for field, value in row.data.items() if field not in {"queues", "ring_group"}}
            hass.config_entries.async_update_subentry(entry, row, data=data)
        for row in tuple(entry.subentries.values()):
            if row.subentry_type == 'contact' and 'ring_group' in (row.data.get('metadata') or {}):
                metadata = {key: value for key, value in row.data['metadata'].items() if key != 'ring_group'}
                hass.config_entries.async_update_subentry(entry, row, data={**row.data, 'metadata': metadata})
        data = {**entry.data, GROUP_MEMBERSHIP_VERSION: 1}
        from .const import CONF_PHONEBOOK_CONTACTS
        if CONF_PHONEBOOK_CONTACTS in data:
            data[CONF_PHONEBOOK_CONTACTS] = [{**contact, 'metadata': {key: value for key, value in
                (contact.get('metadata') or {}).items() if key != 'ring_group'}}
                if isinstance(contact, dict) else contact for contact in data[CONF_PHONEBOOK_CONTACTS]]
        hass.config_entries.async_update_entry(entry, data=data)
    from homeassistant.helpers import issue_registry as ir
    from .const import DOMAIN
    if conflicts:
        ir.async_create_issue(hass, DOMAIN, "group_membership_conflict", is_fixable=False,
            is_persistent=False, severity=ir.IssueSeverity.ERROR,
            translation_key="group_membership_conflict", translation_placeholders={"groups": ", ".join(conflicts)},
            learn_more_url=f"/config/integrations/integration/{DOMAIN}")
    else:
        ir.async_delete_issue(hass, DOMAIN, "group_membership_conflict")
    sync_group_devices(hass, entry)
    from .paging_config import sync_paging_devices
    sync_paging_devices(hass, entry)


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
    for identity, data in group_records(entry, "ring").items():
        yield RosterEntry(id=data["name"], name=data["name"], extension=data.get("extension", ""),
            ha_bridge=True, enabled=bool(data.get("enabled", True)),
            metadata={"endpoint_id": f"group:ring:{identity}", "endpoint_kind": "group", "group_type": "ring",
                      "group_id": identity, "members": list(group_members(hass, entry, data)), "ring_members": [],
                      "ring_policy": dict(data.get("policy", {})),
                      "fallback_destination": str(data.get("fallback") or ""), "auto": data.get("origin") == "auto"})


async def async_apply_group_updates(hass, entry, previous=None):
    """Consume an interrupted editor command into its native ordered member list."""
    replaced = set()
    for item in tuple(group_subentries(entry)):
        if "member_update" not in item.data:
            continue
        data = dict(item.data)
        data["members"] = data.pop("member_update")
        replaced.add((data['kind'], normalize_roster_key(data['name'])))
        old_name = data.pop("previous_name", "")
        if old_name:
            replaced.add((data["kind"], normalize_roster_key(old_name)))
        data = normalize_group_data(data, identity=data.get("group_id", item.subentry_id),
                                    legacy_policy=entry.version < 7 or entry.minor_version < 7)
        hass.config_entries.async_update_subentry(entry, item, data=data)
    reconcile_group_records(hass, entry, replaced=replaced)
    from .phone_queue_membership import sync_group_membership_projection
    sync_group_membership_projection(hass, entry)
