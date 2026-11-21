"""Transport-independent PBX routing primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .endpoint_routing import peer_for_target, same_route_name
from .phone_endpoint import EndpointAvailability

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry


def unique_group_members(value: object) -> list[str]:
    """Return ordered, case-insensitively unique dial-group members."""

    members: list[str] = []
    seen: set[str] = set()
    raw_members = value.split(",") if isinstance(value, str) else (value or [])
    for raw in raw_members:
        member = str(raw).strip()
        key = member.casefold()
        if member and key not in seen:
            seen.add(key)
            members.append(member)
    return members


def dtmf_extension_routes(entries: list[RosterEntry]) -> dict[str, str]:
    """Build exact extension routes accepted by the trunk DTMF collector."""

    return {
        str(entry.extension).strip(): str(entry.extension).strip()
        for entry in entries
        if str(getattr(entry, "extension", "") or "").strip()
    }


def roster_entry_for_target(
    target: str,
    entries: list[RosterEntry],
) -> RosterEntry | None:
    """Resolve a public dial target through the canonical directory rules."""
    from .roster import find_entry

    return find_entry(entries, target, include_number=False)


def browser_endpoint_can_ring(endpoint: object | None) -> bool:
    """Return whether a logical browser phone may enter ringing state.

    Browser presence is media availability, not phone existence. Offline
    logical phones remain routable so automations can observe ringing and a
    dashboard can reconnect during the ring window. DND and administratively
    unavailable phones are excluded.
    """

    return bool(
        endpoint is None
        or (
            not getattr(endpoint, "dnd", False)
            and getattr(endpoint, "availability", None)
            is not EndpointAvailability.UNAVAILABLE
        )
    )


def caller_matches_group_member(
    caller: str,
    source_host: str,
    member: str,
    peers: list[Peer],
    *,
    source_endpoint_id: str = "",
) -> bool:
    """Return whether a group member is the originating endpoint.

    SIP identity wins over transport address. The host fallback is retained
    only for one uniquely identified legacy peer, because multiple phones may
    legitimately share an address through NAT.
    """

    if same_route_name(member, caller):
        return True
    peer = peer_for_target(member, peers)
    if source_endpoint_id:
        return bool(
            peer is not None
            and peer.endpoint_id
            and peer.endpoint_id == source_endpoint_id
        )
    if peer is None or not peer.host or peer.host != source_host:
        return False
    return sum(1 for candidate in peers if candidate.host == source_host) == 1
