"""Per-phone queue declarations, separate from HA-owned agent availability."""

MAX_QUEUE_LIST_BYTES = 240
GROUP_MEMBERSHIP_VERSION = "group_membership_version"
CONF_ESPHOME_PHONE_MEMBERSHIPS = "esphome_phone_memberships"
MEMBERSHIP_FIELDS = {"queue": "queues", "ring": "ring_group"}


def _normalize_membership_names(value: object, *, name_bytes, list_bytes) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ValueError("Queues must be names or extensions separated by commas")
    result = []
    seen = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("Queue names must be text")
        name = raw.strip()
        if not name:
            continue
        if any(ord(char) < 32 or ord(char) == 127 or char in "|,;" for char in name):
            raise ValueError("Queue names cannot contain control characters or delimiters")
        if len(name.encode("utf-8")) > name_bytes:
            raise ValueError(f"Membership names cannot exceed {name_bytes} bytes")
        if name.casefold() not in seen:
            result.append(name)
            seen.add(name.casefold())
    if len(", ".join(result).encode("utf-8")) > list_bytes:
        raise ValueError(f"Membership declarations cannot exceed {list_bytes} bytes")
    return tuple(result)


def normalize_phone_queues(value: object) -> tuple[str, ...]:
    """Bound new queue declarations without restricting existing ring groups."""
    return _normalize_membership_names(value, name_bytes=32, list_bytes=MAX_QUEUE_LIST_BYTES)


def membership_names(field, value):
    if field not in MEMBERSHIP_FIELDS.values():
        raise ValueError("Unsupported phone membership field")
    return (_normalize_membership_names(value, name_bytes=255, list_bytes=255)
            if field == "ring_group" else normalize_phone_queues(value))


def membership_value(field, value):
    """Normalize a canonical phone field, independently from group devices."""
    names = membership_names(field, value)
    return names if field == "queues" else ", ".join(names)


def membership_text(field, value):
    normalized = membership_value(field, value)
    return ", ".join(normalized) if field == "queues" else normalized


def normalize_membership_tracking(value):
    if not isinstance(value, dict):
        return {}
    return {key: membership_text(key, item) for key, item in value.items()
            if key in MEMBERSHIP_FIELDS.values()}


def observe_native_membership(record, field, value, *, allow_change=False):
    """An HA desire survives snapshots; only live changes can replace it.

    Native text has no revision token. A change made locally while HA is
    offline therefore cannot override a previously observed HA-owned value.
    """
    observed = normalize_membership_tracking(record.get("membership_observed"))
    pending = normalize_membership_tracking(record.get("membership_pending"))
    incoming = membership_text(field, value)
    canonical = membership_text(field, record.get(field))
    if field not in pending and (field not in observed or allow_change):
        canonical = incoming
    if incoming == canonical:
        pending.pop(field, None)
    else:
        pending[field] = canonical
    observed[field] = incoming
    return {field: membership_value(field, canonical),
            "membership_pending": pending, "membership_observed": observed}


def _legacy_iter_phone_memberships(hass, entry, kind):
    """Read upgrade input until ordered group membership has been migrated."""
    from .managed_phones import managed_records
    from .phone_config import phone_endpoint_id, phone_subentries

    field = MEMBERSHIP_FIELDS[kind]

    seen = set()
    for phone in phone_subentries(entry):
        endpoint_id = phone_endpoint_id(phone)
        if endpoint_id:
            seen.add(endpoint_id)
            yield endpoint_id, membership_names(field, phone.data.get(field))
    for phone in managed_records(entry).values():
        if phone.endpoint_id in seen:
            continue
        seen.add(phone.endpoint_id)
        if phone.active:
            yield phone.endpoint_id, membership_names(field, getattr(phone, field))
    for endpoint_id, record in entry.data.get(CONF_ESPHOME_PHONE_MEMBERSHIPS, {}).items():
        if endpoint_id not in seen:
            seen.add(endpoint_id)
            yield endpoint_id, membership_names(field, record.get(field))
    from .runtime_data import runtime_data

    runtime = runtime_data(hass)
    if runtime is not None:
        for endpoint in runtime.endpoints:
            if endpoint.endpoint_id not in seen:
                yield endpoint.endpoint_id, membership_names(field, getattr(endpoint, field))


