"""Pure routing helpers used by the HA SIP endpoint."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging

from homeassistant.core import HomeAssistant

from .trunk_policy import trunk_available
from .core.audio_format import (
    AudioFormat,
)
from .core.codec_policy import CodecPolicy
from .core.media import negotiator
from .core.media.bridge import plan_bridge_offer
from .core.media.capability import PeerEvidence
from .core import sdp
from .config import assist_config, trunk_config, trunk_enabled
from .peer import Peer
from .peer_media_profile import legacy_pcm_compatibility, resolve_peer_media_profile
from .roster import RosterEntry, find_entry, normalize_roster_key
from .router import RouteAction, RouteDecision, RouteReason, resolve_ha_router
from .runtime_data import endpoint_directory, preferred_browser_phone, sip_trunk, runtime_data
from .store import manual_roster_entries

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SipAudioCapabilityProfile:
    """Projection of the media engine's ordered local offer capabilities."""

    send_formats: tuple[AudioFormat, ...]
    recv_formats: tuple[AudioFormat, ...]
    send_rtp_formats: tuple[sdp.RtpPcmFormat, ...]
    recv_rtp_formats: tuple[sdp.RtpPcmFormat, ...]
    evidence: PeerEvidence | None = None
    codec_policy: CodecPolicy | None = None
    include_dahua_pcm: bool = False
    match_received_codec: bool = False

    @classmethod
    def from_plan(cls, plan, *, evidence=None, codec_policy=None):
        return cls(
            send_formats=tuple(dict.fromkeys(fmt.audio_format for fmt in plan.send_formats)),
            recv_formats=tuple(dict.fromkeys(fmt.audio_format for fmt in plan.receive_formats)),
            send_rtp_formats=plan.send_formats, recv_rtp_formats=plan.receive_formats,
            evidence=evidence, codec_policy=codec_policy,
            include_dahua_pcm=("PCM", 16000, 1) in plan.compatibility.optional_mappings, match_received_codec=plan.match_received_codec)


def _binding_audio_evidence(entry):
    metadata = dict(getattr(entry, "metadata", None) or {})
    if not metadata.get("sip_capabilities_known"):
        return PeerEvidence()
    return PeerEvidence.from_options(metadata)


def sip_target_rtp_audio_profile(entry, *, codec_policy=None, peer_user_agent=None, trunk=False):
    """Get one engine plan from backend capabilities and Contact-scoped SDP facts."""
    evidence = _binding_audio_evidence(entry)
    profile = resolve_peer_media_profile(dict(getattr(entry, "metadata", None) or {}),
        user_agent_override=peer_user_agent)
    plan = negotiator.sip_offer_plan(evidence, codec_policy=codec_policy,
        compatibility=profile.audio_compatibility,
        symmetric_preferences=trunk or profile.is_dahua or profile.include_dahua_pcm,
        match_received_codec=profile.match_received_codec)
    return SipAudioCapabilityProfile.from_plan(plan, evidence=evidence, codec_policy=codec_policy)


def bridge_audio_profile(invite, profile):
    """Project the bridge planner's destination offer without another selection."""
    plan = plan_bridge_offer(profile.send_formats, profile.recv_formats, invite.recv_format,
        send_rtp_formats=profile.send_rtp_formats,
        receive_rtp_formats=profile.recv_rtp_formats,
        evidence=profile.evidence, codec_policy=profile.codec_policy,
        common=False, compatibility=legacy_pcm_compatibility(profile.include_dahua_pcm),
        match_received_codec=profile.match_received_codec)
    return SipAudioCapabilityProfile.from_plan(plan,
        evidence=profile.evidence, codec_policy=profile.codec_policy)


def same_route_name(left: str, right: str) -> bool:
    return bool(left and right and normalize_roster_key(left) == normalize_roster_key(right))


def is_ha_target(hass: HomeAssistant, value: str) -> bool:
    """Return whether a dial token names the configured HA browser phone."""

    # Kept deferred because websocket_api imports routing helpers during setup.
    from .call_events import home_phone_name as _ha_peer_name

    return same_route_name(value, _ha_peer_name(hass)) or same_route_name(value, "ha")


