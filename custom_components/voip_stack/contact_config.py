"""Native contact records shared by the config flow and phonebook services."""

from __future__ import annotations

from collections.abc import Mapping

from homeassistant.config_entries import ConfigSubentry

from .const import CONF_PHONEBOOK_CONTACTS
from .duration_policy import configured_service_timeout, validate_duration

CONTACT_SUBENTRY_TYPE = "contact"


def contact_subentries(entry):
    return [
        item
        for item in entry.subentries.values()
        if item.subentry_type == CONTACT_SUBENTRY_TYPE
    ]


def contact_dicts(entry) -> list[dict]:
    """Read legacy records only until their one-time migration completes."""
    if CONF_PHONEBOOK_CONTACTS in entry.data:
        return [
            dict(item)
            for item in entry.data[CONF_PHONEBOOK_CONTACTS]
            if isinstance(item, dict)
        ]
    return [dict(item.data) for item in contact_subentries(entry)]


def materialize_contact_timeout(raw: Mapping, timeout) -> dict:
    """Copy an imported contact, seeding only its absent automation duration."""
    if not isinstance(raw, Mapping):
        raise ValueError("A contact record must be a mapping")
    result = dict(raw)
    metadata = dict(raw.get("metadata") or {})
    if raw.get("type") == "automation" or metadata.get("virtual_endpoint") == "automation":
        if "automation_timeout" not in metadata:
            selected = raw["timeout"] if "timeout" in raw else timeout
            metadata["automation_timeout"] = validate_duration(selected, "automation_timeout")
        result["metadata"] = metadata
    elif "metadata" in raw:
        result["metadata"] = metadata
    return result


def replace_contacts(hass, entry, contacts: list[dict], *, automation_timeout=None) -> None:
    """Validate the entire replacement before the synchronous registry update."""
    from .roster import parse_roster_json

    if any(not isinstance(raw, Mapping) for raw in contacts):
        raise ValueError("Contact records must be mappings")
    if any(
        (raw.get("type") == "automation" or (raw.get("metadata") or {}).get("virtual_endpoint") == "automation")
        and "automation_timeout" not in (raw.get("metadata") or {})
        for raw in contacts
    ):
        if automation_timeout is None:
            automation_timeout = configured_service_timeout(entry.data, "automation_timeout")
        contacts = [materialize_contact_timeout(raw, automation_timeout) for raw in contacts]
    parsed = parse_roster_json(contacts)
    wanted = {item.id: raw for item, raw in zip(parsed, contacts, strict=True)}
    if len(wanted) != len(contacts):
        raise ValueError("Contact IDs must be unique")
    existing = {str(item.data["id"]): item for item in contact_subentries(entry)}
    for identity, data in wanted.items():
        title = str(data.get("name") or identity)
        if identity in existing:
            hass.config_entries.async_update_subentry(
                entry, existing[identity], data=data, title=title
            )
        else:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=data,
                    subentry_type=CONTACT_SUBENTRY_TYPE,
                    title=title,
                    unique_id=f"contact:{identity}",
                ),
            )
    for identity in existing.keys() - wanted.keys():
        hass.config_entries.async_remove_subentry(entry, existing[identity].subentry_id)
    if CONF_PHONEBOOK_CONTACTS in entry.data:
        data = dict(entry.data)
        data.pop(CONF_PHONEBOOK_CONTACTS)
        hass.config_entries.async_update_entry(entry, data=data)

    from .automation_contact_device import async_sync_automation_contact_devices
    async_sync_automation_contact_devices(hass, entry)
