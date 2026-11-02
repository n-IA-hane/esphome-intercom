"""Originate an application-owned SIP call for a normal HA automation."""

from __future__ import annotations

import asyncio

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .automation_call import AutomationCall
from .automation_context import current_execution
from .call_projection import publish_bridge_projection
from .core.sip import sip_default_port
from .dial_fork import DialCandidate, DialDisposition, DialForkController, DialOutcome
from .endpoint_dialing import EndpointDialer, OutboundLegPolicy
from .endpoint_lifecycle import call_registry
from .endpoint_routing import EndpointRouteResolver, roster_from_peers
from .endpoint_registry import EndpointBusyError
from .endpoint_session import CleanupStage, TerminationIntent
from .fsm import CallState
from .local_call_media import LocalAudioContract, LocalCallMedia
from .media_ports import bind_sip_rtp_socket
from .outbound_attempts import async_close_outbound_leg
from .pbx_routing import roster_entry_for_target
from .peer_snapshot import async_advertise_host, async_build_peer_snapshot
from .phonebook_runtime import registered_roster_entries
from .runtime_data import require_runtime_data
from .sip_runtime import enable_reused_tcp_connection, uri_transport
from .trunk_policy import is_trunk_uri
from .route_abort import RouteAbortContext, RouteAbortIntent, async_abort_route


async def _dial_attempt(hass, registry, session, attempt, destination, timeout):
    """Arbitrate an outgoing leg and route decisions with the existing fork."""
    client = attempt.client
    decision = hass.loop.create_future()
    registry.set_pending_route(session.call_id, {"future": decision, "outgoing_automation": True})

    async def dial():
        # Bind the advertised media port before SIP can allocate an ephemeral
        # socket or the peer can send its first RTP packet (RFC 3264, 5.1).
        client.audio_rtp_socket = attempt.ports.take_audio_socket(client.local_rtp_port)
        if client.audio_rtp_socket is None:
            client.audio_rtp_socket = bind_sip_rtp_socket(client.local_rtp_port)
        result = await client.invite(
            target=attempt.uri.user, target_display_name=destination,
            remote_host=attempt.uri.host, remote_sip_port=sip_default_port(attempt.uri),
            request_uri=str(attempt.uri), timeout=timeout,
        )
        if result in {"ringing", "calling", "remote_ringing"}:
            registry.transition(session.call_id, state=CallState.REMOTE_RINGING.value,
                                expected_generation=session.generation)
            publish_bridge_projection(hass, session, event_type="remote_ringing",
                                      direction="outgoing", route_kind="automation")
            result = await client.wait_for_final(timeout=timeout)
        if result == "in_call" and session.owner == "automation":
            registry.transition(session.call_id, state=CallState.IN_CALL.value,
                                expected_generation=session.generation)
        if result != "in_call" and not decision.done():
            decision.set_result({"action": "failed"})
        return DialOutcome(
            DialDisposition.ANSWERED if result == "in_call" and client.dialog is not None
            else DialDisposition.UNAVAILABLE, reason=result,
        )

    async def reroute():
        route = await decision
        return DialOutcome(DialDisposition.REROUTE if route.get("action") == "forward"
                           else DialDisposition.UNAVAILABLE)

    async def close(_mode):
        await registry.close_leg(session.call_id, client.dialog_ids.call_id, reason="route_replaced")
        attempt.ports.release()

    async def close_control(_mode):
        return None

    fork = DialForkController(session, (
        DialCandidate(client.dialog_ids.call_id, dial, close, endpoint_id=attempt.endpoint_id),
        DialCandidate("route-control", reroute, close_control, control=True),
    ), overall_timeout=timeout)
    try:
        result = await fork.run(lambda _candidate, _outcome: session.live and session.owner == "automation")
        return result.outcome, decision.result() if decision.done() and not decision.cancelled() else None
    finally:
        registry.take_pending_route(session.call_id)


