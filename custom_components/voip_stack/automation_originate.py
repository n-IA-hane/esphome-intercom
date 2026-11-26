"""Originate an application-owned SIP call for a normal HA automation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .automation_call import AutomationCall, application_idle_duration
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
from .ringing_policy import resolve_ringing_timeout, wait_for_ringing_result
from .sip_runtime import enable_reused_tcp_connection, uri_transport
from .trunk_policy import is_trunk_uri
from .trunk_selection import trunk_destination_uri
from .route_abort import RouteAbortContext, RouteAbortIntent, async_abort_route


@dataclass(frozen=True, slots=True)
class ApplicationCallPreparation:
    """Directory and network discovery shared only by one bounded paging job."""

    peers: tuple
    entries: tuple
    local_ip: str


async def async_prepare_application_calls(hass):
    """Prepare shared routing inputs before the per-call owners start."""
    local_ip = await async_advertise_host(hass)
    peers = await async_build_peer_snapshot(hass)
    entries = roster_from_peers(hass, peers, registered_roster_entries(hass))
    return ApplicationCallPreparation(tuple(peers), tuple(entries), local_ip)


def _application_call_routes(hass, preparation):
    """Rejoin shared declarations with current Contact and endpoint owners."""
    from .directory_runtime import routing_snapshot

    runtime = require_runtime_data(hass)
    directory = runtime.directory
    entries = (routing_snapshot(runtime, entries=preparation.entries)
               if directory is not None and not directory.closed else list(preparation.entries))
    phone_kinds = {"sip_account", "esphome", "browser", "ha_softphone"}
    retired = {entry.id.casefold() for entry in preparation.entries
               if entry.metadata.get("endpoint_kind") in phone_kinds
               and runtime.endpoints.get(entry.metadata.get("endpoint_id", "")) is None}
    entries = [entry for entry in entries if entry.id.casefold() not in retired
               and (entry.metadata.get("endpoint_kind") not in phone_kinds
                    or runtime.endpoints.get(entry.metadata.get("endpoint_id", "")) is not None)]
    # Physical peers are discovery inputs, not a Contact cache. Their current
    # routes and capabilities now come from the registrar overlay above.
    peers = [peer for peer in preparation.peers if peer.endpoint_kind not in phone_kinds]
    return peers, entries


async def _bind_outgoing_application_media(hass, registry, session, application, client, media, *, started=False):
    """Install one admitted SIP output under the existing application owner."""
    call_id = session.call_id
    media.release_reservation_on_stop = False
    registry.attach_relay(call_id, media)
    client.audio_rtp_socket = None
    application.media = media
    media.on_dtmf = application.handle_dtmf
    media.on_dtmf_fallback = application.send_dtmf_info
    client.on_info_dtmf = lambda digit, _duration: application.handle_dtmf("right", digit, "sip_info")
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
    if not started:
        await session.create_task(media.start(), name="automation-media-start")
    application.require_current()
    if client.dialog is None:
        raise ServiceValidationError("The destination ended before audio became ready")
    application.answered = True


async def _dial_attempt(hass, registry, session, attempt, destination, timeout, *, answer_mode="", audio_direction="sendrecv", transition_call=True):
    """Arbitrate an outgoing leg and route decisions with the existing fork."""
    client = attempt.client
    decision = hass.loop.create_future()
    if not answer_mode:
        registry.set_pending_route(session.call_id, {"future": decision, "outgoing_automation": True})

    async def dial():
        try:
            # Bind the advertised media port before SIP can allocate an ephemeral
            # socket or the peer can send its first RTP packet (RFC 3264, 5.1).
            client.audio_rtp_socket = attempt.ports.take_audio_socket(client.local_rtp_port)
            if client.audio_rtp_socket is None:
                client.audio_rtp_socket = bind_sip_rtp_socket(client.local_rtp_port)
            result = await client.invite(
                target=attempt.uri.user, target_display_name=destination,
                remote_host=attempt.uri.host, remote_sip_port=sip_default_port(attempt.uri),
                request_uri=str(attempt.uri),
                **({"answer_mode": answer_mode, "audio_direction": audio_direction}
                   if answer_mode or audio_direction != "sendrecv" else {}),
            )
            if result in {"ringing", "calling", "remote_ringing"}:
                if transition_call:
                    registry.transition(session.call_id, state=CallState.REMOTE_RINGING.value,
                                        expected_generation=session.generation)
                    publish_bridge_projection(hass, session, event_type="remote_ringing",
                                              direction="outgoing", route_kind="automation")
                else:
                    registry.observe_leg(session.call_id, client.dialog_ids.call_id, role="callee",
                        state="remote_ringing", generation=session.generation)
                if answer_mode:
                    remaining = (None if timeout is None else
                        max(0.0, client.invite_sent_at + timeout - hass.loop.time()))
                    result = await wait_for_ringing_result(client, remaining)
                else:
                    # The session policy owns ringing; protocol timers stay in SIP.
                    result = await client.wait_for_final()
            if result == "in_call" and session.owner == "automation":
                registry.transition(session.call_id, state=CallState.IN_CALL.value,
                                    expected_generation=session.generation)
            if result != "in_call" and not decision.done():
                decision.set_result({"action": "failed"})
            return DialOutcome(
                DialDisposition.ANSWERED if result == "in_call" and client.dialog is not None
                else DialDisposition.UNAVAILABLE, reason=result,
            )
        except Exception:
            # A local setup/media failure ends this attempt before a SIP
            # result exists. Release its route-control waiter as well.
            if not decision.done():
                decision.set_result({"action": "failed"})
            raise

    async def reroute():
        route = await decision
        return DialOutcome(DialDisposition.REROUTE if route.get("action") == "forward"
                           else DialDisposition.UNAVAILABLE)

    async def close(_mode):
        await registry.close_leg(session.call_id, client.dialog_ids.call_id, reason="route_replaced")
        attempt.ports.release()

    async def close_control(_mode):
        return None

    # Paging already uses the shared retain-all fork at the job level. Keep
    # this one branch's timeout at first INVITE, after transport preparation.
    if answer_mode:
        return await dial(), None
    fork = DialForkController(session, (
        DialCandidate(client.dialog_ids.call_id, dial, close, endpoint_id=attempt.endpoint_id),
        DialCandidate("route-control", reroute, close_control, control=True),
    ), overall_timeout=float("inf"))
    try:
        result = await fork.run(lambda _candidate, _outcome: session.live and session.owner == "automation")
        return result.outcome, decision.result() if decision.done() and not decision.cancelled() else None
    finally:
        registry.take_pending_route(session.call_id)


async def async_originate_automation_call(call: ServiceCall) -> dict:
    """Return a call token only after final acceptance and local media binding."""
    return await async_originate_application_call(call)


async def _async_originate_application_call(
    call: ServiceCall, *, source=None, answer_mode="", audio_direction="sendrecv", routing_scope, preparation=None,
) -> dict:
    """Share the existing application originate owner with server-side paging."""
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
    preparation = preparation or await async_prepare_application_calls(hass)
    peers, entries = _application_call_routes(hass, preparation)
    supplied_source = source is not None
    source = source or roster_entry_for_target(str(call.data['source_automation']), entries)
    if source is None or not source.enabled or (not supplied_source and source.metadata.get('virtual_endpoint') != 'automation'):
        raise ServiceValidationError("Select an enabled Automation contact as the calling identity")
    application_idle_duration(source)
    destination = str(call.data['destination']).strip()
    from .inbound_routing.targets import resolve_phone_target
    from .router import RouteAction, RouteReason
    from .endpoint_routing import ha_router_decision
    from .trunk_selection import select_call_trunk

    trunk_selection = select_call_trunk(hass, requested=str(call.data.get("trunk_id") or ""))
    def resolve_route(target, roster):
        return ha_router_decision(hass, target, roster, trunk_id=trunk_selection.identity)

    initial_route = resolve_route(destination, entries)
    routing_call_id = None
    explicit_route = False
    from .automation_routing import RouteRequest, has_native_route_window

    identity = RouteRequest("", source.display_name, destination,
        source_account_username=(source.id if source.metadata.get("endpoint_kind") in {"sip_account", "esphome", "browser", "ha_softphone"}
                                 and not source.metadata.get("virtual_endpoint") else ""),
        source_extension=source.extension,
        caller_number=source.extension or source.id, caller_name=source.display_name,
        called_number=str(getattr(initial_route.entry, "number", "") or destination),
        outbound_trunk_id=trunk_selection.identity)
    if has_native_route_window(hass, identity, initial_route):
        from .core.sip import make_call_id

        identity = replace(identity, call_id=make_call_id("ha"))
        from types import SimpleNamespace
        from .inbound_routing.automation import request_route_override

        routing_session = registry.upsert(identity.call_id, state=CallState.CONNECTING.value, owner="router",
            caller=source.display_name, callee=destination, route_kind=initial_route.action.value,
            caller_number=identity.caller_number, caller_name=identity.caller_name,
            source_automation=source.id, direction="outgoing", ingress="extension",
            outbound_trunk_id=trunk_selection.identity)
        routing_scope["session"] = routing_session
        registry.bind_controller(routing_session.call_id, context=call.context)
        override = await request_route_override(runtime=SimpleNamespace(hass=hass, ha_peer_name=lambda _: source.display_name),
            invite=identity, decision=initial_route, authenticated_account=True, caller_is_trusted_endpoint=True,
            automation_routing_enabled=True, trunk_invite=False, native_override=True)
        if override.action in {"decline", "busy", "cancel"}:
            raise ServiceValidationError("The routing automation declined this call")
        if override.trunk_id:
            trunk_selection = select_call_trunk(hass, requested=override.trunk_id)
        if override.action in {"forward", "bridge"} and override.destination:
            destination = override.destination
            explicit_route = True
        peers, entries = _application_call_routes(hass, preparation)
        initial_route = replace(resolve_route(destination, entries), ringing_timeout=override.ringing_timeout)
        routing_call_id = routing_session.call_id
    resolved = resolve_phone_target(
        decision=initial_route, endpoint_registry=runtime.endpoints,
        roster_entries=entries, route_resolver=resolve_route,
        call_id=routing_call_id or "", apply_policy=not explicit_route,
    )
    if resolved.failure is not None:
        raise ServiceValidationError(f"Phone routing failed: {resolved.failure.decline_reason}")
    if resolved.decision != initial_route:
        destination = resolved.decision.target
    local_ip = preparation.local_ip
    resolver = EndpointRouteResolver(hass=hass, local_ip=local_ip, sip_port=int(runtime.transport_config['sip_port']))
    dialer = EndpointDialer(
        hass=hass, local_ip=local_ip, config=runtime.transport_config,
        route_resolver=resolver, sip_uri_transport=uri_transport,
        enable_reused_tcp_connection=enable_reused_tcp_connection,
    )
    def prepare(target, *, initial=False, contact=None):
        if answer_mode:
            from .ring_group import endpoint_preflight_disposition

            endpoint = resolver.logical_endpoint(target, peers, entries)
            disposition = endpoint_preflight_disposition(endpoint, call_id=routing_call_id or "", browser=False)
            if disposition is not None:
                raise ServiceValidationError(f"The paging destination is {disposition.value}")
        browser_leg = dialer.browser_leg_for_member(target, peers, entries)
        if browser_leg is not None:
            from .account_dialing import account_uses_ha_only

            endpoint = runtime.endpoints.get(browser_leg.endpoint_id)
            if endpoint is None or account_uses_ha_only(hass, endpoint):
                raise ServiceValidationError("Outgoing automation calls currently require a SIP or ESPHome destination")
        uri, _, _ = dialer.sip_uri_for_member(target, peers, entries)
        if contact is not None:
            from .core.sip import parse_sip_uri
            uri = parse_sip_uri(contact.uri)
        route = resolve_route(target, entries)
        if route.reason is RouteReason.TRUNK_UNAVAILABLE:
            raise trunk_selection.unavailable_error()
        use_trunk = route.action is RouteAction.TRUNK
        cfg = trunk_selection.config
        if use_trunk:
            if not trunk_selection.available:
                raise trunk_selection.unavailable_error()
            uri = trunk_destination_uri(cfg, route.target or target)
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
            roster_entry_override=contact.roster_entry if contact is not None else None,
            policy=OutboundLegPolicy(
                call_id=routing_call_id if initial and routing_call_id else "",
                local_uri_user=source.id if use_trunk and source.metadata.get("endpoint_kind") == "sip_account" else source.extension or source.id,
                username=cfg.get("trunk_username", "") if use_trunk else "",
                auth_username=cfg.get("trunk_auth_username", "") if use_trunk else "",
                password=cfg.get("trunk_password", "") if use_trunk else "",
                outbound_proxy=cfg.get("trunk_outbound_proxy", "") if use_trunk else "",
                use_trunk_flow=use_trunk,
                reuse_registered_flow=not use_trunk, allow_video=False,
                trunk_id=trunk_selection.identity if use_trunk else "",
            ),
        )
        if candidate is None:
            raise ServiceValidationError("The destination cannot receive an outgoing automation call")
        return candidate

    try:
        attempt = prepare(destination, initial=True)
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
            route_ringing_timeout=(initial_route.ringing_timeout
                if initial_route.ringing_timeout is not None else call.data.get("answer_timeout")),
            application_ringing_owned=bool(answer_mode),
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
            nonlocal attempt, client, destination, trunk_selection, peers, entries
            session.ensure_live()
            if registry.resource_for(call_id, "automation") is not application:
                raise ServiceValidationError("The outgoing call changed application")
            trunk_selection = selection
            peers, entries = _application_call_routes(hass, preparation)
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

        # Paging owns admission, while ordinary calls use the session policy.
        # Both resolve the same rule/account/PBX configuration without defaults.
        timeout = (resolve_ringing_timeout(runtime.transport_config,
            account=runtime.endpoints.get(attempt.endpoint_id),
            override=session.metadata.get("route_ringing_timeout")) if answer_mode else None)
        while True:
            outcome, route = await _dial_attempt(
                hass, registry, session, attempt, destination, timeout,
                answer_mode=answer_mode, audio_direction=audio_direction,
            )
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
                session.update_metadata(route_ringing_timeout=route.get("ringing_timeout"))
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
        await _bind_outgoing_application_media(hass, registry, session, application, client, media)


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


async def async_originate_application_call(call, *, source=None, answer_mode="", audio_direction="sendrecv", preparation=None):
    """Close the existing pre-call session if an application route cannot start."""
    routing_scope = {}
    try:
        return await _async_originate_application_call(call, source=source, answer_mode=answer_mode,
            audio_direction=audio_direction, routing_scope=routing_scope, preparation=preparation)
    except BaseException:
        session = routing_scope.get("session")
        if session is not None and session.live:
            await call_registry(call.hass).request_termination(session.call_id, TerminationIntent("cancelled"))
        raise
