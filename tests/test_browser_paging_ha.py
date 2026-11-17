"""Browser paging shares call ownership and waits for receive-only playback."""

import asyncio
from types import SimpleNamespace

from aiohttp import WSMsgType
import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack import audio_ws_view
from custom_components.voip_stack.audio_ws import decode_audio_frame, encode_audio_frame
from custom_components.voip_stack.browser_paging import async_originate_browser_paging
from custom_components.voip_stack.endpoint_lifecycle import project_session_termination
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.softphone_answer import async_answer_browser_call
from custom_components.voip_stack.softphone_commands import BrowserCallCommand

from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


class Browser:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.frames = []
        self.contracts = []
        self.closed = False

    async def send_json(self, value):
        self.contracts.append(value)

    async def send_bytes(self, value):
        self.frames.append(decode_audio_frame(value))

    def force_close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait(SimpleNamespace(type=WSMsgType.CLOSED, data=""))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        return await self.incoming.get()

    def ready(self):
        self.incoming.put_nowait(SimpleNamespace(type=WSMsgType.TEXT, data='{"type":"audio_ready"}'))


@pytest.fixture
def browser_lab(lab):
    phone = PhoneEndpoint(endpoint_id="paging-browser", name="Paging browser", device_id="paging-browser-device",
                          kind=EndpointKind.BROWSER, availability="available")
    lab.runtime.endpoints.register(phone)
    lab.runtime.softphone_presence[phone.endpoint_id] = 1
    lab.calls._bind_endpoint_registry(lab.runtime.endpoints)
    lab.calls.bind_termination_observer(lambda session, intent: project_session_termination(lab.hass, session, intent))
    job = SimpleNamespace(manager=SimpleNamespace(hass=lab.hass), identity="job-identity", data={"answer_timeout": 2})
    return lab, phone, job


async def _ringing(lab, phone, job):
    task = asyncio.create_task(async_originate_browser_paging(job, phone))
    async with asyncio.timeout(2):
        while not any(session.metadata.get("application") == "browser_paging" for session in lab.calls.sessions.values()):
            await asyncio.sleep(0)
    session = next(session for session in lab.calls.sessions.values() if session.metadata.get("application") == "browser_paging")
    return task, session


async def _answer(lab, phone, session):
    call = ServiceCall(lab.hass, "voip_stack", "answer", {"call_id": session.call_id, "media_client_id": "browser-client-identity"}, context=Context())
    command = BrowserCallCommand(phone.endpoint_id, phone, phone.name, phone.device_id, session.call_id, lab.calls)
    await async_answer_browser_call(lab.hass, call, command)
    return lab.calls.resource_for(session.call_id, "browser_paging")


async def test_browser_waits_for_answer_and_ready_then_receives_exact_pcm_without_rtp_or_capture(browser_lab):
    lab, phone, job = browser_lab
    task, session = await _ringing(lab, phone, job)
    browser, worker = Browser(), None
    try:
        assert not task.done() and session.state == "ringing"
        assert lab.runtime.endpoints.get(phone.endpoint_id).active_call_id == session.call_id
        assert session.metadata["announcement_id"] == job.identity
        application = await _answer(lab, phone, session)
        assert session.state == "in_call" and not task.done()
        media_session = audio_ws_view._active_softphone_media_session(lab.hass, phone.endpoint_id)
        assert media_session.application_playback is application.media
        assert media_session.local_rtp_port == 0 and media_session.local_audio_direction == "recvonly"
        application.playback.attach(browser)
        worker = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, media_session,
                                    endpoint_id=phone.endpoint_id, playback=application.playback))
        await asyncio.sleep(0)
        assert browser.contracts[0]["audio_direction"] == "recvonly" and not browser.frames
        assert not task.done()
        browser.ready()
        token = await asyncio.wait_for(task, 2)
        assert token == {"call_id": session.call_id, "generation": session.generation}
        assert not lab.runtime.rtp_port_pool.get("used") and not lab.calls.sip_client_for(session.call_id)
        payload = b"\x12\x34" * 960
        async def stream():
            yield payload[:713]
            yield payload[713:]
        # Recipient microphone frames must never feed the source or other outputs.
        browser.incoming.put_nowait(SimpleNamespace(type=WSMsgType.BINARY, data=encode_audio_frame(b"\x55\x55" * 320)))
        await application.media.play_pcm_stream(stream())
        await application.media.drain_output()
        assert b"".join(browser.frames) == payload
        assert application.media.counters["drop_direction"] == 1
        assert application.media.counters["tx_error"] == 0
        await lab.calls.terminate_call_wait(session.call_id, reason="paging_complete")
        await asyncio.wait_for(worker, 2)
        assert not lab.calls.sessions
        assert not lab.runtime.endpoints.get(phone.endpoint_id).active_call_id
        assert application.media.closed.is_set() and browser.closed
        assert not lab.runtime.media.owners_for("audio")
    finally:
        if session.live:
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        task.cancel()
        if worker is not None:
            worker.cancel()
        await asyncio.gather(task, *( [worker] if worker is not None else []), return_exceptions=True)


