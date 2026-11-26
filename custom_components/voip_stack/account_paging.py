"""Admit one paging output through a canonical account's existing adapters."""

import asyncio

from homeassistant.exceptions import ServiceValidationError

from .account_dialing import account_call_plan
from .browser_paging import BrowserPagingMedia, OutgoingBrowserPagingApplication
from .browser_playback import BROWSER_MEDIA_START_TIMEOUT, BrowserPlayback
from .call_projection import publish_phone_projection
from .dial_fork import DialCandidate, DialDisposition, DialForkController, DialOutcome
from .dial_plan import build_sip_contact_targets
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage, TerminationIntent
from .local_call_media import LocalAudioContract, LocalCallMedia
from .outbound_attempts import async_close_outbound_leg
from .ringing_policy import resolve_ringing_timeout
from .runtime_data import require_runtime_data
from .websocket_owner import async_revoke_media_owners


async def async_originate_account_paging(call, *, source, endpoint, decision, entries,
        prepare, local_ip, answer_mode, audio_direction, dial_attempt, bind_media,
        preferred_call_id=None, allow_browser=False, policy_endpoint_ids=(), ringing_timeout=None):
    """Fork prepared adapters under one call, claim and winning application."""
    hass, registry = call.hass, call_registry(call.hass)
    runtime = require_runtime_data(hass)
    plan = account_call_plan(decision.entry)
    policy = plan.ring_policy
    targets = build_sip_contact_targets(decision.entry.metadata["members"], entries, policy=policy)
    attempts, prepared_media = {}, {}
    closed_candidates = set()
    session = None
    browser_staged = False
    browser_release_failed = False
    try:
        for index, target in enumerate(targets):
            attempts[target.candidate_id] = prepare(endpoint.username, initial=index == 0, contact=target)
        if not attempts:
            raise ServiceValidationError("The account has no registered SIP destination")
        call_id = preferred_call_id or next(iter(attempts.values())).client.dialog_ids.call_id
        session = registry.upsert(call_id, state="ringing", owner="paging",
            caller=source.display_name, callee=endpoint.name, route_kind="paging",
            origin="automation", source_automation=source.id, endpoint_id=endpoint.endpoint_id,
            dest_endpoint_id=endpoint.endpoint_id, session_device_id=endpoint.device_id,
            application_ringing_owned=True, policy_endpoint_ids=policy_endpoint_ids,
            route_ringing_timeout=ringing_timeout, announcement_id=source.id.removeprefix("paging:"),
            answer_mode="auto", audio_direction="recvonly", application="browser_paging")
        registry.claim_endpoint(call_id, endpoint.endpoint_id, role="destination")
        playback = BrowserPlayback()
        browser_media = BrowserPagingMedia(playback)
        application = OutgoingBrowserPagingApplication(hass, session, None, source, local_ip,
            media=browser_media, playback=playback, endpoint_id=endpoint.endpoint_id,
            outgoing=True, controller=call.context.id)
        registry.own_resource(call_id, f"automation:{call_id}", application, application.close,
            stage=CleanupStage.MEDIA, generation=session.generation)
        timeout = resolve_ringing_timeout(runtime.transport_config, account=endpoint,
            override=ringing_timeout)
        candidates = []

        async def close_browser(_mode):
            nonlocal browser_staged, browser_release_failed
            if not browser_staged:
                return
            browser_staged = False
            owner_key = f"{endpoint.endpoint_id}|{call_id}"
            owner = runtime.media.owners_for("audio").get(owner_key)
            if owner is not None:
                unreleased = await async_revoke_media_owners({owner_key: owner},
                    runtime.media.owner_lock_for("audio"), timeout=BROWSER_MEDIA_START_TIMEOUT)
                if unreleased:
                    browser_release_failed = True
                    raise ConnectionError("The losing browser media owner did not release")
            await browser_media.stop("answered_elsewhere")
            await playback.close("answered_elsewhere")
            registry.release_resource(call_id, f"browser_paging:{call_id}", value=application)
            registry.release_resource(call_id, f"browser_playback:{call_id}", value=playback)
            descriptor = registry.resource_for(call_id, "softphone_media")
            if isinstance(descriptor, dict) and descriptor.get("application_playback") is browser_media:
                registry.take_media(call_id)
            registry.remove_leg(call_id, f"browser:{endpoint.endpoint_id}")
            closed_candidates.add("account:ha")

        if allow_browser and runtime.softphone_presence.get(endpoint.endpoint_id, 0):
            registry.own_resource(call_id, f"browser_paging:{call_id}", application, lambda _reason: None,
                generation=session.generation)
            registry.own_resource(call_id, f"browser_playback:{call_id}", playback, playback.close,
                stage=CleanupStage.MEDIA, generation=session.generation)
            registry.attach_media(call_id, {"endpoint_id": endpoint.endpoint_id,
                "application_playback": browser_media})
            browser_staged = True
            registry.observe_leg(call_id, f"browser:{endpoint.endpoint_id}", role="ha_softphone",
                endpoint_id=endpoint.endpoint_id, state="ringing", generation=session.generation)
            publish_phone_projection(hass, session, endpoint.endpoint_id, direction="incoming",
                peer_name=source.display_name, audio_direction="recvonly", answer_mode="auto",
                announcement_id=session.metadata["announcement_id"], event_type="ringing")

            async def dial_browser():
                async with asyncio.timeout(timeout):
                    await browser_media._while_open(application.accepted.wait())
                    await browser_media._while_open(playback.wait_ready())
                application.require_current()
                return DialOutcome(DialDisposition.ANSWERED, 200)

            candidates.append(DialCandidate("account:ha", dial_browser, close_browser,
                endpoint_id=endpoint.endpoint_id))

        for target in targets:
            attempt = attempts[target.candidate_id]
            client, identity = attempt.client, target.candidate_id
            leg_id = client.dialog_ids.call_id
            registry.own_resource(call_id, f"automation_ports:{leg_id}", attempt.ports,
                lambda _reason, ports=attempt.ports: ports.release(),
                stage=CleanupStage.RESERVATION, generation=session.generation)
            registry.add_leg(call_id, leg_id, role="callee", state="calling")
            registry.attach_sip_client(call_id, leg_id, client, role="callee", state="calling",
                trunk_id=attempt.trunk_id)

            async def dial_sip(attempt=attempt, client=client, identity=identity):
                outcome, _route = await dial_attempt(hass, registry, session, attempt,
                    endpoint.name, timeout, answer_mode=answer_mode, audio_direction=audio_direction)
                if not outcome.answered or client.dialog is None:
                    return outcome

                async def complete(reason):
                    if application.media is media:
                        registry.request_termination(call_id, TerminationIntent(reason), generation=session.generation)

                media = LocalCallMedia(hass, invite=LocalAudioContract.from_dialog(client.dialog),
                    local_rtp_port=attempt.ports.ports[0], reservation=attempt.ports,
                    on_complete=complete, rtp_socket=client.audio_rtp_socket)
                if not media.can_send:
                    raise ServiceValidationError("The destination cannot receive announcement audio")
                media.release_reservation_on_stop = False
                client.audio_rtp_socket = None
                prepared_media[identity] = media
                registry.own_resource(call_id, f"paging_candidate_media:{client.dialog_ids.call_id}", media,
                    lambda _reason, media=media: media.stop(), stage=CleanupStage.MEDIA,
                    generation=session.generation)
                await session.create_task(media.start(), name="paging-sip-media-start")
                if media.closed.is_set() or client.dialog is None:
                    return DialOutcome(DialDisposition.UNAVAILABLE)
                return outcome

            async def close_sip(_mode, attempt=attempt, client=client, identity=identity):
                media = prepared_media.pop(identity, None)
                if media is not None:
                    await media.stop()
                    registry.release_resource(call_id, f"paging_candidate_media:{client.dialog_ids.call_id}", value=media)
                await registry.close_leg(call_id, client.dialog_ids.call_id, reason="answered_elsewhere")
                attempt.ports.release()
                closed_candidates.add(identity)

            candidates.append(DialCandidate(identity, dial_sip, close_sip,
                endpoint_id=endpoint.endpoint_id, tier=target.tier, order=target.order))

        fork = await DialForkController(session, candidates,
            strategy=policy.strategy, tier_strategies=policy.tier_strategies,
            overall_timeout=timeout if timeout is not None else float("inf"),
            step_timeout=policy.step_timeout).run(
            lambda winner, _outcome: session.live and all(candidate == winner or
                candidate.candidate_id in closed_candidates for candidate in candidates))
        if browser_release_failed:
            raise ServiceValidationError("The losing browser media owner did not release")
        if fork.winner is None:
            raise ServiceValidationError(f"The destination did not answer: {fork.outcome.reason or fork.outcome.disposition.value}")
        application.require_current()
        if fork.winner.candidate_id == "account:ha":
            application.media = browser_media
            application.answered = True
        else:
            # No HA adapter was staged when its configured card was offline.
            await close_browser(None)
            if not browser_media.closed.is_set():
                await browser_media.stop("sip_selected")
                await playback.close("sip_selected")
            identity = fork.winner.candidate_id
            attempt = attempts[identity]
            media = prepared_media.pop(identity)
            registry.release_resource(call_id, f"paging_candidate_media:{attempt.client.dialog_ids.call_id}", value=media)
            session.metadata.pop("application", None)
            await bind_media(hass, registry, session, application, attempt.client, media, started=True)
        registry.transition(call_id, state="in_call", expected_generation=session.generation)
        application.arm_deadline()
        publish_phone_projection(hass, session, endpoint.endpoint_id, direction="incoming",
            peer_name=source.display_name, audio_direction="recvonly" if browser_staged else "sendonly",
            announcement_id=session.metadata["announcement_id"], answer_mode="auto", event_type="answered")
        return {"call_id": call_id, "generation": session.generation}
    except BaseException:
        try:
            if session is not None:
                await registry.terminate_call_wait(session.call_id, reason="paging_recipient_unavailable")
        finally:
            # Preparation precedes the account claim. Some attempts may not
            # have transferred to session resources when admission fails.
            await asyncio.gather(*(async_close_outbound_leg(attempt, bye_or_cancel=True)
                for attempt in attempts.values()))
        raise
