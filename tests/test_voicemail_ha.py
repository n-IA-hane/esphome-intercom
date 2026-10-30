"""Application handoff and mailbox persistence through real SIP/RTP calls."""

import asyncio
import re
import struct
import wave
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import Context, callback
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers import config_validation as cv

from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.mailbox import get_archive, EVENT_VOICEMAIL_RECEIVED
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.websocket_api import CALL_EVENT


from .test_automation_originate_ha import lab as lab, simple_sequence, tts_fixture, dial
from .test_browser_phone_policy_ha import browser_lab as browser_lab, originate
from .test_trunk_calls_ha import trunks as trunks

pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def configured_tts_provider(monkeypatch):
    """The suite's injected TTS stream represents its configured test provider."""
    from types import SimpleNamespace
    from custom_components.voip_stack import voicemail_config
    monkeypatch.setattr(voicemail_config, "voice_engine", lambda _hass, identity:
                        SimpleNamespace(available=True) if identity == "tts.test" else None)


@pytest.fixture
async def speech_entity(mailbox_lab):
    import logging
    from homeassistant.components import stt
    from homeassistant.helpers.entity_component import EntityComponent

    class Speech(stt.SpeechToTextEntity):
        _attr_name = "Mailbox STT"
        _attr_should_poll = False
        supported_languages = ["en"]
        supported_formats = [stt.AudioFormats.WAV]
        supported_codecs = [stt.AudioCodecs.PCM]
        supported_bit_rates = [stt.AudioBitRates.BITRATE_16]
        supported_sample_rates = [stt.AudioSampleRates.SAMPLERATE_16000]
        supported_channels = [stt.AudioChannels.CHANNEL_MONO]

        def __init__(self):
            self.received = []
            self.wait = False
            self.fail = False
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()

        async def async_process_audio_stream(self, metadata, stream):
            pcm = bytearray()
            try:
                async for chunk in stream:
                    self.started.set()
                    pcm.extend(chunk)
                    if self.wait:
                        await asyncio.Event().wait()
                self.received.append((metadata, bytes(pcm)))
                return stt.SpeechResult("A package arrived", stt.SpeechResultState.ERROR if self.fail else stt.SpeechResultState.SUCCESS)
            finally:
                self.stopped.set()

    hass = mailbox_lab.hass
    component = EntityComponent(logging.getLogger(__name__), "stt", hass)
    hass.data[stt.DATA_COMPONENT] = component
    engine = Speech()
    await component.async_add_entities([engine])
    yield engine
    await component.async_remove_entity(engine.entity_id)


async def test_transcription_uses_native_stt_and_preserves_message(mailbox_lab, speech_entity):
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    archive = await get_archive(lab.hass)
    writer = await archive.begin("kitchen", caller="Delivery")
    pcm = struct.pack("<640h", *range(640))
    assert writer.offer(pcm, AudioFormat(16000, frame_ms=20))
    message = await writer.finish()
    result = await lab.hass.services.async_call("voip_stack", "transcribe_voicemail", {
        "mailbox": "mailbox-device", "message_id": message["id"],
        "stt_entity_id": speech_entity.entity_id, "language": "en",
    }, blocking=True, return_response=True)
    assert result == {"text": "A package arrived", "language": "en", "mailbox_id": "kitchen",
                      "message_id": message["id"], "caller": "Delivery"}
    assert speech_entity.received[0][1] == pcm
    assert speech_entity.received[0][0].sample_rate == 16000
    assert archive.counts("kitchen") == (1, 0)
    assert archive.message(message["id"], "kitchen") == message


async def test_message_reference_uses_existing_ha_media_file(mailbox_lab, tmp_path):
    from homeassistant.components import media_source
    from homeassistant.setup import async_setup_component
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    assert await async_setup_component(lab.hass, "media_source", {"media_source": {}})
    archive = await get_archive(lab.hass)
    writer = await archive.begin("kitchen", caller="Delivery")
    pcm = struct.pack("<640h", *range(640))
    assert writer.offer(pcm, AudioFormat(16000, frame_ms=20), offset=0)
    message = await writer.finish()
    result = await lab.hass.services.async_call("voip_stack", "get_voicemail", {
        "mailbox": "mailbox-device", "message_id": message["id"],
    }, blocking=True, return_response=True)
    resolved = await media_source.async_resolve_media(lab.hass, result["media_source_id"], None)
    assert resolved.path == Path(result["file_path"])
    assert resolved.path == archive.segment_path(message["id"], "kitchen")
    assert resolved.mime_type.startswith("audio/")
    with wave.open(result["file_path"], "rb") as audio:
        assert audio.readframes(audio.getnframes()) == pcm
    assert archive.counts("kitchen") == (1, 0)
    assert archive.message(message["id"], "kitchen") == message
    assert list(archive.root.glob("*.wav")) == [resolved.path]
    await archive.delete(message["id"], "kitchen")
    assert not resolved.path.exists()


async def test_message_metadata_selection_reaches_older_pages(mailbox_lab, hass_admin_user):
    from inspect import unwrap
    from types import SimpleNamespace
    from custom_components.voip_stack.mailbox_api import websocket_list
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    lab.runtime.endpoints.update("kitchen", entity_ids=frozenset({"sensor.kitchen"}))
    archive = await get_archive(lab.hass)
    identities = []
    for _ in range(3):
        writer = await archive.begin("kitchen")
        assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
        identities.append((await writer.finish())["id"])
    results, errors = [], []
    connection = SimpleNamespace(user=hass_admin_user,
                                 send_result=lambda _id, data: results.append(data),
                                 send_error=lambda *args: errors.append(args))
    request = {"id": 1, "endpoint_id": "kitchen", "offset": 0, "limit": 1}
    await unwrap(websocket_list)(lab.hass, connection, request)
    assert results[-1]["messages"][0]["id"] == identities[-1]
    await unwrap(websocket_list)(lab.hass, connection, {**request, "message_id": identities[0]})
    assert results[-1]["messages"][0]["id"] == identities[0]
    assert not errors


@pytest.mark.parametrize("failure", ["language", "provider", "timeout", "cancel"])
async def test_transcription_errors_leave_mailbox_reusable(mailbox_lab, speech_entity, failure):
    from homeassistant.exceptions import ServiceValidationError
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    archive = await get_archive(lab.hass)
    writer = await archive.begin("kitchen")
    assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
    message = await writer.finish()
    data = {"mailbox": "mailbox-device", "message_id": message["id"],
            "stt_entity_id": speech_entity.entity_id, "language": "xx" if failure == "language" else "en", "timeout": 1}
    speech_entity.fail = failure == "provider"
    speech_entity.wait = failure in {"timeout", "cancel"}
    task = asyncio.create_task(lab.hass.services.async_call("voip_stack", "transcribe_voicemail", data,
                                                         blocking=True, return_response=True))
    if failure == "cancel":
        await asyncio.wait_for(speech_entity.started.wait(), 2)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ServiceValidationError):
        await task
    assert archive.counts("kitchen") == (1, 0)
    assert not archive._active and not archive._reserved
    speech_entity.wait = speech_entity.fail = False
    result = await lab.hass.services.async_call("voip_stack", "transcribe_voicemail", {**data, "language": "en"},
                                              blocking=True, return_response=True)
    assert result["message_id"] == message["id"] and result["text"] == "A package arrived"


