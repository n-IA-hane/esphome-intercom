"""Compose SIP call routing from the existing dialer, feature handlers and active PBX."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
import logging
from typing import TYPE_CHECKING

from .call_events import home_phone_name
from .call_forwarder import ForwardRuntime, async_forward_existing_call
from .conference_ringing import ConferenceRingRuntime, async_ring_conference_members
from .config import trunk_config, trunk_enabled
from .core import sdp as sip_sdp
from .core.audio_capabilities import HA_AUDIO_CAPABILITIES
from .core.audio_format import HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS
from .endpoint_group_calls import EndpointGroupCalls
from .endpoint_lifecycle import call_registry
from .endpoint_routing import EndpointRouteResolver
from .endpoint_session import TerminationInitiator, TerminationIntent
from .endpoint_termination import EndpointTerminationHandler
from .fsm import CallState, TerminalReason
from .inbound_routing.softphone import defer_invite, publish_pending_ringing
from .invite_router import InviteRuntime, route_invite
from .media_ports import RtpPortReservation, reserve_delayed_offer_ports, take_delayed_offer_ports
from .media_renegotiation import async_prepare_media_update
from .ring_group_orchestrator import RingGroupRuntime, run_ring_group_call
from .router import RouteReason
from .runtime_data import sip_endpoint_manager
from .sip_listener import SipInitialInvite, SipInvite, SipInviteResult, UasDelayedOfferPlan
from .sip_runtime import send_final_response, uri_transport
from .trunk_inbound_router import TrunkInboundRuntime, async_route_trunk_invite

if TYPE_CHECKING:
    from .assist_endpoint import AssistEndpoint
    from .call_forwarder import TransferResult
    from .endpoint_dialing import EndpointDialer
    from .pbx_runtime import SipEndpointRuntime
    from .peer import Peer
    from .roster import RosterEntry
    from .runtime_data import VoipStackRuntime
    from .sip_ingress import SipIngressPolicy

_LOGGER = logging.getLogger(__name__)


def inbound_route_decision(
    dialer: EndpointDialer, invite: SipInvite, peers: list[Peer], entries: list[RosterEntry],
    *, lookup_target: str | None = None,
):
    # At this point HA owns routing. Resolve the stable HA alias to the user's
    # current peer name before consulting the canonical dial plan.
    route_target = invite.routing_target if lookup_target is None else lookup_target
    target = (
        home_phone_name(dialer.hass) if dialer.route_resolver.is_ha_target(route_target) else route_target
    )
    return EndpointRouteResolver(
        dialer.hass,
        dialer.local_ip,
        int(dialer.config["sip_port"]),
        source_endpoint_id=invite.identified_source_endpoint_id,
        trunk_id=invite.outbound_trunk_id,
    ).route(target, entries)


async def run_trunk_route(
    dialer: EndpointDialer,
    forward_call: Callable,
    defer_to_phone: Callable,
    start_assist: Callable,
    invite: SipInvite,
    *,
    bridge_ports: RtpPortReservation,
) -> None:
    """Route a detached trunk call using the current HA name and release it on failure."""
    try:
        await async_route_trunk_invite(
            TrunkInboundRuntime(
                hass=dialer.hass,
                config=dialer.config,
                local_ip=dialer.local_ip,
                ha_peer_name=home_phone_name(dialer.hass),
                route_resolver=dialer.route_resolver,
                forward_existing_call=forward_call,
                defer_invite_to_softphone=defer_to_phone,
                start_local_assist_bridge=start_assist,
            ),
            invite,
            bridge_ports=bridge_ports,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - detached call boundary.
        _LOGGER.exception("SIP trunk inbound routing failed call_id=%s", invite.call_id)
        await EndpointTerminationHandler(dialer.hass).terminate(
            invite.call_id,
            TerminationIntent.bye(RouteReason.TARGET_UNREACHABLE.value, TerminationInitiator.ROUTING),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EndpointCallRouting:
    """Listener callbacks borrow the active endpoint's feature dependencies."""

    ingress: SipIngressPolicy
    invite_runtime: InviteRuntime
    groups: EndpointGroupCalls
    forward_call: Callable[..., Awaitable[asyncio.Task[TransferResult]]]
    calls: SipEndpointRuntime
    integration: VoipStackRuntime

    async def on_invite(self, invite: SipInvite) -> SipInviteResult:
        try:
            invite = self.ingress.classify(invite)
        except ValueError:
            return SipInviteResult(403, "Ambiguous trunk source", to_tag="")
        return await route_invite(self.invite_runtime, invite)

    async def on_offerless_invite(
        self,
        initial: SipInitialInvite,
    ) -> UasDelayedOfferPlan | None:
        """Prepare one offer, then continue through the canonical router."""

        hass = self.invite_runtime.hass
        try:
            initial = self.ingress.classify(initial)
        except ValueError:
            return None
        registry = call_registry(hass)
        session = registry.upsert(
            initial.call_id,
            state=CallState.CONNECTING.value,
            owner="router",
            caller=initial.caller,
            callee=initial.target,
            ingress="trunk" if initial.received_via_trunk else "extension",
            origin="trunk" if initial.received_via_trunk else "extension",
        )
        registry.remember_invite_identity(initial.call_id, initial)
        try:
            ports = reserve_delayed_offer_ports(hass, registry, initial.call_id)
        except (OSError, RuntimeError):
            await EndpointTerminationHandler(hass).terminate_reason(
                initial.call_id,
                TerminalReason.TRANSPORT_UNREACHABLE.value,
                TerminationInitiator.MEDIA,
            )
            return None
        try:
            offer_sdp = sip_sdp.build_offer_directional(
                self.invite_runtime.local_ip,
                self.invite_runtime.local_ip,
                ports.ports[0],
                list(HA_SIP_PCM_TX_FORMATS),
                list(HA_SIP_PCM_RX_FORMATS),
                include_common_codecs=True,
                codec_policy=initial.codec_policy,
                local_capabilities=HA_AUDIO_CAPABILITIES,
            )
        except sip_sdp.SdpError:
            await EndpointTerminationHandler(hass).terminate_reason(
                initial.call_id,
                TerminalReason.MEDIA_INCOMPATIBLE.value,
                TerminationInitiator.MEDIA,
            )
            return None

        async def rollback() -> None:
            if not registry.is_generation_current(initial.call_id, session.generation):
                return
            await EndpointTerminationHandler(hass).terminate_reason(
                initial.call_id,
                TerminalReason.MEDIA_INCOMPATIBLE.value,
                TerminationInitiator.MEDIA,
            )

        async def accept_answer(invite: SipInvite) -> SipInviteResult:
            if not registry.is_generation_current(initial.call_id, session.generation):
                return SipInviteResult(487, "Request Terminated")
            result = await self.on_invite(invite)
            if not result.defer_final:
                return result

            reservation = take_delayed_offer_ports(registry, invite.call_id)
            if reservation is None:
                # A deferred bridge or fork already transferred the advertised
                # pair into its asynchronous route owner. Its final answer is
                # now signaling-only on the source dialog.
                return SipInviteResult(200, "OK", media_contract=invite)
            registry.attach_media(
                invite.call_id,
                {
                    "invite": invite,
                    "final_response_sent": True,
                    "local_rtp_port": reservation.ports[0],
                    "local_video_rtp_port": 0,
                    "video_direction": "inactive",
                    "rtp_reservation": reservation,
                },
                provisional=True,
            )
            return SipInviteResult(200, "OK", media_contract=invite)

        return UasDelayedOfferPlan(offer_sdp, accept_answer, rollback)

    async def on_media_update(
        self,
        previous: SipInvite,
        updated: SipInvite,
        method: str,
    ) -> SipInviteResult:
        hass = self.invite_runtime.hass
        registry = self.calls
        session = registry.get_session(registry.resolve_session_id(updated.call_id))
        result = await async_prepare_media_update(
            hass, self.invite_runtime.local_ip, previous, updated, method
        )
        if session is None or not 200 <= result.status < 300:
            return result
        prior_notification = result.on_committed

        def published(actual):
            from .core.sip_dialog import notify_media_commit
            from .call_projection import publish_bridge_projection

            notify_media_commit(prior_notification, actual)
            manager = sip_endpoint_manager(hass)
            if (
                registry.get_session(session.call_id, generation=session.generation) is session
                and session.live
                and manager is not None
                and registry.resolve_session_id(actual.call_id) == session.call_id
                and manager.invite_for_call(actual.call_id) is actual
            ):
                session.state_changed()
                publish_bridge_projection(hass, session, event_type="state_changed")

        return replace(result, on_committed=published)

    async def on_refer(self, call_id, target, *, source_client=None) -> int:
        from .call_transfer import async_transfer_target

        result = await async_transfer_target(
            self.integration, call_id, target, source_client=source_client, hass=self.invite_runtime.hass
        )
        if result.status:
            return result.status
        return 503 if result.state == "call_not_found" else 500


