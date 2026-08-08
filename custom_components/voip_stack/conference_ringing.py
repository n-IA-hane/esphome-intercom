"""Outbound member ringing for HA-anchored conferences."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from homeassistant.core import HomeAssistant

from .conference import MAX_CONFERENCE_LEGS, conference_manager
from .endpoint_lifecycle import call_registry
from .endpoint_registry import EndpointBusyError
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state,
    sip_terminal_reason,
)
from .groups import GROUP_TYPE_CONFERENCE
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
    async_close_outbound_leg,
)
from .pbx_routing import (
    caller_matches_group_member,
    unique_group_members,
)
from .phone_endpoint import EndpointAvailability, EndpointKind
from .runtime_data import endpoint_directory

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry

_LOGGER = logging.getLogger(__name__)
RING_GROUP_TIMEOUT_S = 30.0


@dataclass(slots=True)
class ConferenceRingRuntime:
    """Dependencies required to invite conference members."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    on_inbound_timeout: Callable[[str, str], Awaitable[None]]
    browser_leg_for_member: Callable[..., BrowserLeg | None]
    prepare_outbound_leg: Callable[..., OutboundLeg | None]


async def async_ring_conference_members(
    runtime: ConferenceRingRuntime,
    *,
    room_name: str,
    caller: str,
    source_host: str,
    entry: RosterEntry,
    peers: list[Peer],
    roster_entries: list[RosterEntry],
    owner_call_id: str = "",
) -> None:
    """Invite bounded browser and SIP legs into one conference room."""

    manager = conference_manager(
        runtime.hass,
        local_ip=runtime.local_ip,
        on_inbound_timeout=runtime.on_inbound_timeout,
    )
    registry = call_registry(runtime.hass)
    endpoints = endpoint_directory(runtime.hass)
    owner_session = registry.sessions.get(
        registry.resolve_session_id(str(owner_call_id or "").strip())
    )
    owner_metadata = (
        (owner_session.metadata if owner_session is not None else {}) or {}
    )
    source_endpoint_id = str(
        owner_metadata.get("source_endpoint_id")
        or owner_metadata.get("endpoint_id")
        or ""
    ).strip()
    room = manager.rooms.get(str(room_name or "").strip())
    available_legs = max(
        0,
        MAX_CONFERENCE_LEGS
        - (len(room.legs) if room is not None and not room._closed else 0),
    )
    members = unique_group_members(entry.metadata.get("ring_members"))
    attempts: list[OutboundLeg] = []
    browser_endpoint_ids: list[str] = []

    for member in members:
        if caller_matches_group_member(
            caller,
            source_host,
            member,
            peers,
            source_endpoint_id=source_endpoint_id,
        ):
            continue
        browser_leg = runtime.browser_leg_for_member(
            member,
            peers,
            roster_entries,
        )
        if browser_leg is not None:
            if (
                browser_leg.endpoint_id != source_endpoint_id
                and browser_leg.endpoint_id not in browser_endpoint_ids
                and len(browser_endpoint_ids) + len(attempts) < available_legs
            ):
                browser_endpoint_ids.append(browser_leg.endpoint_id)
            continue
        if len(browser_endpoint_ids) + len(attempts) >= available_legs:
            _LOGGER.warning(
                "SIP conference %s has no capacity for additional ring "
                "members; excess members were skipped",
                room_name,
            )
            break
        try:
            leg = runtime.prepare_outbound_leg(
                member=member,
                peers=peers,
                roster_entries=roster_entries,
                local_name=room_name,
                local_rtp_port_index=0,
            )
        except RuntimeError as err:
            _LOGGER.warning(
                "SIP conference member RTP port allocation failed "
                "member=%s: %s",
                member,
                err,
            )
            break
        if leg is None:
            continue
        if leg.endpoint_id == source_endpoint_id:
            await async_close_outbound_leg(leg)
            continue
        endpoint = endpoints.get(leg.endpoint_id) if leg.endpoint_id else None
        if endpoint is not None and (
            endpoint.dnd
            or endpoint.availability is not EndpointAvailability.AVAILABLE
        ):
            await async_close_outbound_leg(leg)
            continue
        leg_call_id = leg.client.dialog_ids.call_id
        registry.upsert(
            leg_call_id,
            state=CallState.CALLING.value,
            owner="bridge",
            caller=room_name,
            callee=member,
            route_kind=GROUP_TYPE_CONFERENCE,
            source_call_id=owner_call_id,
            dest_endpoint_id=leg.endpoint_id,
        )
        try:
            if leg.endpoint_id:
                registry.claim_endpoint(
                    leg_call_id,
                    leg.endpoint_id,
                    role="conference_member",
                    adopt_transport=(
                        endpoint is not None
                        and endpoint.kind is EndpointKind.ESPHOME
                    ),
                )
        except EndpointBusyError:
            registry.terminate_call(
                leg_call_id,
                reason=TerminalReason.BUSY.value,
                state=CallState.BUSY.value,
            )
            await async_close_outbound_leg(leg)
            continue
        attempts.append(leg)

    if browser_endpoint_ids:
        manager.ring_ha_endpoints(
            room_name,
            tuple(browser_endpoint_ids),
            caller=caller,
        )

    async def _dial(attempt: OutboundLeg) -> None:
        client = attempt.client
        uri = attempt.uri
        owned_by_room = False
        cleanup_reason = TerminalReason.TRANSPORT_UNREACHABLE.value
        try:
            result = await client.invite(
                target=uri.user or attempt.member,
                target_display_name=attempt.member,
                remote_host=uri.host,
                remote_sip_port=uri.port or int(runtime.config["sip_port"]),
                request_uri=str(uri),
                timeout=8.0,
            )
            if result == "ringing":
                result = await client.wait_for_final(
                    timeout=RING_GROUP_TIMEOUT_S
                )
            if result != "in_call" or client.dialog is None:
                return
            owned_by_room = await manager.add_client_leg(
                room_name,
                call_id=client.dialog_ids.call_id,
                caller=attempt.member,
                client=client,
                port_reservation=attempt.ports,
                role="auto_invited",
            )
            if not owned_by_room:
                return
            terminal = await client.wait_for_dialog_termination()
            terminal_reason = (
                TerminalReason.REMOTE_HANGUP.value
                if terminal == "remote_hangup"
                else sip_terminal_reason(terminal, sip_public_state(terminal))
            )
            cleanup_reason = terminal_reason
            await manager.leave_call(
                client.dialog_ids.call_id,
                reason=terminal_reason,
            )
            registry.terminate_call(
                client.dialog_ids.call_id,
                reason=terminal_reason,
                state=CallState.IDLE.value,
            )
        except asyncio.CancelledError:
            cleanup_reason = TerminalReason.CANCELLED.value
            raise
        except Exception as err:
            _LOGGER.debug(
                "SIP conference member invite failed member=%s: %s",
                attempt.member,
                err,
            )
        finally:
            if owned_by_room:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await manager.leave_call(
                        client.dialog_ids.call_id,
                        reason=cleanup_reason,
                    )
                registry.terminate_call(
                    client.dialog_ids.call_id,
                    reason=cleanup_reason,
                    state=CallState.IDLE.value,
                )
            else:
                with contextlib.suppress(Exception):
                    await async_close_outbound_leg(
                        attempt,
                        bye_or_cancel=True,
                    )
                registry.terminate_call(
                    attempt.client.dialog_ids.call_id,
                    reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                    state=CallState.TRANSPORT_UNREACHABLE.value,
                )

    await asyncio.gather(
        *(_dial(attempt) for attempt in attempts),
        return_exceptions=True,
    )
