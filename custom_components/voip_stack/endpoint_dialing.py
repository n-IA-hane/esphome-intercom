"""Endpoint member resolution and outbound SIP leg construction."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from .codec_settings import effective_codec_policy
from .const import CONF_SIP_VIDEO, CONF_VIDEO_TRANSCODING
from .endpoint_lifecycle import create_runtime_task
from .inbound_identity import inbound_from_uri
from .outbound_identity import outbound_identity
from .endpoint_routing import (
    EndpointRouteResolver,
    bridge_audio_profile,
    peer_for_target,
    peer_video_codec,
    sip_target_rtp_audio_profile,
)
from .media_ports import (
    RtpPortReservation,
    release_sip_rtp_port_pair,
    reserve_sip_video_relay_media,
)
from .outbound_attempts import BrowserLeg, OutboundLeg
from .pbx_routing import roster_entry_for_target
from .peer import sip_uri_for_peer
from .runtime_data import preferred_browser_phone, sip_trunk
from .trunk_signaling import reuse_registered_trunk_flow
from .trunk_policy import EXTERNAL_CALL_BLOCKED_ENDPOINTS, call_external_call_denied, is_any_trunk_uri
from .core.sip import SipUri, parse_sip_uri
from .sip_bridge import build_pending_invite_video_relay, video_bridge_offer_formats
from .sip_client import SipCallClient

if TYPE_CHECKING:
    from .forward_operation import ForwardOperation
    from .pending_sip_destination import RetainedSourceMedia
    from .peer import Peer
    from .roster import RosterEntry
    from .sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class OutboundLegPolicy:
    """Policy differences applied by the common outbound-leg builder."""

    local_uri_user: str = ""
    call_id: str = ""
    local_identity_uri: str = ""
    auth_username: str = ""
    username: str = ""
    password: str = ""
    outbound_proxy: str = ""
    reuse_registered_flow: bool = True
    use_trunk_flow: bool = False
    allow_video: bool = True
    trunk_id: str = ""
    retained_source_media: RetainedSourceMedia | None = None
    prepared_audio_owner: ForwardOperation | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class EndpointDialer:
    """Resolve phonebook members and construct unstarted outbound legs."""

    hass: HomeAssistant
    local_ip: str
    config: dict[str, Any]
    route_resolver: EndpointRouteResolver
    sip_uri_transport: Callable[..., str]
    enable_reused_tcp_connection: Callable[..., bool]

    def sip_uri_for_member(
        self,
        member: str,
        peers: list[Peer],
        entries: list[RosterEntry],
    ):
        """Resolve one member to a remote SIP URI and its source record."""

        peer = peer_for_target(member, peers)
        entry = roster_entry_for_target(member, entries)
        if peer is not None and peer.host:
            return (
                sip_uri_for_peer(
                    peer,
                    default_port=int(self.config["sip_port"]),
                    fallback_user=member,
                ),
                peer,
                entry,
            )
        if entry is None:
            return None, None, None
        if entry.sip_uri:
            return parse_sip_uri(entry.sip_uri), None, entry
        if not entry.metadata.get("local_ha") and entry.address:
            bridge_port = int(
                entry.port
                or (entry.metadata or {}).get("port")
                or (entry.metadata or {}).get("sip_port")
                or self.config["sip_port"]
            )
            return (
                SipUri(entry.id, entry.address, bridge_port),
                None,
                entry,
            )
        return None, None, entry

    def browser_leg_for_member(
        self,
        member: str,
        peers: list[Peer],
        entries: list[RosterEntry],
    ) -> BrowserLeg | None:
        """Resolve one member to its browser endpoint candidate."""

        endpoint = self.route_resolver.logical_endpoint(member, peers, entries)
        if endpoint is None and self.route_resolver.is_ha_target(member):
            endpoint = preferred_browser_phone(self.hass)
        from .account_runtime import account_has_ha_binding

        if endpoint is None or not (endpoint.kind.is_softphone or (
            endpoint.supports_ha_client
            and account_has_ha_binding(self.hass, endpoint.endpoint_id)
        )):
            return None
        return BrowserLeg(
            member=member,
            endpoint_id=endpoint.endpoint_id,
            name=endpoint.name,
            device_id=endpoint.device_id,
        )

    def prepare_outbound_leg(
        self,
        *,
        member: str,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
        local_name: str,
        local_rtp_port_index: int,
        uri_override: str = "",
        endpoint_id_override: str = "",
        peer_user_agent_override: str = "",
        candidate_id: str = "",
        tier: int = 0,
        order: int = 0,
        invite: SipInvite | None = None,
        source_call_id: str = "",
        port_reservation: RtpPortReservation | None = None,
        roster_entry_override: RosterEntry | None = None,
        policy: OutboundLegPolicy | None = None,
    ) -> OutboundLeg | None:
        """Build one outbound SIP leg without starting its transaction."""

        policy = policy or OutboundLegPolicy()
        resolved_uri, peer_target, member_entry = self.sip_uri_for_member(
            member,
            peers,
            roster_entries,
        )
        member_entry = roster_entry_override or member_entry
        uri = parse_sip_uri(uri_override) if uri_override else resolved_uri
        if uri is None:
            from .router import RouteAction
            from .trunk_selection import select_call_trunk

            dial_target = str(getattr(member_entry, "number", "") or member)
            decision = self.route_resolver.route(dial_target, roster_entries)
            if decision.action is RouteAction.TRUNK:
                inherited = policy.trunk_id or (invite.outbound_trunk_id if invite is not None else "") or decision.trunk_id
                selected = select_call_trunk(self.hass, inherited=inherited)
                if selected.available:
                    cfg = selected.config
                    uri = SipUri(decision.target, str(cfg.get("trunk_domain") or cfg.get("trunk_server") or ""),
                                 int(cfg.get("trunk_port") or 5060),
                                 params=(("transport", str(cfg.get("trunk_transport") or "udp")),))
                    policy = replace(policy, use_trunk_flow=True, trunk_id=selected.identity,
                        username=str(cfg.get("trunk_username") or ""),
                        auth_username=str(cfg.get("trunk_auth_username") or ""),
                        password=str(cfg.get("trunk_password") or ""),
                        outbound_proxy=str(cfg.get("trunk_outbound_proxy") or ""))
        if uri is None or self.route_resolver.is_local_listener_uri(uri):
            return None
        target_endpoint = self.route_resolver.logical_endpoint(member, peers, roster_entries)
        codec_policy = effective_codec_policy(self.hass,
            endpoint_id=endpoint_id_override or (target_endpoint.endpoint_id if target_endpoint is not None else ""),
            trunk_id=policy.trunk_id if policy.use_trunk_flow else None)
        registration_only = (getattr(invite, "authenticated_username", "")
                             and not getattr(invite, "received_via_trunk", False)
                             and not getattr(invite, "identified_source_endpoint_id", ""))
        if self.config.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS) or registration_only or source_call_id:
            from .runtime_data import require_runtime_data

            access_runtime = require_runtime_data(self.hass)
            if (policy.use_trunk_flow or is_any_trunk_uri(access_runtime, str(uri))) and (registration_only or call_external_call_denied(
                access_runtime, invite.call_id if invite is not None else source_call_id
            )):
                _LOGGER.info("External call denied for group or forward member=%s", member)
                return None
        owns_ports = port_reservation is None
        ports = port_reservation or RtpPortReservation.allocate(self.hass)
        video_relay = None
        try:
            local_audio_port = ports.ports[local_rtp_port_index]
            if policy.prepared_audio_owner is not None:
                if policy.retained_source_media is not None:
                    raise RuntimeError("Outbound audio has conflicting prepared owners")
                owned_socket = policy.prepared_audio_owner.prepared_outbound_audio_socket()
                local_audio_port = owned_socket.getsockname()[1]
            elif policy.retained_source_media is not None:
                from .endpoint_lifecycle import call_registry
                policy.retained_source_media.validate(call_registry(self.hass))
                if ports is not policy.retained_source_media.reservation:
                    raise RuntimeError("retained caller reservation changed before dial")
            else:
                ports.bind_audio_sockets(ports=(ports.ports[local_rtp_port_index],))
            rtp_audio_profile = sip_target_rtp_audio_profile(
                member_entry,
                codec_policy=codec_policy,
                peer_user_agent=peer_user_agent_override or None,
                trunk=policy.use_trunk_flow,
            )
            bridge_to_softphone = bool(
                member_entry is not None
                and member_entry.sip_uri
                and member_entry.metadata.get("registered")
            )
            if invite is not None:
                rtp_audio_profile = bridge_audio_profile(invite, rtp_audio_profile)
            sip_send_formats = list(rtp_audio_profile.send_formats)
            sip_recv_formats = list(rtp_audio_profile.recv_formats)
            target_video_codec = peer_video_codec(peer_target, member_entry)
            video_failure_reason = ""
            trunk = (
                sip_trunk(self.hass, policy.trunk_id) if policy.trunk_id else sip_trunk(self.hass)
            ) if policy.use_trunk_flow else None
            trunk_allows_video = codec_policy.video != () and (trunk is None or trunk.config.video_codecs != ())
            if (
                invite is not None
                and invite.video_format is not None
                and policy.allow_video
                and trunk_allows_video
                and bool(self.config.get(CONF_SIP_VIDEO, False))
                and target_video_codec != ""
            ):
                video_reservation = None
                sockets = ()
                try:
                    (
                        video_reservation,
                        sockets,
                    ) = reserve_sip_video_relay_media(self.hass)
                    source_video_port, destination_video_port = video_reservation.ports
                    video_relay = build_pending_invite_video_relay(
                        invite,
                        remote_host=str(uri.host),
                        left_port=source_video_port,
                        right_port=destination_video_port,
                        sockets=sockets,
                        on_release=lambda reserved: release_sip_rtp_port_pair(
                            self.hass,
                            reserved,
                        ),
                    )
                    video_reservation.detach()
                except (OSError, RuntimeError) as err:
                    for sock in sockets:
                        sock.close()
                    if video_reservation is not None:
                        video_reservation.release()
                    video_relay = None
                    video_failure_reason = "local_video_resources_unavailable"
                    _LOGGER.warning(
                        "SIP fork video reservation unavailable member=%s; "
                        "continuing audio-only: %s",
                        member,
                        err,
                    )
            # These RTP rows are the complete engine plan; the SIP writer
            # numbers them without expanding a second capability catalog.
            caller_user = policy.local_uri_user or (invite.routing_caller if invite is not None else local_name)
            identity = None
            identity_config = {}
            if policy.use_trunk_flow:
                from .config import trunk_config

                identity_config = trunk_config(self.hass, policy.trunk_id or None)
                identity = outbound_identity(
                    self.hass, identity_config, invite=invite, caller_user=caller_user,
                    caller_name=local_name, source_call_id=source_call_id,
                )
            client = SipCallClient(
                max_forwards=max(0, invite.max_forwards - 1) if invite is not None and invite.max_forwards is not None else 70,
                **({"call_id": policy.call_id} if policy.call_id else {}),
                codec_policy=codec_policy,
                local_ip=self.local_ip,
                local_name=identity.name if identity is not None else local_name,
                local_uri_user=str(identity_config.get("trunk_username") or caller_user),
                local_identity_uri=(identity.uri if identity is not None
                                    else policy.local_identity_uri or inbound_from_uri(invite)),
                fixed_local_identity=identity.fixed if identity is not None else False,
                local_sip_port=int(self.config["sip_port"]),
                local_rtp_port=local_audio_port,
                supported_send_formats=sip_send_formats,
                supported_recv_formats=sip_recv_formats,
                supported_send_rtp_formats=rtp_audio_profile.send_rtp_formats,
                supported_recv_rtp_formats=rtp_audio_profile.recv_rtp_formats,
                signaling_transport=self.sip_uri_transport(uri),
                auth_username=policy.auth_username,
                username=policy.username,
                password=policy.password,
                outbound_proxy=policy.outbound_proxy,
                include_common_codecs=False,
                peer_user_agent=(
                    str(peer_user_agent_override or "").strip()
                    or str(
                        (
                            (member_entry.metadata or {}).get("user_agent")
                            if member_entry is not None
                            else ""
                        )
                        or ""
                    ).strip()
                ),
                include_dahua_pcm=rtp_audio_profile.include_dahua_pcm,
                match_received_codec=rtp_audio_profile.match_received_codec,
                local_video_rtp_port=(
                    video_relay.right_port if video_relay is not None else 0
                ),
                video_formats=(
                    video_bridge_offer_formats(
                        invite.video_format,
                        enable_transcoding=bool(
                            self.config.get(CONF_VIDEO_TRANSCODING, False)
                        ),
                        target_codec=target_video_codec,
                    )
                    if video_relay is not None
                    and invite is not None
                    and invite.video_format is not None
                    else ()
                ),
                video_direction=(
                    invite.video_format.direction
                    if video_relay is not None and invite is not None
                    else "inactive"
                ),
                generic_video_relay=video_relay is not None,
                allow_video_transcoding=bool(
                    self.config.get(CONF_VIDEO_TRANSCODING, False)
                ),
            )
            from .sip_runtime import configure_local_contact
            configure_local_contact(self.hass, client)
            if policy.use_trunk_flow:
                from .trunk_media import borrow_pending_media
                borrow_pending_media(client, ports, video_relay)
                reuse_registered_trunk_flow(sip_trunk(self.hass, policy.trunk_id) if policy.trunk_id else sip_trunk(self.hass), client)
            elif policy.reuse_registered_flow:
                from .sip_runtime import select_registered_outbound_flow
                self.enable_reused_tcp_connection(
                    self.hass,
                    client,
                    uri,
                    target=member,
                    default_sip_port=int(self.config["sip_port"]),
                    selected_flow=select_registered_outbound_flow(self.hass, uri,
                        entry=member_entry, peer=peer_target),
                )
            return OutboundLeg(
                member=member,
                uri=uri,
                client=client,
                ports=ports,
                bridge_to_softphone=bridge_to_softphone,
                endpoint_id=str(
                    endpoint_id_override
                    or getattr(target_endpoint, "endpoint_id", "")
                    or ""
                ),
                candidate_id=candidate_id,
                tier=int(tier),
                order=int(order),
                video_relay=video_relay,
                video_failure_reason=video_failure_reason,
                trunk_id=policy.trunk_id if policy.use_trunk_flow else "",
            )
        except Exception:  # noqa: BLE001
            if video_relay is not None:
                create_runtime_task(self.hass, video_relay.stop())
            if owns_ports:
                ports.release()
            raise