def project_phone_memberships(entry, endpoint_id, data):
    """Project account and firmware fields from the native group owner."""
    if getattr(entry, "data", {}).get(GROUP_MEMBERSHIP_VERSION) != 1:
        return dict(data)
    from .group_config import group_records

    result = dict(data)
    for kind, field in MEMBERSHIP_FIELDS.items():
        names = [record["name"] for record in group_records(entry, kind).values()
                 if endpoint_id in record.get("members", ())]
        result[field] = list(names) if field == "queues" else ", ".join(names)
    return result


def iter_phone_memberships(hass, entry, kind):
    """Project each phone's declarations from the one ordered group member list."""
    if entry.data.get(GROUP_MEMBERSHIP_VERSION) != 1:
        yield from _legacy_iter_phone_memberships(hass, entry, kind)
        return
    from .group_config import group_records
    from .phone_config import phone_subentries, phone_endpoint_id
    from .runtime_data import runtime_data

    identities = dict.fromkeys(phone_endpoint_id(row) for row in phone_subentries(entry))
    runtime = runtime_data(hass)
    if runtime is not None:
        identities.update(dict.fromkeys(endpoint.endpoint_id for endpoint in runtime.endpoints.endpoints))
    memberships = {identity: [] for identity in identities}
    for record in group_records(entry, kind).values():
        for identity in record.get("members", ()):
            memberships.setdefault(identity, []).append(record["name"])
    yield from ((identity, tuple(names)) for identity, names in memberships.items())


def iter_phone_queue_memberships(hass, entry):
    yield from iter_phone_memberships(hass, entry, "queue")


def sync_group_membership_projection(hass, entry):
    """Update existing account views and schedule their firmware projection."""
    from .runtime_data import runtime_data
    runtime = runtime_data(hass)
    if runtime is None:
        return
    for endpoint in tuple(runtime.endpoints.endpoints):
        fields = project_phone_memberships(entry, endpoint.endpoint_id, {})
        if not fields:
            continue
        queues, ring_group = tuple(fields["queues"]), fields["ring_group"]
        if endpoint.queues != queues or endpoint.ring_group != ring_group:
            runtime.endpoints.update(endpoint.endpoint_id, queues=queues, ring_group=ring_group)
        store = runtime.softphones.get(endpoint.endpoint_id)
        if store is not None:
            store["queues"] = list(queues)
            store.setdefault("groups", {})["ring_group"] = ring_group
    controller = runtime.phone_provisioning
    if controller is not None:
        controller.schedule()


def _apply_membership_updates(hass, entry, updates):
    """Apply phone-level edits through the native group membership owner."""
    from homeassistant.config_entries import ConfigSubentry
    from .group_config import group_records, normalize_group_data, reconcile_group_records, validate_group_namespace
    from .roster import normalize_roster_key

    reconcile_group_records(hass, entry)
    proposals = {}
    creations = []
    for endpoint_id, fields in updates.items():
        for field, value in fields.items():
            if field not in MEMBERSHIP_FIELDS.values():
                raise ValueError("Unsupported phone membership field")
            kind = next(kind for kind, owned in MEMBERSHIP_FIELDS.items() if field == owned)
            wanted = {normalize_roster_key(name): name for name in membership_names(field, value)}
            records = group_records(entry, kind)
            occupied = {normalize_roster_key(record["name"]): identity for identity, record in records.items()}
            for identity, original in records.items():
                record = proposals.get(identity, original)
                members = [member for member in record.get("members", ()) if member != endpoint_id]
                if normalize_roster_key(record["name"]) in wanted:
                    members = list(record.get("members", ())) if endpoint_id in record.get("members", ()) else [*members, endpoint_id]
                if members != record.get("members", []):
                    proposals[identity] = normalize_group_data({**record, "members": members}, identity=identity)
            for key, name in wanted.items():
                if key in occupied:
                    continue
                existing = next((record for record in creations if record["kind"] == kind
                                 and normalize_roster_key(record["name"]) == key), None)
                if existing is not None:
                    if endpoint_id not in existing["members"]:
                        existing["members"].append(endpoint_id)
                    continue
                if any(normalize_roster_key(record["name"]) == key for record in creations):
                    raise ValueError("The same destination cannot be both a queue and a ring group")
                record = normalize_group_data({"kind": kind, "name": name, "members": [endpoint_id], "origin": "auto",
                    "extension": name if name.isascii() and name.isdigit() else ""})
                validate_group_namespace(hass, entry, record)
                creations.append(record)
    for record in creations:
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="group", title=record["name"],
            unique_id=f"group:{record['group_id']}", data=record))
    for row in tuple(entry.subentries.values()):
        if row.subentry_type == "group" and row.data.get("group_id") in proposals:
            hass.config_entries.async_update_subentry(entry, row, data=proposals[row.data["group_id"]])
    sync_group_membership_projection(hass, entry)


