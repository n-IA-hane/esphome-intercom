"""HA service handlers for the central VoIP phonebook."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, replace
import logging
from typing import Any

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .config import assist_config
from .config_validation import route_namespace_conflicts
from .const import (
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_EXTENSION,
)
from .directory_runtime import async_refresh_directory
from .runtime_data import endpoint_directory
from .roster import RosterEntry, dump_roster_json, normalize_roster_key, parse_roster_json
from .store import manual_roster_entries, store_manual_roster_entries

_LOGGER = logging.getLogger(__name__)


async def _async_directory_json(hass: Any) -> str:
    entries = await async_refresh_directory(hass)
    if entries is None:
        raise ServiceValidationError("The VoIP phone directory is unavailable")
    return dump_roster_json(entries)


def _entry_mapping(entry: RosterEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.name,
        "extension": entry.extension,
        "number": entry.number,
        "metadata": dict(entry.metadata or {}),
    }


def _runtime_route_mappings(hass: Any) -> list[Mapping[str, Any]]:
    from .mailbox import mailbox_settings

    mappings: list[Mapping[str, Any]] = []
    from .parking_config import parking_settings
    parking = parking_settings(hass)
    if parking.get("enabled"):
        mappings.append({"parking_feature_routes": parking})
    mappings.append({"mailbox_feature_routes": True})
    registry = endpoint_directory(hass)
    mappings.extend(
        {
            "id": endpoint.endpoint_id,
            "name": endpoint.name,
            "extension": endpoint.extension,
            "username": endpoint.username,
            "ring_group": endpoint.ring_group,
            "conference_group": endpoint.conference_group,
            "voicemail": mailbox_settings(hass, endpoint.endpoint_id),
        }
        for endpoint in registry.endpoints
    )
    assist = assist_config(hass)
    assist_extension = str(assist.get(CONF_ASSIST_EXTENSION) or "").strip()
    if assist.get(CONF_ASSIST_ENDPOINT_ENABLED) and assist_extension:
        mappings.append(
            {
                "id": "assist",
                "name": str(assist.get("name") or "Assist"),
                "extension": assist_extension,
            }
        )
    from .store import config_entry
    from .group_config import group_records
    from .paging_config import paging_group_configs
    entry = config_entry(hass)
    if entry is not None:
        mappings.extend(group_records(entry).values())
        mappings.extend(paging_group_configs(entry).values())
    return mappings


def _validate_contact_namespace(
    hass: Any,
    entries: list[RosterEntry],
    *,
    existing_manual: list[RosterEntry] | None = None,
) -> None:
    existing = _runtime_route_mappings(hass)
    existing.extend(_entry_mapping(item) for item in existing_manual or [])
    accepted: list[Mapping[str, Any]] = []
    for entry in entries:
        metadata = entry.metadata or {}
        groups = tuple(
            part.strip()
            for field in ("ring_group", "conference_group")
            for part in str(metadata.get(field) or "").split(",")
            if part.strip()
        )
        if route_namespace_conflicts(
            candidate_routes=(
                entry.id,
                entry.name,
                entry.extension,
                entry.number,
            ),
            candidate_groups=groups,
            existing=(*existing, *accepted),
        ):
            raise ValueError(
                f"phonebook route for {entry.display_name!r} conflicts with an "
                "existing phone, contact, Assist extension, or group"
            )
        accepted.append(_entry_mapping(entry))


class ContactProfileError(ValueError):
    """A contact's explicit SIP compatibility settings are inconsistent."""


def _store_administrator_contacts(hass, entries, *, group_commands=()):
    """Consume import membership commands into native groups before storing contacts."""
    from .store import config_entry
    from .group_config import contact_group_destination, prepare_contact_group_commands

    entry = config_entry(hass)
    if entry is None:
        raise ServiceValidationError('VoIP Stack configuration is unavailable')
    commands = list(group_commands)
    cleaned = []
    for contact in entries:
        if 'ring_group' in contact.metadata:
            commands.append((contact_group_destination(asdict(contact)), contact.metadata['ring_group']))
        cleaned.append(replace(contact, metadata={key: value for key, value in contact.metadata.items() if key != 'ring_group'}))
    commit = prepare_contact_group_commands(hass, entry, commands, contacts=[asdict(contact) for contact in cleaned])
    store_manual_roster_entries(hass, cleaned)
    commit()


