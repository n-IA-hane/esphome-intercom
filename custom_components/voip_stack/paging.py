"""Paging through shared account offers and native finite audio replay."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import aclosing
from dataclasses import dataclass, field
from uuid import uuid4

from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .automation_originate import async_prepare_application_calls
from .dial_fork import DialCandidate, DialDisposition, DialForkController, DialOutcome
from .duration_policy import resolve_service_timeout, validate_duration
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage, TerminationIntent
from .local_application import LocalCallApplication, attach_local_application
from .paging_config import MAX_PAGING_TARGETS, paging_group_configs
from .roster import RosterEntry
from .ringing_policy import validate_ringing_timeout
from .runtime_data import require_runtime_data
from .service_endpoints import resolve_service_phone

MAX_PAGING_JOBS = 4
_CHUNK_BYTES = 4096
_FANOUT_CHUNKS = 4
_FINISHED_JOBS = 32


def validate_announcement(data):
    """Keep the same validation for services and internal paging callers."""
    values = dict(data)
    targets = values.get("targets")
    if isinstance(targets, str) or not isinstance(targets, (list, tuple)) or not targets:
        raise ServiceValidationError("Select one or more paging targets")
    values["targets"] = tuple(dict.fromkeys(str(target).strip() for target in targets if str(target).strip()))
    if not values["targets"] or len(values["targets"]) > MAX_PAGING_TARGETS:
        raise ServiceValidationError("Paging accepts at most sixteen targets")
    has_speech = bool(values.get("message")) and bool(values.get("tts_entity_id"))
    has_media = bool(values.get("media"))
    if has_speech == has_media or (bool(values.get("message")) != bool(values.get("tts_entity_id"))):
        raise ServiceValidationError("Supply message and TTS provider, or one media file")
    try:
        values["answer_timeout"] = validate_ringing_timeout(values.get("answer_timeout"), inherit=True)
    except ValueError as error:
        raise ServiceValidationError(str(error)) from error
    try:
        values["timeout"] = validate_duration(values.get("timeout"), "announcement_timeout")
    except ValueError as error:
        raise ServiceValidationError(str(error)) from error
    if values.get("busy_policy", "skip") != "skip":
        raise ServiceValidationError("Paging busy_policy must be skip")
    return values


def resolve_paging_targets(runtime, groups, targets, *, hass=None):
    """Expand saved groups once, deduplicating the existing logical phones."""
    selected = OrderedDict()
    visiting = set()

    def expand(target):
        if hass is not None:
            from .service_endpoints import resolve_service_group_reference
            target = resolve_service_group_reference(hass, target, kind="paging")
        identity = str(target).removeprefix("service:paging:").removeprefix("paging:")
        group = groups.get(identity)
        if group is None:
            matches = [value for value in groups.values()
                       if str(target).casefold() in {str(value.get("name", "")).casefold(), str(value.get("extension", "")).casefold()}]
            if len(matches) > 1:
                raise ServiceValidationError("Ambiguous paging group")
            group = matches[0] if matches else None
        if group is not None:
            identity = str(group.get("paging_group_id") or identity)
            if identity in visiting or not group.get("enabled", True):
                raise ServiceValidationError("Paging group is disabled or recursive")
            visiting.add(identity)
            for member in group.get("members", ()):
                expand(member)
            visiting.remove(identity)
        else:
            endpoint = resolve_service_phone(runtime.endpoints, target, allow_internal=True, hass=hass)
            if endpoint is None:
                raise ServiceValidationError(f"Unknown paging phone: {target}")
            selected[endpoint.endpoint_id] = endpoint
        if len(selected) > MAX_PAGING_TARGETS:
            raise ServiceValidationError("A paging job accepts at most sixteen phones")

    for target in targets:
        expand(target)
    if not selected:
        raise ServiceValidationError("Paging has no phone targets")
    return tuple(selected.values())


@dataclass(slots=True)
class PagingJob:
    manager: PagingRuntime
    identity: str
    call: ServiceCall
    data: dict
    targets: tuple
    session: object
    live_application: PagingCall | None = None
    task: asyncio.Task | None = None
    calls: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    state: str = "preparing"
    finishing: bool = False
    terminating_parent: bool = False
    error: str = ""
    preparation: object | None = None
    clients: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    output_tasks: dict = field(default_factory=dict)
    live_queues: dict = field(default_factory=dict)
    live_source_task: asyncio.Task | None = None
    live_done: bool = False
    deadline: float = 0.0
    deadline_expired: bool = False
    source_stream: object | None = None
    source_input: object | None = None
    source_read: asyncio.Task | None = None
    source_started: bool = False
    source_stopped: bool = False
    source_complete: bool = False
    source_closed: asyncio.Event = field(default_factory=asyncio.Event)
    source_error: str = ""

    def report(self):
        return {"announcement_id": self.identity, "state": self.state,
                **({"error": self.error} if self.error else {}),
                "targets": [dict(endpoint_id=target.endpoint_id, **self.results.get(target.endpoint_id, {"status": "pending"}))
                            for target in self.targets]}

    async def stop(self, _reason="stopped"):
        if self.task is not None and not self.task.done():
            if not self.finishing:
                self.task.cancel()
            if self.task is not asyncio.current_task():
                from .session_cleanup import async_wait_for_cleanup
                await async_wait_for_cleanup(self.task)

    async def close_from_session(self, reason):
        # The job's own final cleanup closes this same parent session.
        if not self.terminating_parent:
            await self.stop(reason)

    async def _close_target(self, endpoint_id):
        token = self.calls.pop(endpoint_id, None)
        if token is None:
            return
        registry = call_registry(self.manager.hass)
        session = registry.get_session(token["call_id"], generation=token["generation"])
        if session is not None:
            await registry.terminate_call_wait(session.call_id, intent=TerminationIntent("paging_complete", public_state="idle"))

    async def _admit(self, endpoint):
        """Offer one canonical account through the common call admission path."""
        from .account_dialing import AccountCallPlan
        from .automation_originate import _application_call_routes, _dial_attempt
        from .browser_paging import (BrowserPagingMedia, BrowserPagingApplication, close_browser_paging,
            stage_browser_paging, wait_browser_paging_ready)
        from .browser_playback import BrowserPlayback
        from .core.sip import make_call_id
        from .endpoint_dialing import EndpointDialer, OutboundLegPolicy
        from .endpoint_routing import EndpointRouteResolver
        from .group_candidates import GroupCandidates, GroupCandidateRuntime, async_prepare_account_candidates
        from .local_call_media import LocalAudioContract, LocalCallMedia
        from .outbound_attempts import async_close_outbound_leg
        from .ring_group_fork import build_offer_candidates
        from .ringing_policy import resolve_ringing_timeout
        from .sip_runtime import enable_reused_tcp_connection, uri_transport

        if not self.session.live:
            return
        hass, registry = self.manager.hass, call_registry(self.manager.hass)
        runtime = require_runtime_data(hass)
        identity = endpoint.endpoint_id
        candidates = GroupCandidates()
        child = None
        try:
            peers, entries = _application_call_routes(hass, self.preparation)
            call_id = make_call_id("ha")
            child = registry.upsert(call_id, state="ringing", owner="paging", caller="Announcement",
                callee=endpoint.name, route_kind="paging", endpoint_id=identity,
                dest_endpoint_id=identity, session_device_id=endpoint.device_id,
                application_ringing_owned=True, announcement_id=self.identity,
                answer_mode="auto", audio_direction="recvonly", application="paging")
            self.calls[identity] = {"call_id": call_id, "generation": child.generation}
            playback = BrowserPlayback()
            browser_media = BrowserPagingMedia(playback)
            application = BrowserPagingApplication(hass, child, None,
                RosterEntry(f"paging:{self.identity}", name="Announcement"), self.preparation.local_ip,
                media=browser_media, playback=playback, endpoint_id=identity,
                outgoing=True, controller=self.call.context.id)
            registry.own_resource(call_id, f"automation:{call_id}", application, application.close,
                stage=CleanupStage.MEDIA, generation=child.generation)
            resolver = EndpointRouteResolver(hass, self.preparation.local_ip,
                int(runtime.transport_config["sip_port"]))
            dialer = EndpointDialer(hass, self.preparation.local_ip, runtime.transport_config,
                resolver, uri_transport, enable_reused_tcp_connection)
            prepared_count = 0

            def prepare(**kwargs):
                nonlocal prepared_count
                policy = OutboundLegPolicy(call_id=call_id if prepared_count == 0 else "",
                    local_uri_user="Announcement", allow_video=False)
                result = dialer.prepare_outbound_leg(**kwargs, policy=policy)
                if result is not None:
                    prepared_count += 1
                return result

            policy = AccountCallPlan(identity,
                resolve_service_timeout(hass, "contact_step_timeout")).ring_policy
            await async_prepare_account_candidates(candidates, GroupCandidateRuntime(
                registry=registry, endpoint_registry=runtime.endpoints,
                browser_leg_for_member=lambda member, _peers, _entries:
                    dialer.browser_leg_for_endpoint(endpoint, member),
                logical_endpoint_for_member=lambda _member, _peers, _entries: endpoint,
                prepare_outbound_leg=prepare), call_id=call_id, members=[endpoint.username or identity],
                peers=peers, roster_entries=entries, local_name="Announcement", ring_policy=policy,
                group_name=endpoint.name, outbound_context={"local_rtp_port_index": 0})
            timeout = resolve_ringing_timeout(runtime.transport_config, account=endpoint,
                override=self.data.get("answer_timeout"))
            timeout = min(timeout if timeout is not None else float("inf"),
                max(.001, self.deadline - hass.loop.time()))
            self.clients.setdefault(identity, {})
            sinks, close_callbacks = {}, {}
            for attempt in candidates.attempts:
                wire_id = attempt.client.dialog_ids.call_id
                registry.own_resource(call_id, f"automation_ports:{wire_id}", attempt.ports,
                    lambda _reason, ports=attempt.ports: ports.release(),
                    stage=CleanupStage.RESERVATION, generation=child.generation)
                registry.add_leg(call_id, wire_id, role="callee", state="calling")
                registry.attach_sip_client(call_id, wire_id, attempt.client, role="callee", state="calling")
                self.clients[identity][attempt.candidate_id] = {"id": attempt.candidate_id,
                    "transport": "sip", "status": "pending"}

            async def close_sip(attempt, _mode=None):
                media = sinks.get(attempt.candidate_id)
                if media is not None:
                    await media.stop()
                    registry.release_resource(call_id, f"relay:{attempt.client.dialog_ids.call_id}", value=media)
                await registry.close_leg(call_id, attempt.client.dialog_ids.call_id, reason="paging_output_ended")
                await async_close_outbound_leg(attempt, bye_or_cancel=True)

            async def dial_sip(attempt):
                outcome, _route = await _dial_attempt(hass, registry, child, attempt,
                    endpoint.name, timeout, answer_mode="Auto", audio_direction="sendonly", transition_call=False)
                if not outcome.answered or attempt.client.dialog is None:
                    return outcome

                async def complete(_reason):
                    await media.stop()

                media = LocalCallMedia(hass, invite=LocalAudioContract.from_dialog(attempt.client.dialog),
                    local_rtp_port=attempt.ports.ports[0], reservation=attempt.ports,
                    on_complete=complete, rtp_socket=attempt.client.audio_rtp_socket)
                if not media.can_send:
                    return DialOutcome(DialDisposition.MEDIA_INCOMPATIBLE, 488, "cannot_receive_audio")
                media.release_reservation_on_stop = False
                attempt.client.audio_rtp_socket = None
                sinks[attempt.candidate_id] = media
                registry.own_resource(call_id, f"relay:{attempt.client.dialog_ids.call_id}", media,
                    lambda _reason, media=media: media.stop(), stage=CleanupStage.MEDIA,
                    generation=child.generation)
                await child.create_task(media.start(), name=f"paging-output-media-start:{attempt.client.dialog_ids.call_id}")
                if attempt.client.dialog is None or media.closed.is_set():
                    return DialOutcome(DialDisposition.UNAVAILABLE, reason="media_ended")

                async def watch():
                    await attempt.client.wait_for_dialog_termination()
                    await media.stop()

                watcher = child.create_task(watch(), name=f"client_watcher:{attempt.client.dialog_ids.call_id}")
                registry.attach_client_watcher(attempt.client.dialog_ids.call_id, watcher)
                close_callbacks[attempt.candidate_id] = lambda: close_sip(attempt)
                return outcome

            def browser_candidate(leg):
                candidate_id = f"ha:{leg.endpoint_id}"
                self.clients[identity][candidate_id] = {"id": candidate_id, "transport": "ha", "status": "pending"}
                staged = bool(runtime.softphone_presence.get(leg.endpoint_id, 0))
                if staged:
                    stage_browser_paging(application)

                async def close_browser(_mode=None):
                    await close_browser_paging(application, "paging_output_ended")

                async def dial_browser():
                    if not staged:
                        return DialOutcome(DialDisposition.UNAVAILABLE, reason="browser_offline")
                    try:
                        await wait_browser_paging_ready(application, timeout)
                    except TimeoutError:
                        return DialOutcome(DialDisposition.TIMEOUT, 408, "no_answer")
                    sinks[candidate_id] = browser_media
                    close_callbacks[candidate_id] = close_browser
                    return DialOutcome(DialDisposition.ANSWERED, 200)

                return DialCandidate(candidate_id, dial_browser, close_browser, endpoint_id=identity)

            offers, _payloads = build_offer_candidates(attempts=candidates.attempts,
                browser_legs=candidates.browser_legs, preflight_failures=candidates.preflight_failures,
                dial_sip=dial_sip, close_sip=close_sip, browser_candidate=browser_candidate)
            for candidate in offers:
                self.clients[identity].setdefault(candidate.candidate_id,
                    {"id": candidate.candidate_id, "transport": "account", "status": "pending"})

            def admit(candidate, _outcome):
                if not child.live or not self.session.live:
                    return False
                accepted = self._accept_output(identity, candidate.candidate_id, sinks[candidate.candidate_id],
                    close_callbacks[candidate.candidate_id])
                if accepted:
                    registry.transition(call_id, state="in_call", expected_generation=child.generation)
                return accepted

            result = await DialForkController(child, offers,
                overall_timeout=max(.001, self.deadline - hass.loop.time()),
                strategy=policy.strategy, step_timeout=policy.step_timeout,
                tier_strategies=policy.tier_strategies).run_all(admit)
            for candidate, outcome in result.outcomes:
                client = self.clients[identity].get(candidate.candidate_id)
                if client is not None and not outcome.answered and client["status"] == "pending":
                    client.update(status="skipped", reason=outcome.reason or outcome.disposition.value)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not self.session.live:
                raise
            # A recipient ending its own call cannot cancel other accounts.
            self.results[identity] = {"status": "failed", "reason": "recipient_ended"}
            for client in self.clients.get(identity, {}).values():
                if client["status"] == "pending":
                    client.update(status="failed", reason="recipient_ended")
        except Exception as error:
            status = "skipped" if isinstance(error, ServiceValidationError) else "failed"
            self.results[identity] = {"status": status, "reason": str(error)}
            for client in self.clients.get(identity, {}).values():
                if client["status"] == "pending":
                    client.update(status=status, reason=str(error))
        finally:
            if not any(key[0] == identity for key in self.outputs):
                if child is not None:
                    await registry.terminate_call_wait(child.call_id, reason="paging_no_ready_output")
                await asyncio.gather(*(async_close_outbound_leg(attempt, bye_or_cancel=True)
                    for attempt in candidates.attempts))
                self.calls.pop(identity, None)
                if identity not in self.results:
                    clients = list(self.clients.get(identity, {}).values())
                    reasons = {client.get("reason", "") for client in clients if client.get("reason")}
                    reason = next(iter(reasons)) if len(reasons) == 1 else "no_ready_output"
                    self.results[identity] = {"status": "skipped", "reason": reason}

    async def _live_chunks(self):
        application = self.live_application
        if application is None:
            raise RuntimeError("Live paging has no source application")
        await application.answer()
        application.require_current()
        async with aclosing(application.media.input_pcm_stream()) as stream:
            async for chunk in stream:
                yield chunk

    def _ensure_source(self):
        """Initialize one public native replay stream only after first readiness."""
        if self.source_error:
            raise ConnectionError(self.source_error)
        if self.live_application is not None:
            if self.live_source_task is None:
                self.live_source_task = self.session.create_task(self._broadcast_live_source(), name="paging-live-source")
            return
        if self.source_stream is not None:
            return
        from homeassistant.components import tts
        from .local_call_media import LocalCallMedia
        options = {**self.data.get("options", {}), **LocalCallMedia._tts_audio_output()}
        try:
            if self.data.get("media"):
                self.source_stream = tts.async_create_audio_stream(self.manager.hass, options)
                self.source_input = self._owned_media_pcm()
                self.source_stream.async_set_audio_stream(tts.PcmFormat(16000, 2, 1), self.source_input)
            else:
                self.source_stream = tts.async_create_stream(self.manager.hass, self.data["tts_entity_id"],
                    self.data.get("language"), options)
                self.source_stream.async_set_message(self.data["message"])
        except Exception as error:
            self.source_error = str(error)
            raise

    async def _owned_media_pcm(self):
        """Keep decoder reads owned; finish with EOF on stop, never cancel Core."""
        from .announcement_media import async_audio_stream
        self.source_started = True
        media = self.data["media"]
        identity = media.get("media_content_id") if isinstance(media, dict) else str(media)
        from .local_call_media import LOCAL_PCM_FORMAT
        sample_bytes = LOCAL_PCM_FORMAT.container_bytes_per_sample
        bytes_per_second = LOCAL_PCM_FORMAT.sample_rate * LOCAL_PCM_FORMAT.channels * sample_bytes
        budget = int(max(0, self.deadline - self.manager.hass.loop.time()) * bytes_per_second) // sample_bytes * sample_bytes
        used = 0
        try:
            async with aclosing(async_audio_stream(self.manager.hass, identity)) as chunks:
                while not self.source_stopped and self.session.live:
                    if self.manager.hass.loop.time() >= self.deadline:
                        self.source_error = "Announcement source exceeded its duration limit"
                        return
                    self.source_read = self.session.create_task(anext(chunks), name="paging-source-read")
                    try:
                        chunk = await self.source_read
                    except StopAsyncIteration:
                        if used % sample_bytes:
                            self.source_error = "Announcement source ended inside a PCM sample"
                        else:
                            self.source_complete = True
                        return
                    except asyncio.CancelledError:
                        if self.source_stopped or not self.session.live:
                            return
                        raise
                    finally:
                        await asyncio.gather(self.source_read, return_exceptions=True)
                        self.source_read = None
                    if used + len(chunk) > budget:
                        self.source_error = "Announcement source exceeded its PCM byte limit"
                        return
                    used += len(chunk)
                    yield chunk
        except Exception as error:
            self.source_error = str(error)
        finally:
            self.source_closed.set()

    async def _finite_chunks(self):
        try:
            pending = bytearray()
            async with aclosing(self.source_stream.async_stream_result()) as chunks:
                async for chunk in chunks:
                    view = memoryview(chunk)
                    offset = 0
                    while offset < len(view):
                        count = min(_CHUNK_BYTES - len(pending), len(view) - offset)
                        pending.extend(view[offset:offset + count])
                        offset += count
                        if len(pending) == _CHUNK_BYTES:
                            yield bytes(pending)
                            pending.clear()
            if len(pending) % 2:
                raise ConnectionError("Announcement source ended inside a PCM sample")
            if pending:
                yield bytes(pending)
            if self.source_error:
                raise ConnectionError(self.source_error)
            if self.source_input is not None and not self.source_complete:
                raise ConnectionError("Announcement source did not complete")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.source_error = str(error)
            raise

    def _accept_output(self, endpoint_id, candidate_id, media, close):
        key = (endpoint_id, candidate_id)
        if key in self.outputs or self.source_stopped or self.live_done:
            return False
        client = self.clients[endpoint_id][candidate_id]
        try:
            if self.live_application is not None:
                self.live_queues[key] = asyncio.Queue(maxsize=_FANOUT_CHUNKS)
            self._ensure_source()
        except Exception as error:
            client.update(status="failed", reason=str(error))
            return False
        self.outputs[key] = (media, close)
        client["status"] = "playing"
        self.state = "playing"
        self.output_tasks[key] = self.session.create_task(self._play_output(key), name=f"voip-page-output-{endpoint_id}-{candidate_id}")
        return True

    async def _play_output(self, key):
        media, close = self.outputs[key]
        client = self.clients[key[0]][key[1]]
        if not self.session.live or self.source_stopped:
            client["status"] = "stopped"
            await close()
            return
        async def chunks():
            if self.live_application is None:
                async for chunk in self._finite_chunks():
                    yield chunk
            else:
                while (chunk := await self.live_queues[key].get()) is not None:
                    yield chunk
        async def play():
            async with aclosing(chunks()) as stream:
                await media.play_pcm_stream(stream)
        task_key = f"{key[0]}:{key[1]}"
        playback = self.session.create_task(play(), name=f"paging-output-playback:{task_key}")
        closed = self.session.create_task(media.closed.wait(), name=f"paging-output-closed:{task_key}")
        errors, suppressed = media.counters["tx_error"], media.counters["tx_suppressed"]
        try:
            done, _ = await asyncio.wait((playback, closed), return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                raise ConnectionError("The paging output ended")
            await playback
            await media.drain_output()
            if self.source_error:
                raise ConnectionError(self.source_error)
            if media.counters["tx_error"] != errors or media.counters["tx_suppressed"] != suppressed:
                raise ConnectionError("Paging audio could not be transmitted")
            client["status"] = "completed" if self.session.live and not self.source_stopped else "stopped"
        except asyncio.CancelledError:
            client.update(status="timeout" if self.deadline_expired else "stopped")
        except Exception as error:
            client.update(status="failed", reason=str(error))
        finally:
            playback.cancel()
            closed.cancel()
            await asyncio.gather(playback, closed, return_exceptions=True)
            try:
                await close()
            except Exception as error:
                client.update(status="failed", reason=str(error))

    async def _enqueue_live(self, key, chunk):
        consumer = self.output_tasks.get(key)
        if consumer is None or consumer.done() or not self.session.live:
            return
        pending = self.session.create_task(self.live_queues[key].put(chunk),
            name=f"paging-live-enqueue:{key[0]}:{key[1]}")
        try:
            await asyncio.wait((pending, consumer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def _broadcast_live_source(self):
        """One existing bounded live-now fanout, with no replay history."""
        try:
            async with aclosing(self._live_chunks()) as chunks:
                async for chunk in chunks:
                    view = memoryview(chunk)
                    for offset in range(0, len(view), _CHUNK_BYTES):
                        piece = bytes(view[offset:offset + _CHUNK_BYTES])
                        for key in tuple(self.live_queues):
                            await self._enqueue_live(key, piece)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.source_error = str(error)
        finally:
            self.live_done = True
            if self.session.live and not self.source_stopped:
                for key in tuple(self.live_queues):
                    await self._enqueue_live(key, None)

    async def _stop_source(self):
        self.source_stopped = True
        try:
            if self.source_read is not None:
                self.source_read.cancel()
                await asyncio.gather(self.source_read, return_exceptions=True)
            if self.source_input is not None:
                if not self.source_started:
                    await self.source_input.aclose()
                    self.source_closed.set()
                await self.source_closed.wait()
                async def drain():
                    async with aclosing(self.source_stream.async_stream_result()) as chunks:
                        async for _chunk in chunks:
                            pass
                await drain()
        finally:
            # Public token removal does not cancel HA-owned finite synthesis.
            if self.source_stream is not None:
                self.source_stream.delete()

    def _finish_results(self):
        completed = False
        partial = False
        for target in self.targets:
            identity = target.endpoint_id
            clients = list(self.clients.get(identity, {}).values())
            if not clients and identity in self.results:
                partial = True
                continue
            for client in clients:
                if client["status"] in {"pending", "playing"}:
                    client["status"] = "timeout" if self.deadline_expired else "stopped"
            successes = sum(client["status"] == "completed" for client in clients)
            completed |= bool(successes)
            status = ("completed" if clients and successes == len(clients) else "partial" if successes
                else "skipped" if clients and all(client["status"] == "skipped" for client in clients)
                else "failed" if any(client["status"] == "failed" for client in clients)
                else "no_recipients")
            partial |= status != "completed"
            reasons = {client.get("reason", "") for client in clients if client.get("reason")}
            reason = (next(iter(reasons)) if len(reasons) == 1 else
                "no_answer" if reasons.intersection({"no_answer", "timeout"}) else "no_ready_output")
            self.results[identity] = {"status": status,
                **({"clients": clients} if len(clients) > 1 else {}),
                **({"reason": reason} if not successes else {})}
        if self.source_error:
            self.state, self.error = "failed", self.source_error
        elif completed and self.state != "stopped":
            self.state = "partial" if partial else "completed"
        elif self.state not in {"stopped", "timeout", "failed"}:
            self.state = "failed" if self.outputs else "no_recipients"

    async def run(self):
        registry = call_registry(self.manager.hass)
        admissions = []
        self.deadline = self.manager.hass.loop.time() + self.data["timeout"]
        try:
            async with asyncio.timeout_at(self.deadline):
                self.preparation = await async_prepare_application_calls(self.manager.hass)
                admissions = [self.session.create_task(self._admit(target), name=f"paging-admit-{target.endpoint_id}")
                    for target in self.targets]
                await asyncio.gather(*admissions)
                if self.live_source_task is not None:
                    await self.live_source_task
                await asyncio.gather(*tuple(self.output_tasks.values()))
        except asyncio.CancelledError:
            self.state = "stopped"
        except TimeoutError:
            self.deadline_expired = True
            self.state = "timeout"
        except Exception as error:
            self.state, self.error = "failed", str(error)
        finally:
            self.finishing = True
            for task in admissions:
                task.cancel()
            for task in self.output_tasks.values():
                if not task.done():
                    task.cancel()
            if self.live_source_task is not None and not self.live_source_task.done():
                self.live_source_task.cancel()
            await asyncio.gather(*admissions, *tuple(self.output_tasks.values()),
                *([self.live_source_task] if self.live_source_task is not None else []), return_exceptions=True)
            from .session_cleanup import async_wait_for_cleanup
            async def cleanup():
                try:
                    await self._stop_source()
                except Exception as error:
                    if self.state not in {"stopped", "timeout"}:
                        self.source_error = self.source_error or str(error)
                await asyncio.gather(*(self._close_target(identity) for identity in tuple(self.calls)))
                if self.session.live:
                    self.terminating_parent = True
                    if self.live_application is not None and not self.live_application.answered:
                        await registry.request_termination(self.session.call_id, TerminationIntent.final_response("paging_ended", 480))
                    else:
                        await registry.terminate_call_wait(self.session.call_id, intent=TerminationIntent("paging_complete", public_state="idle"))
            await async_wait_for_cleanup(asyncio.create_task(cleanup(), name=f"voip-page-cleanup-{self.identity}"))
            self._finish_results()
            self.manager.finished(self)
        return self.report()


class PagingRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.jobs = {}
        self.completed = OrderedDict()
        self.browser_originate = None

    async def originate_browser(self, job, endpoint):
        if self.browser_originate is None:
            raise ServiceValidationError("This browser has no paging playback adapter")
        return await self.browser_originate(job, endpoint)

    def finished(self, job):
        self.jobs.pop(job.identity, None)
        self.completed[job.identity] = job.report()
        while len(self.completed) > _FINISHED_JOBS:
            self.completed.popitem(last=False)

    async def close(self):
        await asyncio.gather(*(job.stop("unload") for job in tuple(self.jobs.values())))

    def start(self, call, data, *, application=None):
        try:
            data = {**data, "timeout": validate_duration(data.get("timeout"), "announcement_timeout")}
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
        if len(self.jobs) >= MAX_PAGING_JOBS:
            raise ServiceValidationError("All paging slots are in use")
        runtime = require_runtime_data(self.hass)
        targets = resolve_paging_targets(runtime, paging_group_configs(self.entry), data["targets"], hass=self.hass)
        identity = uuid4().hex
        registry = call_registry(self.hass)
        session = application.session if application is not None else registry.upsert(
            f"paging:{identity}", state="calling", owner="paging", route_kind="paging",
            caller="Announcement", callee="Paging", announcement_id=identity)
        job = PagingJob(self, identity, call, data, targets, session, live_application=application)
        self.jobs[identity] = job
        if application is not None:
            application.job = job
        registry.own_resource(session.call_id, f"paging_job:{session.call_id}", job, job.close_from_session,
                              stage=CleanupStage.OBSERVER, generation=session.generation)
        job.task = self.hass.async_create_task(job.run(), name=f"voip-paging-{identity}")
        return job


async def async_setup_paging(hass, entry):
    runtime = require_runtime_data(hass)
    manager = runtime.sip.component("paging")
    if manager is None:
        manager = PagingRuntime(hass, entry)
        runtime.sip.adopt_component("paging", manager, closer=manager.close)
    runtime.paging = manager
    return manager


async def async_unload_paging(hass, entry=None):
    runtime = require_runtime_data(hass)
    manager = runtime.sip.component("paging")
    if manager is not None:
        await manager.close()
        runtime.sip.release_component("paging", manager)
    runtime.paging = None


def _manager(hass):
    manager = call_registry(hass).component("paging")
    if manager is None:
        raise ServiceValidationError("Paging is not available")
    return manager


async def async_announce(call):
    await async_require_service_admin(call.hass, call)
    data = {**call.data, "timeout": resolve_service_timeout(call.hass, "announcement_timeout", call.data.get("timeout"))}
    job = _manager(call.hass).start(call, validate_announcement(data))
    if not call.data.get("wait", True):
        return job.report()
    try:
        return await asyncio.shield(job.task)
    except asyncio.CancelledError:
        await job.stop("cancelled")
        raise


async def async_stop_announcement(call):
    await async_require_service_admin(call.hass, call)
    manager = _manager(call.hass)
    identity = str(call.data.get("announcement_id") or "").strip()
    if not identity:
        raise ServiceValidationError("Select an announcement")
    job = manager.jobs.get(identity)
    if job is not None:
        await job.stop()
        return job.report()
    return manager.completed.get(identity, {"announcement_id": identity, "state": "stopped", "targets": []})


@dataclass(slots=True)
class PagingCall(LocalCallApplication):
    owner = "paging"
    job: PagingJob | None = None

    async def close(self, reason):
        if self.job is not None:
            await self.job.close_from_session(reason)


def verified_paging_source(hass, invite, *, session=None, local_source=None):
    """Authorize a verified internal caller, never its untrusted display name."""
    if (getattr(invite, "received_via_trunk", False)
            or (session is not None and session.metadata.get("inbound_trunk_id"))):
        return None
    endpoints = require_runtime_data(hass).endpoints
    identity = str(getattr(invite, "identified_source_endpoint_id", "") or "")
    if identity:
        return endpoints.get(identity)
    if session is not None and local_source is not None:
        registry = call_registry(hass)
        token = getattr(local_source, "token", None)
        if (token is not None and token.call_id == session.call_id and token.generation == session.generation
                and session.live and registry.resource_for(session.call_id, "browser_source") is local_source):
            return endpoints.get(local_source.call.caller_endpoint_id)
    return None


async def route_paging_call(runtime, invite, decision, registry, source_endpoint):
    """Make a saved group callable without a second inbound SIP owner."""
    from .inbound_routing.local import _claim_source
    from .sip_listener import SipInviteResult
    source_endpoint = verified_paging_source(runtime.hass, invite)
    if source_endpoint is None:
        return SipInviteResult(403, "Forbidden", to_tag="")
    if busy := await _claim_source(hass=runtime.hass, registry=registry, invite=invite,
                                   source_endpoint=source_endpoint, state="ringing", route_kind="paging"):
        return busy
    manager = _manager(runtime.hass)
    identity = decision.entry.metadata["paging_group_id"]
    config = paging_group_configs(manager.entry).get(identity)
    if config is None or not config.get("enabled", True):
        return SipInviteResult(404, "Not Found", to_tag="")
    session = registry.upsert(invite.call_id, state="ringing", owner="paging", caller=invite.caller,
                             callee=decision.entry.display_name, route_kind="paging")
    start_paging_application(runtime.hass, session, invite, decision.entry, runtime.local_ip)
    return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)


def start_paging_application(hass, session, invite, contact, local_ip, *, previous=None, local_source=None):
    """Use the same live source for direct and forwarded paging calls."""
    registry = call_registry(hass)
    local_source = local_source or (previous.local_source if previous is not None else None)
    if verified_paging_source(hass, invite, session=session, local_source=local_source) is None:
        raise ServiceValidationError("Live paging requires a verified internal caller")
    manager = _manager(hass)
    identity = contact.metadata["paging_group_id"]
    config = paging_group_configs(manager.entry).get(identity)
    if config is None or not config.get("enabled", True):
        raise ServiceValidationError("The paging group is not available")
    try:
        timeout = validate_duration(config.get("timeout"), "announcement_timeout")
    except ValueError as error:
        raise ServiceValidationError(str(error)) from error
    session = registry.transition(session.call_id, owner="paging", state="in_call" if previous and previous.answered else "ringing",
                                  callee=contact.display_name, route_kind="paging")
    registry.set_pending_invite(invite.call_id, invite)
    application = attach_local_application(PagingCall, hass, session, invite, contact, local_ip,
                                           previous=previous, local_source=local_source)
    data = {"targets": [f"paging:{identity}"], "answer_timeout": config.get("answer_timeout"),
            "timeout": timeout}
    manager.start(ServiceCall(hass, "voip_stack", "announce", {}, context=Context()), data, application=application)
    return application