async def async_set_phone_memberships(hass, entry, endpoint_id, field, value):
    """Persist a phone's membership using the same native group editor contract."""
    _apply_membership_updates(hass, entry, {endpoint_id: {field: value}})


async def async_update_group_members(hass, entry, kind, name, member_ids, old_name=""):
    """Adapt an existing HA action to the native ordered member list."""
    from homeassistant.config_entries import ConfigSubentry
    from .group_config import group_subentries, normalize_group_data, reconcile_group_records, validate_group_namespace
    from .roster import normalize_roster_key
    from .phone_config import phone_subentries, phone_endpoint_id
    from .runtime_data import runtime_data

    key = normalize_roster_key(old_name or name)
    row = next((row for row in group_subentries(entry) if row.data["kind"] == kind
                and normalize_roster_key(row.data["name"]) == key), None)
    if kind == "queue":
        known = {phone_endpoint_id(phone) for phone in phone_subentries(entry)}
        runtime = runtime_data(hass)
        if runtime is not None:
            known.update(endpoint.endpoint_id for endpoint in runtime.endpoints.endpoints
                         if str(endpoint.kind) == "esphome" and endpoint.device_id)
        previous = set(row.data.get("members", ())) if row is not None else set()
        if set(member_ids) - known - previous:
            raise ValueError("The selected phone has no configuration owner")
    reconcile_group_records(hass, entry)
    data = normalize_group_data({**(dict(row.data) if row is not None else {}), "kind": kind,
        "name": name, "members": list(member_ids)}, identity=row.data["group_id"] if row is not None else "")
    validate_group_namespace(hass, entry, data, exclude=data["group_id"] if row is not None else "")
    if row is None:
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="group", title=name,
            unique_id=f"group:{data['group_id']}", data=data))
    else:
        hass.config_entries.async_update_subentry(entry, row, data=data, title=name)
    sync_group_membership_projection(hass, entry)


async def async_set_phone_queues(call):
    """Persist declarations without changing existing login or pause records."""
    await _async_membership_service(call, "queues", call.data["queues"])


async def async_set_phone_memberships_service(call):
    await _async_membership_service(call, call.data["field"], call.data["value"])


async def _async_membership_service(call, field, value):
    from homeassistant.exceptions import ServiceValidationError
    from .phone_state import resolve_phone
    from .service_endpoints import async_require_phone_service_control
    from .store import config_entry
    from .service_endpoints import resolve_service_group_reference
    from .group_config import group_records

    endpoint = resolve_phone(call.hass, str(call.data["phone"]))
    await async_require_phone_service_control(call.hass, call, endpoint=endpoint)
    entry = config_entry(call.hass)
    if entry is None:
        raise ServiceValidationError("VoIP Stack configuration is unavailable")
    values = [item.strip() for item in value.split(',') if item.strip()] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ServiceValidationError('Select the configured groups for this account')
    kind = 'paging' if field == 'paging_groups' else 'queue' if field == 'queues' else 'ring'
    adapted = [resolve_service_group_reference(call.hass, item, kind, strict=True) for item in values]
    if kind != 'paging':
        records = group_records(entry, kind)
        adapted = [records[item]['name'] if item in records else item for item in adapted]
    value = adapted
    if field == "paging_groups":
        from .authorization import async_require_service_admin
        from .paging_config import update_phone_paging_groups
        from .runtime_data import require_runtime_data

        # Receiving announcements is an administrative assignment, never self-enrollment.
        await async_require_service_admin(call.hass, call)
        entry = config_entry(call.hass)
        if entry is None:
            raise ServiceValidationError("VoIP Stack configuration is unavailable")
        runtime = require_runtime_data(call.hass)
        try:
            # HA starts update listeners eagerly. Reuse their reconciliation lock
            # so they see every canonical group in this one membership change.
            async with runtime.group_update_lock:
                changed = update_phone_paging_groups(call.hass, entry, endpoint.endpoint_id, value)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        observer = runtime.sip.component("phone_state") if runtime.sip is not None else None
        if changed and observer is not None:
            observer.refresh()
        return
    try:
        desired = membership_value(field, value)
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    entry = config_entry(call.hass)
    if entry is None:
        raise ServiceValidationError("VoIP Stack configuration is unavailable")
    try:
        await async_set_phone_memberships(call.hass, entry, endpoint.endpoint_id, field, desired)
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
