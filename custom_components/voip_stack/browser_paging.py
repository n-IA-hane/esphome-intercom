"""Receive-only browser paging through the existing call and WebSocket owners."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import ClassVar
from uuid import uuid4

from homeassistant.exceptions import ServiceValidationError

from .automation_call import AutomationCall
from .browser_playback import BrowserPlayback
from .call_projection import publish_phone_projection
from .core.audio_format import AudioFormat
from .core.audio_pcm import PcmFrameConverter
from .endpoint_lifecycle import call_registry
from .endpoint_registry import EndpointBusyError
from .endpoint_session import CleanupStage
from .ring_group import endpoint_preflight_disposition
from .roster import RosterEntry
from .runtime_data import require_runtime_data
from .session_cleanup import async_wait_for_cleanup


BROWSER_PAGING_PCM = AudioFormat(16000, "s16le", 1, 20)
_OUTPUT_FRAMES = 8
_PLAYOUT_TAIL = 0.2


class BrowserPagingMedia:
    """Bounded PCM output consumed by the existing authenticated media socket."""

    def __init__(self, playback):
        self.playback = playback
        self.tx_queue = asyncio.Queue(maxsize=_OUTPUT_FRAMES)
        self.closed = asyncio.Event()
        self.can_send = True
        self.counters = {"tx_error": 0, "tx_suppressed": 0, "ws_tx": 0, "drop_direction": 0}
        self.last_output_deadline = 0.0

    async def _while_open(self, operation):
        if self.closed.is_set():
            if asyncio.iscoroutine(operation):
                operation.close()
            raise ConnectionError("The browser paging recipient disconnected")
        pending = asyncio.ensure_future(operation)
        ended = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait({pending, ended}, return_when=asyncio.FIRST_COMPLETED)
            if ended in done or self.closed.is_set():
                raise ConnectionError("The browser paging recipient disconnected")
            return await pending
        finally:
            pending.cancel()
            ended.cancel()
            await asyncio.gather(pending, ended, return_exceptions=True)

    async def play_pcm_stream(self, chunks, *, source_format=BROWSER_PAGING_PCM):
        converter = PcmFrameConverter(source_format, BROWSER_PAGING_PCM)
        frame_bytes = source_format.nominal_frame_bytes
        pending = bytearray()
        async for chunk in chunks:
            # Bound staging even if a stream producer supplies a large chunk.
            view = memoryview(chunk)
            for offset in range(0, len(view), frame_bytes):
                pending.extend(view[offset:offset + frame_bytes])
                while len(pending) >= frame_bytes:
                    raw = bytes(pending[:frame_bytes])
                    del pending[:frame_bytes]
                    for frame in converter.convert(raw):
                        await self._while_open(self.tx_queue.put(frame))
        if pending:
            pending.extend(bytes(frame_bytes - len(pending)))
            for frame in converter.convert(bytes(pending)):
                await self._while_open(self.tx_queue.put(frame))

    async def drain_output(self):
        await self._while_open(self.tx_queue.join())
        remaining = self.last_output_deadline + _PLAYOUT_TAIL - asyncio.get_running_loop().time()
        if remaining > 0:
            await self._while_open(asyncio.sleep(remaining))

    def discard_output(self):
        while not self.tx_queue.empty():
            self.tx_queue.get_nowait()
            self.tx_queue.task_done()

    async def stop(self, _reason="ended"):
        self.closed.set()
        self.can_send = False
        # Unblock a full-queue producer without letting it refill a drained call.
        self.tx_queue.shutdown(immediate=True)


@dataclass(slots=True)
class BrowserPagingApplication(AutomationCall):
    """Use ordinary application control with a browser output instead of RTP."""

    owner: ClassVar[str] = "paging"
    endpoint_id: str = ""
    accepted: asyncio.Event = field(default_factory=asyncio.Event)

    def accept(self, call):
        from .softphone_commands import bind_service_call_controller

        self.require_current()
        if self.session.metadata.get("endpoint_id") != self.endpoint_id:
            raise ServiceValidationError("This paging call belongs to another phone")
        bind_service_call_controller(call_registry(self.hass), self.session.call_id, call,
                                     endpoint_id=self.endpoint_id)
        if self.answered:
            return
        registry = call_registry(self.hass)
        session = registry.transition(self.session.call_id, state="in_call", owner="paging",
                                      media_client_id=str(call.data.get("media_client_id") or ""),
                                      expected_generation=self.session.generation)
        if session is None:
            raise ServiceValidationError("This paging call has ended")
        registry.observe_leg(session.call_id, f"browser:{self.endpoint_id}", role="ha_softphone",
                             endpoint_id=self.endpoint_id, state="in_call", generation=session.generation)
        self.answered = True
        self.accepted.set()
        publish_phone_projection(self.hass, session, self.endpoint_id, direction="incoming",
                                 peer_name="Announcement", audio_direction="recvonly",
                                 announcement_id=session.metadata["announcement_id"], answer_mode="auto",
                                 event_type="answered")

    async def answer(self):
        self.require_current()
        await self.accepted.wait()
        self.require_current()

    async def forward(self, *_args, **_kwargs):
        raise ServiceValidationError("A paging recipient cannot be forwarded")

    async def close(self, reason):
        await super(BrowserPagingApplication, self).close(reason)
        await self.media.stop(reason)
        await self.playback.close(reason)
        self.accepted.set()


async def async_originate_browser_paging(job, endpoint):
    """Return only after a live browser accepted and acknowledged playback."""
    hass = job.manager.hass
    runtime = require_runtime_data(hass)
    registry = call_registry(hass)
    current = runtime.endpoints.get(endpoint.endpoint_id)
    if current is None or not current.supports_ha_client:
        raise ServiceValidationError("The paging browser no longer exists")
    if not int(runtime.softphone_presence.get(current.endpoint_id, 0)):
        raise ServiceValidationError("The paging browser is offline")
    disposition = endpoint_preflight_disposition(current, call_id="", browser=True)
    if disposition is not None:
        raise ServiceValidationError(f"The paging browser is {disposition.value}")
    session = registry.upsert(f"browser-page:{uuid4().hex}", state="ringing", owner="paging",
                              caller="Announcement", callee=current.name, route_kind="paging",
                              endpoint_id=current.endpoint_id, dest_endpoint_id=current.endpoint_id,
                              session_device_id=current.device_id, application="browser_paging",
                              announcement_id=job.identity, answer_mode="auto", audio_direction="recvonly")
    try:
        if not registry.claim_endpoint(session.call_id, current.endpoint_id, role="destination"):
            raise ServiceValidationError("The paging browser cannot be claimed")
        playback = BrowserPlayback()
        media = BrowserPagingMedia(playback)
        application = BrowserPagingApplication(hass=hass, session=session, invite=None,
            contact=RosterEntry("paging", name="Announcement"), local_ip="", media=media,
            playback=playback, endpoint_id=current.endpoint_id)
        registry.own_resource(session.call_id, f"automation:{session.call_id}", application, application.close,
                              stage=CleanupStage.MEDIA, generation=session.generation)
        registry.own_resource(session.call_id, f"browser_paging:{session.call_id}", application, lambda _: None,
                              generation=session.generation)
        registry.own_resource(session.call_id, f"browser_playback:{session.call_id}", playback, playback.close,
                              stage=CleanupStage.MEDIA, generation=session.generation)
        registry.attach_media(session.call_id, {"endpoint_id": current.endpoint_id, "application_playback": media})
        registry.observe_leg(session.call_id, f"browser:{current.endpoint_id}", role="ha_softphone",
                             endpoint_id=current.endpoint_id, state="ringing", generation=session.generation)
        publish_phone_projection(hass, session, current.endpoint_id, direction="incoming",
                                 peer_name="Announcement", audio_direction="recvonly", answer_mode="auto",
                                 announcement_id=job.identity, event_type="ringing")
        async with asyncio.timeout(float(job.data["answer_timeout"])):
            await media._while_open(application.accepted.wait())
            await media._while_open(playback.wait_ready())
        application.require_current()
        return {"call_id": session.call_id, "generation": session.generation}
    except BaseException as error:
        await async_wait_for_cleanup(asyncio.create_task(
            registry.terminate_call_wait(session.call_id, reason="paging_recipient_unavailable")))
        if isinstance(error, (TimeoutError, ConnectionError, EndpointBusyError)):
            raise ServiceValidationError("The paging browser did not become ready") from error
        raise
