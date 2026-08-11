"""Composition root for the HA-side SIP endpoint and B2BUA adapters.

This module wires transports, routing, media owners and HA projections
together.  It is intentionally not another call-state authority:
``SipEndpointRuntime`` owns logical PBX lifetimes, SIP listener/client objects
own transactions and dialogs, and staged media callbacks own offer/answer
commit or rollback.  Keep new policy in the focused domain modules and pass it
through the runtime dataclasses instead of rebuilding lifecycle state here.
"""

from __future__ import annotations

import asyncio
from functools import partial
import logging
import secrets
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .core import sdp as sip_sdp
from .core.audio_format import (
    HA_SIP_PCM_FORMATS,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
)
from .assist_endpoint import AssistEndpoint
from .call_forwarder import ForwardRuntime, async_forward_existing_call
from .config_entry_runtime import (
    async_refresh_and_push_phonebook as _refresh_and_push_phonebook,
)
from .const import (
    CONF_SIP_VIDEO,
    CONF_REGISTRAR_ENABLED,
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
)
from .endpoint_lifecycle import (
    async_stop_sip_endpoint,
    call_registry as _call_registry,
    create_runtime_task,
)
from .endpoint_dialing import EndpointDialer
from .dtmf_events import handle_sip_info
from .endpoint_routing import (
    EndpointRouteResolver,
    roster_from_peers as _roster_from_peers,
)
from .endpoint_termination import EndpointTerminationHandler
from .fsm import (
    CallState,
    TerminalReason,
)
from .media_ports import (
    RtpPortReservation,
    reserve_delayed_offer_ports,
    take_delayed_offer_ports,
)
from .media_renegotiation import async_prepare_media_update
from .call_projection import publish_phone_projection
from .invite_router import InviteRuntime, route_invite
from .endpoint_registry import EndpointBusyError
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import RouteReason
from .ring_group_orchestrator import RingGroupRuntime, run_ring_group_call
from .runtime_data import (
    browser_phone,
    endpoint_directory,
    require_runtime_data,
    sip_endpoint_manager,
    sip_trunk,
)
from .store import sip_accounts as _sip_accounts
from .trunk_inbound_router import (
    TrunkInboundRuntime,
    async_route_trunk_invite,
)

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry

_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5
MAX_TRUNK_INFO_DIGITS = 16
MAX_PENDING_HA_INVITES = 64