@pytest.mark.parametrize("blocked", ["offline", "busy", "dnd", "unavailable"])
async def test_browser_rejects_unready_or_busy_recipient_before_claim(browser_lab, blocked):
    lab, phone, job = browser_lab
    if blocked == "offline":
        lab.runtime.softphone_presence.clear()
    elif blocked == "busy":
        lab.runtime.endpoints.claim_call(phone.endpoint_id, "existing-call")
    else:
        lab.runtime.endpoints.update(phone.endpoint_id, **{
            "dnd": {"dnd": True},
            "unavailable": {"availability": "unavailable"},
        }[blocked])
    with pytest.raises(ServiceValidationError):
        await async_originate_browser_paging(job, phone)
    assert not lab.calls.sessions


@pytest.mark.parametrize("finish", ["timeout", "cancel", "hangup"])
async def test_browser_admission_never_leaks_call_before_token(browser_lab, finish):
    lab, phone, job = browser_lab
    job.data["answer_timeout"] = .03 if finish == "timeout" else 2
    task, session = await _ringing(lab, phone, job)
    if finish == "cancel":
        task.cancel()
    elif finish == "hangup":
        await lab.calls.terminate_call_wait(session.call_id, reason="local_hangup")
    with pytest.raises(asyncio.CancelledError if finish == "cancel" else ServiceValidationError):
        await task
    assert not lab.calls.sessions
    assert not lab.runtime.endpoints.get(phone.endpoint_id).active_call_id


async def test_browser_pcm_queue_backpressure_and_disconnect_wake_producer(browser_lab):
    lab, phone, job = browser_lab
    task, session = await _ringing(lab, phone, job)
    application = await _answer(lab, phone, session)
    connection = object()
    application.playback.attach(connection)
    application.playback.ready(connection)
    await task
    consumed = 0
    async def stream():
        nonlocal consumed
        for _ in range(50):
            consumed += 1
            yield b"\0" * 640
    sender = asyncio.create_task(application.media.play_pcm_stream(stream()))
    async with asyncio.timeout(2):
        while application.media.tx_queue.qsize() < 8:
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not sender.done() and consumed <= 9
    await lab.calls.terminate_call_wait(session.call_id, reason="browser_disconnected")
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(sender, 2)
    assert not lab.calls.sessions and not application.media.tx_queue.qsize()


async def test_browser_announcement_fields_are_call_scoped_and_not_sticky(browser_lab):
    from custom_components.voip_stack.websocket_api import _ha_softphone_state, _set_ha_softphone_call_state

    lab, phone, job = browser_lab
    task, session = await _ringing(lab, phone, job)
    snapshot = _ha_softphone_state(lab.hass, phone.endpoint_id)
    assert snapshot["announcement_id"] == job.identity
    assert (snapshot["answer_mode"], snapshot["audio_direction"]) == ("auto", "recvonly")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _set_ha_softphone_call_state(lab.hass, "ringing", endpoint_id=phone.endpoint_id,
                                call_id="ordinary-next-call", caller="Ordinary", callee=phone.name)
    snapshot = _ha_softphone_state(lab.hass, phone.endpoint_id)
    assert not snapshot["announcement_id"] and not snapshot["answer_mode"]


