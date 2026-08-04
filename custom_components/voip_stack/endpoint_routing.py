"""Pure routing helpers used by the HA SIP endpoint."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging

from homeassistant.core import HomeAssistant

from .audio_format import (
    AudioFormat,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    parse_audio_format_list,
)
from .config import assist_config, trunk_config, trunk_enabled
from .const import DOMAIN
from .peer import Peer
from .phone_endpoint import DEFAULT_ENDPOINT_ID
from .router import resolve_ha_router
from .store import manual_roster_entries

_LOGGER = logging.getLogger(__name__)


def same_route_name(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    return bool(left and right and norm(left) == norm(right))


def is_ha_target(hass: HomeAssistant, value: str) -> bool:
    """Return whether a dial token names the configured HA browser phone."""

    # Kept deferred because websocket_api imports routing helpers during setup.
    from .websocket_api import _ha_peer_name

    return same_route_name(value, _ha_peer_name(hass)) or same_route_name(value, "ha")


def ha_router_decision(hass: HomeAssistant, target: str, entries: list):
    """Resolve one target against the canonical HA dial plan."""

    trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
    configured_trunk = trunk_config(hass)
    trunk_ready = trunk_enabled(configured_trunk) and bool(
        getattr(trunk, "registered", False)
    )
    return resolve_ha_router(target, entries, trunk_ready=trunk_ready)


def is_local_listener_uri(uri, *, local_ip: str, sip_port: int) -> bool:
    """Return whether a parsed SIP URI points back to the current listener."""

    return bool(
        uri is not None
        and uri.host == local_ip
        and int(uri.port or sip_port) == int(sip_port)
    )


def logical_endpoint_for_member(
    hass: HomeAssistant,
    member: str,
    peers: list[Peer],
    entries: list,
):
    """Resolve a dial-plan member to its transport-independent endpoint."""

    endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    if endpoint_registry is None:
        return None
    entry = next(
        (
            candidate
            for candidate in entries
            if same_route_name(member, getattr(candidate, "id", ""))
            or same_route_name(member, getattr(candidate, "name", ""))
            or (
                bool(getattr(candidate, "extension", ""))
                and str(getattr(candidate, "extension", "")).strip()
                == str(member).strip()
            )
        ),
        None,
    )
    endpoint_id = str(
        ((getattr(entry, "metadata", None) if entry is not None else {}) or {}).get(
            "endpoint_id"
        )
        or ""
    ).strip()
    if not endpoint_id:
        peer = next(
            (
                candidate
                for candidate in peers
                if same_route_name(member, candidate.name)
            ),
            None,
        )
        endpoint_id = str(getattr(peer, "endpoint_id", "") or "").strip()
    if not endpoint_id and is_ha_target(hass, member):
        endpoint_id = DEFAULT_ENDPOINT_ID
    return endpoint_registry.get(endpoint_id) if endpoint_id else None


@dataclass(frozen=True, slots=True)
class EndpointRouteResolver:
    """Runtime-bound routing helpers shared by endpoint orchestrators."""

    hass: HomeAssistant
    local_ip: str
    sip_port: int

    def is_ha_target(self, value: str) -> bool:
        return is_ha_target(self.hass, value)

    def route(self, target: str, entries: list):
        return ha_router_decision(self.hass, target, entries)

    def is_local_listener_uri(self, uri) -> bool:
        return is_local_listener_uri(
            uri,
            local_ip=self.local_ip,
            sip_port=self.sip_port,
        )

    def logical_endpoint(self, member: str, peers: list[Peer], entries: list):
        return logical_endpoint_for_member(self.hass, member, peers, entries)


def peer_for_target(target: str, peers: list[Peer]) -> Peer | None:
    for peer in peers:
        if peer.is_ha:
            continue
        if any(
            same_route_name(target, candidate)
            for candidate in (peer.name, peer.sip_uri_user, peer.extension)
        ):
            return peer
    return None


def peer_audio_formats(peer: Peer | None, key: str) -> list[AudioFormat]:
    if peer is None:
        return []
    raw = ";".join(str(item) for item in (peer.tx_formats if key == "tx_formats" else peer.rx_formats) or [])
    if not raw.strip():
        return []
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning("Ignoring invalid peer %s on %s: %s", key, peer.name, err)
        return []


def peer_video_codec(peer: Peer | None, entry=None) -> str:
    """Return one explicitly advertised SIP video codec for a destination."""

    value = str(
        ((peer.device or {}).get("sip_video_codec") if peer is not None else "")
        or ((getattr(entry, "metadata", None) or {}).get("sip_video_codec"))
        or ""
    ).strip().casefold()
    return value if value in {"h264", "jpeg"} else ""


def device_formats(device: dict | None, key: str) -> list[AudioFormat]:
    if not device:
        return []
    value = device.get(key)
    if value in (None, ""):
        return []
    raw = value if isinstance(value, str) else ";".join(value or [])
    if not raw.strip():
        return []
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid %s on %s: %s",
            key,
            (device or {}).get("name") or (device or {}).get("device_id"),
            err,
        )
        return []


def roster_entry_formats(entry, key: str) -> list[AudioFormat]:
    """Return audio formats from a canonical roster entry metadata field."""
    if entry is None:
        return []
    metadata = getattr(entry, "metadata", {}) or {}
    value = metadata.get(key)
    if value in (None, ""):
        return []
    raw = ";".join(str(item) for item in value) if isinstance(value, list) else str(value or "")
    if not raw.strip():
        return []
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid roster %s on %s: %s",
            key,
            getattr(entry, "display_name", None) or getattr(entry, "id", ""),
            err,
        )
        return []


def sip_target_audio_profile(
    *,
    remote_tx_formats: list[AudioFormat] | None,
    remote_rx_formats: list[AudioFormat] | None,
    target: str,
) -> tuple[list[AudioFormat], list[AudioFormat]]:
    """Constrain HA SIP offers to formats that can actually work with target."""
    remote_tx = list(remote_tx_formats or [])
    remote_rx = list(remote_rx_formats or [])
    send_candidates = (
        [fmt for fmt in HA_SIP_PCM_TX_FORMATS if fmt in set(remote_rx)]
        if remote_rx else list(HA_SIP_PCM_TX_FORMATS)
    )
    recv_candidates = (
        [fmt for fmt in HA_SIP_PCM_RX_FORMATS if fmt in set(remote_tx)]
        if remote_tx else list(HA_SIP_PCM_RX_FORMATS)
    )
    if not send_candidates or not recv_candidates:
        _LOGGER.warning(
            "No compatible directional SIP PCM profile for %s "
            "(ha_send=%s ha_recv=%s remote_tx=%s remote_rx=%s)",
            target,
            [fmt.wire_token() for fmt in HA_SIP_PCM_TX_FORMATS],
            [fmt.wire_token() for fmt in HA_SIP_PCM_RX_FORMATS],
            [fmt.wire_token() for fmt in remote_tx],
            [fmt.wire_token() for fmt in remote_rx],
        )
        return [], []

    common_formats = set(send_candidates) & set(recv_candidates)
    if not common_formats:
        _LOGGER.warning(
            "No bidirectional SIP RTP format for %s (send=%s recv=%s)",
            target,
            [fmt.wire_token() for fmt in send_candidates],
            [fmt.wire_token() for fmt in recv_candidates],
        )
        return [], []

    # A single RFC 3264 sendrecv m=audio cannot assign one payload contract
    # to TX and another to RX. Keep direction-specific preference ordering,
    # but expose only formats supported on both sides of this dialog leg.
    send_candidates = [fmt for fmt in send_candidates if fmt in common_formats]
    recv_candidates = [fmt for fmt in recv_candidates if fmt in common_formats]
    _LOGGER.debug(
        "Bidirectional SIP PCM profile for %s: send=%s recv=%s",
        target,
        [fmt.wire_token() for fmt in send_candidates],
        [fmt.wire_token() for fmt in recv_candidates],
    )
    return send_candidates, recv_candidates


def roster_from_peers(hass: HomeAssistant, peers: list[Peer], registered_entries) -> list:
    from .const import CONF_ASSIST_ENDPOINT_ENABLED, CONF_ASSIST_EXTENSION, DOMAIN
    from .groups import collect_groups
    from .roster import RosterEntry, merge_roster_overrides

    entries: list[RosterEntry] = []
    for peer in peers:
        entries.append(
            RosterEntry(
                id=peer.name,
                name=peer.name,
                address=peer.host,
                extension=peer.extension,
                port=int(peer.sip_port or 0),
                metadata={
                    "local_ha": bool(peer.is_ha),
                    "endpoint_id": peer.endpoint_id,
                    "endpoint_kind": peer.endpoint_kind,
                    "device_id": peer.device_id or "",
                    "capabilities": list(peer.capabilities),
                    "camera_entity_id": str(
                        (peer.device or {}).get("camera_entity_id") or ""
                    ),
                    "sip_video_codec": str(
                        (peer.device or {}).get("sip_video_codec") or ""
                    ),
                    "sip_transport": (
                        str((peer.device or {}).get("sip_transport") or "tcp").lower()
                        if peer.is_ha or peer.device is not None
                        else ""
                    ),
                    "sip_port": peer.sip_port,
                    "rtp_port": peer.rtp_port,
                    "audio_mode": peer.audio_mode,
                    "tx_formats": list(peer.tx_formats or []),
                    "rx_formats": list(peer.rx_formats or []),
                    "conference_group": peer.conference_group,
                    "conference_ring": bool(peer.conference_ring),
                    "ring_group": peer.ring_group,
                },
            )
        )
    manual_entries = manual_roster_entries(hass)
    entries = merge_roster_overrides(entries, manual_entries)
    endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    registered_endpoint_ids: set[str] = set()
    for registered in registered_entries:
        endpoint = None
        if endpoint_registry is not None:
            endpoint = endpoint_registry.by_username(registered.id)
        metadata = dict(registered.metadata or {})
        if endpoint is not None:
            registered_endpoint_ids.add(endpoint.endpoint_id)
            metadata.update(
                {
                    "endpoint_id": endpoint.endpoint_id,
                    "endpoint_kind": endpoint.kind.value,
                    "device_id": endpoint.device_id,
                    "capabilities": sorted(endpoint.capabilities),
                    "registered": True,
                }
            )
        entries.append(replace(registered, metadata=metadata))
    assist = assist_config(hass)
    if assist.get(CONF_ASSIST_ENDPOINT_ENABLED) and assist.get(CONF_ASSIST_EXTENSION):
        extension = assist[CONF_ASSIST_EXTENSION]
        name = str(assist.get("name") or "Assist").strip() or "Assist"
        entries.append(
            RosterEntry(
                id=name,
                name=name,
                extension=extension,
                ha_bridge=True,
                metadata={"virtual_endpoint": "assist_pipeline"},
            )
        )
    groups = collect_groups(peers, manual_entries, registered_entries, existing_entries=entries)
    for group in groups.values():
        entries.append(
            RosterEntry(
                id=group.name,
                name=group.name,
                ha_bridge=True,
                enabled=bool(group.members),
                metadata={
                    "group_type": group.group_type,
                    "members": list(group.members),
                    "ring_members": list(group.ring_members),
                    "auto": bool(group.auto),
                },
            )
        )
    return entries