async def test_voicemail_trigger_binds_concurrent_messages_without_call_state(mailbox_lab, speech_entity):
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components.voip_stack.trigger import VoicemailReceivedTrigger
    from custom_components.voip_stack.mailbox_actions import current_message
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    archive = await get_archive(lab.hass)
    messages = []
    for caller in ("First", "Second"):
        writer = await archive.begin("kitchen", caller=caller)
        assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
        messages.append(await writer.finish())
    release = asyncio.Event()
    tasks = []

    async def transcribe():
        await release.wait()
        return await lab.hass.services.async_call("voip_stack", "transcribe_voicemail", {
            "stt_entity_id": speech_entity.entity_id, "language": "en",
        }, blocking=True, return_response=True)

    def runner(payload, description, context):
        tasks.append(asyncio.create_task(transcribe()))
        return tasks[-1]

    subject = VoicemailReceivedTrigger(lab.hass, TriggerConfig("voip_stack.voicemail_received", options={"mailbox": "mailbox-device"}))
    detach = await subject.async_attach_runner(runner)
    try:
        for message in messages:
            lab.hass.bus.async_fire(EVENT_VOICEMAIL_RECEIVED, {"mailbox_id": "kitchen", "message_id": message["id"]})
        await lab.hass.async_block_till_done()
        assert len(tasks) == 2
        release.set()
        results = await asyncio.gather(*tasks)
        assert {item["message_id"] for item in results} == {item["id"] for item in messages}
        assert current_message.get() is None
        assert not lab.calls.sessions
    finally:
        release.set()
        detach()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def custom_integrations(enable_custom_integrations):
    import custom_components
    previous = custom_components.__path__
    custom_components.__path__ = [*previous, str(Path(__file__).parents[1] / "custom_components")]
    yield
    custom_components.__path__ = previous


@pytest.fixture
def mailbox_lab(lab, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from custom_components.voip_stack import voicemail_config
    monkeypatch.setattr(voicemail_config, "voice_engine", lambda *_: SimpleNamespace(available=True))
    lab.hass.config.config_dir = str(tmp_path)
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title="Kitchen", unique_id="phone:kitchen",
        data={"endpoint_id": "kitchen", "name": "Kitchen", "kind": "browser",
              "voicemail": {"enabled": True, "max_seconds": 2}},
    ))
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="kitchen", name="Kitchen", kind=EndpointKind.BROWSER,
                                               device_id="mailbox-device", availability="available"))
    return lab