async def test_mixed_browser_and_sip_page_waits_for_playback_then_synthesizes_once(browser_lab, monkeypatch):
    from custom_components.voip_stack import paging
    from custom_components.voip_stack.core import rtp
    from .test_automation_originate_ha import tts_fixture
    from .test_paging_ha import manager

    lab, phone, _job = browser_lab
    errors = []
    original_broadcast = paging.PagingJob._broadcast
    async def observe_broadcast(job):
        try:
            return await original_broadcast(job)
        except Exception as error:
            errors.append(repr(error))
            raise
    monkeypatch.setattr(paging.PagingJob, "_broadcast", observe_broadcast)
    owner = manager(lab, monkeypatch)
    owner.browser_originate = async_originate_browser_paging
    stream = tts_fixture(monkeypatch, lab)
    call = ServiceCall(lab.hass, "voip_stack", "announce", {
        "targets": [phone.endpoint_id, "sip:bedroom0"], "tts_entity_id": "tts.test",
        "message": "Wake up", "wait": False,
    }, context=Context())
    result = await paging.async_announce(call)
    browser, worker = Browser(), None
    try:
        async with asyncio.timeout(3):
            while not any(item.metadata.get("application") == "browser_paging" for item in lab.calls.sessions.values()):
                await asyncio.sleep(0)
        session = next(item for item in lab.calls.sessions.values() if item.metadata.get("application") == "browser_paging")
        application = await _answer(lab, phone, session)
        media_session = audio_ws_view._active_softphone_media_session(lab.hass, phone.endpoint_id)
        application.playback.attach(browser)
        worker = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, media_session,
                                    endpoint_id=phone.endpoint_id, playback=application.playback))
        await asyncio.wait_for(lab.phones[0].answered.wait(), 3)
        assert not stream.calls and not browser.frames
        browser.ready()
        async with asyncio.timeout(5):
            while result["announcement_id"] in owner.jobs:
                await asyncio.sleep(.001)
        await asyncio.wait_for(worker, 2)
        report = owner.completed[result["announcement_id"]]
        assert report["state"] == "completed", (report, errors)
        assert len(stream.calls) == 1
        assert all(target["status"] == "completed" for target in report["targets"])
        assert b"".join(browser.frames) == stream.pcm
        wire_pcm = bytearray()
        while not lab.phones[0].audio.empty():
            raw, _ = lab.phones[0].audio.get_nowait()
            payload = rtp.parse_packet(raw).payload
            if any(payload):
                wire_pcm.extend(payload)
        assert bytes(wire_pcm) == b"".join(stream.pcm[index:index + 2][::-1] for index in range(0, len(stream.pcm), 2))
        assert lab.phones[0].bye.is_set() and browser.closed
        assert not lab.calls.sessions and not owner.jobs
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
    finally:
        await owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_paging_uses_existing_websocket_claim_and_rejects_other_media_client(browser_lab):
    from custom_components.voip_stack.media_ws_session import async_media_websocket_session
    from custom_components.voip_stack.websocket_owner import (
        MediaWebSocketOwner, WebSocketOwnerBusyError, async_claim_call_media_owner,
    )

    lab, phone, job = browser_lab
    task, session = await _ringing(lab, phone, job)
    application = await _answer(lab, phone, session)
    browser = Browser()
    owner = MediaWebSocketOwner(websocket=browser, user_id="fixture", client_id="browser-client-identity")
    worker = None
    try:
        async with async_media_websocket_session(lab.runtime.media, lab.calls, session.call_id, phone.endpoint_id,
            owner, channel="audio", timeout=1, shutdown_event=lab.runtime.media.shutdown,
            pin_client_identity=True, local_bridge=None, publish_state=lambda: None):
            key = f"{phone.endpoint_id}|{session.call_id}"
            assert lab.runtime.media.owners_for("audio")[key] is owner
            other = MediaWebSocketOwner(websocket=Browser(), user_id="fixture", client_id="another-browser-client")
            with pytest.raises(WebSocketOwnerBusyError):
                await async_claim_call_media_owner(lab.runtime.media, lab.calls, session.call_id,
                    phone.endpoint_id, other, channel="audio", timeout=1)
            application.playback.attach(browser)
            media_session = audio_ws_view._active_softphone_media_session(lab.hass, phone.endpoint_id)
            worker = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, media_session,
                endpoint_id=phone.endpoint_id, playback=application.playback, handoff_requested=owner.handoff_requested))
            browser.ready()
            await task
            await lab.calls.terminate_call_wait(session.call_id, reason="local_hangup")
            await asyncio.wait_for(worker, 2)
        assert not lab.runtime.media.owners_for("audio")
        assert not lab.runtime.media.identity_locks
        assert owner.released.is_set() and not lab.calls.sessions
    finally:
        if session.live:
            await lab.calls.terminate_call_wait(session.call_id, reason="local_hangup")
        task.cancel()
        if worker is not None:
            worker.cancel()
        await asyncio.gather(task, *([worker] if worker is not None else []), return_exceptions=True)


async def test_failed_browser_contract_send_immediately_closes_pending_media(browser_lab):
    lab, phone, job = browser_lab
    task, session = await _ringing(lab, phone, job)
    application = await _answer(lab, phone, session)
    browser = Browser()
    async def disconnected(_contract):
        raise ConnectionError("Disconnected before the media contract")
    browser.send_json = disconnected
    application.playback.attach(browser)
    media_session = audio_ws_view._active_softphone_media_session(lab.hass, phone.endpoint_id)
    try:
        with pytest.raises(ConnectionError, match="before the media contract"):
            await audio_ws_view._run_audio_session(lab.hass, browser, media_session,
                endpoint_id=phone.endpoint_id, playback=application.playback)
        assert application.media.closed.is_set()
        with pytest.raises(ServiceValidationError):
            await asyncio.wait_for(task, .2)
        assert not lab.calls.sessions
        assert not lab.runtime.endpoints.get(phone.endpoint_id).active_call_id
    finally:
        if session.live:
            await lab.calls.terminate_call_wait(session.call_id, reason="local_hangup")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