def compose_call_routing(
    *,
    dialer: EndpointDialer,
    ingress: SipIngressPolicy,
    assist: AssistEndpoint,
    calls: SipEndpointRuntime,
    integration: VoipStackRuntime,
) -> EndpointCallRouting:
    """Wire stable handler dependencies once before publishing the listeners."""
    from .peer_snapshot import async_build_peer_snapshot
    from .softphone_termination import async_terminate_sip_bridge_session

    hass = dialer.hass
    config = dialer.config
    local_ip = dialer.local_ip
    resolver = dialer.route_resolver
    defer_to_phone = partial(defer_invite, hass)
    forward = partial(
        async_forward_existing_call,
        ForwardRuntime(
            hass=hass,
            config=config,
            local_ip=local_ip,
            route_resolver=resolver,
            browser_leg_for_member=dialer.browser_leg_for_member,
            prepare_outbound_leg=dialer.prepare_outbound_leg,
            defer_invite_to_softphone=defer_to_phone,
            publish_pending_ringing=partial(publish_pending_ringing, hass),
            start_local_assist_bridge=assist.start,
        ),
    )
    ring_group = partial(
        run_ring_group_call,
        RingGroupRuntime(
            hass=hass,
            config=config,
            local_ip=local_ip,
            ha_peer_name=home_phone_name,
            browser_leg_for_member=dialer.browser_leg_for_member,
            logical_endpoint_for_member=resolver.logical_endpoint,
            prepare_outbound_leg=dialer.prepare_outbound_leg,
            terminate_sip_bridge=async_terminate_sip_bridge_session,
        ),
    )
    conference = partial(
        async_ring_conference_members,
        ConferenceRingRuntime(
            hass=hass,
            config=config,
            local_ip=local_ip,
            browser_leg_for_member=dialer.browser_leg_for_member,
            prepare_outbound_leg=dialer.prepare_outbound_leg,
        ),
    )
    groups = EndpointGroupCalls(
        hass=hass,
        config=config,
        local_ip=local_ip,
        build_peer_snapshot=async_build_peer_snapshot,
        run_ring_group=ring_group,
        ring_conference=conference,
    )
    invite = InviteRuntime(
        hass=hass,
        config=config,
        local_ip=local_ip,
        registrar=ingress.registrar,
        ha_peer_name=home_phone_name,
        get_trunk_config=trunk_config,
        trunk_enabled=trunk_enabled,
        is_trunk_invite=ingress.is_trunk,
        is_ha_target=resolver.is_ha_target,
        ha_router_decision=resolver.route,
        inbound_route_decision=partial(inbound_route_decision, dialer),
        build_peer_snapshot=async_build_peer_snapshot,
        browser_leg_for_member=dialer.browser_leg_for_member,
        defer_invite_to_softphone=defer_to_phone,
        enable_reused_sip_tcp_connection=dialer.enable_reused_tcp_connection,
        ring_conference_members=conference,
        run_ring_group_call=ring_group,
        run_trunk_inbound_route_guarded=partial(
            run_trunk_route, dialer, forward, defer_to_phone, assist.start
        ),
        send_final_response=send_final_response,
        sip_uri_transport=uri_transport,
        start_local_assist_bridge=assist.start,
        terminate_sip_bridge=async_terminate_sip_bridge_session,
    )
    return EndpointCallRouting(
        ingress=ingress,
        invite_runtime=invite,
        groups=groups,
        forward_call=forward,
        calls=calls,
        integration=integration,
    )
