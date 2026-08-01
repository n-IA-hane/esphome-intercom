"""Endpoint adapter for inbound calls routed to Home Assistant Assist."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .assist_runtime import AssistMediaSession, build_call_connected_intent
from .automation_routing import canonical_call_origin
from .const import (
    CONF_ASSIST_ADVANCED_CALL_CONTEXT,
    CONF_ASSIST_PIPELINE,
    DOMAIN,
)
from .endpoint_lifecycle import call_registry
from .fsm import CallState, TerminalReason
from .pbx_routing import roster_entry_for_target
from .router import RouteAction
from .websocket_api import _set_sip_bridge_call_state

if TYPE_CHECKING:
    from .media_ports import RtpPortReservation
    from .roster import RosterEntry
    from .sip_listener import SipInvite


@dataclass(slots=True)
class AssistEndpoint:
    """Start and project one inbound Assist media leg."""

    hass: HomeAssistant
    terminate_sip_bridge: Callable[..., Awaitable[Any]]

    async def start(
        self,
        invite: SipInvite,
        *,
        reservation: RtpPortReservation,
        local_rtp_port: int,
        roster_entries: list[RosterEntry],
        source: str,
        called_extension: str,
        release_reservation_on_failure: bool = True,
    ) -> AssistMediaSession:
        """Attach an accepted SIP dialog to the configured Assist pipeline."""

        assist_cfg = self.hass.data.setdefault(DOMAIN, {}).get("assist_config", {})
        caller_entry = roster_entry_for_target(
            invite.routing_caller,
            roster_entries,
        )
        if caller_entry is None:
            caller_token = str(invite.caller or "").strip()
            caller_entry = next(
                (
                    entry
                    for entry in roster_entries
                    if caller_token and str(entry.number or "").strip() == caller_token
                ),
                None,
            )
        caller_id = str(
            invite.routing_caller
            or invite.source_host
            or "Unknown"
        ).strip()
        caller_name = (
            str(caller_entry.name or caller_entry.id).strip()
            if caller_entry is not None
            else str(invite.caller or caller_id or "Unknown").strip()
        )
        caller_uri = str(invite.caller_uri) if invite.caller_uri is not None else ""
        destination_name = str(assist_cfg.get("name") or "Assist").strip() or "Assist"
        assist_leg_id = f"assist:{invite.call_id}"
        registry = call_registry(self.hass)
        existing_session = registry.sessions.get(
            registry.resolve_session_id(invite.call_id)
        )
        existing_metadata = (
            existing_session.metadata if existing_session is not None else {}
        )
        call_ingress = canonical_call_origin(
            existing_metadata.get("ingress")
            or existing_metadata.get("origin")
            or ("trunk" if invite.received_via_trunk or source == "trunk" else source),
            existing_session.route_kind if existing_session is not None else "",
        )

        async def complete(reason: str) -> None:
            await self.terminate_sip_bridge(
                self.hass,
                invite.call_id,
                terminal_reason=reason or TerminalReason.PROTOCOL_ERROR.value,
            )

        media = AssistMediaSession(
            self.hass,
            invite=invite,
            local_rtp_port=local_rtp_port,
            reservation=reservation,
            pipeline_id=str(assist_cfg.get(CONF_ASSIST_PIPELINE) or "preferred"),
            call_connected_intent=build_call_connected_intent(
                caller=caller_name,
                caller_id=caller_id,
                caller_in_phonebook=caller_entry is not None,
                source=source,
                called_extension=called_extension,
                include_advanced_context=bool(
                    assist_cfg.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)
                ),
            ),
            on_complete=complete,
        )
        try:
            await media.start()
        except BaseException:
            if release_reservation_on_failure:
                reservation.release()
            raise

        registry.bridge_clients[invite.call_id] = assist_leg_id
        registry.upsert(
            invite.call_id,
            state=CallState.IN_CALL.value,
            owner="assist",
            caller=caller_name,
            callee=destination_name,
            route_kind=RouteAction.ASSIST.value,
            ingress=call_ingress,
            origin=call_ingress,
        )
        registry.attach_relay(invite.call_id, media)
        registry.add_leg(
            invite.call_id,
            invite.call_id,
            role="trunk" if call_ingress == "trunk" else "caller",
            state=CallState.IN_CALL.value,
        )
        registry.add_leg(
            invite.call_id,
            assist_leg_id,
            role="assist",
            state=CallState.IN_CALL.value,
        )
        _set_sip_bridge_call_state(
            self.hass,
            CallState.IN_CALL.value,
            caller=caller_name,
            callee=destination_name,
            peer_name=destination_name,
            call_id=invite.call_id,
            dest_call_id=assist_leg_id,
            direction="incoming",
            route_kind=RouteAction.ASSIST.value,
            ingress=call_ingress,
            origin=call_ingress,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_direction=invite.local_audio_direction,
            audio_connection_held=invite.remote_audio_connection_held,
            sip_status_code=200,
            last_sip_event="ASSIST_PIPELINE",
            caller_uri=caller_uri,
            source=source,
        )
        return media
