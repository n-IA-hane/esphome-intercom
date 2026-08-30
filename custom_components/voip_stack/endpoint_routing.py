"""Pure routing helpers used by the HA SIP endpoint."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging

from .roster import normalize_roster_key

from homeassistant.core import HomeAssistant

from .core.audio_format import (
    AudioFormat,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    choose_common_frame_ms,
    parse_audio_format_list,
)
from .core import sdp
from .core.codec_capabilities import common_sip_codecs
from .config import assist_config, trunk_config, trunk_enabled
from .peer import Peer
from .router import resolve_ha_router
from .runtime_data import endpoint_directory, preferred_browser_phone, sip_trunk
from .store import manual_roster_entries

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SipAudioCapabilityProfile:
    """One endpoint's ordered, directional RTP wire capabilities."""

    send_formats: tuple[AudioFormat, ...]
    recv_formats: tuple[AudioFormat, ...]
    send_rtp_formats: tuple[sdp.RtpPcmFormat, ...]
    recv_rtp_formats: tuple[sdp.RtpPcmFormat, ...]
    sdp_features: frozenset[str]


def _rtp_capability_tokens(
    peer: Peer | None,
    entry,
    direction: str,
    device: dict | None = None,
) -> list[str]:
    field = f"sip_audio_{direction}_formats"
    if peer is not None:
        direct = getattr(peer, field, ())
        if direct:
            return [str(item) for item in direct]
        device = peer.device or {}
        if device.get(field):
            return [str(item) for item in device[field]]
    metadata = dict(getattr(entry, "metadata", None) or {})
    value = metadata.get(field) or (device or {}).get(field) or ()
    return [str(item) for item in value]


def _endpoint_sdp_features(
    peer: Peer | None,
    entry,
    device: dict | None = None,
) -> frozenset[str]:
    values: set[str] = set()
    if peer is not None:
        values.update(str(item).casefold() for item in peer.sdp_features)
        values.update(
            str(item).casefold()
            for item in (peer.device or {}).get("sdp_features", ())
        )
    values.update(
        str(item).casefold()
        for item in (getattr(entry, "metadata", None) or {}).get(
            "sdp_features", ()
        )
    )
    values.update(
        str(item).casefold()
        for item in (device or {}).get("sdp_features", ())
    )
    return frozenset(values & {"directional_audio_v1"})


def _assign_capability_payloads(
    tokens: list[str],
    payloads: dict[tuple[str, int, int, int], int] | None = None,
) -> tuple[sdp.RtpPcmFormat, ...]:
    available = common_sip_codecs()
    payloads = payloads if payloads is not None else {}
    used: set[int] = set(payloads.values())
    dynamic = 96
    out: list[sdp.RtpPcmFormat] = []
    seen: set[tuple[str, int, int, int]] = set()
    static_payloads = {"PCMU": 0, "PCMA": 8, "G722": 9, "OPUS": 98}
    for token in tokens:
        try:
            parsed = sdp.parse_rtp_audio_capability(token)
        except sdp.SdpError as err:
            _LOGGER.warning("Ignoring invalid SIP RTP capability %r: %s", token, err)
            continue
        encoding = parsed.encoding.upper()
        if encoding in {"OPUS", "G722"} and encoding not in available:
            continue
        key = (encoding, parsed.sample_rate, parsed.channels, parsed.frame_ms)
        if key in seen:
            continue
        seen.add(key)
        payload = payloads.get(key)
        if payload is None:
            payload = static_payloads.get(encoding)
        if payload is None or (payload in used and payloads.get(key) != payload):
            while dynamic in used or dynamic in static_payloads.values():
                dynamic += 1
            payload = dynamic
            dynamic += 1
        used.add(payload)
        payloads[key] = payload
        out.append(
            sdp.RtpPcmFormat(
                payload,
                encoding,
                parsed.sample_rate,
                parsed.channels,
                parsed.frame_ms,
                fmtp=parsed.fmtp,
            )
        )
    return tuple(out)


def sip_target_rtp_audio_profile(
    peer: Peer | None,
    entry,
    device: dict | None = None,
) -> SipAudioCapabilityProfile | None:
    """Build the HA-side profile from explicit RTP or legacy PCM metadata."""

    remote_tx_tokens = _rtp_capability_tokens(peer, entry, "tx", device)
    remote_rx_tokens = _rtp_capability_tokens(peer, entry, "rx", device)
    shared_payloads: dict[tuple[str, int, int, int], int] = {}
    remote_tx = _assign_capability_payloads(remote_tx_tokens, shared_payloads)
    remote_rx = _assign_capability_payloads(remote_rx_tokens, shared_payloads)
    if not remote_tx and not remote_tx_tokens:
        legacy_tx = peer_audio_formats(peer, "tx_formats") or roster_entry_formats(
            entry, "tx_formats"
        )
        remote_tx = tuple(
            sdp.audio_format_to_rtp(fmt, 96 + index)
            for index, fmt in enumerate(legacy_tx)
            if sdp.is_rtp_pcm_mappable(fmt)
        )
    if not remote_rx and not remote_rx_tokens:
        legacy_rx = peer_audio_formats(peer, "rx_formats") or roster_entry_formats(
            entry, "rx_formats"
        )
        remote_rx = tuple(
            sdp.audio_format_to_rtp(fmt, 112 + index)
            for index, fmt in enumerate(legacy_rx)
            if sdp.is_rtp_pcm_mappable(fmt)
        )
    if not remote_tx or not remote_rx:
        return None
    # Remote receive capabilities are HA's send capabilities and vice versa.
    send_rtp = tuple(remote_rx)
    recv_rtp = tuple(remote_tx)
    return SipAudioCapabilityProfile(
        send_formats=tuple(fmt.audio_format for fmt in send_rtp),
        recv_formats=tuple(fmt.audio_format for fmt in recv_rtp),
        send_rtp_formats=send_rtp,
        recv_rtp_formats=recv_rtp,
        sdp_features=_endpoint_sdp_features(peer, entry, device),
    )