def ha_router_decision(hass: HomeAssistant, target: str, entries: list, *, source_endpoint_id: str = "", trunk_id: str = ""):
    """Resolve one target against the canonical HA dial plan."""

    if source_endpoint_id or trunk_id:
        from .trunk_selection import parse_public_trunk_destination, select_call_trunk

        explicit, dial_target = parse_public_trunk_destination(hass, target)
        if explicit:
            selected = select_call_trunk(hass, requested=explicit, endpoint_id=source_endpoint_id)
            return RouteDecision(RouteAction.TRUNK if selected.available else RouteAction.REJECT,
                target=dial_target, trunk_id=selected.identity, source="trunk",
                status=0 if selected.available else 503,
                reason=RouteReason.TRUNK_UNAVAILABLE if not selected.available else RouteReason.EXPLICIT_ROUTE)

    from .parking import resolve_parking_route
    parking = resolve_parking_route(hass, target)
    if parking is not None and find_entry(entries, target, literal_only=True) is None:
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
    entry = find_entry(entries, member, include_number=False)
    endpoint_id = str(
        ((getattr(entry, "metadata", None) if entry is not None else {}) or {}).get(
            "endpoint_id"
        )
        or ""
    ).strip()
    if not endpoint_id:
        peer = peer_for_target(member, peers)
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
    rows = [RosterEntry(id=peer.sip_uri_user, name=peer.name, extension=peer.extension,
                        metadata={"endpoint_id": peer.endpoint_id or f"peer:{index}", "peer_index": index})
            for index, peer in enumerate(peers) if not peer.is_ha]
    row = find_entry(rows, target, include_number=False)
    return peers[row.metadata["peer_index"]] if row is not None else None


def peer_video_codec(peer: Peer | None, entry=None) -> str | None:
    """Use Contact SDP or explicit account policy, never hardware diagnostics."""
    metadata = dict(getattr(entry, "metadata", None) or {})
    value = metadata.get("sip_video_codec")
    if value is not None:
        value = str(value).strip().casefold()
        if value in {"", "h264", "jpeg"}:
            return value
    return None


def roster_from_peers(hass: HomeAssistant, peers: list[Peer], registered_entries) -> list:
    """Share canonical identities without caching a call's Contact selection."""
    runtime = runtime_data(hass)
    if runtime is not None and runtime.directory is not None and not runtime.directory.closed:
        from .directory_runtime import compose_directory_entries

        return compose_directory_entries(hass, peers, registered_entries)
    return assemble_directory_entries(hass, peers, registered_entries)


def merge_registered_entries(entries, registered_entries, endpoint_registry):
    """Overlay current registrar Contacts without making a second phone."""
    entries = list(entries)
    for registered in registered_entries:
        endpoint = endpoint_registry.by_username(registered.id)
        metadata = dict(registered.metadata or {})
        if endpoint is not None:
            registered = replace(registered, name=endpoint.name, extension=endpoint.extension)
            declared = next((entry for entry in entries
                             if entry.metadata.get("endpoint_id") == endpoint.endpoint_id), None)
            if declared is not None:
                metadata.setdefault("sip_video_codec", peer_video_codec(None, declared))
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
    return entries


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
                sip_uri=peer.contact_uri,
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
                    "sip_transport": (
                        str((peer.device or {}).get("sip_transport") or "tcp").lower()
                        if peer.is_ha or peer.device is not None
                        else ""
                    ),
                    "sip_port": peer.sip_port,
                    "rtp_port": peer.rtp_port,
                    "registered": bool((peer.device or {}).get("registered")),
                    "sip_contacts": list((peer.device or {}).get("sip_contacts") or ()),

                    "conference_group": peer.conference_group,
                    "conference_ring": bool(peer.conference_ring),
                    "ring_group": peer.ring_group,
                },
            )
        )
    manual_entries = manual_roster_entries(hass)
    entries = merge_roster_overrides(entries, manual_entries)
    endpoint_registry = endpoint_directory(hass)
    entries = merge_registered_entries(entries, registered_entries, endpoint_registry)
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
