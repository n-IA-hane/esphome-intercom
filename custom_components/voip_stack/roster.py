"""Canonical JSON roster and SIP routing decisions for phase-1 VoIP."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from urllib.parse import unquote
from typing import Any


class RosterError(ValueError):
    """Invalid roster data."""


@dataclass(frozen=True, slots=True)
class RosterEntry:
    id: str
    name: str = ""
    address: str = ""
    sip_uri: str = ""
    extension: str = ""
    number: str = ""
    port: int = 0
    ha_bridge: bool = False
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.id


def _entry_from_mapping(raw: dict[str, Any]) -> RosterEntry:
    entry_id = str(raw.get("id") or raw.get("name") or "").strip()
    if not entry_id:
        raise RosterError("roster entry missing id")
    address = str(raw.get("address") or raw.get("host") or "").strip()
    sip_uri = str(raw.get("sip_uri") or "").strip()
    extension = str(raw.get("extension") or "").strip()
    number = str(raw.get("number") or "").strip()
    metadata = dict(raw.get("metadata") or {})
    port = _parse_port(raw.get("port") or raw.get("sip_port") or metadata.get("port") or metadata.get("sip_port"))
    return RosterEntry(
        id=entry_id,
        name=str(raw.get("name") or entry_id).strip(),
        address=address,
        sip_uri=sip_uri,
        extension=extension,
        number=number,
        port=port,
        ha_bridge=bool(raw.get("ha_bridge", False)),
        enabled=bool(raw.get("enabled", True)),
        metadata=metadata,
    )


def _parse_port(value: Any) -> int:
    try:
        port = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


def parse_roster_json(value: str | bytes | dict[str, Any] | list[dict[str, Any]]) -> list[RosterEntry]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        loaded = json.loads(value or "[]")
    else:
        loaded = value
    if isinstance(loaded, dict):
        entries = loaded.get("contacts") or loaded.get("entries") or []
    else:
        entries = loaded
    if not isinstance(entries, list):
        raise RosterError("roster JSON must contain a list of contacts")
    out = [_entry_from_mapping(item) for item in entries if isinstance(item, dict)]
    ids = [entry.id.lower() for entry in out]
    if len(ids) != len(set(ids)):
        raise RosterError("duplicate roster id")
    return out


def dump_roster_json(entries: list[RosterEntry]) -> str:
    payload = {
        "version": 2,
        "capabilities": ["extension", "ring_group", "conference_group", "conference_ring"],
        "contacts": [
            {
                "id": entry.id,
                "name": entry.name,
                "address": entry.address,
                "sip_uri": entry.sip_uri,
                "extension": entry.extension,
                "number": entry.number,
                "port": entry.port,
                "ha_bridge": entry.ha_bridge,
                "enabled": entry.enabled,
                "metadata": entry.metadata,
            }
            for entry in entries
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def normalize_roster_key(value: str) -> str:
    return "".join(ch for ch in unquote(value).strip().lower() if ch.isalnum())


def entry_matches_extension(entry: RosterEntry, target: str) -> bool:
    return bool(entry.extension) and normalize_roster_key(entry.extension) == normalize_roster_key(target)


def find_entry(
    entries: list[RosterEntry],
    target: str,
    *,
    include_extension: bool = True,
    include_number: bool = True,
) -> RosterEntry | None:
    wanted = normalize_roster_key(target)
    for entry in entries:
        keys = {normalize_roster_key(entry.id), normalize_roster_key(entry.name)}
        if include_extension:
            keys.add(normalize_roster_key(entry.extension))
        keys.discard("")
        if wanted in keys:
            return entry
    if include_number:
        for entry in entries:
            if wanted and wanted == normalize_roster_key(entry.number):
                return entry
    return None


def merge_roster_overrides(entries: list[RosterEntry], overrides: list[RosterEntry]) -> list[RosterEntry]:
    """Apply manual phonebook overlays without duplicating discovered endpoints."""

    merged = list(entries)
    for override in overrides:
        override_keys = {
            normalize_roster_key(override.id),
            normalize_roster_key(override.name),
        }
        override_keys.discard("")
        index = -1
        for pos, entry in enumerate(merged):
            entry_keys = {
                normalize_roster_key(entry.id),
                normalize_roster_key(entry.name),
            }
            entry_keys.discard("")
            if override_keys & entry_keys:
                index = pos
                break
        if index < 0:
            merged.append(override)
            continue
        current = merged[index]
        metadata = dict(current.metadata)
        metadata.update({key: value for key, value in override.metadata.items() if value not in (None, "")})
        merged[index] = RosterEntry(
            id=override.id or current.id,
            name=override.name or current.name,
            address=override.address or current.address,
            sip_uri=override.sip_uri or current.sip_uri,
            extension=override.extension or current.extension,
            number=override.number or current.number,
            port=override.port or current.port,
            ha_bridge=bool(override.ha_bridge or current.ha_bridge),
            enabled=bool(override.enabled and current.enabled),
            metadata=metadata,
        )
    return merged