@pytest.mark.parametrize("termination", ["remote", "limit", "removed", "remote_then_removed", "lookup_then_removed"])
async def test_tts_then_voicemail_reuses_media_and_saves_received_pcm(mailbox_lab, monkeypatch, termination):
    lab = mailbox_lab
    stream = tts_fixture(monkeypatch, lab)
    recording, saved = asyncio.Event(), asyncio.Event()
    saved_events = []

    @callback
    def state(event):
        if event.data.get("event_type") == "voicemail_recording":
            recording.set()

    @callback
    def message(event):
        saved_events.append(dict(event.data))
        saved.set()

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, state)
    unsubscribe_saved = lab.hass.bus.async_listen(EVENT_VOICEMAIL_RECEIVED, message)
    actions = simple_sequence()
    actions[-1] = {"action": "voip_stack.send_to_voicemail", "data": {"mailbox": "mailbox-device"}}
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(actions)), "Voicemail", "automation")
    try:
        await script.async_run(context=Context())
        await asyncio.wait_for(recording.wait(), 3)
        assert len(lab.phones[0].invites) == 1 and len(stream.calls) == 1
        call_id = lab.phones[0].invites[0].header("Call-ID")
        session = lab.calls.get_session(call_id)
        voicemail = lab.calls.resource_for(call_id, "voicemail")
        assert session.live and session.owner == "voicemail"
        assert voicemail.media is lab.calls.resource_for(call_id, "relay")
        assert lab.calls.resource_for(call_id, "automation") is None
        received = asyncio.Event()
        chunks = []
        def observe(pcm, _format):
            chunks.append(pcm)
            if len(chunks) == 6:
                received.set()
        detach = voicemail.media.decoder.add_pcm_observer(observe)
        try:
            port = int(re.search(rb"m=audio (\d+)", lab.phones[0].invites[0].body)[1])
            for index in range(6):
                packet = rtp.build_packet(rtp.RtpPacket(lab.phones[0].payload_type, index, index * 320,
                                                       9876, struct.pack("!320h", *([2300] * 320))))
                await lab.hass.loop.sock_sendto(lab.phones[0].rtp_socket, packet, ("127.0.0.1", port))
            await asyncio.wait_for(received.wait(), 2)
        finally:
            detach()
        if termination == "remote":
            lab.phones[0].remote_hangup()
        if termination in {"removed", "remote_then_removed", "lookup_then_removed"}:
            from custom_components.voip_stack.mailbox import async_remove_mailbox

            if termination == "remote_then_removed":
                archive = await get_archive(lab.hass)
                written, release = asyncio.Event(), asyncio.Event()
                original_io = archive.run_io

                async def blocked_finish(function, *args):
                    result = await original_io(function, *args)
                    if function.__name__ == "finish":
                        written.set()
                        await release.wait()
                    return result

                archive.run_io = blocked_finish
                lab.phones[0].remote_hangup()
                await asyncio.wait_for(written.wait(), 2)
                assert not session.live
                removal = asyncio.create_task(async_remove_mailbox(lab.hass, "kitchen"))
                try:
                    await asyncio.sleep(0)  # Start removal while finalization is held.
                finally:
                    release.set()
                await asyncio.wait_for(removal, 2)
            elif termination == "lookup_then_removed":
                from custom_components.voip_stack import voicemail as voicemail_module

                lookup, release = asyncio.Event(), asyncio.Event()
                original_get = voicemail_module.get_archive

                async def blocked_lookup(hass):
                    lookup.set()
                    await release.wait()
                    return await original_get(hass)

                monkeypatch.setattr(voicemail_module, "get_archive", blocked_lookup)
                lab.phones[0].remote_hangup()
                await asyncio.wait_for(lookup.wait(), 2)
                removal = asyncio.create_task(async_remove_mailbox(lab.hass, "kitchen"))
                try:
                    await asyncio.sleep(0)
                finally:
                    release.set()
                await asyncio.wait_for(removal, 2)
            else:
                await async_remove_mailbox(lab.hass, "kitchen")
            await lab.hass.async_block_till_done()
            assert not (await session.terminate(session.termination_intent)).errors
            archive = await get_archive(lab.hass)
            assert not lab.calls.sessions and not saved_events
            assert not archive.list("kitchen")
            assert not archive._active and archive._reserved == 0
            assert not tuple(archive.root.glob("*.wav"))
            await archive.load()
            assert not archive.list("kitchen")
            return
        await asyncio.wait_for(saved.wait(), 4)
        barrier = lab.calls.request_termination(call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions and len(saved_events) == 1
        archive = await get_archive(lab.hass)
        entry = archive.list("kitchen")[0]
        assert entry["caller"] == "bedroom0"
        assert archive.counts("kitchen") == (1, 0)
        with wave.open(str(archive.segment_path(entry["id"], "kitchen")), "rb") as wav:
            assert wav.getframerate() == 16000
            assert wav.readframes(wav.getnframes()) == struct.pack("<1920h", *([2300] * 1920))
        assert not archive._active and archive._reserved == 0
    finally:
        unsubscribe()
        unsubscribe_saved()
        await script.async_stop()


async def test_reinvite_during_voicemail_keeps_recording_at_each_native_rate(mailbox_lab, monkeypatch):
    lab = mailbox_lab
    ready, saved = asyncio.Event(), asyncio.Event()

    @callback
    def state(event):
        if event.data.get("event_type") == "voicemail_recording":
            ready.set()

    @callback
    def message(_event):
        saved.set()

    unsub = lab.hass.bus.async_listen(CALL_EVENT, state)
    unsub_saved = lab.hass.bus.async_listen(EVENT_VOICEMAIL_RECEIVED, message)
    ctx = Context()
    try:
        result = await dial(lab, ctx)
        await lab.hass.services.async_call("voip_stack", "send_to_voicemail", {"mailbox": "mailbox-device"},
                                           blocking=True, context=ctx)
        await asyncio.wait_for(ready.wait(), 2)
        voicemail = lab.calls.resource_for(result["call_id"], "voicemail")
        first, second = asyncio.Event(), asyncio.Event()
        def observe(_pcm, fmt):
            (first if fmt.sample_rate == 16000 else second).set()
        detach = voicemail.media.decoder.add_pcm_observer(observe)
        phone = lab.phones[0]
        port = int(re.search(rb"m=audio (\d+)", phone.invites[0].body)[1])
        await lab.hass.loop.sock_sendto(phone.rtp_socket, rtp.build_packet(rtp.RtpPacket(
            phone.payload_type, 0, 0, 9876, struct.pack("!320h", *([2100] * 320)))), ("127.0.0.1", port))
        await asyncio.wait_for(first.wait(), 2)
        response = asyncio.Queue()
        receive = phone.datagram_received
        def messages(raw, addr):
            msg = sip.parse_message(raw)
            if msg.is_response and msg.header("CSeq") == "2 INVITE":
                response.put_nowait(msg)
            else:
                receive(raw, addr)
        monkeypatch.setattr(phone, "datagram_received", messages)
        original = phone.invites[0]
        used_payloads = {int(value) for value in re.findall(rb"a=rtpmap:(\d+)", original.body)}
        new_payload = next(value for value in range(96, 128) if value not in used_payloads)
        local_port = phone.transport.get_extra_info("sockname")[1]
        target = str(sip.parse_sip_uri(original.header("Contact")))
        headers = [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch=z9hG4bK-mailbox-update"),
            ("From", original.header("To") + ";tag=bedroom"), ("To", original.header("From")),
            ("Call-ID", result["call_id"]), ("CSeq", "2 INVITE"),
            ("Contact", f"<sip:bedroom@127.0.0.1:{local_port}>"), ("Content-Type", "application/sdp"),
        ]
        body = ("v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=Phone\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio {phone.rtp_socket.getsockname()[1]} RTP/AVP {new_payload}\r\n"
                f"a=rtpmap:{new_payload} L16/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode()
        from custom_components.voip_stack.core import sdp
        client = lab.calls.sip_client_for(result["call_id"])
        selected = sdp.negotiate_directional(body, client.supported_send_formats, client.supported_recv_formats)
        assert selected is not None, (client.supported_send_formats, client.supported_recv_formats)
        phone.transport.sendto(sip.build_request("INVITE", target, headers, body), phone.remote)
        async with asyncio.timeout(2):
            while True:
                answer = await response.get()
                if answer.status_code >= 200:
                    break
        assert answer.status_code == 200
        ack_headers = [(key, "2 ACK" if key == "CSeq" else value.replace("mailbox-update", "mailbox-ack") if key == "Via" else value)
                       for key, value in headers if key != "Content-Type"]
        phone.transport.sendto(sip.build_request("ACK", target, ack_headers), phone.remote)
        await lab.hass.loop.sock_sendto(phone.rtp_socket, rtp.build_packet(rtp.RtpPacket(
            new_payload, 1, 160, 9876, struct.pack("!160h", *([3100] * 160)))), ("127.0.0.1", port))
        await asyncio.wait_for(second.wait(), 2)
        detach()
        phone.remote_hangup(cseq=3)
        await asyncio.wait_for(saved.wait(), 3)
        archive = await get_archive(lab.hass)
        entry = archive.list("kitchen")[0]
        assert [item["sample_rate"] for item in entry["segments"]] == [16000]
        with wave.open(str(archive.segment_path(entry["id"], "kitchen")), "rb") as wav:
            samples = struct.unpack(f"<{wav.getnframes()}h", wav.readframes(wav.getnframes()))
        assert sum(value == 2100 for value in samples) >= 300
        assert sum(3050 <= value <= 3150 for value in samples) >= 250
        assert len(list(archive.root.glob("*.wav"))) == 1
    finally:
        unsub()
        unsub_saved()


@pytest.mark.parametrize("kind", ["browser", "sip_account"])
async def test_native_phone_flow_can_enable_mailbox(mailbox_lab, custom_integrations, kind):
    lab = mailbox_lab
    entry = config_entry(lab.hass)
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    form = await lab.hass.config_entries.subentries.async_init(
        (entry.entry_id, "phone"), context={"source": "user"},
    )
    form = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], {"kind": kind})
    values = {"name": "Study", "extension": "202", "voicemail_enabled": True}
    if kind == "sip_account":
        values.update(username="study", password="test-only")
        invalid = await lab.hass.config_entries.subentries.async_configure(
            form["flow_id"], form["data_schema"]({**values, "username": "invalid user"}),
        )
        assert invalid["type"] == "form"
        assert invalid["errors"] == {"username": "phone_username_invalid"}
        assert not any(item.title == "Study" for item in entry.subentries.values())
    result = await lab.hass.config_entries.subentries.async_configure(form["flow_id"], form["data_schema"](values))
    assert result["type"] == "create_entry"
    created = next(item for item in entry.subentries.values() if item.title == "Study")
    assert created.data["voicemail"] == {"enabled": True}


async def test_phone_rename_preserves_private_mailbox_settings(mailbox_lab, custom_integrations):
    lab = mailbox_lab
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    child = next(item for item in entry.subentries.values() if item.data.get("endpoint_id") == "kitchen")
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    form = await lab.hass.config_entries.subentries.async_init(
        (entry.entry_id, "phone"), context={"source": "reconfigure", "subentry_id": child.subentry_id},
    )
    result = await lab.hass.config_entries.subentries.async_configure(
        form["flow_id"], {**form["data_schema"]({}), "name": "New kitchen"},
    )
    assert result["type"] == "abort"
    updated = entry.subentries[child.subentry_id]
    assert updated.data["name"] == "New kitchen"
    assert updated.data["voicemail"] == {"enabled": True, "max_seconds": 2}
    assert updated.data["endpoint_id"] == "kitchen"




