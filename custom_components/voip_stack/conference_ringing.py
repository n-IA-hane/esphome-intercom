"""Outbound member ringing for HA-anchored conferences."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from .conference import MAX_CONFERENCE_LEGS, conference_manager
from .endpoint_lifecycle import call_registry
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import CleanupStage, TerminationInitiator
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
from .core.sip import sip_default_port

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
    owner_call_id: str,
) -> None:
    """Invite bounded browser and SIP legs into one conference room."""

    hass = runtime.hass
    manager = conference_manager(
        hass,
        local_ip=runtime.local_ip,
    )
    registry = call_registry(hass)
    endpoints = endpoint_directory(hass)
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
    if (owner_session is None or not owner_session.live or room is None
            or room._closed or owner_call_id not in room.legs):
        return
    available_legs = max(
        0,
        MAX_CONFERENCE_LEGS
        - (len(room.legs) if room is not None and not room._closed else 0),
    )
    members = unique_group_members(entry.metadata.get("ring_members"))
    attempts: list[OutboundLeg] = []
    browser_endpoint_ids: list[str] = []

    settled = set()
    browser_call_ids = ()

    async def close_attempt(attempt, reason):
        if id(attempt) in settled:
            return
        settled.add(id(attempt))
        call_id = attempt.client.dialog_ids.call_id
        if call_id in room.legs:
            claimed = await EndpointTerminationHandler(hass).terminate_reason(
                call_id, reason, TerminationInitiator.ROUTING)
            if not claimed and call_id in room.legs:
                await room.leave(call_id, reason=reason)
            return
        try:
            await async_close_outbound_leg(attempt, bye_or_cancel=True)
        finally:
            await EndpointTerminationHandler(hass).terminate_reason(
                attempt.client.dialog_ids.call_id, reason, TerminationInitiator.ROUTING)

    async def close_pending_browsers(reason):
        for call_id in browser_call_ids:
            if call_id not in room.legs:
                await EndpointTerminationHandler(hass).terminate_reason(
                    call_id, reason, TerminationInitiator.ROUTING)

    try:
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
                    source_call_id=owner_call_id,
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
                await EndpointTerminationHandler(hass).terminate_reason(
                    leg_call_id,
                    TerminalReason.BUSY.value,
                    TerminationInitiator.ROUTING,
                )
                await async_close_outbound_leg(leg)
                continue
            attempts.append(leg)

        browser_call_ids = manager.ring_ha_endpoints(room_name,
            tuple(browser_endpoint_ids), caller=caller) if browser_endpoint_ids else ()

        if browser_call_ids:
            owner_session.add_resource(f"conference_pending_members:{owner_call_id}",
                browser_call_ids, close_pending_browsers, stage=CleanupStage.OBSERVER)

        async def watch_member(attempt):
            cleanup_reason = TerminalReason.TRANSPORT_UNREACHABLE.value
            try:
                terminal = await attempt.client.wait_for_dialog_termination()
                cleanup_reason = (TerminalReason.REMOTE_HANGUP.value if terminal == "remote_hangup"
                    else sip_terminal_reason(terminal, sip_public_state(terminal)))
            except asyncio.CancelledError:
                cleanup_reason = TerminalReason.CANCELLED.value
                raise
            finally:
                await EndpointTerminationHandler(hass).terminate_reason(
                    attempt.client.dialog_ids.call_id, cleanup_reason, TerminationInitiator.ROUTING)

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
                    remote_sip_port=sip_default_port(uri),
                    request_uri=str(uri),
                    timeout=8.0,
                )
                if result == "ringing":
                    result = await client.wait_for_final(
                        timeout=RING_GROUP_TIMEOUT_S
                    )
                if (result != "in_call" or client.dialog is None or not owner_session.live
                        or manager.rooms.get(room.name) is not room or room._closed):
                    return
                owned_by_room = await manager.add_client_leg(
                    room_name, call_id=client.dialog_ids.call_id, caller=attempt.member,
                    client=client, port_reservation=attempt.ports, role="auto_invited")
                if not owned_by_room:
                    return
                member_session = registry.get_session(client.dialog_ids.call_id)
                if member_session is None or not member_session.live:
                    owned_by_room = False
                    return
                member_session.create_task(watch_member(attempt), name="conference_dialog")
                settled.add(id(attempt))
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
                if not owned_by_room:
                    await close_attempt(attempt, cleanup_reason)


        await asyncio.gather(*(_dial(attempt) for attempt in attempts), return_exceptions=True)
    except BaseException:
        await close_pending_browsers(TerminalReason.CANCELLED.value)
        raise
    finally:
        for attempt in attempts:
            if id(attempt) not in settled:
                await close_attempt(attempt, TerminalReason.CANCELLED.value)
