"""Native contact records shared by the config flow and phonebook services."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry

from .const import CONF_PHONEBOOK_CONTACTS

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


def replace_contacts(hass, entry, contacts: list[dict]) -> None:
    """Validate the entire replacement before the synchronous registry update."""
    from .roster import parse_roster_json

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