async def test_no_answer_routes_browser_into_mailbox_without_second_sip_call(browser_lab, tmp_path, monkeypatch):
    import socket
    from unittest.mock import AsyncMock
    from custom_components import voip_stack
    from custom_components.voip_stack.phone_config import _add_phone_subentry
    from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
    from custom_components.voip_stack.endpoint_session import TerminationIntent

    lab = browser_lab
    lab.hass.config.config_dir = str(tmp_path)
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title="Kitchen", unique_id="phone:kitchen",
        data={"endpoint_id": "kitchen", "name": "Kitchen", "kind": "browser",
              "voicemail": {"enabled": True, "max_seconds": 10}},
    ))
    setup_phone_policy_events(lab.hass, entry)
    lab.phones[0].auto_answer = False
    for identity in ("browser", "sip:bedroom0", "sip:bedroom1"):
        endpoint = lab.runtime.endpoints.require(identity)
        _add_phone_subentry(lab.hass, entry, title=endpoint.name, data={
            "endpoint_id": endpoint.endpoint_id, "name": endpoint.name,
            "kind": endpoint.kind.value, "username": endpoint.username,
        })
    monkeypatch.setattr(voip_stack, "_refresh_and_push_phonebook", AsyncMock())
    await lab.hass.services.async_call("voip_stack", "set_phone_settings", {
        "device_id": "device-bedroom0", "ringing_timeout": 1,
        "no_answer_action": "voicemail", "no_answer_to": "kitchen",
    }, blocking=True)
    recording = asyncio.Event()
    projections = []
    connection = object()

    @callback
    def state(event):
        projections.append(event.data)
        if event.data.get("event_type") == "answered":
            application = lab.calls.resource_for(event.data["call_id"], "voicemail")
            if application is not None:
                application.playback.attach(connection)
                application.playback.ready(connection)
        if event.data.get("event_type") == "voicemail_recording":
            recording.set()

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, state)
    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.setblocking(False)
    try:
        session = await originate(lab)
        token = session.token
        await asyncio.wait_for(recording.wait(), 4)
        app = lab.calls.resource_for(session.call_id, "voicemail")
        assert session.token == token and session.owner == "voicemail"
        assert len(lab.phones[0].invites) == 1 and not lab.phones[1].invites
        assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
        assert not lab.runtime.endpoints.require("sip:bedroom0").active_call_id
        assert lab.runtime.endpoints.require("browser").active_call_id == session.call_id
        received = asyncio.Event()
        detach = app.media.decoder.add_pcm_observer(lambda *_args: received.set())
        source.bind(("127.0.0.1", app.invite.remote_rtp_port))
        fmt = app.invite.recv_format
        frames = fmt.sample_rate * fmt.frame_ms // 1000
        packet = rtp.build_packet(rtp.RtpPacket(fmt.payload_type, 0, 0, 567,
                                               struct.pack(f"!{frames}h", *([1800] * frames))))
        await lab.hass.loop.sock_sendto(source, packet, ("127.0.0.1", app.media.local_rtp_port))
        await asyncio.wait_for(received.wait(), 2)
        detach()
        await lab.calls.request_termination(session.call_id, TerminationIntent("test_hangup"))
        archive = await get_archive(lab.hass)
        message = archive.list("kitchen")[0]
        with wave.open(str(archive.segment_path(message["id"], "kitchen")), "rb") as wav:
            assert wav.readframes(frames) == struct.pack(f"<{frames}h", *([1800] * frames))
        assert message["caller"] == "browser"
        assert not lab.calls.sessions
    finally:
        source.close()
        unsubscribe()




@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_sip_phone_deposits_through_real_listener_without_browser(trunks, tmp_path):
    import socket
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

    lab = trunks
    lab.hass.config.config_dir = str(tmp_path)
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title="Kitchen", unique_id="phone:kitchen",
        data={"endpoint_id": "kitchen", "name": "Kitchen", "kind": "browser",
              "voicemail": {"enabled": True, "max_seconds": 10}},
    ))
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="kitchen", name="Kitchen", kind=EndpointKind.BROWSER))
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.2", local_name="Caller", local_uri_user="caller",
                           local_sip_port=5060, local_rtp_port=audio.getsockname()[1],
                           supported_formats=[AudioFormat(sample_rate=16000, frame_ms=20)])
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0),
    )
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    recording = asyncio.Event()

    @callback
    def state(event):
        if event.data.get("event_type") == "voicemail_recording":
            recording.set()

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, state)
    try:
        result = await client.invite(target="voicemail.kitchen", remote_host="127.0.0.1",
                                     remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if result == "ringing":
            result = await client.wait_for_final(timeout=3)
        assert result == "in_call"
        await asyncio.wait_for(recording.wait(), 3)
        session = lab.calls.get_session(client.dialog_ids.call_id)
        app = lab.calls.resource_for(session.call_id, "voicemail")
        assert app is not None and app.playback is None and app.local_source is None
        assert not any(phone.invites for phone in lab.phones)
        received = asyncio.Event()
        detach = app.media.decoder.add_pcm_observer(lambda *_args: received.set())
        dialog = client.dialog
        packet = rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type, 0, 0, 918,
                                               struct.pack("!320h", *([2100] * 320))))
        await lab.hass.loop.sock_sendto(audio, packet, (dialog.remote_rtp_host, dialog.remote_rtp_port))
        await asyncio.wait_for(received.wait(), 2)
        detach()
        assert client.bye()
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        archive = await get_archive(lab.hass)
        message = archive.list("kitchen")[0]
        with wave.open(str(archive.segment_path(message["id"], "kitchen")), "rb") as wav:
            assert wav.readframes(320) == struct.pack("<320h", *([2100] * 320))
        assert not lab.calls.sessions
    finally:
        unsubscribe()
        await client.close()
        audio.close()


async def test_mailbox_settings_reject_contact_collision_before_persisting(mailbox_lab):
    from homeassistant.exceptions import ServiceValidationError
    from custom_components.voip_stack.mailbox import mailbox_settings

    lab = mailbox_lab
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    await lab.hass.services.async_call("voip_stack", "set_voicemail_settings", {
        "device_id": "mailbox-device", "enabled": False,
    }, blocking=True)
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="contact", title="Existing", unique_id="contact:existing",
        data={"id": "existing", "name": "voicemail.Kitchen", "number": "12345"},
    ))
    with pytest.raises(ServiceValidationError, match="conflicts"):
        await lab.hass.services.async_call("voip_stack", "set_voicemail_settings", {
            "device_id": "mailbox-device", "enabled": True,
        }, blocking=True)
    assert mailbox_settings(lab.hass, "kitchen") == {}


async def test_enabled_mailbox_reserves_contact_aliases_and_can_be_reconfigured(mailbox_lab):
    from custom_components.voip_stack.mailbox import mailbox_settings, resolve_mailbox_route
    from custom_components.voip_stack.phonebook_services import _validate_contact_namespace
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.router import RouteAction

    lab = mailbox_lab
    await lab.hass.services.async_call("voip_stack", "set_voicemail_settings", {
        "device_id": "mailbox-device", "max_seconds": 60,
    }, blocking=True)
    assert mailbox_settings(lab.hass, "kitchen")["max_seconds"] == 60
    with pytest.raises(ValueError, match="conflicts"):
        _validate_contact_namespace(lab.hass, [RosterEntry("collision", name="voicemail.Kitchen")])
    assert resolve_mailbox_route(lab.hass, "voicemail.Kitchen", []).action is RouteAction.VOICEMAIL
    assert resolve_mailbox_route(lab.hass, "voicemail.Missing", []).action is RouteAction.REJECT
    assert resolve_mailbox_route(lab.hass, "sip:voicemail.Kitchen@example.com", []) is None
    assert resolve_mailbox_route(lab.hass, "voicemail.Kitchen@example.com", []) is None



