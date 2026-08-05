"""HA service handlers for the central VoIP phonebook."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import logging
from typing import Any

from homeassistant.core import ServiceCall

from .config import assist_config
from .config_validation import route_namespace_conflicts
from .const import (
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_EXTENSION,
)
from .phonebook_runtime import push_roster_json_to_esps
from .runtime_data import endpoint_directory, runtime_data
from .roster import RosterEntry, normalize_roster_key, parse_roster_json
from .store import manual_roster_entries, store_manual_roster_entries

_LOGGER = logging.getLogger(__name__)


def _entry_mapping(entry: RosterEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.name,
        "extension": entry.extension,
        "number": entry.number,
        "metadata": dict(entry.metadata or {}),
    }


def _runtime_route_mappings(hass: Any) -> list[Mapping[str, Any]]:
    mappings: list[Mapping[str, Any]] = []
    registry = endpoint_directory(hass)
    mappings.extend(
        {
            "id": endpoint.endpoint_id,
            "name": endpoint.name,
            "extension": endpoint.extension,
            "username": endpoint.username,
            "ring_group": endpoint.ring_group,
            "conference_group": endpoint.conference_group,
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


def build_phonebook_service_handlers(
    refresh_and_push_phonebook: Callable[[object], Awaitable[None]],
) -> dict[str, Callable[[ServiceCall], Awaitable[Any]]]:
    """Build phonebook service handlers with refresh behavior injected."""

    async def add_contact(call: ServiceCall) -> None:
        hass = call.hass
        name = str(call.data["name"]).strip()
        entry_id = str(call.data.get("id") or name).strip()

        def metadata_value(key: str):
            value = call.data.get(key)
            if value in (None, ""):
                return None
            return value

        metadata = {
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
            if key in call.data and metadata_value(key) is not None
        }
        address = str(call.data.get("address") or "").strip()
        sip_uri = str(call.data.get("sip_uri") or "").strip()
        extension = str(call.data.get("extension") or "").strip()
        number = str(call.data.get("number") or "").strip()
        port = int(call.data.get("port") or 0)
        entry = RosterEntry(
            id=entry_id,
            name=name,
            address=address,
            sip_uri=sip_uri,
            extension=extension,
            number=number,
            port=port,
            ha_bridge=bool(call.data.get("ha_bridge", False)),
            metadata=metadata,
        )
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
        store_manual_roster_entries(hass, entries)
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
        store_manual_roster_entries(hass, entries)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook contact removed: %s (%d removed)", name, before - len(entries))

    async def set_contacts(call: ServiceCall) -> None:
        hass = call.hass
        entries = parse_roster_json(str(call.data.get("roster_json") or "[]"))
        _validate_contact_namespace(hass, entries)
        store_manual_roster_entries(hass, entries)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook manual contacts replaced: %d entries", len(entries))

    async def clear_contacts(call: ServiceCall) -> None:
        hass = call.hass
        store_manual_roster_entries(hass, [])
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook manual contacts cleared")

    async def export_phonebook(call: ServiceCall) -> dict[str, str]:
        hass = call.hass
        runtime = runtime_data(hass)
        sensor = runtime.phonebook_sensor if runtime is not None else None
        if sensor is not None:
            await sensor.async_update()
            roster_json = sensor.extra_state_attributes.get("roster_json", "")
        else:
            roster_json = ""
        _LOGGER.info("Phonebook exported (%d bytes)", len(roster_json))
        return {"roster_json": str(roster_json)}

    async def push_phonebook(call: ServiceCall) -> None:
        hass = call.hass
        runtime = runtime_data(hass)
        sensor = runtime.phonebook_sensor if runtime is not None else None
        if sensor is not None:
            await sensor.async_update()
            roster_json = sensor.extra_state_attributes.get("roster_json", "")
        else:
            state = hass.states.get("sensor.voip_phonebook")
            roster_json = str(state.attributes.get("roster_json") or "") if state is not None else ""
        await push_roster_json_to_esps(hass, roster_json)
        _LOGGER.info("Phonebook push requested (%d bytes)", len(roster_json))

    return {
        "add_contact": add_contact,
        "set_contacts": set_contacts,
        "remove_contact": remove_contact,
        "clear_contacts": clear_contacts,
        "export_phonebook": export_phonebook,
        "push_phonebook": push_phonebook,
    }