async def async_start_sip_endpoint(hass: HomeAssistant) -> bool:
    """Bind the enabled SIP signaling listeners for HA softphone and bridge calls."""
    from .config import (
        transport_config as _get_transport_config,
        trunk_config as _get_trunk_config,
        trunk_enabled as _trunk_enabled,
    )
    from .softphone_termination import (
        async_terminate_sip_bridge_session as _terminate_sip_bridge,
    )
    from .websocket_api import _ha_peer_name
    from .peer_snapshot import (
        async_advertise_host as _ha_advertise_host,
        async_build_peer_snapshot as _async_build_peer_snapshot,
    )
    from .sip_runtime import (
        enable_reused_tcp_connection as _enable_reused_sip_tcp_connection,
        send_final_response as _sip_send_final_response,
        uri_transport as _sip_uri_transport,
    )
    from .endpoint_session import TerminationInitiator, TerminationIntent
    from .core.sip import parse_sip_uri
    from .sip_endpoint import SipEndpointManager
    from .sip_listener import (
        SipInitialInvite,
        SipInvite,
        SipInviteResult,
        UasDelayedOfferPlan,
    )
    from .sip_registrar import SipRegistrar
    from .conference_ringing import (
        ConferenceRingRuntime,
        async_ring_conference_members,
    )
    from .groups import GROUP_TYPE_RING

    if sip_endpoint_manager(hass) is not None:
        _LOGGER.debug("Stopping existing SIP endpoint before rebinding listeners")
        await async_stop_sip_endpoint(hass)

    cfg = _get_transport_config(hass)
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        _LOGGER.error("Cannot start SIP endpoint: HA announce IP is unknown")
        return False

    async def _on_conference_inbound_timeout(call_id: str, reason: str) -> None:
        """End a timed-out inbound UAS dialog and release its logical claim."""
        await EndpointTerminationHandler(hass).terminate(
            call_id,
            TerminationIntent.bye(reason, TerminationInitiator.TIMEOUT),
        )

    def _on_registration_change(username: str, registered: bool) -> None:
        from .phone_endpoint import EndpointAvailability

        endpoint_registry = endpoint_directory(hass)
        endpoint = endpoint_registry.by_username(username)
        if (
            endpoint is not None
            and endpoint.availability is not EndpointAvailability.UNAVAILABLE
        ):
            endpoint_registry.update(
                endpoint.endpoint_id,
                availability=(
                    EndpointAvailability.AVAILABLE
                    if registered
                    else EndpointAvailability.OFFLINE
                ),
            )
        create_runtime_task(hass, _refresh_and_push_phonebook(hass))

    registrar = SipRegistrar(
        enabled=bool(cfg.get(CONF_REGISTRAR_ENABLED, False)),
        accounts=_sip_accounts(hass),
        local_ip=local_ip,
        local_sip_port=int(cfg["sip_port"]),
        on_registration_change=_on_registration_change,
    )
    runtime = require_runtime_data(hass)
    registry = _call_registry(hass)
    # Call control and its HA projection share one runtime owner.  It remains
    # dark until every listener component has transferred into it.
    pbx_runtime = registry
    pbx_runtime.attach_component("registrar", registrar)
    route_resolver = EndpointRouteResolver(
        hass=hass,
        local_ip=local_ip,
        sip_port=int(cfg["sip_port"]),
    )
    _is_ha_target = route_resolver.is_ha_target
    _ha_router_decision = route_resolver.route
    _logical_endpoint_for_member = route_resolver.logical_endpoint

    async def _on_register(request, addr, transport):
        result = await registrar.handle_register(request, addr, transport)
        if 200 <= int(result.status) < 300:
            await _refresh_and_push_phonebook(hass)
        return result

    _on_info = partial(handle_sip_info, hass)
    from .sip_application import SipApplicationMethods

    application_methods = SipApplicationMethods(hass, registrar)
    pbx_runtime.attach_component(
        "sip_application",
        application_methods,
        closer=application_methods.stop,
    )

    def _is_trunk_invite(invite: SipInvite) -> bool:
        trunk_cfg = _get_trunk_config(hass)
        trunk = sip_trunk(hass)
        return bool(
            _trunk_enabled(trunk_cfg)
            and invite.received_via_trunk
            and getattr(trunk, "registered", False)
        )

    assist_endpoint = AssistEndpoint(
        hass=hass,
        terminate_sip_bridge=_terminate_sip_bridge,
    )
    _start_local_assist_bridge = assist_endpoint.start

    endpoint_dialer = EndpointDialer(
        hass=hass,
        local_ip=local_ip,
        config=cfg,
        route_resolver=route_resolver,
        sip_uri_transport=_sip_uri_transport,
        enable_reused_tcp_connection=_enable_reused_sip_tcp_connection,
    )
    _browser_leg_for_member = endpoint_dialer.browser_leg_for_member
    _prepare_outbound_leg = endpoint_dialer.prepare_outbound_leg

    def _publish_pending_ha_softphone_ringing(
        invite: SipInvite,
        *,
        route_kind: str,
        endpoint_id: str,
        endpoint_device_id: str,
        callee: str,
        sip_uri: str | None = None,
        last_sip_event: str = "INVITE",
    ) -> None:
        """Project one pending SIP dialog onto its owning browser phone."""
        registry = _call_registry(hass)
        endpoint = endpoint_directory(hass).get(endpoint_id)
        video_enabled = bool(
            invite.video_format is not None
            and (endpoint is None or endpoint.supports("video"))
        )
        session = registry.get_session(invite.call_id)
        if session is None:
            return
        publish_phone_projection(
            hass,
            session,
            endpoint_id, peer_name=invite.caller, direction="incoming",
            dialed_target=invite.target,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            route_kind=route_kind,
            sip_uri=sip_uri,
            sip_status_code=(
                200
                if registry.resource_for(invite.call_id, "preanswered") is not None
                else 180
            ),
            last_sip_event=last_sip_event,
            video_offered=video_enabled,
            video_format=(invite.video_format.wire_token() if video_enabled else ""),
            video_send_format=(
                invite.send_video_format.wire_token()
                if video_enabled and invite.send_video_format is not None
                else ""
            ),
            video_receive_format=(
                invite.recv_video_format.wire_token()
                if video_enabled and invite.recv_video_format is not None
                else ""
            ),
        )

    def _defer_invite_to_ha_softphone(
        invite: SipInvite,
        *,
        route_kind: str,
        endpoint_id: str,
        endpoint_device_id: str,
        callee: str | None = None,
        sip_uri: str | None = None,
        last_sip_event: str = "INVITE",
    ) -> None:
        registry = _call_registry(hass)
        session = registry.upsert(
            invite.call_id,
            state=CallState.RINGING.value,
            caller=invite.caller,
            callee=callee or invite.target,
            route_kind=route_kind,
            owner="ha_softphone",
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            dialed_target=invite.target,
            ingress="trunk" if invite.received_via_trunk else "extension",
            origin="trunk" if invite.received_via_trunk else "extension",
        )
        registry.set_pending_invite(invite.call_id, invite)
        registry.claim_endpoint(
            invite.call_id,
            endpoint_id,
            role="destination",
        )
        registry.add_leg(
            invite.call_id,
            invite.call_id,
            role="ha_softphone",
            state=CallState.RINGING.value,
        )
        expected_revision = session.revision

        def _publish_ringing_if_current() -> None:
            if not registry.is_current(
                invite.call_id,
                revision=expected_revision,
                owner="ha_softphone",
            ):
                _LOGGER.debug(
                    "Ignoring stale HA ringing callback for call %s revision %s",
                    invite.call_id,
                    expected_revision,
                )
                return
            _publish_pending_ha_softphone_ringing(
                invite,
                route_kind=route_kind,
                endpoint_id=endpoint_id,
                endpoint_device_id=endpoint_device_id,
                callee=callee or invite.target,
                sip_uri=sip_uri,
                last_sip_event=last_sip_event,
            )

        hass.loop.call_soon(_publish_ringing_if_current)

    def _inbound_route_decision(
        invite: SipInvite, peers: list[Peer], entries: list[RosterEntry]
    ):
        # Once an INVITE reached HA, HA is the router. ESP-origin direct-vs-HA
        # decisions are made before dialing by the ESP phonebook mirror.
        # ``HA`` is the stable config-flow alias; the phonebook entry carries
        # the user-selected HA peer name (for example ``Casa``). Resolve the
        # alias before consulting the canonical phonebook dial plan.
        route_target = invite.routing_target
        target = _ha_peer_name(hass) if _is_ha_target(route_target) else route_target
        return _ha_router_decision(target, entries)

    async def _async_forward_existing_call(
        *,
        call_id: str,
        destination: str,
        on_failure: str = "resume",
        expected_state: str = "",
        expected_sequence: int = 0,
        initial_selection: bool = False,
    ) -> None:
        await async_forward_existing_call(
            ForwardRuntime(
                hass=hass,
                config=cfg,
                local_ip=local_ip,
                route_resolver=route_resolver,
                browser_leg_for_member=_browser_leg_for_member,
                defer_invite_to_softphone=_defer_invite_to_ha_softphone,
                prepare_outbound_leg=_prepare_outbound_leg,
                publish_pending_ringing=_publish_pending_ha_softphone_ringing,
                start_local_assist_bridge=_start_local_assist_bridge,
            ),
            call_id=call_id,
            destination=destination,
            on_failure=on_failure,
            expected_state=expected_state,
            expected_sequence=expected_sequence,
            initial_selection=initial_selection,
        )

    async def _run_trunk_inbound_route_guarded(
        invite: SipInvite,
        *,
        bridge_ports: RtpPortReservation,
    ) -> None:
        """Fail one detached trunk route closed and release all ownership."""

        try:
            await async_route_trunk_invite(
                TrunkInboundRuntime(
                    hass=hass,
                    config=cfg,
                    local_ip=local_ip,
                    ha_peer_name=_ha_peer_name(hass),
                    route_resolver=route_resolver,
                    forward_existing_call=_async_forward_existing_call,
                    defer_invite_to_softphone=_defer_invite_to_ha_softphone,
                    start_local_assist_bridge=_start_local_assist_bridge,
                ),
                invite,
                bridge_ports=bridge_ports,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - detached call boundary.
            _LOGGER.exception(
                "SIP trunk inbound routing failed call_id=%s", invite.call_id
            )
            await EndpointTerminationHandler(hass).terminate(
                invite.call_id,
                TerminationIntent.bye(
                    RouteReason.TARGET_UNREACHABLE.value,
                    TerminationInitiator.ROUTING,
                ),
            )

    async def _run_ring_group_call(
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
        await run_ring_group_call(
            RingGroupRuntime(
                hass=hass,
                config=cfg,
                local_ip=local_ip,
                ha_peer_name=_ha_peer_name,
                browser_leg_for_member=_browser_leg_for_member,
                logical_endpoint_for_member=_logical_endpoint_for_member,
                prepare_outbound_leg=_prepare_outbound_leg,
                terminate_sip_bridge=_terminate_sip_bridge,
            ),
            invite,
            entry,
            peers,
            roster_entries,
            origin_endpoint_id=origin_endpoint_id,
            origin_media_client_id=origin_media_client_id,
            request_video=request_video,
            enable_caller_video_send=enable_caller_video_send,
        )

    async def _ring_conference_members(
        *,
        room_name: str,
        caller: str,
        source_host: str,
        entry: RosterEntry,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
        owner_call_id: str = "",
    ) -> None:
        await async_ring_conference_members(
            ConferenceRingRuntime(
                hass=hass,
                config=cfg,
                local_ip=local_ip,
                on_inbound_timeout=_on_conference_inbound_timeout,
                browser_leg_for_member=_browser_leg_for_member,
                prepare_outbound_leg=_prepare_outbound_leg,
            ),
            room_name=room_name,
            caller=caller,
            source_host=source_host,
            entry=entry,
            peers=peers,
            roster_entries=roster_entries,
            owner_call_id=owner_call_id,
        )

    async def _ring_conference_members_from_ha(
        entry: RosterEntry,
        *,
        owner_call_id: str = "",
    ) -> None:
        peers = await _async_build_peer_snapshot(hass)
        roster_entries = _roster_from_peers(
            hass, peers, _registered_roster_entries(hass)
        )
        room_name = str(entry.name or entry.id or "")
        await _ring_conference_members(
            room_name=room_name,
            caller=_ha_peer_name(hass),
            source_host=local_ip,
            entry=entry,
            peers=peers,
            roster_entries=roster_entries,
            owner_call_id=owner_call_id,
        )

    async def _start_ring_group_from_ha(
        entry: RosterEntry,
        *,
        context: Any | None = None,
        endpoint_id: str = "",
        media_client_id: str = "",
        request_video: bool = False,
        enable_caller_video_send: bool = False,
    ) -> str:
        browser_endpoint = browser_phone(hass, endpoint_id)
        if browser_endpoint is None:
            raise ValueError("a Home Assistant phone is required")
        endpoint_id = browser_endpoint.endpoint_id
        local_name = browser_endpoint.name
        endpoint_device_id = browser_endpoint.device_id
        group_name = str(entry.name or entry.id or "")
        # A timestamp is not a dialog identifier: two phones can start in the
        # same millisecond.  Use cryptographic entropy just like the normal SIP
        # client path so concurrent HA callers cannot alias one registry entry.
        call_id = f"ha-{secrets.token_hex(16)}"
        send_format = next(
            fmt
            for fmt in HA_SIP_PCM_TX_FORMATS
            if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
        )
        recv_format = next(
            fmt
            for fmt in HA_SIP_PCM_RX_FORMATS
            if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
        )
        invite = SipInvite(
            source_host=local_ip,
            source_port=int(cfg["sip_port"]),
            request_uri=parse_sip_uri(
                f"sip:{group_name.replace(' ', '_')}@{local_ip};transport=tcp"
            ),
            caller_uri=parse_sip_uri(
                f"sip:{local_name.replace(' ', '_')}@{local_ip};transport=tcp"
            ),
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
        registry = _call_registry(hass)
        session = registry.upsert(
            call_id,
            state=CallState.REMOTE_RINGING.value,
            owner="ha_softphone",
            caller=local_name,
            callee=group_name,
            route_kind=GROUP_TYPE_RING,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            source_endpoint_id=endpoint_id,
            media_client_id=str(media_client_id or "").strip(),
        )
        try:
            registry.claim_endpoint(call_id, endpoint_id, role="source")
        except EndpointBusyError:
            await EndpointTerminationHandler(hass).terminate_reason(
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
            hass,
            session,
            endpoint_id,
            leg_id=origin_leg_id,
            peer_name=group_name,
            direction="outgoing",
            route_kind=GROUP_TYPE_RING,
            sip_status_code=180,
            last_sip_event="LOCAL_RING_GROUP",
        )
        try:
            peers = await _async_build_peer_snapshot(hass)
            roster_entries = _roster_from_peers(
                hass, peers, _registered_roster_entries(hass)
            )
        except Exception:
            await EndpointTerminationHandler(hass).terminate_reason(
                call_id,
                TerminalReason.TRANSPORT_UNREACHABLE.value,
                TerminationInitiator.RUNTIME,
            )
            raise
        create_runtime_task(
            hass,
            _run_ring_group_call(
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

    pbx_runtime.ring_conference_members_from_ha = _ring_conference_members_from_ha
    pbx_runtime.start_ring_group_from_ha = _start_ring_group_from_ha

    async def _on_invite(invite: SipInvite) -> SipInviteResult:
        return await route_invite(
            InviteRuntime(
                hass=hass,
                config=cfg,
                local_ip=local_ip,
                registrar=registrar,
                ha_peer_name=_ha_peer_name,
                get_trunk_config=_get_trunk_config,
                trunk_enabled=_trunk_enabled,
                is_trunk_invite=_is_trunk_invite,
                is_ha_target=_is_ha_target,
                ha_router_decision=_ha_router_decision,
                inbound_route_decision=_inbound_route_decision,
                build_peer_snapshot=_async_build_peer_snapshot,
                browser_leg_for_member=_browser_leg_for_member,
                defer_invite_to_softphone=_defer_invite_to_ha_softphone,
                enable_reused_sip_tcp_connection=_enable_reused_sip_tcp_connection,
                on_conference_inbound_timeout=_on_conference_inbound_timeout,
                ring_conference_members=_ring_conference_members,
                run_ring_group_call=_run_ring_group_call,
                run_trunk_inbound_route_guarded=_run_trunk_inbound_route_guarded,
                send_final_response=_sip_send_final_response,
                sip_uri_transport=_sip_uri_transport,
                start_local_assist_bridge=_start_local_assist_bridge,
                terminate_sip_bridge=_terminate_sip_bridge,
            ),
            invite,
        )

    async def _on_offerless_invite(
        initial: SipInitialInvite,
    ) -> UasDelayedOfferPlan | None:
        """Prepare one offer, then continue through the canonical router."""

        registry = _call_registry(hass)
        session = registry.upsert(
            initial.call_id,
            state=CallState.CONNECTING.value,
            owner="router",
            caller=initial.caller,
            callee=initial.target,
            ingress="trunk" if initial.received_via_trunk else "extension",
            origin="trunk" if initial.received_via_trunk else "extension",
        )
        try:
            ports = reserve_delayed_offer_ports(hass, registry, initial.call_id)
        except RuntimeError:
            await EndpointTerminationHandler(hass).terminate_reason(
                initial.call_id,
                TerminalReason.TRANSPORT_UNREACHABLE.value,
                TerminationInitiator.MEDIA,
            )
            return None
        offer_sdp = sip_sdp.build_offer_directional(
            local_ip,
            local_ip,
            ports.ports[0],
            list(HA_SIP_PCM_TX_FORMATS),
            list(HA_SIP_PCM_RX_FORMATS),
            include_common_codecs=True,
        )

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
            result = await _on_invite(invite)
            if not result.defer_final:
                return result

            reservation = take_delayed_offer_ports(registry, invite.call_id)
            if reservation is None:
                # A deferred bridge or fork already transferred the advertised
                # pair into its asynchronous route owner. Its final answer is
                # now signaling-only on the source dialog.
                return SipInviteResult(200, "OK")
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
            return SipInviteResult(200, "OK")

        return UasDelayedOfferPlan(offer_sdp, accept_answer, rollback)

    async def _on_media_update(
        previous: SipInvite,
        updated: SipInvite,
        method: str,
    ) -> SipInviteResult:
        return await async_prepare_media_update(
            hass,
            local_ip,
            previous,
            updated,
            method,
        )

    async def _on_refer(call_id, target) -> int:
        from .call_transfer import async_transfer_target

        result = await async_transfer_target(runtime, call_id, target)
        if result.status:
            return result.status
        return 503 if result.state == "call_not_found" else 500

    endpoint_termination = EndpointTerminationHandler(hass)
    _on_terminated = endpoint_termination.handle

    supported_formats = list(HA_SIP_PCM_FORMATS)
    endpoint = SipEndpointManager(
        host="0.0.0.0",
        port=int(cfg["sip_port"]),
        local_ip=local_ip,
        local_rtp_port=int(cfg["rtp_port"]),
        supported_formats=supported_formats,
        supported_send_formats=list(HA_SIP_PCM_TX_FORMATS),
        supported_recv_formats=list(HA_SIP_PCM_RX_FORMATS),
        on_invite=_on_invite,
        on_offerless_invite=_on_offerless_invite,
        on_terminated=_on_terminated,
        on_register=_on_register,
        on_info=_on_info,
        on_media_update=_on_media_update,
        on_refer=_on_refer,
        on_request=application_methods.handle,
        udp_enabled=True,
        tcp_enabled=True,
        enable_video=bool(cfg.get(CONF_SIP_VIDEO, False)),
        enable_video_transcoding=bool(cfg.get(CONF_VIDEO_TRANSCODING, False)),
        prefer_browser_video_send=bool(cfg.get(CONF_VIDEO_CAMERA_SEND, False)),
    )
    # Atomic ownership cutover: the runtime and registry are authoritative
    # before either listener can dispatch its first INVITE.  The two component
    # names expose both transports while only one closer stops their shared
    # SipEndpointManager instance.
    pbx_runtime.attach_component("tcp_listener", endpoint)
    pbx_runtime.attach_component("udp_listener", endpoint, closer=endpoint.stop)
    pbx_runtime.activate()
    runtime.sip = pbx_runtime
    try:
        started = await endpoint.start()
    except BaseException:
        await pbx_runtime.shutdown()
        if runtime.sip is pbx_runtime:
            runtime.sip = None
        raise
    if not started:
        await pbx_runtime.shutdown()
        if runtime.sip is pbx_runtime:
            runtime.sip = None
        return False
    pbx_runtime.forward_call = _async_forward_existing_call
    _LOGGER.info(
        "SIP endpoint enabled on UDP+TCP/%s (RTP base %s)",
        cfg["sip_port"],
        cfg["rtp_port"],
    )
    return True
