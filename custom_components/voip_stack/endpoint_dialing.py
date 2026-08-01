"""Endpoint member resolution and outbound SIP leg construction."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from .audio_format import HA_TRUNK_AUDIO_FORMATS
from .const import CONF_SIP_VIDEO, HA_SOFTPHONE_DEVICE_ID
from .endpoint_lifecycle import create_runtime_task
from .endpoint_routing import (
    EndpointRouteResolver,
    peer_audio_formats,
    peer_for_target,
    roster_entry_formats,
    sip_target_audio_profile,
)
from .media_ports import (
    RtpPortReservation,
    release_sip_rtp_port_pair,
    reserve_sip_video_relay_media,
)
from .outbound_attempts import BrowserLeg, OutboundLeg
from .pbx_routing import roster_entry_for_target
from .phone_endpoint import DEFAULT_ENDPOINT_ID, EndpointKind
from .sip import parse_sip_uri
from .sip_bridge import build_pending_invite_video_relay
from .sip_client import SipCallClient

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry
    from .sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class EndpointDialer:
    """Resolve phonebook members and construct unstarted outbound legs."""

    hass: HomeAssistant
    local_ip: str
    config: dict[str, Any]
    route_resolver: EndpointRouteResolver
    ha_peer_name: Callable[..., str]
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
        if peer is not None and peer.host:
            sip_transport = str(
                (peer.device or {}).get("sip_transport") or "tcp"
            ).lower()
            if sip_transport not in {"tcp", "udp"}:
                sip_transport = "tcp"
            return (
                parse_sip_uri(
                    f"sip:{member}@{peer.host}:"
                    f"{peer.sip_port or self.config['sip_port']};"
                    f"transport={sip_transport}"
                ),
                peer,
                None,
            )
        entry = roster_entry_for_target(member, entries)
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
                parse_sip_uri(f"sip:{entry.id}@{entry.address}:{bridge_port}"),
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
        if endpoint is not None:
            if endpoint.kind is not EndpointKind.BROWSER:
                return None
            return BrowserLeg(
                member=member,
                endpoint_id=endpoint.endpoint_id,
                name=endpoint.name,
                device_id=str(endpoint.device_id or HA_SOFTPHONE_DEVICE_ID),
            )
        if self.route_resolver.is_ha_target(member):
            return BrowserLeg(
                member=member,
                endpoint_id=DEFAULT_ENDPOINT_ID,
                name=self.ha_peer_name(self.hass),
                device_id=HA_SOFTPHONE_DEVICE_ID,
            )
        return None

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
    ) -> OutboundLeg | None:
        """Build one outbound SIP leg without starting its transaction."""

        resolved_uri, peer_target, member_entry = self.sip_uri_for_member(
            member,
            peers,
            roster_entries,
        )
        uri = parse_sip_uri(uri_override) if uri_override else resolved_uri
        if uri is None or self.route_resolver.is_local_listener_uri(uri):
            return None
        ports = RtpPortReservation.allocate(self.hass)
        video_relay = None
        try:
            remote_tx_formats = peer_audio_formats(
                peer_target,
                "tx_formats",
            ) or roster_entry_formats(member_entry, "tx_formats")
            remote_rx_formats = peer_audio_formats(
                peer_target,
                "rx_formats",
            ) or roster_entry_formats(member_entry, "rx_formats")
            sip_send_formats, sip_recv_formats = sip_target_audio_profile(
                remote_tx_formats=remote_tx_formats,
                remote_rx_formats=remote_rx_formats,
                target=member,
            )
            bridge_to_softphone = bool(
                member_entry is not None
                and member_entry.sip_uri
                and member_entry.metadata.get("registered")
            )
            if bridge_to_softphone:
                sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
                sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
            target_endpoint = self.route_resolver.logical_endpoint(
                member,
                peers,
                roster_entries,
            )
            video_failure_reason = ""
            if (
                invite is not None
                and invite.video_format is not None
                and bool(self.config.get(CONF_SIP_VIDEO, False))
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
            client = SipCallClient(
                local_ip=self.local_ip,
                local_name=local_name,
                local_uri_user=(
                    invite.routing_caller
                    if invite is not None
                    else local_name
                ),
                local_sip_port=int(self.config["sip_port"]),
                local_rtp_port=ports.ports[local_rtp_port_index],
                supported_send_formats=sip_send_formats,
                supported_recv_formats=sip_recv_formats,
                signaling_transport=self.sip_uri_transport(uri),
                include_common_codecs=bridge_to_softphone,
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
                local_video_rtp_port=(
                    video_relay.right_port if video_relay is not None else 0
                ),
                video_formats=(
                    (invite.video_format,)
                    if video_relay is not None and invite is not None
                    else ()
                ),
                video_direction=(
                    invite.video_format.direction
                    if video_relay is not None and invite is not None
                    else "inactive"
                ),
                generic_video_relay=video_relay is not None,
            )
            self.enable_reused_tcp_connection(
                self.hass,
                client,
                uri,
                target=member,
                default_sip_port=int(self.config["sip_port"]),
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
            )
        except Exception:  # noqa: BLE001
            if video_relay is not None:
                create_runtime_task(self.hass, video_relay.stop())
            ports.release()
            raise