@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("destination,mailbox_id,peer_name", [
    ("voicemail.kitchen", "kitchen", "Kitchen"),
    ("*97", "browser", "Voicemail: Browser"),
    ("mailbox.browser", "browser", "Voicemail: Browser"),
])
async def test_browser_dials_same_mailbox_destination_as_sip_phone(
    trunks, tmp_path, monkeypatch, destination, mailbox_id, peer_name,
):
    from unittest.mock import AsyncMock
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.roster import dump_roster_json
    from custom_components.voip_stack.endpoint_session import TerminationIntent

    lab = trunks
    # This case exercises a local application, not the two providers sharing
    # loopback in the trunk fixture. Remove their source classification first.
    from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {}
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    lab.hass.config.config_dir = str(tmp_path)
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title="Kitchen", unique_id="phone:kitchen",
        data={"endpoint_id": "kitchen", "name": "Kitchen", "kind": "browser",
              "voicemail": {"enabled": True, "max_seconds": 10}},
    ))
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="kitchen", name="Kitchen", kind=EndpointKind.BROWSER))
    source = PhoneEndpoint(endpoint_id="browser", name="Browser", kind=EndpointKind.BROWSER,
                          device_id="browser-device", availability="available")
    lab.runtime.endpoints.register(source)
    if mailbox_id == source.endpoint_id:
        from homeassistant.components import tts
        from custom_components.voip_stack.core.audio_format import AudioFormat

        lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type="phone", title=source.name, unique_id="phone:browser",
            data={"endpoint_id": source.endpoint_id, "name": source.name, "kind": "browser",
                  "voicemail": {"enabled": True}},
        ))
        archive = await get_archive(lab.hass)
        writer = await archive.begin(source.endpoint_id, caller="Recorded caller")
        assert writer.offer(bytes(6400), AudioFormat(16000, frame_ms=20))
        await writer.finish()

        class Speech:
            def async_set_message(self, _message):
                pass

            async def async_stream_result(self):
                yield bytes(640)

        monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Speech())
    lab.hass.states.async_set("sensor.voip_phonebook", "2", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    recording = asyncio.Event()
    sessions = []
    phone_updates = []
    connected_identity = asyncio.Event()

    @callback
    def state(event):
        if event.data.get("endpoint_id") == source.endpoint_id:
            phone_updates.append(event.data)
            if event.data.get("last_sip_event") == "UPDATE":
                connected_identity.set()
        if event.data.get("event_type") == "answered":
            application = lab.calls.resource_for(event.data["call_id"], "voicemail")
            if application is not None and application.playback is not None:
                application.playback.attach(recording)
                application.playback.ready(recording)
        if event.data.get("event_type") in {"voicemail_recording", "voicemail_menu_ready"}:
            sessions.append(event.data["call_id"])
            recording.set()

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, state)
    try:
        await softphone_originate.async_originate_browser_call(
            ServiceCall(lab.hass, "voip_stack", "call", {
                "device_id": source.device_id, "destination": destination,
                "media_client_id": "browser-mailbox-caller",
            }), endpoint_id=source.endpoint_id, browser_endpoint=source,
        )
        await asyncio.wait_for(recording.wait(), 3)
        assert not any(phone.invites for phone in lab.phones)
        application = lab.calls.resource_for(sessions[0], "voicemail")
        assert application is not None and application.mailbox_id == mailbox_id
        # A real browser attaches audio only while its own participant is
        # connected. The playback adapter above must not hide stale calling
        # state after the mailbox acquires the application owner.
        participant = application.session.legs[f"browser:{source.endpoint_id}"]
        assert participant.state == "in_call"
        assert lab.runtime.softphones[source.endpoint_id]["state"] == "in_call"
        assert phone_updates[-1]["state"] == "in_call"
        await asyncio.wait_for(connected_identity.wait(), 3)
        assert application.session.callee == peer_name
        assert application.contact.display_name == peer_name
        assert lab.runtime.softphones[source.endpoint_id]["peer_name"] == peer_name
        from custom_components.voip_stack.websocket_api import _ha_softphone_state
        assert _ha_softphone_state(lab.hass, source.endpoint_id)["peer_name"] == peer_name
        for session in tuple(lab.calls.sessions.values()):
            if session.live:
                await lab.calls.request_termination(session.call_id, TerminationIntent("test_hangup"))
        assert not lab.calls.sessions
    finally:
        unsubscribe()


@pytest.mark.parametrize("conflict", [None, "*97", "mailbox.other", "*60101"])
@pytest.mark.parametrize("provider_default", ["en", "ar_JO"])
async def test_global_voicemail_configuration_requires_tts_and_language(mailbox_lab, custom_integrations, monkeypatch, conflict, provider_default):
    from types import SimpleNamespace
    from custom_components.voip_stack import voicemail_config as config_module

    lab = mailbox_lab
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={})
    if conflict:
        lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type="contact", title=conflict, unique_id="contact:feature-conflict",
            data={"id": "feature-conflict", "name": conflict, "number": "12345"},
        ))
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    monkeypatch.setattr(config_module, "voice_engine", lambda *_args: None)
    form = await lab.hass.config_entries.flow.async_init(
        "voip_stack", context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    form = await lab.hass.config_entries.flow.async_configure(
        form["flow_id"], {"next_step_id": "voicemail"},
    )
    assert form["step_id"] == "voicemail" and not entry.data.get("voicemail")
    form = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {"tts_entity_id": "tts.missing"})
    assert form["errors"] == {"tts_entity_id": "voicemail_tts_unavailable"}
    monkeypatch.setattr(config_module, "voice_engine", lambda _hass, identity:
                        SimpleNamespace(available=True, supported_languages=["ar_JO", "en", "it"], default_language=provider_default)
                        if identity == "tts.test" else None)
    form = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {"tts_entity_id": "tts.test"})
    assert form["step_id"] == "voicemail_language"
    assert form["data_schema"]({})["language"] in {"en", "it"}
    from homeassistant.data_entry_flow import InvalidData
    with pytest.raises(InvalidData):
        await lab.hass.config_entries.flow.async_configure(form["flow_id"], {"language": "xx"})
    assert not entry.data.get("voicemail")
    result = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {"language": "it"})
    if conflict:
        assert result["errors"] == {"base": "voicemail_route_conflict"}
        assert not entry.data.get("voicemail")
        contact = next(item for item in entry.subentries.values() if item.unique_id == "contact:feature-conflict")
        lab.hass.config_entries.async_remove_subentry(entry, contact.subentry_id)
        result = await lab.hass.config_entries.flow.async_configure(result["flow_id"], {"language": "it"})
    assert result["type"] == "abort"
    assert entry.data["voicemail"] == {"tts_entity_id": "tts.test", "language": "it"}