def same_route_name(left: str, right: str) -> bool:
    return bool(
        left
        and right
        and normalize_roster_key(left) == normalize_roster_key(right)
    )


def is_ha_target(hass: HomeAssistant, value: str) -> bool:
    """Return whether a dial token names the configured HA browser phone."""

    # Kept deferred because websocket_api imports routing helpers during setup.
    from .websocket_api import _ha_peer_name

    return same_route_name(value, _ha_peer_name(hass)) or same_route_name(value, "ha")


def ha_router_decision(hass: HomeAssistant, target: str, entries: list):
    """Resolve one target against the canonical HA dial plan."""

    trunk = sip_trunk(hass)
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

    endpoint_registry = endpoint_directory(hass)
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
        endpoint = preferred_browser_phone(hass)
        endpoint_id = endpoint.endpoint_id if endpoint is not None else ""
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


def peer_video_codec(peer: Peer | None, entry=None) -> str | None:
    """Return the destination video contract.

    A codec names an explicitly video-capable peer, an empty string names a
    known audio-only peer, and ``None`` preserves generic SIP negotiation when
    the peer did not advertise capabilities.
    """

    metadata = (getattr(entry, "metadata", None) or {}) if entry is not None else {}
    device = (peer.device or {}) if peer is not None else {}
    value = str(
        device.get("sip_video_codec")
        or metadata.get("sip_video_codec")
        or ""
    ).strip().casefold()
    if value in {"h264", "jpeg"}:
        return value

    endpoint_kind = str(
        (getattr(peer, "endpoint_kind", "") if peer is not None else "")
        or metadata.get("endpoint_kind")
        or ""
    ).strip().casefold()
    capabilities = {
        str(item).strip().casefold()
        for item in (
            (getattr(peer, "capabilities", ()) if peer is not None else ())
            or metadata.get("capabilities")
            or ()
        )
        if str(item).strip()
    }
    if endpoint_kind == "esphome" or capabilities:
        return "" if "video" not in capabilities else None
    return None


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

    common_frame_ms = choose_common_frame_ms(send_candidates, recv_candidates)
    if common_frame_ms is None:
        _LOGGER.warning(
            "No common SIP RTP packet time for %s (send=%s recv=%s)",
            target,
            [fmt.wire_token() for fmt in send_candidates],
            [fmt.wire_token() for fmt in recv_candidates],
        )
        return [], []

    send_candidates = [
        fmt for fmt in send_candidates if fmt.frame_ms == common_frame_ms
    ]
    recv_candidates = [
        fmt for fmt in recv_candidates if fmt.frame_ms == common_frame_ms
    ]
    _LOGGER.debug(
        "Directional SIP PCM profile for %s: ptime=%sms send=%s recv=%s",
        target,
        common_frame_ms,
        [fmt.wire_token() for fmt in send_candidates],
        [fmt.wire_token() for fmt in recv_candidates],
    )
    return send_candidates, recv_candidates


def supports_directional_audio_payloads(peer: Peer | None, entry) -> bool:
    """Return whether the destination implements the ESP directional SDP profile."""
    return "directional_audio_v1" in _endpoint_sdp_features(peer, entry)


def roster_from_peers(hass: HomeAssistant, peers: list[Peer], registered_entries) -> list:
    from .const import CONF_ASSIST_ENDPOINT_ENABLED, CONF_ASSIST_EXTENSION
    from .groups import collect_groups
    from .roster import RosterEntry, merge_roster_overrides

    entries: list[RosterEntry] = []
    for peer in peers:
        entries.append(
            RosterEntry(
                id=peer.sip_uri_user or peer.name,
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
                    "sip_audio_tx_formats": list(peer.sip_audio_tx_formats),
                    "sip_audio_rx_formats": list(peer.sip_audio_rx_formats),
                    "sdp_features": sorted(peer.sdp_features),
                    "conference_group": peer.conference_group,
                    "conference_ring": bool(peer.conference_ring),
                    "ring_group": peer.ring_group,
                },
            )
        )
    manual_entries = manual_roster_entries(hass)
    entries = merge_roster_overrides(entries, manual_entries)
    endpoint_registry = endpoint_directory(hass)
    registered_endpoint_ids: set[str] = set()
    for registered in registered_entries:
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
