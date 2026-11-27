"""Native HA device identity for existing Automation contacts, without SIP accounts."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .automation_routing import automation_event_type
from .const import DOMAIN
from .roster import RosterEntry, parse_roster_json

AUTOMATION_CONTACT_MODEL = "Automation contact"
_IDENTIFIER_PREFIX = "automation_contact:"
_EVENT_SUFFIX = ":call_event"


def automation_contact_records(entry) -> dict[str, RosterEntry]:
    """Read the canonical contact owner, including disabled records for identity."""
    from .contact_config import contact_dicts

    return {contact.id: contact for contact in parse_roster_json(contact_dicts(entry))
            if contact.metadata.get("virtual_endpoint") == "automation"}


def automation_contact_device_identifier(entry_id: str, contact_id: str) -> str:
    return f"{_IDENTIFIER_PREFIX}{entry_id}:{contact_id}"


def automation_contact_event_unique_id(entry_id: str, contact_id: str) -> str:
    return automation_contact_device_identifier(entry_id, contact_id) + _EVENT_SUFFIX


def automation_contact_subentry_id(entry, contact_id: str) -> str | None:
    return next((item.subentry_id for item in entry.subentries.values()
                 if item.subentry_type == "contact" and item.data.get("id") == contact_id), None)


@callback
def async_sync_automation_contact_devices(hass, entry) -> None:
    """Reconcile only this entry's real contact devices and event registrations."""
    contacts = automation_contact_records(entry)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    prefix = f"{_IDENTIFIER_PREFIX}{entry.entry_id}:"
    wanted = {automation_contact_device_identifier(entry.entry_id, identity) for identity in contacts}
    for contact in contacts.values():
        devices.async_get_or_create(config_entry_id=entry.entry_id,
            config_subentry_id=automation_contact_subentry_id(entry, contact.id),
            identifiers={(DOMAIN, automation_contact_device_identifier(entry.entry_id, contact.id))},
            name=contact.display_name, manufacturer="VoIP Stack", model=AUTOMATION_CONTACT_MODEL,
            entry_type=dr.DeviceEntryType.SERVICE)
    for entity in tuple(er.async_entries_for_config_entry(entities, entry.entry_id)):
        if (entity.platform == DOMAIN and entity.unique_id.startswith(prefix)
                and entity.unique_id.endswith(_EVENT_SUFFIX)
                and entity.unique_id.removesuffix(_EVENT_SUFFIX) not in wanted):
            entities.async_remove(entity.entity_id)
    for device in tuple(devices.devices):
        owned = {value for domain, value in device.identifiers if domain == DOMAIN and value.startswith(prefix)}
        if not owned or owned.intersection(wanted):
            continue
        if entry.entry_id not in device.config_entries and device.config_entries:
            continue
        for entity in tuple(er.async_entries_for_device(entities, device.id, include_disabled_entities=True)):
            if (entity.platform == DOMAIN and entity.config_entry_id == entry.entry_id
                    and entity.unique_id.startswith(prefix)):
                entities.async_remove(entity.entity_id)
        if device.config_entries - {entry.entry_id}:
            devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
        else:
            devices.async_remove_device(device.id)


def _contact_for_identifier(hass, identifier: str, entry_id: str | None):
    if not identifier.startswith(_IDENTIFIER_PREFIX):
        return None
    owner, separator, identity = identifier[len(_IDENTIFIER_PREFIX):].partition(":")
    if not separator or not identity or (entry_id is not None and owner != entry_id):
        return None
    entry = hass.config_entries.async_get_entry(owner)
    if entry is None or entry.domain != DOMAIN:
        return None
    try:
        contact = automation_contact_records(entry).get(identity)
    except ValueError:
        return None
    return contact.id if contact is not None and contact.enabled else None


def automation_contact_id_for_device(hass, device_id: str, entry_id: str | None = None) -> str | None:
    """Resolve a native device to its enabled contact, never to a SIP endpoint."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None or (entry_id is not None and entry_id not in device.config_entries):
        return None
    candidates = set()
    for domain, identifier in device.identifiers:
        if domain != DOMAIN or not identifier.startswith(_IDENTIFIER_PREFIX):
            continue
        owner = identifier[len(_IDENTIFIER_PREFIX):].partition(":")[0]
        if owner not in device.config_entries:
            continue
        identity = _contact_for_identifier(hass, identifier, entry_id)
        if identity is not None:
            candidates.add((owner, identity))
    return next(iter(candidates))[1] if len(candidates) == 1 else None


def automation_contact_id_for_entity(hass, entity_id: str, entry_id: str | None = None) -> str | None:
    """Resolve the integration's event entity through its native device identity."""
    entity = er.async_get(hass).async_get(entity_id)
    if (entity is None or entity.platform != DOMAIN or entity.domain != "event"
            or entity.device_id is None or not entity.unique_id.endswith(_EVENT_SUFFIX)):
        return None
    owner = entity.config_entry_id
    if owner is None or (entry_id is not None and owner != entry_id):
        return None
    identity = automation_contact_id_for_device(hass, entity.device_id, owner)
    if identity is None:
        return None
    expected = automation_contact_event_unique_id(owner, identity)
    return identity if entity.unique_id == expected else None


def resolve_automation_contact_selection(hass, options) -> RosterEntry:
    """Validate one context-bound trigger selection against its actual owner."""
    from .store import config_entry

    selected = [key for key in ("destination_device", "destination_entity") if options.get(key)]
    manual = ("destination", "destination_account", "destination_number", "destination_extension",
              "destination_range_end")
    if (len(selected) != 1 or any(options.get(key) for key in manual)
            or options.get("destination_match", "exact") != "exact"):
        raise ValueError("Choose one Automation contact device or call event, without a manual destination filter")
    entry = config_entry(hass)
    if entry is None:
        raise ValueError("The PBX is not configured")
    key = selected[0]
    resolver = automation_contact_id_for_device if key == "destination_device" else automation_contact_id_for_entity
    identity = resolver(hass, options[key], entry.entry_id)
    if identity is None:
        raise ValueError("Select an enabled Automation contact device or call event")
    contact = automation_contact_records(entry).get(identity)
    if contact is None or not contact.enabled:
        raise ValueError("The Automation contact is unavailable")
    return contact


def automation_contact_matches_destination(hass, contact: RosterEntry, payload) -> bool:
    """Match a real incoming application, not mutable Event Entity state."""
    from .runtime_data import runtime_data

    runtime = runtime_data(hass)
    registry = runtime.sip if runtime is not None else None
    application = registry.resource_for(str(payload.get("call_id") or ""), "automation") if registry else None
    if application is not None:
        if automation_event_type(payload) in {"ringing", "automation_requested"}:
            # Forwarding retains this application for rollback. Its new
            # destination's ringing must not start the old contact again.
            try:
                application.require_current(int(payload.get("generation") or 0))
            except (ServiceValidationError, TypeError, ValueError):
                return False
        return (not application.outgoing and application.contact.id == contact.id
                and application.contact.metadata.get("virtual_endpoint") == "automation")
    if payload.get("direction") == "outgoing" or payload.get("route_kind") != "automation":
        return False
    aliases = {contact.id.casefold(), contact.display_name.casefold()}
    if contact.extension:
        aliases.add(contact.extension.casefold())
    return any(str(payload.get(key) or "").casefold() in aliases
               for key in ("callee", "destination", "called_extension"))