@pytest.mark.parametrize("language,expected", [
    ("it", "nove, nove, zero, uno"), ("en", "nine, nine, zero, one"),
    ("de", "neun, neun, null, eins"), ("pt-BR", "nove, nove, zero, um"),
])
def test_voicemail_speaks_phone_digits_without_changing_named_callers(language, expected):
    from custom_components.voip_stack.voicemail_prompts import phrase

    assert expected in phrase(language, "message", number=1, caller="9901")
    assert "Reception" in phrase(language, "message", number=1, caller="Reception")
    assert "9901" not in phrase(language, "message", number=1, caller="9901")
    assert phrase(language, "count", count=1) != phrase(language, "count", count=2)


def test_voicemail_preserves_leading_zeroes_and_phone_number_prefixes():
    from custom_components.voip_stack.voicemail_prompts import spoken_caller

    assert spoken_caller("it", "00101") == "zero, zero, uno, zero, uno"
    assert spoken_caller("en", "+39 (055) 10-20") == "plus, three, nine, zero, five, five, one, zero, two, zero"
    assert spoken_caller("it", "") == "numero sconosciuto"


async def test_voicemail_voice_selection_uses_ha_catalog_and_persists(mailbox_lab, custom_integrations, monkeypatch):
    from types import SimpleNamespace
    from custom_components.voip_stack import voicemail_config as config_module

    lab = mailbox_lab
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={})
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    engine = SimpleNamespace(available=True, supported_languages=["it"], default_language="it",
        async_get_supported_voices=lambda language: [SimpleNamespace(voice_id="raffa_reference", name="Raffa")])
    monkeypatch.setattr(config_module, "voice_engine", lambda *_args: engine)
    form = await lab.hass.config_entries.flow.async_init(
        "voip_stack", context={"source": "reconfigure", "entry_id": entry.entry_id})
    form = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {"next_step_id": "voicemail"})
    form = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {"tts_entity_id": "tts.test"})
    form = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {"language": "it"})
    assert form["step_id"] == "voicemail_voice" and not entry.data.get("voicemail")
    result = await lab.hass.config_entries.flow.async_configure(form["flow_id"], {"voice": "raffa_reference"})
    assert result["type"] == "abort"
    assert entry.data["voicemail"] == {
        "tts_entity_id": "tts.test", "language": "it", "voice": "raffa_reference"}


async def test_voicemail_menu_passes_selected_voice_to_native_tts(mailbox_lab, monkeypatch):
    from types import SimpleNamespace
    from homeassistant.components import tts
    from custom_components.voip_stack.voicemail_menu import VoicemailMenu
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.local_call_media import LocalCallMedia

    calls = []
    class Speech:
        def async_set_message(self, text):
            calls[-1]["text"] = text
        async def async_stream_result(self):
            yield bytes(640)
    def create(hass, identity, language, options):
        calls.append({"entity": identity, "language": language, "options": options})
        return Speech()
    async def play(stream):
        async for pcm in stream:
            assert pcm == bytes(640)
        return True
    monkeypatch.setattr(tts, "async_create_stream", create)
    menu = VoicemailMenu(mailbox_lab.hass, None, None, RosterEntry("Test"), "127.0.0.1",
        settings={"tts_entity_id": "tts.test", "language": "it", "voice": "raffa_reference"})
    menu.media = SimpleNamespace(_tts_audio_output=LocalCallMedia._tts_audio_output)
    menu.ivr = SimpleNamespace(digits=asyncio.Queue(), play=play)
    assert await menu.say("message", number=1, caller="9901")
    assert calls[0]["options"]["voice"] == "raffa_reference"
    assert calls[0]["text"] == "Messaggio 1, ricevuto da nove, nove, zero, uno."


@pytest.mark.parametrize("address", ["*97", "*98", "mailbox", "mailbox.Other", "voicemail.Other", "*60101"])
async def test_mailbox_addresses_are_reserved_but_do_not_steal_existing_routes(mailbox_lab, address):
    from custom_components.voip_stack.mailbox import resolve_mailbox_route
    from custom_components.voip_stack.phonebook_services import _validate_contact_namespace
    from custom_components.voip_stack.roster import RosterEntry

    lab = mailbox_lab
    contact = RosterEntry("existing", name=address, number="12345")
    with pytest.raises(ValueError, match="conflicts"):
        _validate_contact_namespace(lab.hass, [contact])
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {"enabled": False}})
    assert resolve_mailbox_route(lab.hass, address, [contact]) is None
    with pytest.raises(ValueError, match="conflicts"):
        _validate_contact_namespace(lab.hass, [contact])


async def test_feature_code_does_not_shadow_plain_numeric_extension(mailbox_lab):
    from custom_components.voip_stack.config_validation import route_namespace_conflicts
    from custom_components.voip_stack.mailbox import resolve_mailbox_route
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.router import RouteAction

    lab = mailbox_lab
    ordinary = RosterEntry("Desk", extension="97")
    assert resolve_mailbox_route(lab.hass, "*97", [ordinary], source_endpoint_id="kitchen").action is RouteAction.VOICEMAIL
    assert resolve_mailbox_route(lab.hass, "97", [ordinary]) is None
    assert not route_namespace_conflicts(candidate_routes=("60101",), existing=(
        {"id": "kitchen", "extension": "101", "voicemail": {"enabled": True}},
        {"mailbox_feature_routes": True},
    ))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("digits_transport", ["info", "rtp"])
