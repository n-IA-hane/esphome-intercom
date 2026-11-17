"""Shared inventory validation, independent of native configuration forms."""

from types import SimpleNamespace

from .phone_config import endpoint_from_data, endpoint_from_subentry, phone_subentries
from .phone_endpoint import EndpointKind
from .phone_profile import resolve_phone_keys
from .physical_phone import PHYSICAL_LINE_TRANSPORTS, normalize_physical_phone, physical_phone_subentries


def physical_slot(value):
    text = str(value or "")
    if not text.isascii() or not text.isdigit() or int(text) < 1:
        raise ValueError("invalid_phone_bindings")
    return str(int(text))


def physical_validation_runtime(hass, entry, account_drafts=None):
    """Overlay uncommitted accounts before building one immutable routing view."""
    from .endpoint_registry import EndpointRegistry
    from .group_config import group_members, group_records
    from .contact_config import contact_dicts
    from .phone_directory import PhoneDirectory
    from .roster import parse_roster_json

    runtime = getattr(entry, "runtime_data", None)
    proposed = {endpoint.endpoint_id: endpoint for item in phone_subentries(entry)
                for endpoint in (endpoint_from_subentry(entry, item),)}
    for identity, data in (account_drafts or {}).items():
        proposed[identity] = endpoint_from_data(entry, data, fallback_endpoint_id=identity)
    configured = EndpointRegistry()
    for endpoint in proposed.values():
        configured.register(endpoint)
    actual = getattr(runtime, "endpoints", None)
    endpoints = SimpleNamespace(resolve=lambda ref: configured.resolve(ref)
        or (actual.resolve(ref) if actual is not None else None))
    queues = getattr(runtime, "queues", None)
    if queues is None:
        rows = {identity: SimpleNamespace(**record, queue_id=identity,
                members=frozenset(group_members(hass, entry, record)))
                for identity, record in group_records(entry, "queue").items()}
        queues = SimpleNamespace(resolve=lambda ref: rows.get(str(ref)))
    return SimpleNamespace(endpoints=endpoints, queues=queues,
        directory=PhoneDirectory(parse_roster_json(contact_dicts(entry))))


def validate_physical_inventory(hass, entry, draft, profile, *, previous=None,
                                current_subentry_id=None, account_drafts=None):
    """Validate an entire future inventory record without writing any owner."""
    previous = previous or {}
    data = {**previous, **normalize_physical_phone(draft, previous=previous)}
    if any(item.data.get("mac") == data["mac"] and item.subentry_id != current_subentry_id
           for item in physical_phone_subentries(entry)):
        raise ValueError("phone_mac_exists")
    caps = profile["capabilities"]
    runtime = physical_validation_runtime(hass, entry, account_drafts)
    for line in data["lines"]:
        if int(line["slot"]) > int(caps["max_lines"]):
            raise ValueError("phone_line_unsupported")
        endpoint = runtime.endpoints.resolve(line["endpoint_id"])
        if endpoint is None or endpoint.kind is not EndpointKind.SIP_ACCOUNT:
            raise ValueError("phone_sip_account_required")
        if str(line.get("transport") or "udp").lower() not in PHYSICAL_LINE_TRANSPORTS:
            raise ValueError("invalid_phone_transport")
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
    resolve_phone_keys(runtime, data, profile)
    from .physical_phone_provisioning import validate_slot_removal
    owner = getattr(getattr(entry, "runtime_data", None), "physical_phones", None)
    validator = getattr(owner, "validate_inventory_change", None)
    if validator is not None:
        validator(data, profile)
    else:
        validate_slot_removal(previous, data, profile)
    return data
