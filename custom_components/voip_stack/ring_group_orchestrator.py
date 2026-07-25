"""PBX ring-group call orchestration."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
import secrets
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from . import sdp as sip_sdp
from .call_scope import pending_routes as _pending_routes
from .config import debug_mode as _debug_mode
from .const import DOMAIN, HA_SOFTPHONE_DEVICE_ID
from .dial_fork import (
    DialCandidate,
    DialDisposition,
    DialForkController,
    DialOutcome,
    LegCloseMode,
)
from .dial_plan import RingPolicy, build_sip_contact_targets
from .dtmf_events import attach_dtmf_event_bridge as _attach_dtmf_event_bridge
from .endpoint_lifecycle import call_registry as _call_registry
from .endpoint_registry import EndpointBusyError
from .fsm import (
    CallState,
    TerminalReason,
    sip_failure_response as _sip_failure_response,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .groups import GROUP_TYPE_RING
from .media_ports import (
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    release_media_reservation as _release_media_reservation,
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
)
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
    async_cleanup_outbound_attempts as _cleanup_outbound_attempts,
    async_close_outbound_leg as _close_outbound_leg,
)
from .pbx_routing import (
    caller_matches_group_member as _caller_matches_member,
    unique_group_members as _unique_group_members,
)
from .phone_endpoint import DEFAULT_ENDPOINT_ID
from .ring_group import (
    endpoint_is_esphome as _endpoint_is_esphome,
    endpoint_preflight_disposition as _endpoint_preflight_disposition,
    settle_browser_candidates as _settle_ring_browser_candidates,
)
from .sdp import build_answer_directional
from .session_cleanup import async_cleanup_sip_runtime, async_wait_for_cleanup
from .sip_bridge import (
    build_invite_client_relay,
    build_local_client_relay,
    configure_answered_invite_video_relay,
)
from .sip_runtime import send_final_response as _sip_send_final_response
from .websocket_api import (
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry
    from .sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)
RING_GROUP_TIMEOUT_S = 30.0
MAX_RING_GROUP_ATTEMPTS = 16


def _invite_dtmf_format(invite):
    formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
    return formats[0] if formats else None


@dataclass(slots=True)
class RingGroupRuntime:
    """Explicit dependencies required by one ring-group call."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    ha_peer_name: Callable[[HomeAssistant], str]
    browser_leg_for_member: Callable[..., BrowserLeg | None]
    logical_endpoint_for_member: Callable[..., Any]
    prepare_outbound_leg: Callable[..., Any]
    attach_client_media_update: Callable[..., None]
    terminate_sip_bridge: Callable[..., Any]


