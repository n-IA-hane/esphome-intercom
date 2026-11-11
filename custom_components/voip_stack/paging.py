"""Bounded paging through the existing application calls and RTP outputs."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import aclosing
from dataclasses import dataclass, field
from uuid import uuid4

from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .automation_originate import async_originate_application_call
from .dial_fork import DialCandidate, DialDisposition, DialForkController, DialOutcome
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage, TerminationIntent
from .local_application import LocalCallApplication, attach_local_application
from .paging_config import MAX_PAGING_TARGETS, paging_group_configs
from .ring_group import endpoint_preflight_disposition
from .roster import RosterEntry
from .runtime_data import require_runtime_data

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
    values["answer_timeout"] = float(values.get("answer_timeout", 5))
    values["timeout"] = float(values.get("timeout", 120))
    if not 1 <= values["answer_timeout"] <= 120 or not 1 <= values["timeout"] <= 600:
        raise ServiceValidationError("Paging timeouts exceed the supported limits")
    if values.get("busy_policy", "skip") != "skip":
        raise ServiceValidationError("Paging busy_policy must be skip")
    return values


def resolve_paging_targets(runtime, groups, targets):
    """Expand saved groups once, deduplicating the existing logical phones."""
    selected = OrderedDict()
    visiting = set()

    def expand(target):
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
            endpoint = runtime.endpoints.by_device_id(target) or runtime.endpoints.resolve(target)
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
    error: str = ""

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
        if not self.finishing:
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
        identity = endpoint.endpoint_id
        endpoint = require_runtime_data(self.manager.hass).endpoints.get(identity)
        if endpoint is None:
            self.results[identity] = {"status": "skipped", "reason": "removed"}
            return DialOutcome(DialDisposition.UNAVAILABLE)
        disposition = endpoint_preflight_disposition(endpoint, call_id=self.session.call_id,
                                                     browser=endpoint.kind.is_softphone)
        if disposition is not None:
            self.results[endpoint.endpoint_id] = {"status": "skipped", "reason": disposition.value}
            return DialOutcome(disposition)
        # A selector identity such as sip:desk is not the URI sip:desk.
        # Feed its canonical phonebook name into the existing route resolver.
        data = {"destination": endpoint.name, "answer_timeout": self.data["answer_timeout"]}
        call = ServiceCall(self.manager.hass, "voip_stack", "call", data, context=self.call.context)
        try:
            if endpoint.kind.is_softphone:
                token = await self.manager.originate_browser(self, endpoint)
            else:
                token = await async_originate_application_call(
                    call, source=RosterEntry(f"paging:{self.identity}", name="Announcement",
                                            metadata={"virtual_endpoint": "paging", "automation_timeout": self.data["timeout"]}),
                    answer_mode="Auto", audio_direction="sendonly",
                )
            self.calls[endpoint.endpoint_id] = token
            self.results[endpoint.endpoint_id] = {"status": "ready"}
            return DialOutcome(DialDisposition.ANSWERED, 200)
        except ServiceValidationError as error:
            self.results[endpoint.endpoint_id] = {"status": "skipped", "reason": str(error)}
            return DialOutcome(DialDisposition.UNAVAILABLE)

    def _application(self, endpoint_id):
        token = self.calls[endpoint_id]
        registry = call_registry(self.manager.hass)
        session = registry.get_session(token["call_id"], generation=token["generation"])
        application = registry.resource_for(token["call_id"], "automation") if session is not None else None
        if application is None or application.media is None:
            raise ConnectionError("The paging recipient ended its call")
        application.cancel_deadline()
        return application

    async def _chunks(self):
        if self.live_application is not None:
            application = self.live_application
            await application.answer()
            application.require_current()
            async with aclosing(application.media.input_pcm_stream()) as stream:
                async for chunk in stream:
                    yield chunk
            return
        if self.data.get("media"):
            from .announcement_media import async_audio_stream
            media = self.data["media"]
            identity = media.get("media_content_id") if isinstance(media, dict) else str(media)
            async with aclosing(async_audio_stream(self.manager.hass, identity)) as stream:
                async for chunk in stream:
                    yield chunk
            return
        from homeassistant.components import tts
        from .local_call_media import LocalCallMedia
        stream = tts.async_create_stream(self.manager.hass, self.data["tts_entity_id"], self.data.get("language"),
                                        {**self.data.get("options", {}), **LocalCallMedia._tts_audio_output()})
        stream.async_set_message(self.data["message"])
        async with aclosing(stream.async_stream_result()) as chunks:
            async for chunk in chunks:
                yield chunk

    async def _broadcast(self):
        """One producer, bounded queues, and existing per-dialog PCM encoders."""
        consumers = {}

        async def consume(endpoint_id, queue):
            async def chunks():
                while (chunk := await queue.get()) is not None:
                    yield chunk
            try:
                application = self._application(endpoint_id)
                media = application.media
                errors, suppressed = media.counters["tx_error"], media.counters["tx_suppressed"]
                async def play():
                    async with aclosing(chunks()) as stream:
                        await media.play_pcm_stream(stream)
                playback = asyncio.create_task(play())
                closed = asyncio.create_task(media.closed.wait())
                try:
                    done, _ = await asyncio.wait((playback, closed), return_when=asyncio.FIRST_COMPLETED)
                    if closed in done:
                        raise ConnectionError("The paging recipient ended its call")
                    await playback
                finally:
                    playback.cancel()
                    closed.cancel()
                    await asyncio.gather(playback, closed, return_exceptions=True)
                await media.drain_output()
                if media.counters["tx_error"] != errors or media.counters["tx_suppressed"] != suppressed:
                    raise ConnectionError("Paging audio could not be transmitted")
                self.results[endpoint_id] = {"status": "completed"}
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.results[endpoint_id] = {"status": "failed", "reason": str(error)}
            finally:
                await self._close_target(endpoint_id)

        async def put(endpoint_id, chunk):
            queue, consumer = consumers[endpoint_id]
            if consumer.done():
                return
            pending = asyncio.create_task(queue.put(chunk))
            try:
                await asyncio.wait((pending, consumer), return_when=asyncio.FIRST_COMPLETED)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        for endpoint_id in tuple(self.calls):
            queue = asyncio.Queue(maxsize=_FANOUT_CHUNKS)
            consumers[endpoint_id] = (queue, asyncio.create_task(consume(endpoint_id, queue), name=f"voip-page-output-{endpoint_id}"))
        async def wait_for_outputs():
            await asyncio.gather(*(task for _queue, task in consumers.values()))
        outputs_ended = asyncio.create_task(wait_for_outputs())
        try:
            async with aclosing(self._chunks()) as chunks:
                while True:
                    next_chunk = asyncio.create_task(anext(chunks))
                    try:
                        done, _ = await asyncio.wait((next_chunk, outputs_ended), return_when=asyncio.FIRST_COMPLETED)
                        if outputs_ended in done:
                            raise ConnectionError("All paging recipients ended their calls")
                        try:
                            chunk = await next_chunk
                        except StopAsyncIteration:
                            break
                    finally:
                        next_chunk.cancel()
                        await asyncio.gather(next_chunk, return_exceptions=True)
                    for offset in range(0, len(chunk), _CHUNK_BYTES):
                        piece = bytes(chunk[offset:offset + _CHUNK_BYTES])
                        await asyncio.gather(*(put(endpoint_id, piece) for endpoint_id in consumers))
            await asyncio.gather(*(put(endpoint_id, None) for endpoint_id in consumers))
            await asyncio.gather(*(task for _queue, task in consumers.values()))
        finally:
            for _queue, task in consumers.values():
                task.cancel()
            outputs_ended.cancel()
            await asyncio.gather(*(task for _queue, task in consumers.values()), return_exceptions=True)
            await asyncio.gather(outputs_ended, return_exceptions=True)

    async def run(self):
        registry = call_registry(self.manager.hass)
        try:
            async with asyncio.timeout(self.data["timeout"]):
                candidates = []
                for target in self.targets:
                    async def dial(endpoint=target):
                        return await self._admit(endpoint)
                    async def close(_mode, endpoint=target):
                        await self._close_target(endpoint.endpoint_id)
                    candidates.append(DialCandidate(target.endpoint_id, dial, close, endpoint_id=target.endpoint_id))
                await DialForkController(self.session, candidates, overall_timeout=self.data["timeout"]).run_all(
                    lambda _candidate, _outcome: self.session.live)
                if not self.calls:
                    self.state = "no_recipients"
                else:
                    self.state = "playing"
                    await self._broadcast()
                    self.state = "completed"
        except asyncio.CancelledError:
            self.state = "stopped"
        except TimeoutError:
            self.state = "timeout"
        except Exception as error:
            self.state = "failed"
            self.error = str(error)
        finally:
            self.finishing = True
            for target in self.targets:
                if self.results.get(target.endpoint_id, {}).get("status", "pending") in {"ready", "pending"}:
                    self.results[target.endpoint_id] = {"status": self.state,
                        **({"reason": self.error} if self.error else {})}
            from .session_cleanup import async_wait_for_cleanup
            async def cleanup():
                await asyncio.gather(*(self._close_target(identity) for identity in tuple(self.calls)))
                if self.session.live:
                    if self.live_application is not None and not self.live_application.answered:
                        await registry.request_termination(self.session.call_id, TerminationIntent.final_response("paging_ended", 480))
                    else:
                        await registry.terminate_call_wait(self.session.call_id, intent=TerminationIntent("paging_complete", public_state="idle"))
            await async_wait_for_cleanup(asyncio.create_task(cleanup(), name=f"voip-page-cleanup-{self.identity}"))
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
        if len(self.jobs) >= MAX_PAGING_JOBS:
            raise ServiceValidationError("All paging slots are in use")
        runtime = require_runtime_data(self.hass)
        targets = resolve_paging_targets(runtime, paging_group_configs(self.entry), data["targets"])
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
    job = _manager(call.hass).start(call, validate_announcement(call.data))
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
            await self.job.stop(reason)


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
    session = registry.transition(session.call_id, owner="paging", state="in_call" if previous and previous.answered else "ringing",
                                  callee=contact.display_name, route_kind="paging")
    registry.set_pending_invite(invite.call_id, invite)
    application = attach_local_application(PagingCall, hass, session, invite, contact, local_ip,
                                           previous=previous, local_source=local_source)
    data = {"targets": [f"paging:{identity}"], "answer_timeout": float(config.get("answer_timeout", 5)),
            "timeout": float(config.get("timeout", 120))}
    manager.start(ServiceCall(hass, "voip_stack", "announce", {}, context=Context()), data, application=application)
    return application
