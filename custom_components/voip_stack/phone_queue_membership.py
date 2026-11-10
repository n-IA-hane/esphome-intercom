"""Per-phone queue declarations, separate from HA-owned agent availability."""

MAX_QUEUE_LIST_BYTES = 240
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


def iter_phone_memberships(hass, entry, kind):
    """Project the owning phone records, including temporarily offline ESPs."""
    from .managed_phones import managed_records
    from .phone_config import CONF_PHONE_ENDPOINT_ID, phone_subentries

    field = MEMBERSHIP_FIELDS[kind]

    seen = set()
    for phone in phone_subentries(entry):
        endpoint_id = str(phone.data.get(CONF_PHONE_ENDPOINT_ID) or "").strip()
        if endpoint_id:
            seen.add(endpoint_id)
            yield endpoint_id, membership_names(field, phone.data.get(field))
    for phone in managed_records(entry).values():
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


def iter_phone_queue_memberships(hass, entry):
    return iter_phone_memberships(hass, entry, "queue")


def _apply_membership_updates(hass, entry, updates):
    """Validate every target before synchronously committing the batch."""
    from dataclasses import replace
    from .managed_phones import CONF_MANAGED_PHONES, managed_records
    from .phone_config import endpoint_from_data, phone_subentry_by_endpoint_id, sync_registry_from_entry
    from .phone_endpoint import EndpointKind
    from .runtime_data import runtime_data

    runtime = runtime_data(hass)
    managed = managed_records(entry)
    managed_by_endpoint = {phone.endpoint_id: phone for phone in managed.values()}
    legacy = dict(entry.data.get(CONF_ESPHOME_PHONE_MEMBERSHIPS, {}))
    subentries, endpoint_updates = [], {}
    managed_changed = legacy_changed = False
    for endpoint_id, fields in updates.items():
        normalized = {field: membership_value(field, value) for field, value in fields.items()}
        subentry = phone_subentry_by_endpoint_id(entry, endpoint_id)
        if subentry is not None:
            data = {**subentry.data, **normalized}
            if "queues" in normalized:
                data["queues"] = list(normalized["queues"])
            endpoint_from_data(entry, data)
            subentries.append((subentry, data))
        elif endpoint_id in managed_by_endpoint:
            phone = managed_by_endpoint[endpoint_id]
            pending = dict(phone.membership_pending)
            for field, value in normalized.items():
                desired = membership_text(field, value)
                if phone.membership_observed.get(field) != desired:
                    pending[field] = desired
                else:
                    pending.pop(field, None)
            changed = replace(phone, **normalized, membership_pending=pending)
            managed[phone.device_id] = changed
            managed_changed |= changed != phone
        else:
            endpoint = runtime.endpoints.get(endpoint_id) if runtime is not None else None
            previous = legacy.get(endpoint_id)
            if (endpoint is None or endpoint.kind is not EndpointKind.ESPHOME) and previous is None:
                raise ValueError("The selected phone has no configuration owner")
            record = dict(previous or {"device_id": endpoint.device_id,
                "queues": list(endpoint.queues), "ring_group": endpoint.ring_group})
            pending = normalize_membership_tracking(record.get("membership_pending"))
            observed = normalize_membership_tracking(record.get("membership_observed"))
            for field, value in normalized.items():
                desired = membership_text(field, value)
                if observed.get(field) != desired:
                    pending[field] = desired
                else:
                    pending.pop(field, None)
                record[field] = list(value) if field == "queues" else value
            record.update(membership_pending=pending, membership_observed=observed)
            legacy_changed |= record != previous
            legacy[endpoint_id] = record
        endpoint_updates[endpoint_id] = normalized
    # No await between these writes: listeners observe the complete desired batch.
    for subentry, data in subentries:
        hass.config_entries.async_update_subentry(entry, subentry, data=data)
    if managed_changed or legacy_changed:
        data = dict(entry.data)
        if managed_changed:
            data[CONF_MANAGED_PHONES] = {key: phone.record() for key, phone in managed.items()}
        if legacy_changed:
            data[CONF_ESPHOME_PHONE_MEMBERSHIPS] = legacy
        hass.config_entries.async_update_entry(entry, data=data)
    if subentries:
        sync_registry_from_entry(hass, entry)
    if runtime is not None:
        for endpoint_id, fields in endpoint_updates.items():
            if runtime.endpoints.get(endpoint_id) is not None:
                runtime.endpoints.update(endpoint_id, **fields)
            store = getattr(runtime, "softphones", {}).get(endpoint_id)
            if store is not None:
                if "queues" in fields:
                    store["queues"] = list(fields["queues"])
                if "ring_group" in fields:
                    store.setdefault("groups", {})["ring_group"] = fields["ring_group"]
        controller = getattr(runtime, "phone_provisioning", None)
        if controller is not None:
            controller.schedule()


async def async_set_phone_memberships(hass, entry, endpoint_id, field, value):
    """Persist one phone's desired declarations, including an offline ESP."""
    _apply_membership_updates(hass, entry, {endpoint_id: {field: value}})


async def async_update_group_members(hass, entry, kind, name, member_ids, old_name=""):
    """Write the checkbox editor through the same phone fields as native text."""
    from .roster import normalize_roster_key

    field = MEMBERSHIP_FIELDS[kind]
    name = membership_text(field, [name])
    if not name:
        raise ValueError("Group name must not be empty")
    selected = set(member_ids)
    configured = dict(iter_phone_memberships(hass, entry, kind))
    if selected - configured.keys():
        raise ValueError("Select existing VoIP phones")
    aliases = {normalize_roster_key(name), normalize_roster_key(str(old_name))} - {""}
    updates = {}
    for endpoint_id, names in configured.items():
        wanted = [item for item in names if normalize_roster_key(item) not in aliases]
        if endpoint_id in selected:
            wanted.append(name)
        if tuple(wanted) != names:
            updates[endpoint_id] = {field: wanted}
    _apply_membership_updates(hass, entry, updates)


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

    endpoint = resolve_phone(call.hass, str(call.data["phone"]))
    await async_require_phone_service_control(call.hass, call, endpoint=endpoint)
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
