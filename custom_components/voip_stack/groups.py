"""Phonebook group aggregation for HA-anchored SIP routing."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Iterable

from .roster import RosterEntry, normalize_roster_key

_LOGGER = logging.getLogger(__name__)

GROUP_TYPE_CONFERENCE = "conference"
GROUP_TYPE_RING = "ring"


@dataclass(slots=True)
class GroupDef:
    name: str
    group_type: str
    members: list[str] = field(default_factory=list)
    ring_members: list[str] = field(default_factory=list)
    auto: bool = True


def _append_member(group: GroupDef, member: str) -> None:
    if member and member not in group.members:
        group.members.append(member)


def _append_ring_member(group: GroupDef, member: str) -> None:
    _append_member(group, member)
    if member and member not in group.ring_members:
        group.ring_members.append(member)


def _metadata_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _declare(
    groups: dict[str, GroupDef],
    *,
    name: str,
    group_type: str,
    member: str,
    ring: bool = False,
) -> None:
    group_name = (name or "").strip()
    if not group_name or not member:
        return
    key = normalize_roster_key(group_name)
    existing = groups.get(key)
    if existing is not None and existing.group_type != group_type:
        if existing.group_type != GROUP_TYPE_CONFERENCE and group_type == GROUP_TYPE_CONFERENCE:
            _LOGGER.warning("Group %s declared as both ring and conference; conference wins", group_name)
            ring_members = list(existing.members)
            existing.group_type = GROUP_TYPE_CONFERENCE
            existing.members.clear()
            existing.ring_members.clear()
            for ring_member in ring_members:
                _append_ring_member(existing, ring_member)
            (_append_ring_member if ring else _append_member)(existing, member)
        else:
            _LOGGER.warning("Group %s declared as both conference and ring; ignoring ring declaration", group_name)
        return
    if existing is None:
        existing = GroupDef(name=group_name, group_type=group_type)
        groups[key] = existing
    (_append_ring_member if ring else _append_member)(existing, member)


def _entry_group(entry: RosterEntry, key: str) -> str:
    return str((entry.metadata or {}).get(key) or "").strip()


def _group_names(value: object) -> list[str]:
    names: list[str] = []
    for raw in str(value or "").split(","):
        name = raw.strip()
        if name and name not in names:
            names.append(name)
    return names


def _declare_peer(groups: dict[str, GroupDef], peer) -> None:
    member = str(getattr(peer, "name", "") or "").strip()
    for name in _group_names(getattr(peer, "conference_group", "")):
        _declare(
            groups,
            name=name,
            group_type=GROUP_TYPE_CONFERENCE,
            member=member,
            ring=bool(getattr(peer, "conference_ring", False)),
        )
    for name in _group_names(getattr(peer, "ring_group", "")):
        _declare(groups, name=name, group_type=GROUP_TYPE_RING, member=member)


def collect_groups(
    peers,
    manual_entries: Iterable[RosterEntry],
    registered_entries: Iterable[RosterEntry],
    *,
    existing_entries: Iterable[RosterEntry] = (),
) -> dict[str, GroupDef]:
    """Collect auto group definitions from ESP peers and roster metadata."""
    groups: dict[str, GroupDef] = {}
    ha_peers = []
    for peer in peers:
        if bool(getattr(peer, "is_ha", False)):
            ha_peers.append(peer)
            continue
        _declare_peer(groups, peer)
    for entry in list(manual_entries) + list(registered_entries):
        member = entry.id or entry.name
        metadata = entry.metadata or {}
        for name in _group_names(_entry_group(entry, "conference_group")):
            _declare(
                groups,
                name=name,
                group_type=GROUP_TYPE_CONFERENCE,
                member=member,
                ring=_metadata_bool(metadata.get("conference_ring")),
            )
        for name in _group_names(_entry_group(entry, "ring_group")):
            _declare(groups, name=name, group_type=GROUP_TYPE_RING, member=member)

    for peer in ha_peers:
        _declare_peer(groups, peer)

    owners: dict[str, list[RosterEntry]] = {}
    for entry in existing_entries:
        for key in {normalize_roster_key(entry.id), normalize_roster_key(entry.name)}:
            if key and key in groups:
                owners.setdefault(key, []).append(entry)
    for key, entries in owners.items():
        group = groups.pop(key)
        if len(entries) == 1:
            metadata = entries[0].metadata or {}
            group_id = metadata.get("group_id")
            # The saved ring group already publishes this membership declaration.
            if (group_id and group.group_type == GROUP_TYPE_RING
                    and metadata.get("endpoint_id") == f"group:ring:{group_id}"
                    and metadata.get("group_type") == GROUP_TYPE_RING
                    and metadata.get("group_id")):
                continue
        _LOGGER.warning("Skipping group %s because it collides with an existing roster entry", group.name)
    return {group.name: group for group in groups.values()}
