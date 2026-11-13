"""Optional recording owned by the existing call, independent of call audio."""

import asyncio
from dataclasses import dataclass, field

from homeassistant.exceptions import ServiceValidationError

from .call_scope import call_belongs_to_endpoint
from .endpoint_session import CleanupStage
from .mailbox import get_archive, message_audio_reference
from .runtime_data import require_runtime_data


def _publish_recording_state(hass, session):
    from .call_scope import call_endpoint_ids
    from .websocket_api import _publish_ha_softphone_state
    runtime = require_runtime_data(hass)
    for identity in call_endpoint_ids(runtime.sip, session.call_id):
        endpoint = runtime.endpoints.get(identity)
        if endpoint is not None and endpoint.supports_ha_client:
            _publish_ha_softphone_state(hass, endpoint_id=identity)


@dataclass(slots=True)
class CallRecording:
    hass: object
    session: object
    endpoint: object
    archive: object
    writer: object
    max_seconds: float
    detach: object = None
    task: object = None
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict | None = None
    error: str = ""

    def bind_media(self, observe):
        if not self.stopped.is_set() and self.detach is None:
            self.detach = observe(self.capture, self.end)

    def capture(self, side, pcm, fmt):
        try:
            self.writer.offer(pcm, fmt, channel="caller" if side == "left" else "callee")
        except Exception:
            self.end("capture_error")

    def end(self, reason):
        if reason in {"capture_error", "decode_error"}:
            self.error = reason
        self.stopped.set()

    async def run(self):
        stopped = asyncio.create_task(self.stopped.wait())
        failed = asyncio.create_task(self.writer.failed.wait())
        try:
            await asyncio.wait((stopped, failed), timeout=self.max_seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.stopped.set()
            if self.detach is not None:
                self.detach()
                self.detach = None
            for task in (stopped, failed):
                task.cancel()
            await asyncio.gather(stopped, failed, return_exceptions=True)
            if self.error:
                self.writer.fail(self.error)
            try:
                self.result = await self.writer.finish()
            except Exception:
                self.error = "finalization_failed"
            self.error = self.error or self.writer.error
            registry = require_runtime_data(self.hass).sip
            if registry is not None and self.session.live:
                registry.release_resource(self.session.call_id, f"recording:{self.session.call_id}", value=self,
                    generation=self.session.generation)
            _publish_recording_state(self.hass, self.session)
            event = {"call_id": self.session.call_id, "recording_id": self.writer.identity,
                "device_id": self.endpoint.device_id, "endpoint_id": self.endpoint.endpoint_id,
                "caller": self.session.caller, "callee": self.session.callee,
                "status": self.result["status"] if self.result is not None else "failed" if self.error else "empty",
                "error": self.error}
            if self.result is not None:
                try:
                    event.update(message_audio_reference(self.hass, self.archive, self.result))
                except ServiceValidationError:
                    pass
            if require_runtime_data(self.hass).endpoints.get(self.endpoint.endpoint_id) is not None:
                self.hass.bus.async_fire("voip_stack.recording_finished", event)

    async def close(self, _reason="stopped"):
        self.stopped.set()
        if self.task is not None and self.task is not asyncio.current_task():
            from .session_cleanup import async_wait_for_cleanup
            await async_wait_for_cleanup(self.task)


def _media_observer(runtime, session, endpoint_id):
    """Select the existing media adapter, without starting another receiver."""
    bridge = runtime.local_bridge
    local = bridge.get_call(session.call_id) if bridge is not None else None
    if local is not None:
        from .local_softphone_runtime import LOCAL_AUDIO_FORMAT
        def attach(recording):
            return bridge.observe_audio(session.call_id,
                lambda sender, pcm: recording.capture("left" if sender == local.caller_endpoint_id else "right", pcm, LOCAL_AUDIO_FORMAT),
                recording.end)
        return attach
    relay = runtime.sip.resource_for(session.call_id, "relay")
    if relay is not None and hasattr(relay, "observe_audio"):
        return lambda recording: relay.observe_audio(recording.capture, recording.end)
    audio = runtime.media.sessions_for("audio").get((session.call_id, endpoint_id))
    if audio is not None and audio.observe_audio is not None:
        return lambda recording: audio.observe_audio(recording.capture, recording.end)
    endpoint = runtime.endpoints.get(endpoint_id)
    if (endpoint is not None and endpoint.supports_ha_client
            and session.owner == "ha_softphone" and session.route_kind != "conference"):
        # The connected event can precede the browser media attachment.
        # The existing adapter binds this session-owned tap when it starts.
        return lambda _recording: None
    raise ServiceValidationError("Call audio is not ready or does not pass through Home Assistant")


async def _selected_call(call):
    runtime = require_runtime_data(call.hass)
    phone = await runtime.phones.resolve_source(call)
    endpoint = runtime.endpoints.get(phone.endpoint_id)
    identity = str(call.data.get("call_id") or (endpoint.active_call_id if endpoint else "") or "")
    registry = runtime.sip
    session = registry.get_session(registry.resolve_session_id(identity)) if registry else None
    if session is None or not session.live or not call_belongs_to_endpoint(registry, identity, phone.endpoint_id):
        raise ServiceValidationError("The selected phone does not own this call")
    expected = call.data.get("expected_generation")
    if expected is not None and expected != session.generation:
        raise ServiceValidationError("The selected call generation is no longer current")
    return runtime, session, endpoint


async def async_start_recording(call):
    runtime, session, endpoint = await _selected_call(call)
    lock = runtime.sip.resource_for(session.call_id, "recording_lock")
    if lock is None:
        lock = asyncio.Lock()
        runtime.sip.own_resource(session.call_id, f"recording_lock:{session.call_id}", lock, None,
            generation=session.generation)
    async with lock:
        if not runtime.sip.is_generation_current(session.call_id, session.generation):
            raise ServiceValidationError("The selected call has ended")
        return await _start_recording(call, runtime, session, endpoint)


async def _start_recording(call, runtime, session, endpoint):
    maximum = float(call.data.get("max_seconds", 3600))
    if not 1 <= maximum <= 3600:
        raise ServiceValidationError("Recording duration must be between 1 and 3600 seconds")
    if session.state != "in_call" or session.owner not in {"bridge", "ha_softphone", "local_bridge"}:
        raise ServiceValidationError("Select an established conversation to record")
    if runtime.sip.resource_for(session.call_id, "recording") is not None:
        raise ServiceValidationError("This call is already being recorded")
    _media_observer(runtime, session, endpoint.endpoint_id)
    archive = await get_archive(call.hass)
    writer = await archive.begin(endpoint.endpoint_id, kind="recording", caller=session.caller)
    recording = CallRecording(call.hass, session, endpoint, archive, writer, maximum)
    try:
        if (not runtime.sip.is_generation_current(session.call_id, session.generation)
                or session.state != "in_call" or session.owner not in {"bridge", "ha_softphone", "local_bridge"}
                or runtime.endpoints.get(endpoint.endpoint_id) is None
                or not call_belongs_to_endpoint(runtime.sip, session.call_id, endpoint.endpoint_id)):
            raise ServiceValidationError("The call changed while recording was being prepared")
        recording.detach = _media_observer(runtime, session, endpoint.endpoint_id)(recording)
        runtime.sip.own_resource(session.call_id, f"recording:{session.call_id}", recording, recording.close,
            stage=CleanupStage.MEDIA, generation=session.generation)
        recording.task = call.hass.async_create_task(recording.run())
        _publish_recording_state(call.hass, session)
    except BaseException:
        if recording.detach is not None:
            recording.detach()
        await writer.finish()
        raise
    return {"call_id": session.call_id, "recording_id": writer.identity, "generation": session.generation}


async def async_stop_recording(call):
    runtime, session, endpoint = await _selected_call(call)
    recording = runtime.sip.resource_for(session.call_id, "recording")
    if recording is None:
        return {"call_id": session.call_id, "status": "not_recording"}
    if recording.endpoint.endpoint_id != endpoint.endpoint_id:
        raise ServiceValidationError("This phone has no active recording for the selected call")
    await recording.close()
    if recording.error:
        raise ServiceValidationError("Recording stopped: " + recording.error)
    result = {"call_id": session.call_id, "recording_id": recording.writer.identity,
        "status": "complete" if recording.result else "empty"}
    if recording.result:
        result.update(message_audio_reference(call.hass, recording.archive, recording.result))
    return result


async def _selected_recording(call):
    from .mailbox import selected_mailbox
    from .core.media_archive import ArchiveError
    endpoint = await selected_mailbox(call, call.data["device_id"])
    archive = await get_archive(call.hass)
    try:
        message = archive.message(call.data["recording_id"], endpoint.endpoint_id)
    except ArchiveError as err:
        raise ServiceValidationError(str(err)) from err
    if message["kind"] != "recording":
        raise ServiceValidationError("Select a conversation recording")
    return archive, message


async def async_get_recording(call):
    archive, message = await _selected_recording(call)
    return {"recording_id": message["id"], "status": message["status"],
        **message_audio_reference(call.hass, archive, message)}


async def async_delete_recording(call):
    archive, message = await _selected_recording(call)
    await archive.delete(message["id"], message["mailbox_id"])
    return {"recording_id": message["id"], "deleted": True}