async def async_originate_automation_call(call: ServiceCall) -> dict:
    """Return a call token only after final acceptance and local media binding."""
    await async_require_service_admin(call.hass, call)
    if call.data.get("device_id"):
        raise ServiceValidationError("Choose either a calling phone or an Automation contact")
    if call.data.get("send_video"):
        raise ServiceValidationError("Outgoing automation calls support audio only")
    if current_execution.get() is not None:
        raise ServiceValidationError("Start outgoing automation calls from a normal Home Assistant automation")
    hass = call.hass
    runtime = require_runtime_data(hass)
    registry = call_registry(hass)
    peers = await async_build_peer_snapshot(hass)
    entries = roster_from_peers(hass, peers, registered_roster_entries(hass))
    source = roster_entry_for_target(str(call.data['source_automation']), entries)
    if source is None or not source.enabled or source.metadata.get('virtual_endpoint') != 'automation':
        raise ServiceValidationError("Select an enabled Automation contact as the calling identity")
    destination = str(call.data['destination']).strip()
    from .inbound_routing.targets import resolve_phone_target
    from .router import RouteAction, RouteReason
    from .endpoint_routing import ha_router_decision
    from .trunk_selection import select_call_trunk
    from .config import trunk_identity_uri, trunk_leg_identity

    trunk_selection = select_call_trunk(hass, requested=str(call.data.get("trunk_id") or ""))
    def resolve_route(target, roster):
        return ha_router_decision(hass, target, roster, trunk_id=trunk_selection.identity)

    initial_route = resolve_route(destination, entries)
    resolved = resolve_phone_target(
        decision=initial_route, endpoint_registry=runtime.endpoints,
        roster_entries=entries, route_resolver=resolve_route,
    )
    if resolved.failure is not None:
        raise ServiceValidationError(f"Phone routing failed: {resolved.failure.decline_reason}")
    if resolved.decision != initial_route:
        destination = resolved.decision.target
    local_ip = await async_advertise_host(hass)
    resolver = EndpointRouteResolver(hass=hass, local_ip=local_ip, sip_port=int(runtime.transport_config['sip_port']))
    dialer = EndpointDialer(
        hass=hass, local_ip=local_ip, config=runtime.transport_config,
        route_resolver=resolver, sip_uri_transport=uri_transport,
        enable_reused_tcp_connection=enable_reused_tcp_connection,
    )
    def prepare(target):
        if dialer.browser_leg_for_member(target, peers, entries) is not None:
            raise ServiceValidationError("Outgoing automation calls currently require a SIP or ESPHome destination")
        uri, _, _ = dialer.sip_uri_for_member(target, peers, entries)
        route = resolve_route(target, entries)
        if route.reason is RouteReason.TRUNK_UNAVAILABLE:
            raise trunk_selection.unavailable_error()
        use_trunk = route.action is RouteAction.TRUNK
        cfg = trunk_selection.config
        if use_trunk:
            from .core.sip import SipUri

            if not trunk_selection.available:
                raise trunk_selection.unavailable_error()
            uri = SipUri(route.target or target, cfg["trunk_server"], cfg["trunk_port"],
                         (("transport", cfg["trunk_transport"]),))
        if uri is None and target.lower().startswith(('sip:', 'sips:')):
            from .core.sip import parse_sip_uri
            uri = parse_sip_uri(target)
        if uri is None or resolver.is_local_listener_uri(uri):
            raise ServiceValidationError("Select a reachable SIP account, ESPHome phone or direct SIP address")
        if not use_trunk and is_trunk_uri(uri, cfg):
            raise ServiceValidationError("Select an external phonebook number to use the configured trunk")
        candidate = dialer.prepare_outbound_leg(
            member=target, peers=peers, roster_entries=entries,
            local_name=source.display_name, local_rtp_port_index=0,
            uri_override=str(uri),
            policy=OutboundLegPolicy(
                local_uri_user=trunk_leg_identity(cfg, source.extension or source.id) if use_trunk else source.extension or source.id,
                local_identity_uri=trunk_identity_uri(cfg) if use_trunk else "",
                username=cfg.get("trunk_username", "") if use_trunk else "",
                auth_username=cfg.get("trunk_auth_username", "") if use_trunk else "",
                password=cfg.get("trunk_password", "") if use_trunk else "",
                outbound_proxy=cfg.get("trunk_outbound_proxy", "") if use_trunk else "",
                force_common_audio=use_trunk, use_trunk_flow=use_trunk,
                reuse_registered_flow=not use_trunk, allow_video=False,
                trunk_id=trunk_selection.identity if use_trunk else "",
            ),
        )
        if candidate is None:
            raise ServiceValidationError("The destination cannot receive an outgoing automation call")
        return candidate

    try:
        attempt = prepare(destination)
    except OSError as error:
        raise ServiceValidationError("The destination RTP port is unavailable") from error
    client = attempt.client
    call_id = client.dialog_ids.call_id
    session = None
    try:
        session = registry.upsert(
            call_id, state=CallState.CALLING.value, owner='automation',
            caller=source.display_name, callee=destination, route_kind='automation',
            origin='automation', source_automation=source.id,
            dest_endpoint_id=attempt.endpoint_id,
            policy_endpoint_ids=resolved.forwarded_endpoint_ids,
            outbound_trunk_id=attempt.trunk_id,
        )
        registry.own_resource(
            call_id, f'automation_ports:{call_id}', attempt.ports,
            lambda _reason, ports=attempt.ports: ports.release(), stage=CleanupStage.RESERVATION,
            generation=session.generation,
        )
        registry.attach_sip_client(call_id, call_id, client, role='callee', state=CallState.CALLING.value,
                                   trunk_id=attempt.trunk_id)
        if attempt.endpoint_id:
            registry.claim_endpoint(call_id, attempt.endpoint_id, role='destination')
        application = AutomationCall(
            hass, session, None, source, local_ip,
            controller=call.context.id, outgoing=True,
        )
        registry.own_resource(
            call_id, f'automation:{call_id}', application, application.close,
            stage=CleanupStage.MEDIA, generation=session.generation,
        )
        async def install_attempt(target, selection):
            nonlocal attempt, client, destination, trunk_selection
            session.ensure_live()
            if registry.resource_for(call_id, "automation") is not application:
                raise ServiceValidationError("The outgoing call changed application")
            trunk_selection = selection
            rerouted = resolve_phone_target(
                decision=resolve_route(target, entries), endpoint_registry=runtime.endpoints,
                roster_entries=entries, route_resolver=resolve_route, call_id=call_id,
            )
            if rerouted.failure is not None:
                raise ServiceValidationError(f"Phone routing failed: {rerouted.failure.decline_reason}")
            target = rerouted.decision.target or target
            candidate = prepare(target)
            candidate_id = candidate.client.dialog_ids.call_id
            try:
                if candidate.endpoint_id:
                    registry.claim_endpoint(call_id, candidate.endpoint_id, role='destination')
                registry.own_resource(
                    call_id, f'automation_ports:{candidate_id}', candidate.ports,
                    lambda _reason, ports=candidate.ports: ports.release(), stage=CleanupStage.RESERVATION,
                    generation=session.generation,
                )
                registry.add_leg(call_id, candidate_id, role='callee', state='calling')
                registry.attach_sip_client(call_id, candidate_id, candidate.client, role='callee', state='calling',
                                           trunk_id=candidate.trunk_id)
                registry.transition(call_id, owner='automation', state=CallState.CALLING.value,
                                    callee=target, dest_endpoint_id=candidate.endpoint_id,
                                    outbound_trunk_id=candidate.trunk_id)
                session.update_metadata(policy_endpoint_ids=tuple(dict.fromkeys((
                    *session.metadata.get("policy_endpoint_ids", ()), *rerouted.forwarded_endpoint_ids,
                ))))
            except BaseException:
                if candidate.endpoint_id:
                    registry.release_endpoint_claim(call_id, candidate.endpoint_id)
                await async_close_outbound_leg(candidate, bye_or_cancel=True)
                raise
            attempt, client, destination = candidate, candidate.client, target

        def release_attempt_claim():
            if attempt.endpoint_id:
                registry.release_endpoint_claim(call_id, attempt.endpoint_id)
                session.update_metadata(policy_endpoint_ids=tuple(dict.fromkeys((
                    *session.metadata.get("policy_endpoint_ids", ()), attempt.endpoint_id,
                ))))

        fallback = None
        failure_policy = 'terminate'

        async def abort_forward(reason):
            return await async_abort_route(fallback, RouteAbortIntent(
                'busy' if failure_policy == 'busy' else reason, failure_policy,
                486 if failure_policy == 'busy' else 0,
            ))

        timeout = float(call.data.get('answer_timeout', 30))
        async with asyncio.timeout(timeout):
            while True:
                outcome, route = await _dial_attempt(hass, registry, session, attempt, destination, timeout)
                if outcome.disposition is DialDisposition.ANSWERED:
                    break
                if outcome.disposition is not DialDisposition.REROUTE:
                    if fallback is not None:
                        release_attempt_claim()
                        if await abort_forward(outcome.reason or 'forward_failed'):
                            fallback = None
                            continue
                    break
                session.ensure_live()
                previous_destination, previous_trunk = destination, trunk_selection.identity

                async def resume(target=previous_destination, identity=previous_trunk):
                    await install_attempt(target, select_call_trunk(hass, requested=identity))
                    publish_bridge_projection(hass, session, event_type='forwarding',
                                              direction='outgoing', route_kind='automation',
                                              last_sip_event='ROUTE_RESUME')

                fallback = RouteAbortContext(
                    hass, registry, call_id, transition_resume=True, resume_owner='automation',
                    resume_state=CallState.CALLING.value, resume_callee=previous_destination,
                    resume_route_kind='automation', publish_resume=resume,
                )
                failure_policy = route.get('on_failure', 'terminate')
                release_attempt_claim()
                try:
                    selection = select_call_trunk(hass, requested=route.get('trunk_id') or previous_trunk)
                    await install_attempt(route['destination'], selection)
                except Exception as err:
                    if await abort_forward(str(err)):
                        fallback = None
                        continue
                    raise
            if outcome.disposition is not DialDisposition.ANSWERED or client.dialog is None:
                raise ServiceValidationError(f"The destination did not answer: {outcome.reason or outcome.disposition.value}")
            application.require_current()
            # Final SIP acceptance ends ringing even if media preparation
            # needs another event-loop turn. TTS still waits for media.start().
            registry.transition(call_id, state=CallState.IN_CALL.value, expected_generation=session.generation)

            async def complete(reason):
                registry.request_termination(call_id, TerminationIntent(reason), generation=session.generation)

            media = LocalCallMedia(
                hass, invite=LocalAudioContract.from_dialog(client.dialog),
                local_rtp_port=attempt.ports.ports[0], reservation=attempt.ports,
                on_complete=complete, rtp_socket=client.audio_rtp_socket,
            )
            if not media.can_send:
                raise ServiceValidationError("The destination cannot receive announcement audio")
            media.release_reservation_on_stop = False
            registry.attach_relay(call_id, media)
            client.audio_rtp_socket = None
            application.media = media
            async def update_media(_current, updated, _method):
                commit = media.prepare_media_update(LocalAudioContract.from_dialog(updated))
                async def apply():
                    if (not registry.is_generation_current(call_id, session.generation)
                            or registry.resource_for(call_id, "relay") is not media):
                        raise RuntimeError("The local application media owner changed")
                    commit()
                return apply
            client.on_media_update = update_media
            from .bridge_manager import async_watch_owned_sip_dialog
            watcher = hass.async_create_task(async_watch_owned_sip_dialog(registry, call_id, client, session.generation))
            registry.attach_client_watcher(client.dialog_ids.call_id, watcher)
            application.require_current()
            # Teardown must cancel preparation before closing its media/ports.
            # Otherwise a remote BYE can leave start() waiting outside the owner.
            await session.create_task(media.start(), name="automation-media-start")
            application.require_current()
            if client.dialog is None:
                raise ServiceValidationError("The destination ended before audio became ready")
            application.answered = True


        application.arm_deadline()
        publish_bridge_projection(hass, session, event_type='answered', direction='outgoing', route_kind='automation')
        return {'call_id': call_id, 'generation': session.generation}
    except BaseException as err:
        if session is None:
            await async_close_outbound_leg(attempt, bye_or_cancel=True)
        else:
            await registry.terminate_call_wait(call_id, reason='automation_originate_failed')
        if isinstance(err, EndpointBusyError):
            raise ServiceValidationError("The destination is already in another call") from err
        if isinstance(err, TimeoutError):
            raise ServiceValidationError("The destination did not answer before the answer timeout") from err
        if isinstance(err, asyncio.CancelledError) and not asyncio.current_task().cancelling():
            raise ServiceValidationError("The destination ended while preparing call audio") from err
        raise