@pytest.mark.parametrize("other_mailbox", [False, True])
async def test_standard_phone_own_mailbox_play_delete_and_hangup(trunks, tmp_path, monkeypatch, caplog, digits_transport, other_mailbox):
    import socket
    from homeassistant.components import tts
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
    from custom_components.voip_stack.roster import RosterEntry

    from custom_components.voip_stack.mailbox import _pin_hash
    pin = "819273"
    lab = trunks
    lab.hass.config.config_dir = str(tmp_path)
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={"voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title="Source", unique_id="phone:source",
        data={"endpoint_id": "source", "name": "Source", "kind": "sip_account",
              "username": "source", "extension": "101", "voicemail": {"enabled": True, "pin_hash": _pin_hash(pin)}},
    ))
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="source", name="Source", username="source",
                                               extension="101", kind=EndpointKind.SIP_ACCOUNT, availability="available"))
    lab.roster.append(RosterEntry("source", address="127.0.0.2", metadata={"endpoint_id": "source"}))
    spoken = []
    class Speech:
        def async_set_message(self, text):
            spoken.append(text)
        async def async_stream_result(self):
            yield bytes(640)
    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Speech())
    archive = await get_archive(lab.hass)
    messages = []
    for caller, value in (("Older", 1100), ("Newer", 2200)):
        writer = await archive.begin("source", caller=caller)
        for _ in range(10):
            assert writer.offer(struct.pack("<3200h", *([value] * 3200)), AudioFormat(16000, frame_ms=20))
        messages.append(await writer.finish())
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.2", local_name="Source", local_uri_user="other" if other_mailbox else "source",
                           local_sip_port=5060, local_rtp_port=audio.getsockname()[1],
                           supported_formats=[AudioFormat(sample_rate=16000, frame_ms=20)])
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0),
    )
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    deleted = asyncio.Event()
    results = []
    @callback
    def operation(event):
        results.append(event.data)
        deleted.set()
    unsubscribe = lab.hass.bus.async_listen("voip_stack_voicemail_operation", operation)
    sequence = 0
    async def press(digits):
        nonlocal sequence
        for digit in digits:
            if digits_transport == "info":
                assert await client.send_dtmf_info(digit)
            else:
                code = "0123456789*#ABCD".index(digit)
                timestamp = sequence * 1600
                for end in (False, True, True):
                    payload = struct.pack("!BBH", code, (0x80 if end else 0) | 10, 800)
                    raw = rtp.build_packet(rtp.RtpPacket(client.dialog.dtmf_payload_type, sequence, timestamp, 1931, payload))
                    sequence += 1
                    await lab.hass.loop.sock_sendto(audio, raw, (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
    try:
        result = await client.invite(target="mailbox.source" if other_mailbox else "*97", remote_host="127.0.0.1",
                                     remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if result == "ringing":
            result = await client.wait_for_final(timeout=3)
        assert result == "in_call"
        session = lab.calls.get_session(client.dialog_ids.call_id)
        if other_mailbox:
            await press("0000#" + pin + "#")
        async with asyncio.timeout(3):
            while True:
                packet = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
                if packet.payload_type == client.dialog.recv_format.payload_type and any(packet.payload):
                    assert set(struct.unpack(f"!{len(packet.payload) // 2}h", packet.payload)) == {2200}
                    break
        if not other_mailbox:
            assert not any("PIN" in text for text in spoken)
            from custom_components.voip_stack.mailbox_api import _call_selections

            selections = await _call_selections(lab.hass, session.call_id, "source")
            assert selections == {messages[1]["id"]: "01#", messages[0]["id"]: "02#"}
            with pytest.raises(ValueError, match="own mailbox"):
                await _call_selections(lab.hass, session.call_id, "another-phone")
            writer = await archive.begin("source", caller="Arrived during playback")
            assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
            added = await writer.finish()
            assert await _call_selections(lab.hass, session.call_id, "source") == selections
            await archive.delete(added["id"], "source")
        assert pin not in caplog.text
        await press("01#2")
        await asyncio.wait_for(deleted.wait(), 3)
        assert results[0]["message_id"] == messages[1]["id"]
        assert [item["id"] for item in archive.list("source")] == [messages[0]["id"]]
        assert session.live
        assert client.bye()
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions
    finally:
        unsubscribe()
        await client.close()
        audio.close()


@pytest.mark.parametrize("remove_mailbox", [False, True])
async def test_mwi_uses_existing_subscription_lifecycle_and_mailbox_counts(mailbox_lab, remove_mailbox):
    import time
    from custom_components.voip_stack.sip_application import SipApplicationMethods
    from custom_components.voip_stack.sip_registrar import SipRegistrar, SipAccount, SipRegistration
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.mailbox import EVENT_VOICEMAIL_CHANGED

    lab = mailbox_lab
    lab.runtime.endpoints.update("kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen")
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("kitchen", "Kitchen", "test-only")],
                             local_ip="127.0.0.1", local_sip_port=5060)
    registrar.registrations["kitchen"] = SipRegistration(
        username="kitchen", contact_uri="sip:kitchen@127.0.0.2:5060", source_host="127.0.0.2",
        source_port=5060, transport="UDP", expires_at=time.time() + 300,
    )
    methods = SipApplicationMethods(lab.hass, registrar)
    notifications = asyncio.Queue()
    async def send(request, address, result):
        await notifications.put(result.follow_up)
        return sip.SipMessage(status_code=200, reason="OK")
    def request(target="kitchen", *, tag="", expires=300, cseq=1):
        return sip.parse_message(sip.build_request("SUBSCRIBE", f"sip:{target}@127.0.0.1:5060", [
            ("From", "<sip:kitchen@127.0.0.1>;tag=caller"),
            ("To", f"<sip:{target}@127.0.0.1>" + (f";tag={tag}" if tag else "")),
            ("Contact", "<sip:kitchen@127.0.0.2:5060>"), ("Call-ID", "mailbox-mwi"),
            ("CSeq", f"{cseq} SUBSCRIBE"), ("Event", "message-summary"), ("Expires", str(expires)),
        ]))
    try:
        denied = await methods.handle(request(), ("127.0.0.3", 5060), "UDP", send)
        assert denied.status == 403
        denied = await methods.handle(request("other"), ("127.0.0.2", 5060), "UDP", send)
        assert denied.status == 403
        first = await methods.handle(request(), ("127.0.0.2", 5060), "UDP", send)
        assert first.status == 200 and first.follow_up.content_type == "application/simple-message-summary"
        assert b"Messages-Waiting: no\r\n" in first.follow_up.body
        assert b"Voice-Message: 0/0 (0/0)\r\n" in first.follow_up.body
        assert b"Message-Account: sip:mailbox.kitchen@127.0.0.1:5060\r\n" in first.follow_up.body
        first.follow_up_scheduler()
        initial_notify = await asyncio.wait_for(notifications.get(), 2)
        assert initial_notify.cseq == first.follow_up.cseq
        archive = await get_archive(lab.hass)
        writer = await archive.begin("kitchen", caller="Door")
        assert writer.offer(b"\x01\x00" * 320, AudioFormat(16000, frame_ms=20))
        message = await writer.finish()
        lab.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": "kitchen"})
        created = await asyncio.wait_for(notifications.get(), 2)
        assert b"Messages-Waiting: yes\r\n" in created.body and b"Voice-Message: 1/0" in created.body
        assert created.cseq > first.follow_up.cseq
        await archive.mark_read(message["id"], "kitchen")
        lab.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": "kitchen"})
        read = await asyncio.wait_for(notifications.get(), 2)
        assert b"Messages-Waiting: no\r\n" in read.body and b"Voice-Message: 0/1" in read.body
        await archive.delete(message["id"], "kitchen")
        lab.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": "kitchen"})
        removed = await asyncio.wait_for(notifications.get(), 2)
        assert b"Voice-Message: 0/0" in removed.body
        if remove_mailbox:
            from custom_components.voip_stack.mailbox import async_remove_mailbox

            await async_remove_mailbox(lab.hass, "kitchen")
            ended = await asyncio.wait_for(notifications.get(), 2)
            assert dict(ended.headers)["Subscription-State"] == "terminated;reason=noresource"
            assert ended.cseq > removed.cseq
        else:
            ended = await methods.handle(request(tag=first.to_tag, expires=0, cseq=2), ("127.0.0.2", 5060), "UDP", send)
            assert ended.status == 200 and dict(ended.follow_up.headers)["Subscription-State"].startswith("terminated")
        assert not methods.subscriptions and not methods.subscription_timers
    finally:
        await methods.stop()
    assert not methods.tasks and methods.mailbox_unsubscribe is None


async def test_phone_removal_purges_mailbox_but_offline_does_not(mailbox_lab):
    from custom_components.voip_stack.config_entry_runtime import (
        async_config_entry_updated, entry_phone_signature, entry_runtime_signature,
    )
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    entry = config_entry(lab.hass)
    archive = await get_archive(lab.hass)
    writer = await archive.begin("kitchen", caller="Door")
    assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
    await writer.finish()
    lab.runtime.entry_phone_signature = entry_phone_signature(entry)
    lab.runtime.entry_runtime_signature = entry_runtime_signature(entry)
    lab.runtime.endpoints.update("kitchen", availability="offline")
    await async_config_entry_updated(lab.hass, entry)
    assert len(archive.list("kitchen")) == 1
    child = next(item for item in entry.subentries.values() if item.data.get("endpoint_id") == "kitchen")
    lab.hass.config_entries.async_remove_subentry(entry, child.subentry_id)
    await async_config_entry_updated(lab.hass, entry)
    assert not archive.list("kitchen")
    await archive.load()
    assert not archive.list("kitchen")


async def test_removing_phone_without_voicemail_needs_no_media_directory(lab):
    from custom_components.voip_stack.mailbox import async_remove_mailbox

    lab.hass.config.media_dirs = {}
    assert lab.calls.component("media_archive") is None
    await async_remove_mailbox(lab.hass, "sip:bedroom0")
    assert lab.calls.component("media_archive") is None


async def test_cancelled_removal_waits_for_message_deletion(mailbox_lab):
    from custom_components.voip_stack.mailbox import async_remove_mailbox
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    archive = await get_archive(lab.hass)
    writer = await archive.begin("kitchen")
    assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
    message = await writer.finish()
    path = archive.segment_path(message["id"], "kitchen")
    written, release = asyncio.Event(), asyncio.Event()
    original_io = archive.run_io

    async def gated(function, *args):
        result = await original_io(function, *args)
        if function.__name__ == "remove":
            written.set()
            await release.wait()
        return result

    archive.run_io = gated
    task = asyncio.create_task(async_remove_mailbox(lab.hass, "kitchen"))
    try:
        await asyncio.wait_for(written.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not path.exists() and not archive.list("kitchen")
        assert not archive._active and archive._reserved == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_removing_integration_deletes_its_mailboxes_after_shutdown(mailbox_lab):
    from custom_components import voip_stack
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    archive = await get_archive(lab.hass)
    paths = {}
    for kind in ("voicemail", "recording"):
        writer = await archive.begin("kitchen", kind=kind)
        assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
        message = await writer.finish()
        paths[kind] = archive.segment_path(message["id"], "kitchen")
    await voip_stack.async_remove_entry(lab.hass, config_entry(lab.hass))
    assert lab.calls.phase.value == "stopped"
    assert not paths["voicemail"].exists()
    assert paths["recording"].exists()
    await archive.load()
    assert not archive.list("kitchen")


async def test_permanent_esphome_device_removal_purges_mailbox(mailbox_lab):
    from homeassistant.helpers import device_registry as dr
    from custom_components.voip_stack.mailbox import async_setup_mailboxes, EVENT_VOICEMAIL_CHANGED
    from custom_components.voip_stack.core.audio_format import AudioFormat

    lab = mailbox_lab
    entry = config_entry(lab.hass)
    devices = dr.async_get(lab.hass)
    device = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("esphome", "test-esp")})
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="esp-test", name="ESP", kind=EndpointKind.ESPHOME,
                                               device_id=device.id, availability="offline"))
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
        "esphome_mailboxes": {"esp-test": {"enabled": True, "pin_hash": "test-placeholder"}}})
    await async_setup_mailboxes(lab.hass, entry)
    archive = await get_archive(lab.hass)
    writer = await archive.begin("esp-test")
    assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
    await writer.finish()
    deleted = asyncio.Event()
    @callback
    def changed(event):
        if event.data.get("endpoint_id") == "esp-test" and event.data.get("removed"):
            deleted.set()
    remove = lab.hass.bus.async_listen(EVENT_VOICEMAIL_CHANGED, changed)
    try:
        devices.async_remove_device(device.id)
        await asyncio.wait_for(deleted.wait(), 2)
        assert not archive.list("esp-test")
        assert "esp-test" not in entry.data["esphome_mailboxes"]
    finally:
        remove()


