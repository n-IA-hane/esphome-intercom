"""Pure routing helpers used by the HA SIP endpoint."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging

from homeassistant.core import HomeAssistant

from .trunk_policy import trunk_available
from .core.audio_format import (
    AudioFormat,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    choose_common_frame_ms,
    parse_audio_format_list,
)
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES, local_rtp_audio_formats
from .core import sdp
from .config import assist_config, trunk_config, trunk_enabled
from .peer import Peer
from .router import resolve_ha_router
from .runtime_data import endpoint_directory, preferred_browser_phone, sip_trunk, runtime_data
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

    @classmethod
    def from_rtp(
        cls,
        send_rtp_formats: tuple[sdp.RtpPcmFormat, ...],
        recv_rtp_formats: tuple[sdp.RtpPcmFormat, ...],
        sdp_features: frozenset[str] = frozenset(),
    ) -> SipAudioCapabilityProfile:
        """Keep local PCM selection independent of advisory RTP offer order."""
        return cls(
            send_formats=tuple(HA_AUDIO_CAPABILITIES.expand(
                list(HA_SIP_PCM_TX_FORMATS), list(send_rtp_formats),
            )),
            recv_formats=tuple(HA_AUDIO_CAPABILITIES.expand(
                list(HA_SIP_PCM_RX_FORMATS), list(recv_rtp_formats), receive=True,
            )),
            send_rtp_formats=send_rtp_formats,
            recv_rtp_formats=recv_rtp_formats,
            sdp_features=sdp_features,
        )


def _rtp_capability_tokens(
    peer: Peer | None,
    entry,
    direction: str,
    device: dict | None = None,
) -> list[str]:
    field = f"sip_audio_{direction}_formats"
    metadata = dict(getattr(entry, "metadata", None) or {})
    if metadata.get("sip_capabilities_known"):
        return [str(item) for item in metadata.get(field, ())]
    if peer is not None:
        direct = getattr(peer, field, ())
        if direct:
            return [str(item) for item in direct]
        device = peer.device or {}
        if device.get(field):
            return [str(item) for item in device[field]]
    value = metadata.get(field) or (device or {}).get(field) or ()
    return [str(item) for item in value]


def _endpoint_sdp_features(
    peer: Peer | None,
    entry,
    device: dict | None = None,
) -> frozenset[str]:
    metadata = dict(getattr(entry, "metadata", None) or {})
    known = {"directional_audio_v1", "independent_audio_ptime_v1"}
    if metadata.get("sip_capabilities_known"):
        return frozenset(str(item).casefold() for item in metadata.get("sdp_features", ())) & known
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
    return frozenset(values & known)


def _assign_capability_payloads(
    formats: tuple[sdp.RtpPcmFormat, ...],
    payloads: dict[tuple[str, int, int], int] | None = None,
) -> tuple[sdp.RtpPcmFormat, ...]:
    """Share one payload mapping between local send and receive capabilities."""
    payloads = payloads if payloads is not None else {}
    used: set[int] = set(payloads.values())
    dynamic = 96
    out: list[sdp.RtpPcmFormat] = []
    seen: set[tuple[str, int, int, int]] = set()
    preferred_payloads = {
        ("PCMU", 8000, 1): 0,
        ("PCMA", 8000, 1): 8,
        ("G722", 8000, 1): 9,
        ("OPUS", 48000, 2): 98,
    }
    for fmt in formats:
        key = (fmt.encoding.upper(), fmt.sample_rate, fmt.channels)
        wire_key = (*key, fmt.frame_ms)
        if wire_key in seen:
            continue
        seen.add(wire_key)
        payload = payloads.get(key)
        if payload is None:
            payload = preferred_payloads.get(key)
        if payload is None or (payload in used and payloads.get(key) != payload):
            while dynamic in used or dynamic in preferred_payloads.values():
                dynamic += 1
            if dynamic > 127:
                raise sdp.SdpError("No RTP payload type available for local audio capabilities")
            payload = dynamic
            dynamic += 1
        used.add(payload)
        payloads[key] = payload
        out.append(replace(fmt, payload_type=payload))
    return tuple(out)


def _rtp_audio_hints(
    tokens: list[str], legacy_formats: list[AudioFormat]
) -> list[sdp.RtpPcmFormat]:
    """Parse advisory preferences without treating them as local capabilities."""
    hints = []
    for token in tokens:
        try:
            hints.append(sdp.parse_rtp_audio_capability(token))
        except sdp.SdpError as err:
            _LOGGER.warning("Ignoring invalid SIP RTP capability %r: %s", token, err)
    if not tokens:
        encodings = {"s16le": "L16", "s24le": "L24", "s24le_in_s32": "L24"}
        hints.extend(
            sdp.RtpPcmFormat(96, encodings[fmt.pcm_format], fmt.sample_rate,
                             fmt.channels, fmt.frame_ms)
            for fmt in legacy_formats if fmt.pcm_format in encodings
        )
    return hints


def sip_target_has_unspecified_audio(
    peer: Peer | None, entry, device: dict | None = None
) -> bool:
    """Unregistered SIP targets need negotiation, not an implicit PCM-only policy."""
    metadata = dict(getattr(entry, "metadata", None) or {})
    info = dict(device or (peer.device if peer is not None else None) or {})
    kind = str(
        (peer.endpoint_kind if peer is not None else "")
        or metadata.get("endpoint_kind")
        or info.get("endpoint_kind")
        or ""
    )
    if kind in {"browser", "assist"}:
        return False
    return not any(
        _rtp_capability_tokens(peer, entry, direction, device)
        or peer_audio_formats(peer, direction + "_formats")
        or roster_entry_formats(entry, direction + "_formats")
        or device_formats(device, direction + "_formats")
        for direction in ("tx", "rx")
    )


def sip_target_offer_common_audio(
    peer: Peer | None,
    entry,
    device: dict | None = None,
) -> bool:
    """Keep backend codec alternatives available for authoritative SIP SDP."""
    return True


def sip_target_rtp_audio_profile(
    peer: Peer | None,
    entry,
    device: dict | None = None,
) -> SipAudioCapabilityProfile | None:
    """Order local backend capabilities using advisory remote preferences."""

    remote_tx_tokens = _rtp_capability_tokens(peer, entry, "tx", device)
    remote_rx_tokens = _rtp_capability_tokens(peer, entry, "rx", device)
    metadata = dict(getattr(entry, "metadata", None) or {})
    legacy_tx = [] if metadata.get("sip_capabilities_known") else (
        peer_audio_formats(peer, "tx_formats") or roster_entry_formats(entry, "tx_formats")
        or device_formats(device, "tx_formats")
    )
    legacy_rx = [] if metadata.get("sip_capabilities_known") else (
        peer_audio_formats(peer, "rx_formats") or roster_entry_formats(entry, "rx_formats")
        or device_formats(device, "rx_formats")
    )
    # Remote RX preferences rank HA's transmitter; remote TX ranks our receiver.
    shared_payloads: dict[tuple[str, int, int], int] = {}
    send_rtp = _assign_capability_payloads(local_rtp_audio_formats(
        list(HA_SIP_PCM_TX_FORMATS), hints=_rtp_audio_hints(remote_rx_tokens, legacy_rx),
    ), shared_payloads)
    recv_rtp = _assign_capability_payloads(local_rtp_audio_formats(
        list(HA_SIP_PCM_RX_FORMATS), hints=_rtp_audio_hints(remote_tx_tokens, legacy_tx),
        receive=True,
    ), shared_payloads)
    return SipAudioCapabilityProfile.from_rtp(
        send_rtp_formats=send_rtp,
        recv_rtp_formats=recv_rtp,
        sdp_features=_endpoint_sdp_features(peer, entry, device),
    )


def bridge_audio_profile(
    invite,
    send_formats: list[AudioFormat],
    recv_formats: list[AudioFormat],
    profile: SipAudioCapabilityProfile | None,
    *,
    include_common_codecs: bool,
) -> SipAudioCapabilityProfile:
    """Prefer the caller's wire audio while retaining destination fallbacks.

    Discovery ranks the backend's capabilities. Bridge preferences retain
    those alternatives and the destination's independent receive packet time.
    """
    if profile is None:
        frame_ms = choose_common_frame_ms(send_formats, recv_formats)
        formats = sdp.audio_offer_formats(
            [fmt for fmt in [*send_formats, *recv_formats] if fmt.frame_ms == frame_ms],
            capability_formats=[*send_formats, *recv_formats],
            include_common_codecs=include_common_codecs,
            preferred_frame_ms=frame_ms,
        )
        send = tuple(fmt for fmt in formats if fmt.audio_format in send_formats)
        recv = tuple(fmt for fmt in formats if fmt.audio_format in recv_formats)
        profile = SipAudioCapabilityProfile.from_rtp(send, recv)

    def prefer(formats, source):
        # Favor a passthrough codec while retaining the destination's ordered
        # packet times. The other dialog leg cannot own this receiver's ptime.
        return tuple(sorted(formats, key=lambda fmt:
            (fmt.encoding.upper(), fmt.sample_rate, fmt.channels)
            != (source.encoding.upper(), source.sample_rate, source.channels)
        ))

    send = prefer(profile.send_rtp_formats, invite.recv_format)
    # Keep the destination transmitter's preferences for our receiver.
    # A backend fallback must not override that order using another leg.
    recv = profile.recv_rtp_formats
    # SDP's advertised preference does not remove the decoder/encoder quanta
    # needed for a legal answer (for example G.722 with a 20 ms packet time).
    return replace(profile, send_rtp_formats=send, recv_rtp_formats=recv)


def same_route_name(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    return bool(left and right and norm(left) == norm(right))


def is_ha_target(hass: HomeAssistant, value: str) -> bool:
    """Return whether a dial token names the configured HA browser phone."""

    # Kept deferred because websocket_api imports routing helpers during setup.
    from .websocket_api import _ha_peer_name

    return same_route_name(value, _ha_peer_name(hass)) or same_route_name(value, "ha")


def ha_router_decision(hass: HomeAssistant, target: str, entries: list, *, source_endpoint_id: str = "", trunk_id: str = ""):
    """Resolve one target against the canonical HA dial plan."""

    from .parking import resolve_parking_route
    parking = resolve_parking_route(hass, target)
    if parking is not None:
        return parking

    if str(target or "").strip().casefold().startswith(("voicemail.", "mailbox", "*97", "*98", "*60")):
        from .mailbox import resolve_mailbox_route

        mailbox = resolve_mailbox_route(hass, target, entries, source_endpoint_id=source_endpoint_id)
        if mailbox is not None:
            return mailbox
    runtime = runtime_data(hass)
    if source_endpoint_id or trunk_id or (runtime is not None and runtime.trunk_configs):
        from .trunk_selection import select_call_trunk

        selected = select_call_trunk(hass, endpoint_id=source_endpoint_id, inherited=trunk_id)
        decision = resolve_ha_router(target, entries, trunk_ready=selected.available)
        return replace(decision, trunk_id=selected.identity)
    trunk = sip_trunk(hass)
    configured_trunk = trunk_config(hass)
    trunk_ready = trunk_enabled(configured_trunk) and bool(
        trunk_available(trunk)
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
    source_endpoint_id: str = ""
    trunk_id: str = ""

    def for_call(self, call_id: str, *, trunk_id: str = ""):
        from .endpoint_lifecycle import call_registry
        from .trunk_selection import select_call_trunk

        session = call_registry(self.hass).get_session(call_id)
        metadata = session.metadata if session is not None else {}
        source_id = str(metadata.get("source_endpoint_id") or self.source_endpoint_id)
        selection = select_call_trunk(self.hass, requested=trunk_id, endpoint_id=source_id,
                                      inherited=str(metadata.get("outbound_trunk_id") or self.trunk_id))
        return replace(self, source_endpoint_id=source_id, trunk_id=selection.identity)

    def is_ha_target(self, value: str) -> bool:
        return is_ha_target(self.hass, value)

    def route(self, target: str, entries: list):
        return ha_router_decision(self.hass, target, entries, source_endpoint_id=self.source_endpoint_id,
                                  trunk_id=self.trunk_id)

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
    for contract in (device, metadata):
        value = contract.get("sip_video_codec")
        if value is not None:
            value = str(value).strip().casefold()
            if value in {"", "h264", "jpeg"}:
                return value
    if any("sip_video_codec" in contract and contract["sip_video_codec"] is None
           for contract in (device, metadata)):
        # An incomplete discovery is not an audio-only endpoint declaration.
        return None
    capabilities = {
        str(item).strip().casefold()
        for item in (
            (getattr(peer, "capabilities", ()) if peer is not None else ())
            or metadata.get("capabilities")
            or ()
        )
        if str(item).strip()
    }
    if capabilities:
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
    """Rank backend PCM capabilities without constraining authoritative SDP."""
    send_rtp = local_rtp_audio_formats(
        list(HA_SIP_PCM_TX_FORMATS), hints=_rtp_audio_hints([], list(remote_rx_formats or [])),
    )
    recv_rtp = local_rtp_audio_formats(
        list(HA_SIP_PCM_RX_FORMATS), hints=_rtp_audio_hints([], list(remote_tx_formats or [])),
        receive=True,
    )
    profile = SipAudioCapabilityProfile.from_rtp(send_rtp, recv_rtp)
    send_candidates, recv_candidates = list(profile.send_formats), list(profile.recv_formats)
    _LOGGER.debug(
        "Directional SIP PCM profile for %s: send=%s recv=%s",
        target,
        [fmt.wire_token() for fmt in send_candidates],
        [fmt.wire_token() for fmt in recv_candidates],
    )
    return send_candidates, recv_candidates


def supports_directional_audio_payloads(peer: Peer | None, entry) -> bool:
    """Return whether the destination implements the ESP directional SDP profile."""
    return "directional_audio_v1" in _endpoint_sdp_features(peer, entry)


def supports_independent_audio_ptime(peer: Peer | None, entry) -> bool:
    """Allow the D1 timing upgrade only for an explicitly capable target."""
    return "independent_audio_ptime_v1" in _endpoint_sdp_features(peer, entry)


def roster_from_peers(hass: HomeAssistant, peers: list[Peer], registered_entries) -> list:
    """Share canonical identities without caching a call's Contact selection."""
    runtime = runtime_data(hass)
    if runtime is not None and runtime.directory is not None and not runtime.directory.closed:
        from .directory_runtime import compose_directory_entries

        return compose_directory_entries(hass, peers, registered_entries)
    return assemble_directory_entries(hass, peers, registered_entries)


def assemble_directory_entries(hass: HomeAssistant, peers: list[Peer], registered_entries) -> list:
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
                    "sip_video_codec": peer_video_codec(peer),
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
            declared = next((entry for entry in entries
                             if entry.metadata.get("endpoint_id") == endpoint.endpoint_id), None)
            if declared is not None:
                metadata.setdefault("sip_video_codec", peer_video_codec(None, declared))
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
        if endpoint is not None:
            # The Contact is the transport of the existing physical identity.
            # It must not create a second phone alongside ESP discovery.
            entries = [entry for entry in entries
                       if entry.metadata.get("endpoint_id") != endpoint.endpoint_id]
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
    # Endpoint policies require HA to see the call before the target rings.
    # Keep physical addressing intact for negotiation; only set the existing
    # routing hint consumed by ESP phonebooks.
    for index, entry in enumerate(entries):
        endpoint = endpoint_registry.get((entry.metadata or {}).get("endpoint_id", ""))
        if endpoint is not None:
            entries[index] = replace(
                entry,
                ha_bridge=entry.ha_bridge or endpoint.policy.requires_ha_routing,
                metadata={**entry.metadata, "call_policy": endpoint.policy.as_data()},
            )
    return entries