def contact_from_data(data: Mapping[str, Any]) -> RosterEntry:
    """Normalize a contact from either the HA editor or a service request."""
    name = str(data["name"]).strip()
    entry_id = str(data.get("id") or name).strip()

    def metadata_value(key: str):
        value = data.get(key)
        if value in (None, ""):
            return None
        return value

    metadata = {
        **dict(data.get("metadata") or {}),
        **{
        key: metadata_value(key)
        for key in (
            "transport",
            "rtp_port",
            "tx_rate",
            "rx_rate",
            "tx_formats",
            "rx_formats",
            "max_payload_bytes",
            "conference_group",
            "conference_ring",
            "ring_group",
        )
        if key in data and metadata_value(key) is not None
        },
    }
    profile = str(data.get("sip_profile", metadata.get("sip_profile", "auto"))).strip().lower()
    audio = str(data.get("dahua_audio", metadata.get("dahua_audio", "auto"))).strip().lower()
    if profile not in {"auto", "dahua"} or audio not in {"auto", "standard", "pcm", "symmetric"}:
        raise ContactProfileError("Unknown SIP profile or Dahua audio mode")
    if audio != "auto" and profile != "dahua":
        raise ContactProfileError("Select the Dahua SIP profile before choosing its audio compatibility")
    if (data.get("type") == "automation" or metadata.get("virtual_endpoint") == "automation") and (
        profile != "auto" or audio != "auto"
    ):
        raise ContactProfileError("Automation contacts cannot select a SIP device profile")
    for key, value in (("sip_profile", profile), ("dahua_audio", audio)):
        if value == "auto":
            metadata.pop(key, None)
        else:
            metadata[key] = value
    address = str(data.get("address") or "").strip()
    sip_uri = str(data.get("sip_uri") or "").strip()
    extension = str(data.get("extension") or "").strip()
    number = str(data.get("number") or "").strip()
    port = int(data.get("port") or 0)
    if data.get("type") == "automation":
        if not name:
            raise ValueError("An automation contact needs a name")
        if extension and not extension.isdigit():
            raise ValueError("An automation extension must be numeric")
        if address or sip_uri or number or port:
            raise ValueError("An automation contact has no physical address or external number")
        metadata.update(
            virtual_endpoint="automation",
            fallback_destination=str(data.get("fallback_destination") or "").strip(),
            automation_timeout=float(data.get("timeout", 30)),
        )
    entry = RosterEntry(
        id=entry_id,
        name=name,
        address=address,
        sip_uri=sip_uri,
        extension=extension,
        number=number,
        port=port,
        ha_bridge=bool(data.get("ha_bridge", False) or data.get("type") == "automation"),
        enabled=bool(data.get("enabled", True)),
        metadata=metadata,
    )
    return entry


def build_phonebook_service_handlers(
    refresh_and_push_phonebook: Callable[[object], Awaitable[None]],
) -> dict[str, Callable[[ServiceCall], Awaitable[Any]]]:
    """Build phonebook service handlers with refresh behavior injected."""

    async def add_contact(call: ServiceCall) -> None:
        hass = call.hass
        try:
            entry = contact_from_data(call.data)
        except ContactProfileError as err:
            raise ServiceValidationError(str(err)) from err
        entry_keys = {
            normalize_roster_key(entry.id),
            normalize_roster_key(entry.name),
        }
        entry_keys.discard("")
        entries = [
            item
            for item in manual_roster_entries(hass)
            if not entry_keys
            & {
                normalize_roster_key(getattr(item, "id", "")),
                normalize_roster_key(getattr(item, "name", "")),
            }
        ]
        _validate_contact_namespace(hass, [entry], existing_manual=entries)
        entries.append(entry)
        commands = ()
        if 'ring_group' in call.data:
            from .group_config import contact_group_destination
            commands = ((contact_group_destination(asdict(entry)), call.data['ring_group']),)
            entries[-1] = replace(entry, metadata={key: value for key, value in entry.metadata.items() if key != 'ring_group'})
        _store_administrator_contacts(hass, entries, group_commands=commands)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook contact added: %s", entry.id)

    async def remove_contact(call: ServiceCall) -> None:
        hass = call.hass
        name = str(call.data["name"]).strip()
        wanted = name.lower()
        entries = manual_roster_entries(hass)
        before = len(entries)
        entries = [
            item
            for item in entries
            if getattr(item, "id", "").lower() != wanted
            and getattr(item, "name", "").lower() != wanted
            and getattr(item, "extension", "").lower() != wanted
            and getattr(item, "number", "").lower() != wanted
        ]
        _store_administrator_contacts(hass, entries)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook contact removed: %s (%d removed)", name, before - len(entries))

    async def set_contacts(call: ServiceCall) -> None:
        hass = call.hass
        entries = parse_roster_json(str(call.data.get("roster_json") or "[]"))
        _validate_contact_namespace(hass, entries)
        _store_administrator_contacts(hass, entries)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook manual contacts replaced: %d entries", len(entries))

    async def clear_contacts(call: ServiceCall) -> None:
        hass = call.hass
        _store_administrator_contacts(hass, [])
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook manual contacts cleared")

    async def export_phonebook(call: ServiceCall) -> dict[str, str]:
        roster_json = await _async_directory_json(call.hass)
        _LOGGER.info("Phonebook exported (%d bytes)", len(roster_json))
        return {"roster_json": str(roster_json)}

    async def push_phonebook(call: ServiceCall) -> None:
        hass = call.hass
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("LDAP phone directory refresh requested")

    return {
        "add_contact": add_contact,
        "set_contacts": set_contacts,
        "remove_contact": remove_contact,
        "clear_contacts": clear_contacts,
        "export_phonebook": export_phonebook,
        "push_phonebook": push_phonebook,
    }
