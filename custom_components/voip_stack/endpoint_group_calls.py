"""HA-origin calls to ring groups and conference rooms."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import secrets
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .call_events import home_phone_name
from .call_projection import publish_phone_projection
from .core import sdp as sip_sdp
from .core.audio_format import HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS
from .core.sip import parse_sip_uri
from .endpoint_lifecycle import call_registry, create_runtime_task
from .endpoint_registry import EndpointBusyError
from .endpoint_routing import roster_from_peers
from .endpoint_session import TerminationInitiator, TerminationIntent
from .endpoint_termination import EndpointTerminationHandler
from .fsm import CallState, TerminalReason
from .groups import GROUP_TYPE_RING
from .phonebook_runtime import registered_roster_entries
from .runtime_data import browser_phone
from .sip_listener import SipInvite

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry


async def end_timed_out_conference_call(hass: HomeAssistant, call_id: str, reason: str) -> None:
    """End a timed-out inbound UAS dialog and release its logical claim."""
    await EndpointTerminationHandler(hass).terminate(
        call_id,
        TerminationIntent.bye(reason, TerminationInitiator.TIMEOUT),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class EndpointGroupCalls:
    """Run HA-origin group calls through the same SIP and browser member dialers."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    build_peer_snapshot: Callable[[HomeAssistant], Awaitable[list[Peer]]]
    run_ring_group: Callable[..., Awaitable[None]]
    ring_conference: Callable[..., Awaitable[None]]

    async def member_roster(self) -> tuple[list[Peer], list[RosterEntry]]:
        peers = await self.build_peer_snapshot(self.hass)
        return peers, roster_from_peers(self.hass, peers, registered_roster_entries(self.hass))

    async def ring_conference_from_ha(
        self,
        entry: RosterEntry,
        *,
        owner_call_id: str = "",
    ) -> None:
        peers, roster_entries = await self.member_roster()
        room_name = str(entry.name or entry.id or "")
        await self.ring_conference(
            room_name=room_name,
            caller=home_phone_name(self.hass),
            source_host=self.local_ip,
            entry=entry,
            peers=peers,
            roster_entries=roster_entries,
            owner_call_id=owner_call_id,
        )

    async def start_ring_group_from_ha(
        self,
        entry: RosterEntry,
        *,
        context: Any | None = None,
        endpoint_id: str = "",
        media_client_id: str = "",
        request_video: bool = False,
        enable_caller_video_send: bool = False,
        policy_endpoint_ids: tuple[str, ...] = (),
        outbound_trunk_id: str = "",
        call_id: str = "",
    ) -> str:
        browser_endpoint = browser_phone(self.hass, endpoint_id)
        if browser_endpoint is None:
            raise ValueError("a Home Assistant phone is required")
        endpoint_id = browser_endpoint.endpoint_id
        local_name = browser_endpoint.name
        endpoint_device_id = browser_endpoint.device_id
        group_name = str(entry.name or entry.id or "")
        route_kind = str(entry.metadata.get("route_kind") or GROUP_TYPE_RING)
        # A timestamp is not a dialog identifier: two phones can start in the
        # same millisecond.  Use cryptographic entropy just like the normal SIP
        # client path so concurrent HA callers cannot alias one registry entry.
        call_id = call_id or f"ha-{secrets.token_hex(16)}"
        invite = _group_origin_invite(
            call_id, group_name, local_name, self.local_ip, int(self.config["sip_port"])
        )
        registry = call_registry(self.hass)
        session = registry.upsert(
            call_id,
            state=CallState.REMOTE_RINGING.value,
            owner="ha_softphone",
            caller=local_name,
            callee=group_name,
            route_kind=route_kind,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            source_endpoint_id=endpoint_id,
            policy_endpoint_ids=policy_endpoint_ids,
            outbound_trunk_id=outbound_trunk_id,
            media_client_id=str(media_client_id or "").strip(),
        )
        try:
            registry.claim_endpoint(call_id, endpoint_id, role="source")
        except EndpointBusyError:
            await EndpointTerminationHandler(self.hass).terminate_reason(
                call_id,
                TerminalReason.BUSY.value,
                TerminationInitiator.ROUTING,
            )
            raise
        registry.bind_controller(
            call_id,
            context=context,
            endpoint_id=endpoint_id,
        )
        registry.scope_controllers_by_endpoint(call_id, endpoint_id)
        origin_leg_id = f"browser-origin:{endpoint_id}"
        registry.add_leg(
            call_id,
            origin_leg_id,
            role="ha_softphone",
            state=CallState.REMOTE_RINGING.value,
            endpoint_id=endpoint_id,
        )
        publish_phone_projection(
            self.hass,
            session,
            endpoint_id,
            leg_id=origin_leg_id,
            peer_name=group_name,
            direction="outgoing",
            route_kind=route_kind,
            sip_status_code=180,
            last_sip_event="LOCAL_RING_GROUP",
        )
        try:
            peers, roster_entries = await self.member_roster()
        except Exception:
            await EndpointTerminationHandler(self.hass).terminate_reason(
                call_id,
                TerminalReason.TRANSPORT_UNREACHABLE.value,
                TerminationInitiator.RUNTIME,
            )
            raise
        create_runtime_task(
            self.hass,
            self.run_ring_group(
                invite,
                entry,
                peers,
                roster_entries,
                origin_endpoint_id=endpoint_id,
                origin_media_client_id=str(media_client_id or "").strip(),
                request_video=bool(request_video),
                enable_caller_video_send=bool(enable_caller_video_send),
            ),
        )
        return call_id


def _group_origin_invite(
    call_id: str, group_name: str, local_name: str, local_ip: str, sip_port: int
) -> SipInvite:
    """Describe a browser caller to the normal group forking path."""
    send_format = next(
        fmt for fmt in HA_SIP_PCM_TX_FORMATS if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
    )
    recv_format = next(
        fmt for fmt in HA_SIP_PCM_RX_FORMATS if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
    )
    return SipInvite(
        source_host=local_ip,
        source_port=sip_port,
        request_uri=parse_sip_uri(f"sip:{group_name.replace(' ', '_')}@{local_ip};transport=tcp"),
        caller_uri=parse_sip_uri(f"sip:{local_name.replace(' ', '_')}@{local_ip};transport=tcp"),
        target=group_name,
        caller=local_name,
        call_id=call_id,
        cseq="1 INVITE",
        remote_sdp=b"",
        send_format=sip_sdp.audio_format_to_rtp(send_format, 96),
        recv_format=sip_sdp.audio_format_to_rtp(recv_format, 96),
        remote_rtp_host=local_ip,
        remote_rtp_port=0,
    )