async def run_ring_group_call(
    runtime: RingGroupRuntime,
    invite: SipInvite,
    entry: RosterEntry,
    peers: list[Peer],
    roster_entries: list[RosterEntry],
    *,
    origin_endpoint_id: str = "",
    origin_media_client_id: str = "",
    request_video: bool = False,
    enable_caller_video_send: bool = False,
) -> None:
    hass = runtime.hass
    cfg = runtime.config
    local_ip = runtime.local_ip
    _ha_peer_name = runtime.ha_peer_name
    _browser_leg_for_member = runtime.browser_leg_for_member
    _logical_endpoint_for_member = runtime.logical_endpoint_for_member
    _prepare_outbound_leg = runtime.prepare_outbound_leg
    _attach_client_media_update = runtime.attach_client_media_update
    _terminate_sip_bridge = runtime.terminate_sip_bridge
    registry = _call_registry(hass)
    origin_endpoint_id = str(origin_endpoint_id or "").strip()
    endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    origin_endpoint = (
        endpoint_registry.get(origin_endpoint_id)
        if endpoint_registry is not None and origin_endpoint_id
        else None
    )
    origin_device_id = str(
        getattr(origin_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
    )
    origin_name = str(
        getattr(origin_endpoint, "name", "") or _ha_peer_name(hass)
    ).strip()
    ha_origin = bool(origin_endpoint_id)
    call_ingress = "trunk" if invite.received_via_trunk else "extension"
    members = _unique_group_members(entry.metadata.get("members"))
    try:
        ring_policy = RingPolicy.from_metadata(entry.metadata)
    except (TypeError, ValueError) as err:
        _LOGGER.error(
            "SIP ring group has invalid policy call_id=%s group=%s: %s",
            invite.call_id,
            entry.display_name,
            err,
        )
        _sip_send_final_response(
            hass,
            invite.call_id,
            500,
            "Server Internal Error",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.PROTOCOL_ERROR.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        return
    attempts: list[OutboundLeg] = []
    browser_legs: list[BrowserLeg] = []
    preflight_failures: list[tuple[str, str, DialDisposition, int, int]] = []
    session = registry.sessions.get(registry.resolve_session_id(invite.call_id))
    if session is None or not registry.is_generation_current(
        invite.call_id,
        session.generation,
    ):
        _LOGGER.info(
            "SIP ring group did not start for a terminated source call_id=%s",
            invite.call_id,
        )
        return
    call_generation = session.generation

    def _call_is_current() -> bool:
        return registry.is_generation_current(
            invite.call_id,
            call_generation,
        )

    source_endpoint_id = str(
        origin_endpoint_id
        or ((session.metadata if session is not None else {}) or {}).get(
            "source_endpoint_id"
        )
        or ""
    ).strip()

    def _settle_browser_candidates(
        state: str,
        reason: str,
        *,
        keep_endpoint_id: str = "",
    ) -> None:
        """Release and publish every browser candidate except the winner."""
        _settle_ring_browser_candidates(
            hass,
            registry,
            browser_legs,
            call_id=invite.call_id,
            caller=invite.caller,
            callee=entry.display_name,
            state=state,
            reason=reason,
            route_kind=GROUP_TYPE_RING,
            keep_endpoint_id=keep_endpoint_id,
        )
    async def _prepare_candidates() -> None:
        for member_order, member in enumerate(members):
            if _caller_matches_member(
                invite.caller,
                invite.source_host,
                member,
                peers,
                source_endpoint_id=source_endpoint_id,
            ):
                continue
            browser_leg = _browser_leg_for_member(
                member, peers, roster_entries
            )
            if browser_leg is not None:
                if browser_leg.endpoint_id == source_endpoint_id:
                    continue
                endpoint = (
                    endpoint_registry.get(browser_leg.endpoint_id)
                    if endpoint_registry is not None
                    else None
                )
                disposition = _endpoint_preflight_disposition(
                    endpoint,
                    call_id=invite.call_id,
                    browser=True,
                )
                if disposition is not None:
                    preflight_failures.append(
                        (
                            f"preflight:{member_order}:{disposition.value}:{browser_leg.endpoint_id}",
                            browser_leg.endpoint_id,
                            disposition,
                            ring_policy.member_tiers.get(member.casefold(), 0),
                            member_order * 1000,
                        )
                    )
                    continue
                try:
                    registry.claim_endpoint(
                        invite.call_id,
                        browser_leg.endpoint_id,
                        role="group_candidate",
                    )
                except EndpointBusyError:
                    preflight_failures.append(
                        (
                            f"preflight:{member_order}:busy:{browser_leg.endpoint_id}",
                            browser_leg.endpoint_id,
                            DialDisposition.BUSY,
                            ring_policy.member_tiers.get(member.casefold(), 0),
                            member_order * 1000,
                        )
                    )
                    continue
                browser_legs.append(browser_leg)
                continue
            logical_endpoint = _logical_endpoint_for_member(
                member, peers, roster_entries
            )
            logical_endpoint_id = str(
                getattr(logical_endpoint, "endpoint_id", "") or ""
            ).strip()
            if logical_endpoint_id == source_endpoint_id:
                continue
            disposition = _endpoint_preflight_disposition(
                logical_endpoint,
                call_id=invite.call_id,
                browser=False,
            )
            if disposition is not None:
                preflight_failures.append(
                    (
                        f"preflight:{member_order}:{disposition.value}:{logical_endpoint_id}",
                        logical_endpoint_id,
                        disposition,
                        ring_policy.member_tiers.get(member.casefold(), 0),
                        member_order * 1000,
                    )
                )
                continue
            contact_targets = build_sip_contact_targets(
                (member,),
                roster_entries,
                policy=ring_policy,
                exclude_endpoint_id=source_endpoint_id,
            )
            target_specs = contact_targets or (None,)
            for contact_order, target_spec in enumerate(target_specs):
                if len(attempts) >= MAX_RING_GROUP_ATTEMPTS:
                    _LOGGER.warning(
                        "SIP ring group %s has more than %d dialable contacts; "
                        "excess contacts were skipped",
                        entry.display_name,
                        MAX_RING_GROUP_ATTEMPTS,
                    )
                    return
                try:
                    leg = _prepare_outbound_leg(
                        member=member,
                        peers=peers,
                        roster_entries=roster_entries,
                        local_name=invite.caller or _ha_peer_name(hass),
                        local_rtp_port_index=1,
                        uri_override=(
                            target_spec.uri if target_spec is not None else ""
                        ),
                        endpoint_id_override=(
                            target_spec.endpoint_id
                            if target_spec is not None
                            else ""
                        ),
                        peer_user_agent_override=(
                            target_spec.user_agent
                            if target_spec is not None
                            else ""
                        ),
                        candidate_id=(
                            target_spec.candidate_id
                            if target_spec is not None
                            else f"sip:{member_order}:{contact_order}:{member}"
                        ),
                        tier=(
                            target_spec.tier
                            if target_spec is not None
                            else ring_policy.member_tiers.get(
                                member.casefold(), 0
                            )
                        ),
                        order=(
                            target_spec.order
                            if target_spec is not None
                            else member_order * 1000 + contact_order
                        ),
                        invite=invite,
                    )
                except RuntimeError as err:
                    _LOGGER.warning(
                        "SIP ring group RTP port allocation failed member=%s: %s",
                        member,
                        err,
                    )
                    return
                if leg is None:
                    continue
                if leg.endpoint_id == source_endpoint_id:
                    await _close_outbound_leg(leg)
                    continue
                try:
                    if leg.endpoint_id:
                        registry.claim_endpoint(
                            invite.call_id,
                            leg.endpoint_id,
                            role="group_candidate",
                            adopt_transport=_endpoint_is_esphome(
                                logical_endpoint
                            ),
                        )
                except EndpointBusyError:
                    await _close_outbound_leg(leg)
                    preflight_failures.append(
                        (
                            f"preflight:{leg.candidate_id}:busy",
                            leg.endpoint_id,
                            DialDisposition.BUSY,
                            leg.tier,
                            leg.order,
                        )
                    )
                    continue
                attempts.append(leg)

    try:
        await _prepare_candidates()
        if not _call_is_current():
            _settle_browser_candidates(
                CallState.CANCELLED.value,
                TerminalReason.CANCELLED.value,
            )
            await _cleanup_outbound_attempts([], attempts)
            return
    except asyncio.CancelledError:
        _settle_browser_candidates(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
        )
        await _cleanup_outbound_attempts([], attempts)
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.CANCELLED.value,
            state=CallState.CANCELLED.value,
        )
        raise
    except Exception as err:
        _LOGGER.exception(
            "SIP ring group candidate preparation failed call_id=%s: %s",
            invite.call_id,
            err,
        )
        _settle_browser_candidates(
            CallState.TRANSPORT_UNREACHABLE.value,
            TerminalReason.PROTOCOL_ERROR.value,
        )
        await _cleanup_outbound_attempts([], attempts)
        _sip_send_final_response(
            hass,
            invite.call_id,
            500,
            "Server Internal Error",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.PROTOCOL_ERROR.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        return
    route_future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_routes(hass)[invite.call_id] = {
        "invite": invite,
        "future": route_future,
        "ring_group_endpoint_ids": tuple(
            leg.endpoint_id for leg in browser_legs
        ),
        "declined_endpoint_ids": set(),
    }
    try:
        if browser_legs:
            registry.upsert(
                invite.call_id,
                state=CallState.RINGING.value,
                owner="ha_softphone",
                caller=invite.caller,
                callee=entry.display_name,
                route_kind=GROUP_TYPE_RING,
                endpoint_id=(origin_endpoint_id if ha_origin else ""),
                source_endpoint_id=source_endpoint_id,
                ring_endpoint_ids=tuple(
                    leg.endpoint_id for leg in browser_legs
                ),
                media_client_id=origin_media_client_id,
            )
            for browser_leg in browser_legs:
                registry.add_leg(
                    invite.call_id,
                    f"browser:{browser_leg.endpoint_id}",
                    role="ha_softphone",
                    state=CallState.RINGING.value,
                )
                _set_ha_softphone_call_state(
                    hass,
                    CallState.RINGING.value,
                    endpoint_id=browser_leg.endpoint_id,
                    session_device_id=browser_leg.device_id,
                    caller=invite.caller,
                    callee=entry.display_name,
                    peer_name=invite.caller,
                    direction="incoming",
                    call_id=invite.call_id,
                    selected_tx_format=(
                        invite.send_format.audio_format.wire_token()
                    ),
                    selected_rx_format=(
                        invite.recv_format.audio_format.wire_token()
                    ),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    audio_mode="full_duplex",
                    route_kind=GROUP_TYPE_RING,
                    sip_status_code=180,
                    last_sip_event="INVITE",
                )
    except asyncio.CancelledError:
        _pending_routes(hass).pop(invite.call_id, None)
        _settle_browser_candidates(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
        )
        await _cleanup_outbound_attempts([], attempts)
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.CANCELLED.value,
            state=CallState.CANCELLED.value,
        )
        raise
    except Exception as err:
        _LOGGER.exception(
            "SIP ring group state publication failed call_id=%s: %s",
            invite.call_id,
            err,
        )
        _pending_routes(hass).pop(invite.call_id, None)
        _settle_browser_candidates(
            CallState.TRANSPORT_UNREACHABLE.value,
            TerminalReason.PROTOCOL_ERROR.value,
        )
        await _cleanup_outbound_attempts([], attempts)
        _sip_send_final_response(
            hass,
            invite.call_id,
            500,
            "Server Internal Error",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.PROTOCOL_ERROR.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        return
    if not attempts and not browser_legs and not preflight_failures:
        _pending_routes(hass).pop(invite.call_id, None)
        if ha_origin:
            _set_ha_softphone_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value,
                endpoint_id=origin_endpoint_id,
                session_device_id=origin_device_id,
                caller=origin_name,
                callee=entry.display_name,
                peer_name=entry.display_name,
                direction="outgoing",
                call_id=invite.call_id,
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                terminal_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                origin="remote",
                sip_status_code=480,
                last_sip_event="SIP_RESPONSE",
                route_kind=GROUP_TYPE_RING,
            )
        _sip_send_final_response(
            hass,
            invite.call_id,
            480,
            "Temporarily Unavailable",
            decline_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
        )
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        return

    async def _dial(attempt: OutboundLeg) -> tuple[str, OutboundLeg]:
        client = attempt.client
        uri = attempt.uri
        result = await client.invite(
            target=uri.user or attempt.member,
            remote_host=uri.host,
            remote_sip_port=uri.port or int(cfg["sip_port"]),
            request_uri=str(uri),
            timeout=8.0,
        )
        if result == "ringing":
            result = await client.wait_for_final(timeout=RING_GROUP_TIMEOUT_S)
        return result, attempt

    browser_decision: dict[str, Any] = {}

    async def _wait_browser() -> tuple[str, BrowserLeg | dict]:
        try:
            decision = await asyncio.wait_for(
                route_future, timeout=RING_GROUP_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return "timeout", {"member": "__browser__", "browser": True}
        action = str((decision or {}).get("action") or "").strip().lower()
        browser_decision.update(decision or {})
        selected_endpoint_id = str(
            (decision or {}).get("endpoint_id") or ""
        ).strip()
        selected = next(
            (
                leg
                for leg in browser_legs
                if leg.endpoint_id == selected_endpoint_id
            ),
            None,
        )
        if action in {"answer_ha", "default"}:
            if selected is None:
                return "declined", {
                    "member": "__browser__",
                    "browser": True,
                }
            return "in_call_browser", selected
        if action in {"forward", "bridge"}:
            return "reroute", dict(decision or {})
        if action == "busy":
            return "busy", selected or {
                "member": "__browser__",
                "browser": True,
            }
        if action == "cancel":
            return "cancelled", selected or {
                "member": "__caller__",
                "caller_control": True,
            }
        return "declined", selected or {
            "member": "__browser__",
            "browser": True,
        }

    async def _wait_caller_cancel() -> tuple[str, dict]:
        try:
            decision = await asyncio.wait_for(
                route_future, timeout=RING_GROUP_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return "timeout", {"member": "__caller__", "caller_control": True}
        action = str((decision or {}).get("action") or "").strip().lower()
        return (
            "cancelled" if action == "cancel" else "ignored",
            {"member": "__caller__", "caller_control": True},
        )

    # DialForkController owns every branch task and its loser cleanup
    # barrier.  Keep this compatibility list empty for later rollback
    # helpers, which may still close the selected branch after media setup
    # fails but must never own the fork tasks themselves.
    tasks: list[asyncio.Task] = []

    def _outcome(result: str) -> DialOutcome:
        disposition = {
            "in_call": DialDisposition.ANSWERED,
            "in_call_browser": DialDisposition.ANSWERED,
            "busy": DialDisposition.BUSY,
            "dnd": DialDisposition.DND,
            "declined": DialDisposition.DECLINED,
            "timeout": DialDisposition.TIMEOUT,
            "media_incompatible": DialDisposition.MEDIA_INCOMPATIBLE,
            "auth_required_unsupported": DialDisposition.AUTH_FAILED,
            "proxy_auth_required_unsupported": DialDisposition.AUTH_FAILED,
            "cancelled": DialDisposition.CANCELLED,
            "reroute": DialDisposition.REROUTE,
        }.get(result, DialDisposition.UNAVAILABLE)
        return DialOutcome(disposition, reason=result)

    candidate_payloads: dict[str, OutboundLeg | BrowserLeg | dict] = {}
    fork_candidates: list[DialCandidate] = []
    for candidate_id, endpoint_id, disposition, tier, order in preflight_failures:
        async def _dial_preflight(
            result: DialDisposition = disposition,
        ) -> DialOutcome:
            return DialOutcome(result)

        async def _close_preflight(_mode: LegCloseMode) -> None:
            return None

        fork_candidates.append(
            DialCandidate(
                candidate_id,
                _dial_preflight,
                _close_preflight,
                tier=tier,
                order=order,
                endpoint_id=endpoint_id,
            )
        )
    for attempt in attempts:
        candidate_id = attempt.candidate_id or (
            f"sip:{attempt.client.dialog_ids.call_id}"
        )
        candidate_payloads[candidate_id] = attempt

        async def _dial_sip(
            outbound: OutboundLeg = attempt,
        ) -> DialOutcome:
            result, _attempt = await _dial(outbound)
            if result == "in_call" and outbound.client.dialog is None:
                return DialOutcome(
                    DialDisposition.PROTOCOL_ERROR,
                    500,
                    "protocol_error",
                )
            return _outcome(result)

        async def _close_sip(
            mode: LegCloseMode,
            outbound: OutboundLeg = attempt,
        ) -> None:
            await _close_outbound_leg(
                outbound,
                bye_or_cancel=mode
                in {LegCloseMode.CANCEL_OR_BYE, LegCloseMode.BYE},
            )

        fork_candidates.append(
            DialCandidate(
                candidate_id,
                _dial_sip,
                _close_sip,
                tier=attempt.tier,
                order=attempt.order,
                endpoint_id=attempt.endpoint_id,
            )
        )

    control_tier = min(
        (candidate.tier for candidate in fork_candidates),
        default=0,
    )
    if browser_legs:
        browser_candidate_id = "browser:route-control"

        async def _dial_browser() -> DialOutcome:
            result, selected = await _wait_browser()
            candidate_payloads[browser_candidate_id] = selected
            if result == "cancelled":
                return DialOutcome(
                    DialDisposition.SOURCE_CANCELLED,
                    487,
                    result,
                )
            return _outcome(result)

        async def _close_browser(_mode: LegCloseMode) -> None:
            return None

        fork_candidates.append(
            DialCandidate(
                browser_candidate_id,
                _dial_browser,
                _close_browser,
                tier=control_tier,
                order=-2,
                control=True,
            )
        )
    else:
        caller_candidate_id = "caller:route-control"

        async def _dial_caller_control() -> DialOutcome:
            result, selected = await _wait_caller_cancel()
            candidate_payloads[caller_candidate_id] = selected
            if result == "cancelled":
                return DialOutcome(
                    DialDisposition.SOURCE_CANCELLED,
                    487,
                    result,
                )
            return _outcome(result)

        async def _close_caller_control(_mode: LegCloseMode) -> None:
            return None

        fork_candidates.append(
            DialCandidate(
                caller_candidate_id,
                _dial_caller_control,
                _close_caller_control,
                tier=control_tier,
                order=-2,
                control=True,
            )
        )

    async def _cleanup_ring_resources(reason: str) -> None:
        """Tear down every ownership layer after an aborted group call."""
        _pending_routes(hass).pop(invite.call_id, None)
        (
            _source_call_id,
            _dest_call_id,
            relay,
            bridge_client,
            watcher,
            _called_by_dest,
        ) = registry.detach_bridge(invite.call_id)
        if relay is not None or bridge_client is not None:
            current = asyncio.current_task()
            cleanup = asyncio.create_task(
                async_cleanup_sip_runtime(
                    relay=relay,
                    client=bridge_client,
                    watcher=(watcher if watcher is not current else None),
                    terminate_client=True,
                    relay_first=True,
                ),
                name=f"voip-ring-group-bridge-cleanup-{invite.call_id}",
            )
            await async_wait_for_cleanup(cleanup)
        remaining_attempts = [
            attempt
            for attempt in attempts
            if attempt.client is not bridge_client
        ]
        await _cleanup_outbound_attempts(tasks, remaining_attempts)
        active_media = registry.take_media(invite.call_id)
        _release_media_reservation(active_media)

        # HA-to-HA ring groups switch to the transport-neutral local bridge
        # after selection.  If answer publication then fails, terminate
        # that newly-created call as part of the same rollback boundary.
        from .local_softphone_runtime import local_softphone_bridge

        local_bridge = local_softphone_bridge(hass)
        local_call = (
            local_bridge.get_call(invite.call_id)
            if local_bridge is not None
            else None
        )
        if local_call is not None:
            with contextlib.suppress(Exception):
                local_bridge.hangup(
                    invite.call_id,
                    local_call.caller_endpoint_id,
                )
        registry.finish_and_pop(
            invite.call_id,
            reason=reason,
            state=(
                CallState.CANCELLED.value
                if reason == TerminalReason.CANCELLED.value
                else CallState.TRANSPORT_UNREACHABLE.value
            ),
        )

    async def _abort_stale_ring_group() -> bool:
        """Close every fork if the source generation lost ownership."""

        if _call_is_current():
            return False
        _settle_browser_candidates(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
        )
        await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
        return True

    winner: OutboundLeg | BrowserLeg | dict | None = None
    browser_winner = False
    reroute_decision: dict[str, Any] | None = None
    final_result = "timeout"
    try:
        pbx_runtime = hass.data.get(DOMAIN, {}).get("pbx_runtime")
        authoritative_session = (
            pbx_runtime.get_session(
                invite.call_id,
                generation=call_generation,
            )
            if pbx_runtime is not None
            else None
        )
        if authoritative_session is None:
            await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
            return
        fork_result = await DialForkController(
            authoritative_session,
            fork_candidates,
            strategy=ring_policy.strategy,
            tier_strategies=ring_policy.tier_strategies,
            overall_timeout=ring_policy.overall_timeout,
            step_timeout=ring_policy.step_timeout,
        ).run(
            lambda _candidate, _dial_outcome: _call_is_current()
        )
        if fork_result.winner is not None:
            winner = candidate_payloads.get(
                fork_result.winner.candidate_id
            )
            browser_winner = isinstance(winner, BrowserLeg)
        elif fork_result.outcome.disposition is DialDisposition.REROUTE:
            reroute_decision = dict(browser_decision)
        final_result = {
            DialDisposition.BUSY: "busy",
            DialDisposition.DND: "dnd",
            DialDisposition.DECLINED: "declined",
            DialDisposition.TIMEOUT: "timeout",
            DialDisposition.MEDIA_INCOMPATIBLE: "media_incompatible",
            DialDisposition.AUTH_FAILED: "auth_required_unsupported",
            DialDisposition.CANCELLED: "cancelled",
            DialDisposition.SOURCE_CANCELLED: "cancelled",
            DialDisposition.PROTOCOL_ERROR: "protocol_error",
            DialDisposition.UNAVAILABLE: "transport_unreachable",
        }.get(fork_result.outcome.disposition, final_result)
        if await _abort_stale_ring_group():
            return
        winner_endpoint_id = str(
            getattr(winner, "endpoint_id", "") or ""
        )
        for losing_endpoint_id in {
            attempt.endpoint_id
            for attempt in attempts
            if attempt.endpoint_id
            and attempt.endpoint_id != winner_endpoint_id
        }:
            registry.release_endpoint_claim(
                invite.call_id,
                losing_endpoint_id,
            )
        if await _abort_stale_ring_group():
            return
        if reroute_decision is not None:
            route = _pending_routes(hass).pop(invite.call_id, None) or {}
            _settle_browser_candidates(
                CallState.IDLE.value,
                "forwarded",
            )
            handoff = route.get("forward_handoff")
            if handoff is not None and not handoff.done():
                handoff.set_result(dict(reroute_decision))
            return
        if winner is not None:
            candidate_state = CallState.CANCELLED.value
            candidate_reason = TerminalReason.CANCELLED.value
        else:
            (
                _candidate_status,
                _candidate_sip_reason,
                candidate_reason,
                candidate_state,
            ) = _sip_failure_response(final_result)
        _settle_browser_candidates(
            candidate_state,
            candidate_reason,
            keep_endpoint_id=(
                winner.endpoint_id
                if browser_winner and isinstance(winner, BrowserLeg)
                else ""
            ),
        )
        if winner is None:
            _pending_routes(hass).pop(invite.call_id, None)
            status_code, sip_reason, terminal_reason, public_state = (
                _sip_failure_response(final_result)
            )
            if ha_origin:
                _set_ha_softphone_call_state(
                    hass,
                    public_state,
                    endpoint_id=origin_endpoint_id,
                    session_device_id=origin_device_id,
                    caller=origin_name,
                    callee=entry.display_name,
                    peer_name=entry.display_name,
                    direction="outgoing",
                    call_id=invite.call_id,
                    reason=terminal_reason,
                    terminal_reason=terminal_reason,
                    origin="remote",
                    sip_status_code=status_code,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
            _sip_send_final_response(
                hass,
                invite.call_id,
                status_code,
                sip_reason,
                decline_reason=terminal_reason,
            )
            _set_sip_bridge_call_state(
                hass,
                public_state,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.target,
                call_id=invite.call_id,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                sip_status_code=status_code,
                last_sip_event="SIP_RESPONSE",
                route_kind=GROUP_TYPE_RING,
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=terminal_reason,
                state=public_state,
            )
            return
        if browser_winner and isinstance(winner, BrowserLeg):
            if await _abort_stale_ring_group():
                return
            _pending_routes(hass).pop(invite.call_id, None)
            connected_party = winner.name
            winner_media_client_id = str(
                browser_decision.get("media_client_id") or ""
            ).strip()
            if ha_origin:
                from .local_softphone_runtime import (
                    local_softphone_bridge,
                    start_local_softphone_call,
                )

                original_context = registry.ha_context(invite.call_id)
                if await _abort_stale_ring_group():
                    return
                registry.finish_and_pop(
                    invite.call_id,
                    reason="local_group_selected",
                    state=CallState.IDLE.value,
                )
                snapshot = start_local_softphone_call(
                    hass,
                    origin_endpoint_id,
                    winner.endpoint_id,
                    call_id=invite.call_id,
                    request_video=request_video,
                    enable_caller_video_send=enable_caller_video_send,
                    caller_owner_id=origin_media_client_id,
                    context=original_context,
                )
                bridge = local_softphone_bridge(hass)
                if bridge is None:
                    raise RuntimeError("local softphone bridge is unavailable")
                bridge.answer(
                    snapshot.call_id,
                    winner.endpoint_id,
                    winner_media_client_id,
                    enable_video_send=bool(
                        browser_decision.get("send_video", False)
                    ),
                )
                return
            local_rtp_port = _allocate_sip_rtp_port(hass)
            answer = build_answer_directional(
                local_ip,
                local_ip,
                local_rtp_port,
                invite.send_format,
                invite.recv_format,
                dtmf=_invite_dtmf_format(invite),
                remote_sdp=invite.remote_sdp,
            )
            committed = registry.transition(
                invite.call_id,
                state=CallState.IN_CALL.value,
                owner="ha_softphone",
                caller=invite.caller,
                callee=entry.display_name,
                route_kind=GROUP_TYPE_RING,
                expected_generation=call_generation,
                endpoint_id=winner.endpoint_id,
                dest_endpoint_id=winner.endpoint_id,
                media_client_id=winner_media_client_id,
            )
            if committed is None:
                await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
                return
            media = {
                "invite": invite,
                "local_rtp_port": local_rtp_port,
                "endpoint_id": winner.endpoint_id,
                "media_client_id": winner_media_client_id,
            }
            registry.pending_invites.pop(invite.call_id, None)
            registry.attach_media(invite.call_id, media)
            registry.add_leg(
                invite.call_id,
                f"browser:{winner.endpoint_id}",
                role="ha_softphone",
                state=CallState.IN_CALL.value,
            )
            if not _sip_send_final_response(
                hass, invite.call_id, 200, "OK", answer_sdp=answer
            ):
                await _cleanup_ring_resources(
                    TerminalReason.CANCELLED.value
                    if not _call_is_current()
                    else TerminalReason.PROTOCOL_ERROR.value
                )
                return
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                endpoint_id=winner.endpoint_id,
                session_device_id=winner.device_id,
                caller=invite.caller,
                callee=entry.display_name,
                peer_name=invite.caller,
                direction="incoming",
                call_id=invite.call_id,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                audio_mode="full_duplex",
                route_kind=GROUP_TYPE_RING,
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
            )
            # Mirror the same established-call contract used when a SIP
            # endpoint wins: retain the group as dialed target and expose
            # the HA softphone as the party that actually answered.
            _set_sip_bridge_call_state(
                hass,
                CallState.IN_CALL.value,
                caller=invite.caller,
                callee=entry.display_name,
                peer_name=connected_party,
                call_id=invite.call_id,
                dialed_target=entry.display_name,
                connected_party=connected_party,
                answered_by=connected_party,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                route_kind=GROUP_TYPE_RING,
            )
            return
        _pending_routes(hass).pop(invite.call_id, None)
        if not isinstance(winner, OutboundLeg):
            _LOGGER.error(
                "SIP ring group selected an invalid winner for call_id=%s",
                invite.call_id,
            )
            _sip_send_final_response(
                hass,
                invite.call_id,
                500,
                "Server Internal Error",
                decline_reason=TerminalReason.PROTOCOL_ERROR.value,
            )
            if ha_origin:
                _set_ha_softphone_call_state(
                    hass,
                    CallState.TRANSPORT_UNREACHABLE.value,
                    endpoint_id=origin_endpoint_id,
                    session_device_id=origin_device_id,
                    caller=origin_name,
                    callee=entry.display_name,
                    peer_name=entry.display_name,
                    direction="outgoing",
                    call_id=invite.call_id,
                    reason=TerminalReason.PROTOCOL_ERROR.value,
                    terminal_reason=TerminalReason.PROTOCOL_ERROR.value,
                    origin="self",
                    sip_status_code=500,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.PROTOCOL_ERROR.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return
        client = winner.client
        source_relay_port, dest_relay_port = winner.ports.ports
        video_answer = None
        if winner.video_relay is not None and client.dialog is not None:
            video_answer = configure_answered_invite_video_relay(
                invite,
                client.dialog,
                winner.video_relay,
            )
            if video_answer is None:
                _LOGGER.info(
                    "SIP ring group video rejected by winning branch "
                    "call_id=%s member=%s",
                    invite.call_id,
                    winner.member,
                )
                await winner.video_relay.stop()
                winner.video_relay = None
                winner.video_failure_reason = "remote_video_rejected"
        bridge_session = registry.register_bridge(
            source_call_id=invite.call_id,
            dest_call_id=client.dialog_ids.call_id,
            client=client,
            state=CallState.CONNECTING.value,
            caller=invite.caller,
            callee=invite.target,
            route_kind=GROUP_TYPE_RING,
            ingress=call_ingress,
            origin=call_ingress,
            source_state=CallState.CONNECTING.value,
            dest_state=CallState.IN_CALL.value,
            expected_generation=call_generation,
        )
        if bridge_session is None:
            await _close_outbound_leg(winner, bye_or_cancel=True)
            return
        try:
            if ha_origin:
                relay = build_local_client_relay(
                    client=client,
                    local_host=local_ip,
                    local_to_relay_format=invite.recv_format,
                    relay_to_local_format=invite.send_format,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    capture_name=f"{invite.call_id}_{client.dialog_ids.call_id}",
                    debug_capture=_debug_mode(hass),
                    on_release=lambda ports: _release_sip_rtp_port_pair(
                        hass, ports
                    ),
                )
            else:
                relay = build_invite_client_relay(
                    invite=invite,
                    client=client,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    debug_capture=_debug_mode(hass),
                    on_release=lambda ports: _release_sip_rtp_port_pair(
                        hass, ports
                    ),
                )
            _attach_dtmf_event_bridge(
                hass,
                relay,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                caller=invite.caller,
                callee=str(winner.member or invite.target),
                client=client,
            )
            if winner.video_relay is not None:
                relay.attach_video_relay(winner.video_relay)
            await relay.start()
        except Exception as err:
            _LOGGER.warning("SIP ring group media bridge unavailable: %s", err)
            _sip_send_final_response(
                hass,
                invite.call_id,
                488,
                "Not Acceptable Here",
                decline_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
            )
            if ha_origin:
                _set_ha_softphone_call_state(
                    hass,
                    CallState.MEDIA_INCOMPATIBLE.value,
                    endpoint_id=origin_endpoint_id,
                    session_device_id=origin_device_id,
                    caller=origin_name,
                    callee=entry.display_name,
                    peer_name=str(winner.member or entry.display_name),
                    direction="outgoing",
                    call_id=invite.call_id,
                    reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                    terminal_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                    origin="self",
                    sip_status_code=488,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
            registry.discard_bridge_session(
                invite.call_id,
                client.dialog_ids.call_id,
                reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                state=CallState.MEDIA_INCOMPATIBLE.value,
            )
            await _close_outbound_leg(winner)
            return
        if not _call_is_current():
            await relay.stop()
            await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
            return
        winner.ports.detach()
        if winner.video_relay is not None:
            # The audio relay now owns and tears down the video relay.
            winner.video_relay = None
        _attach_client_media_update(
            client,
            relay,
            source_call_id=invite.call_id,
        )
        dialed_target = entry.display_name or invite.target
        connected_party = str(winner.member or "").strip() or invite.target
        if ha_origin:
            # The synthetic HA caller has no SIP/RTP socket of its own.
            # Feed the already-running source side of the relay from the
            # authenticated browser websocket via a local UDP endpoint.
            softphone_media = {
                "rtp_loopback": True,
                "remote_rtp_host": local_ip,
                "remote_rtp_port": source_relay_port,
                "send_format": invite.recv_format,
                "recv_format": invite.send_format,
                "local_ssrc": secrets.randbelow(0xFFFFFFFF) + 1,
                "endpoint_id": origin_endpoint_id,
            }
        else:
            softphone_media = None
        committed = registry.transition(
            invite.call_id,
            state=CallState.IN_CALL.value,
            owner="ha_softphone",
            caller=invite.caller,
            callee=dialed_target,
            route_kind=GROUP_TYPE_RING,
            endpoint_id=origin_endpoint_id if ha_origin else "",
            source_endpoint_id=source_endpoint_id,
            dest_endpoint_id=winner.endpoint_id,
            media_client_id=origin_media_client_id,
            expected_generation=call_generation,
        )
        if committed is None:
            await relay.stop()
            await _close_outbound_leg(winner, bye_or_cancel=True)
            return
        registry.attach_relay(invite.call_id, relay)
        if softphone_media is not None:
            registry.attach_media(invite.call_id, softphone_media)
        if not ha_origin:
            answer = build_answer_directional(
                local_ip,
                local_ip,
                source_relay_port,
                invite.send_format,
                invite.recv_format,
                dtmf=_invite_dtmf_format(invite),
                remote_sdp=invite.remote_sdp,
                video_port=(
                    relay.video_relay.left_port
                    if relay.video_relay is not None
                    else 0
                ),
                video_format=(
                    video_answer.video_format
                    if video_answer is not None
                    else None
                ),
                video_direction=(
                    video_answer.direction
                    if video_answer is not None
                    else "inactive"
                ),
            )
            if not _sip_send_final_response(
                hass,
                invite.call_id,
                200,
                "OK",
                answer_sdp=answer,
            ):
                await _cleanup_ring_resources(
                    TerminalReason.CANCELLED.value
                    if not _call_is_current()
                    else TerminalReason.PROTOCOL_ERROR.value
                )
                return
        _set_sip_bridge_call_state(
            hass,
            CallState.IN_CALL.value,
            caller=invite.caller,
            callee=dialed_target,
            peer_name=connected_party,
            call_id=invite.call_id,
            dest_call_id=client.dialog_ids.call_id,
            dialed_target=dialed_target,
            connected_party=connected_party,
            answered_by=connected_party,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            route_kind=GROUP_TYPE_RING,
            sip_uri=str(winner.uri),
        )
        if ha_origin:
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                endpoint_id=origin_endpoint_id,
                session_device_id=origin_device_id,
                caller=invite.caller,
                callee=dialed_target,
                peer_name=connected_party,
                direction="outgoing",
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                dialed_target=dialed_target,
                connected_party=connected_party,
                answered_by=connected_party,
                selected_tx_format=invite.recv_format.audio_format.wire_token(),
                selected_rx_format=invite.send_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.recv_format.wire_token(),
                selected_rx_rtp_format=invite.send_format.wire_token(),
                audio_mode="full_duplex",
                route_kind=GROUP_TYPE_RING,
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                sip_uri=str(winner.uri),
            )
        current_task = asyncio.current_task()
        if current_task is not None:
            registry.attach_client_watcher(
                client.dialog_ids.call_id,
                current_task,
            )
        terminal = await client.wait_for_dialog_termination()
        terminal_reason = (
            TerminalReason.REMOTE_HANGUP.value
            if terminal == "remote_hangup"
            else _sip_terminal_reason(terminal, _sip_public_state(terminal))
        )
        await _terminate_sip_bridge(
            hass,
            client.dialog_ids.call_id,
            endpoint_id=(origin_endpoint_id if ha_origin else DEFAULT_ENDPOINT_ID),
            session_device_id=(
                origin_device_id if ha_origin else HA_SOFTPHONE_DEVICE_ID
            ),
            terminal_reason=terminal_reason,
        )
    except asyncio.CancelledError:
        _settle_browser_candidates(
            CallState.CANCELLED.value,
            TerminalReason.CANCELLED.value,
        )
        if ha_origin:
            with contextlib.suppress(Exception):
                _set_ha_softphone_call_state(
                    hass,
                    CallState.CANCELLED.value,
                    endpoint_id=origin_endpoint_id,
                    session_device_id=origin_device_id,
                    caller=origin_name,
                    callee=entry.display_name,
                    peer_name=entry.display_name,
                    direction="outgoing",
                    call_id=invite.call_id,
                    reason=TerminalReason.CANCELLED.value,
                    terminal_reason=TerminalReason.CANCELLED.value,
                    origin="self",
                    last_sip_event="CANCEL",
                    route_kind=GROUP_TYPE_RING,
                )
        await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
        raise
    except Exception as err:
        _LOGGER.exception(
            "SIP ring group runtime failed call_id=%s: %s",
            invite.call_id,
            err,
        )
        _settle_browser_candidates(
            CallState.TRANSPORT_UNREACHABLE.value,
            TerminalReason.PROTOCOL_ERROR.value,
        )
        if ha_origin:
            with contextlib.suppress(Exception):
                _set_ha_softphone_call_state(
                    hass,
                    CallState.TRANSPORT_UNREACHABLE.value,
                    endpoint_id=origin_endpoint_id,
                    session_device_id=origin_device_id,
                    caller=origin_name,
                    callee=entry.display_name,
                    peer_name=entry.display_name,
                    direction="outgoing",
                    call_id=invite.call_id,
                    reason=TerminalReason.PROTOCOL_ERROR.value,
                    terminal_reason=TerminalReason.PROTOCOL_ERROR.value,
                    origin="self",
                    sip_status_code=500,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
        _sip_send_final_response(
            hass,
            invite.call_id,
            500,
            "Server Internal Error",
            decline_reason=TerminalReason.PROTOCOL_ERROR.value,
        )
        await _cleanup_ring_resources(TerminalReason.PROTOCOL_ERROR.value)