@pytest.mark.parametrize("via_flow", [False, True])
async def test_disabling_mailbox_deletes_only_its_messages_and_recreates_empty(
    mailbox_lab, custom_integrations, via_flow,
):
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.mailbox import mailbox_settings

    lab = mailbox_lab
    archive = await get_archive(lab.hass)
    paths = {}
    for mailbox, kind in (("kitchen", "voicemail"), ("other", "voicemail"), ("kitchen", "recording")):
        writer = await archive.begin(mailbox, kind=kind)
        assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
        message = await writer.finish()
        paths[mailbox, kind] = archive.segment_path(message["id"], mailbox)
    if via_flow:
        entry = config_entry(lab.hass)
        child = next(item for item in entry.subentries.values() if item.data.get("endpoint_id") == "kitchen")
        lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
        form = await lab.hass.config_entries.subentries.async_init(
            (entry.entry_id, "phone"), context={"source": "reconfigure", "subentry_id": child.subentry_id},
        )
        result = await lab.hass.config_entries.subentries.async_configure(
            form["flow_id"], {**form["data_schema"]({}), "voicemail_enabled": False},
        )
        assert result["type"] == "abort"
    else:
        await lab.hass.services.async_call("voip_stack", "set_voicemail_settings", {
            "device_id": "mailbox-device", "enabled": False,
        }, blocking=True)
    assert mailbox_settings(lab.hass, "kitchen") == {}
    assert archive.counts("kitchen") == (0, 0)
    assert not paths["kitchen", "voicemail"].exists()
    assert paths["other", "voicemail"].exists()
    assert paths["kitchen", "recording"].exists()
    await archive.load()
    assert archive.counts("kitchen") == (0, 0)
    await lab.hass.services.async_call("voip_stack", "set_voicemail_settings", {
        "device_id": "mailbox-device", "enabled": True,
    }, blocking=True)
    assert mailbox_settings(lab.hass, "kitchen") == {"enabled": True}
    assert archive.counts("kitchen") == (0, 0)


async def test_mailbox_delete_storage_failure_is_retryable(mailbox_lab):
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.mailbox import mailbox_settings
    from homeassistant.exceptions import ServiceValidationError

    lab = mailbox_lab
    archive = await get_archive(lab.hass)
    writer = await archive.begin("kitchen")
    assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
    message = await writer.finish()
    path = archive.segment_path(message["id"], "kitchen")
    original_io = archive.run_io

    async def unavailable(function, *args):
        if function.__name__ == "remove":
            raise PermissionError("Media directory unavailable")
        return await original_io(function, *args)

    archive.run_io = unavailable
    data = {"device_id": "mailbox-device", "enabled": False}
    try:
        with pytest.raises(ServiceValidationError, match="could not be deleted"):
            await lab.hass.services.async_call("voip_stack", "set_voicemail_settings", data, blocking=True)
        assert mailbox_settings(lab.hass, "kitchen")["enabled"]
        assert path.exists()
        assert archive.counts("kitchen") == (1, 0)
    finally:
        archive.run_io = original_io
    await lab.hass.services.async_call("voip_stack", "set_voicemail_settings", data, blocking=True)
    assert not path.exists()
    assert mailbox_settings(lab.hass, "kitchen") == {}
    assert archive.counts("kitchen") == (0, 0)
