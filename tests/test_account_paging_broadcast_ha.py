"""Account announcements deliver complete speech to every available adapter."""

import asyncio
from contextlib import aclosing
from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace
import wave

from homeassistant.components import tts
from homeassistant.setup import async_setup_component
import pytest

from custom_components.voip_stack import paging
from custom_components.voip_stack.core import rtp, sip

from .test_account_paging_ha import (
    account_lab as account_lab, _finished, _playback, _staged,
)
from .test_automation_originate_ha import PHONE_PASSWORD, assert_call_resources_released, lab as lab
from .test_paging_ha import announce

pytestmark = pytest.mark.ha


def _ffmpeg_children():
    processes = set()
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if (path / "comm").read_text().strip() != "ffmpeg":
                continue
            parent = next(line.split()[1] for line in (path / "status").read_text().splitlines()
                if line.startswith("PPid:"))
        except FileNotFoundError:
            continue
        if int(parent) == os.getpid():
            processes.add(int(path.name))
    return processes


@pytest.fixture
async def native_speech(account_lab, monkeypatch, tmp_path):
    """Use a real TTS entity, ResultStream cache and installed FFmpeg."""
    hass = account_lab.lab.hass
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, "ffmpeg", {"ffmpeg": {"ffmpeg_bin": "/usr/bin/ffmpeg"}})
    assert await async_setup_component(hass, "tts", {"tts": []})
    baseline_processes = _ffmpeg_children()
    witness = SimpleNamespace(created=[], audio_created=[], messages=[], provider_calls=[], streams=[], readers=[],
        pcm=b"".join(index.to_bytes(2, "little") * 320 for index in range(1, 51)))
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(witness.pcm)

    class ControlledSpeech(tts.TextToSpeechEntity):
        _attr_name = "Native paging speech"
        _attr_default_language = "en"
        _attr_supported_languages = ["en"]

        async def async_get_tts_audio(self, message, language, options):
            witness.provider_calls.append((message, language))
            return "wav", output.getvalue()

    provider = ControlledSpeech()
    provider.entity_id = "tts.paging_native"
    await hass.data[tts.DATA_COMPONENT].async_add_entities([provider])
    original_create = tts.async_create_stream
    original_audio = tts.async_create_audio_stream
    original_message = tts.ResultStream.async_set_message
    original_results = tts.ResultStream.async_stream_result

    def create(*args, **kwargs):
        stream = original_create(*args, **kwargs)
        assert isinstance(stream, tts.ResultStream)
        witness.created.append((args[1:], kwargs))
        witness.streams.append(stream)
        return stream

    def message(stream, value):
        witness.messages.append(value)
        return original_message(stream, value)

    def audio(*args, **kwargs):
        stream = original_audio(*args, **kwargs)
        assert isinstance(stream, tts.ResultStream)
        witness.audio_created.append(stream)
        witness.streams.append(stream)
        return stream

    async def results(stream):
        witness.readers.append(stream.token)
        async with aclosing(original_results(stream)) as chunks:
            async for chunk in chunks:
                yield chunk

    monkeypatch.setattr(tts, "async_create_stream", create)
    monkeypatch.setattr(tts, "async_create_audio_stream", audio)
    monkeypatch.setattr(tts.ResultStream, "async_set_message", message)
    monkeypatch.setattr(tts.ResultStream, "async_stream_result", results)
    yield witness
    for stream in witness.streams:
        stream.delete()
    await asyncio.wait_for(hass.async_block_till_done(wait_background_tasks=True), 3)
    pending = [task.get_name() for task in asyncio.all_tasks() if not task.done()
        and any(name in task.get_name() for name in (
            "tts_load_audio_stream", "tts_ffmpeg_conversion", "tts_load_data_into_cache"))]
    assert not pending, pending
    assert _ffmpeg_children() == baseline_processes


def _sip_pcm(phone):
    """L16 wire samples are big endian; omit only pre-source silence."""
    output = bytearray()
    while not phone.audio.empty():
        raw, _ = phone.audio.get_nowait()
        payload = rtp.parse_packet(raw).payload
        if any(payload):
            output.extend(payload)
    return b"".join(output[index:index + 2][::-1] for index in range(0, len(output), 2))


def _one_synthesis(speech, *, readers=2):
    assert len(speech.created) == 1
    assert speech.messages == ["Wake up"]
    assert speech.provider_calls == [("Wake up", "en")]
    assert len(speech.readers) >= readers
    assert set(speech.readers) == {speech.streams[0].token}


def _partial_delivery(report, *, transport, status):
    """A failed client must remain visible beside a complete survivor."""
    assert report["state"] == "partial", report
    target = report["targets"][0]
    assert target["status"] == "partial", target
    clients = {client["transport"]: client for client in target["clients"]}
    assert set(clients) == {"ha", "sip"}
    assert clients[transport]["status"] == status, clients
    assert clients[transport]["reason"]
    survivor = "sip" if transport == "ha" else "ha"
    assert clients[survivor]["status"] == "completed", clients


async def _register_additional_contact(lab):
    """Authenticate a second real SIP transport as the same logical account."""
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization

    peer = lab.phones[1]
    address = peer.transport.get_extra_info("sockname")
    uri = f"sip:127.0.0.1:{lab.runtime.transport_config['sip_port']}"
    registration_id = "broadcast-second-contact-bedroom0"
    responses = asyncio.Queue()
    previous = peer.datagram_received

    def receive(raw, remote):
        message = sip.parse_message(raw)
        if message.is_response and message.header("Call-ID") == registration_id:
            responses.put_nowait(message)
        else:
            previous(raw, remote)

    peer.datagram_received = receive

    async def exchange(cseq, authorization=""):
        headers = [
            ("Via", f"SIP/2.0/UDP {address[0]}:{address[1]};branch=z9hG4bK-broadcast-{cseq};rport"),
            ("From", "<sip:bedroom0@127.0.0.1>;tag=broadcast-contact"),
            ("To", "<sip:bedroom0@127.0.0.1>"),
            ("Call-ID", registration_id), ("CSeq", f"{cseq} REGISTER"),
            ("Contact", f"<sip:bedroom0@{address[0]}:{address[1]}>"), ("Expires", "120"),
        ]
        if authorization:
            headers.append(("Authorization", authorization))
        peer.transport.sendto(sip.build_request("REGISTER", uri, headers),
            ("127.0.0.1", lab.runtime.transport_config["sip_port"]))
        return await asyncio.wait_for(responses.get(), 2)

    challenge = await exchange(1)
    assert challenge.status_code == 401
    authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
        username="bedroom0", password=PHONE_PASSWORD, method="REGISTER", uri=uri)
    assert (await exchange(2, authorization)).status_code == 200
    assert len(lab.calls.component("registrar").registered_contacts("bedroom0")) == 2


async def test_same_account_ha_and_registered_sip_both_receive_complete_native_tts(account_lab, native_speech,
        monkeypatch):
    from custom_components.voip_stack import group_candidates, ring_group_fork

    account, speech = account_lab, native_speech
    lab = account.lab
    shared_calls = []
    original_prepare = group_candidates.async_prepare_account_candidates
    original_offer = ring_group_fork.build_offer_candidates

    async def prepare(*args, **kwargs):
        shared_calls.append("prepare_account")
        return await original_prepare(*args, **kwargs)

    def offers(*args, **kwargs):
        shared_calls.append("build_offer")
        return original_offer(*args, **kwargs)

    monkeypatch.setattr(group_candidates, "async_prepare_account_candidates", prepare)
    monkeypatch.setattr(ring_group_fork, "build_offer_candidates", offers)
    lab.phones[0].auto_answer = False
    result = await paging.async_announce(announce(lab, targets=["bedroom0"],
        tts_entity_id="tts.paging_native", wait=False, answer_timeout=2))
    worker = None
    try:
        session, _application = await _staged(account)
        browser, worker = await _playback(account, session)
        browser.ready()
        lab.phones[0].answer()
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert report["state"] == "completed", report
        _one_synthesis(speech)
        assert b"".join(browser.frames) == speech.pcm
        assert _sip_pcm(lab.phones[0]) == speech.pcm
        assert shared_calls == ["prepare_account", "build_offer"]
        assert lab.phones[0].bye.is_set() and browser.closed
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_live_ha_and_every_registered_contact_receive_one_complete_synthesis(account_lab, native_speech):
    account, speech = account_lab, native_speech
    lab = account.lab
    await _register_additional_contact(lab)
    for phone in lab.phones:
        phone.auto_answer = False
    result = await paging.async_announce(announce(lab, targets=["bedroom0"],
        tts_entity_id="tts.paging_native", wait=False, answer_timeout=2))
    worker = None
    try:
        session, _application = await _staged(account)
        browser, worker = await _playback(account, session)
        browser.ready()
        for phone in lab.phones:
            await asyncio.wait_for(phone.invited.wait(), 2)
            phone.answer()
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert b"".join(browser.frames) == speech.pcm
        assert [_sip_pcm(phone) for phone in lab.phones] == [speech.pcm, speech.pcm]
        _one_synthesis(speech, readers=3)
        assert report["state"] == "completed", report
        assert all(phone.bye.is_set() for phone in lab.phones) and browser.closed
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("sip_answer", ["late", "never", "late_after_cancel"])
async def test_ready_ha_starts_before_sip_and_late_sip_replays_the_beginning(account_lab, native_speech, sip_answer,
        monkeypatch):
    from custom_components.voip_stack.local_call_media import LocalCallMedia

    account, speech = account_lab, native_speech
    lab = account.lab
    input_sizes = []
    original_play = LocalCallMedia.play_pcm_stream

    async def play(media, chunks, **kwargs):
        async def measured():
            async with aclosing(chunks) as source:
                async for chunk in source:
                    input_sizes.append(len(chunk))
                    yield chunk
        async with aclosing(measured()) as source:
            return await original_play(media, source, **kwargs)

    monkeypatch.setattr(LocalCallMedia, "play_pcm_stream", play)
    lab.phones[0].auto_answer = False
    lab.phones[0].answer_on_cancel = sip_answer == "late_after_cancel"
    result = await paging.async_announce(announce(lab, targets=["bedroom0"],
        tts_entity_id="tts.paging_native", wait=False, answer_timeout=2))
    worker = None
    try:
        session, _application = await _staged(account)
        browser, worker = await _playback(account, session)
        browser.ready()
        # An unanswered Contact must not hold ready HA playback behind a barrier.
        async with asyncio.timeout(1):
            while not browser.frames:
                await asyncio.sleep(.001)
        assert not lab.phones[0].answered.is_set()
        if sip_answer == "late":
            lab.phones[0].answer()
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert b"".join(browser.frames) == speech.pcm
        _one_synthesis(speech, readers=2 if sip_answer == "late" else 1)
        if sip_answer == "late":
            assert _sip_pcm(lab.phones[0]) == speech.pcm
            # A late native cache reader may return the complete cached source
            # in one blob. The production media boundary must remain bounded.
            assert input_sizes and max(input_sizes) <= 4096
            assert all(size % 2 == 0 for size in input_sizes)
            assert sum(input_sizes) == len(speech.pcm)
            assert lab.phones[0].bye.is_set()
            assert report["state"] == "completed", report
        else:
            assert not _sip_pcm(lab.phones[0])
            assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
            _partial_delivery(report, transport="sip", status="skipped")
            if sip_answer == "late_after_cancel":
                assert lab.phones[0].bye.is_set()
                assert any(request.method == "ACK" for request in lab.phones[0].requests)
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_media_pcm_split_at_odd_byte_boundaries_reaches_both_adapters(account_lab, native_speech, monkeypatch):
    from custom_components.voip_stack import announcement_media

    account, lab = account_lab, account_lab.lab
    lab.phones[0].auto_answer = False
    source = SimpleNamespace(starts=0, closed=asyncio.Event())
    expected = native_speech.pcm

    async def decoded(_hass, identity):
        assert identity == "media-source://odd-fragments"
        source.starts += 1
        try:
            for chunk in (expected[:13], expected[13:777], expected[777:]):
                yield chunk
        finally:
            source.closed.set()

    monkeypatch.setattr(announcement_media, "async_audio_stream", decoded)
    result = await paging.async_announce(announce(lab, targets=["bedroom0"], message="", tts_entity_id="",
        media={"media_content_id": "media-source://odd-fragments"}, wait=False, answer_timeout=2))
    worker = None
    try:
        session, _application = await _staged(account)
        browser, worker = await _playback(account, session)
        browser.ready()
        lab.phones[0].answer()
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert b"".join(browser.frames) == expected
        assert _sip_pcm(lab.phones[0]) == expected
        assert report["state"] == "completed", report
        assert source.starts == 1 and source.closed.is_set()
        assert len(native_speech.audio_created) == 1
        assert len(native_speech.readers) >= 2
        assert set(native_speech.readers) == {native_speech.audio_created[0].token}
        assert not native_speech.created and not native_speech.provider_calls
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_browser_disconnect_keeps_sip_receiving_the_complete_source(account_lab, native_speech):
    account, speech = account_lab, native_speech
    lab = account.lab
    lab.phones[0].auto_answer = False
    result = await paging.async_announce(announce(lab, targets=["bedroom0"],
        tts_entity_id="tts.paging_native", wait=False, answer_timeout=2))
    worker = None
    try:
        session, _application = await _staged(account)
        browser, worker = await _playback(account, session)
        browser.ready()
        lab.phones[0].answer()
        async with asyncio.timeout(1):
            while not browser.frames:
                await asyncio.sleep(.001)
        browser.force_close()
        await asyncio.wait_for(worker, 2)
        report = await _finished(account, result)
        assert _sip_pcm(lab.phones[0]) == speech.pcm
        assert lab.phones[0].bye.is_set()
        _one_synthesis(speech)
        _partial_delivery(report, transport="ha", status="failed")
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_sip_disconnect_keeps_browser_receiving_the_complete_source(account_lab, native_speech):
    account, speech = account_lab, native_speech
    lab = account.lab
    lab.phones[0].auto_answer = False
    result = await paging.async_announce(announce(lab, targets=["bedroom0"],
        tts_entity_id="tts.paging_native", wait=False, answer_timeout=2))
    worker = None
    try:
        session, _application = await _staged(account)
        browser, worker = await _playback(account, session)
        browser.ready()
        lab.phones[0].answer()
        async with asyncio.timeout(1):
            while not browser.frames:
                await asyncio.sleep(.001)
        lab.phones[0].remote_hangup()
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert b"".join(browser.frames) == speech.pcm
        _one_synthesis(speech)
        _partial_delivery(report, transport="sip", status="failed")
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_ending_one_account_during_pending_sip_does_not_stop_another_account(account_lab, native_speech):
    account, lab = account_lab, account_lab.lab
    lab.phones[0].auto_answer = False
    result = await paging.async_announce(announce(lab, targets=["bedroom0", "bedroom1"],
        tts_entity_id="tts.paging_native", wait=False, answer_timeout=2))
    worker = None
    try:
        await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
        # The second phone belongs to another account in this witness. Its
        # physical Call-ID must resolve to its own independent recipient child.
        async with asyncio.timeout(2):
            while True:
                identity = lab.runtime.endpoints.require("bedroom0").active_call_id
                child = lab.calls.get_session(identity) if identity else None
                if child is not None and lab.calls.resource_for(child.call_id, "browser_paging") is not None:
                    break
                await asyncio.sleep(.001)
        assert lab.calls.resolve_session_id(lab.phones[0].invites[0].header("Call-ID")) == child.call_id
        browser, worker = await _playback(account, child)
        browser.ready()
        await asyncio.wait_for(lab.phones[1].answered.wait(), 2)
        async with asyncio.timeout(1):
            while not browser.frames:
                await asyncio.sleep(.001)
        other_id = lab.runtime.endpoints.require("bedroom1").active_call_id
        assert other_id != child.call_id
        assert lab.calls.resolve_session_id(lab.phones[1].invites[0].header("Call-ID")) == other_id
        await lab.calls.terminate_call_wait(child.call_id, reason="local_hangup")
        report = await _finished(account, result)
        await asyncio.wait_for(worker, 2)
        assert _sip_pcm(lab.phones[1]) == native_speech.pcm
        _one_synthesis(native_speech)
        assert report["state"] == "partial", report
        targets = {target["endpoint_id"]: target for target in report["targets"]}
        assert targets["bedroom1"]["status"] == "completed", targets
        assert targets["bedroom0"]["status"] != "completed", targets
        assert targets["bedroom0"]["clients"]
        assert all(client["status"] not in {"playing", "pending", "completed"}
            for client in targets["bedroom0"]["clients"])
        assert all(client.get("reason") for client in targets["bedroom0"]["clients"])
        assert browser.closed and lab.phones[1].bye.is_set()
        assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("finish", ["stop", "decoded_byte_limit"])
async def test_native_media_source_stops_owned_input_and_enforces_decoded_pcm_budget(account_lab, native_speech,
        monkeypatch, finish):
    from custom_components.voip_stack import announcement_media
    from homeassistant.core import Context, ServiceCall

    account, lab = account_lab, account_lab.lab
    lab.phones[0].auto_answer = False
    source = SimpleNamespace(starts=0, produced=0, closed=asyncio.Event(), blocked=asyncio.Event())
    chunk = b"\x23\x01" * 2048

    async def decoded(_hass, identity):
        assert identity == "media-source://controlled-broadcast"
        source.starts += 1
        try:
            for _ in range(256):
                if finish == "stop" and source.produced == 16:
                    source.blocked.set()
                    await asyncio.Event().wait()
                source.produced += 1
                yield chunk
        finally:
            source.closed.set()

    monkeypatch.setattr(announcement_media, "async_audio_stream", decoded)
    result = await paging.async_announce(announce(lab, targets=["bedroom0"], message="", tts_entity_id="",
        media={"media_content_id": "media-source://controlled-broadcast"}, wait=False,
        timeout=10 if finish == "stop" else 1, answer_timeout=2))
    worker = None
    try:
        session, _application = await _staged(account)
        browser, worker = await _playback(account, session)
        browser.ready()
        lab.phones[0].answer()
        if finish == "stop":
            await asyncio.wait_for(source.blocked.wait(), 3)
            call = ServiceCall(lab.hass, "voip_stack", "stop_announcement",
                {"announcement_id": result["announcement_id"]}, context=Context())
            report = await asyncio.wait_for(paging.async_stop_announcement(call), 3)
            assert report["state"] == "stopped", report
        else:
            report = await _finished(account, result)
            # Decoders can run much faster than playback. The absolute one
            # second budget permits at most 32000 decoded PCM bytes, plus
            # one input chunk observed while discovering that limit.
            assert source.produced * len(chunk) <= 32000 + len(chunk)
            assert report["state"] != "completed", report
        await asyncio.wait_for(source.closed.wait(), 2)
        await asyncio.wait_for(worker, 2)
        assert source.starts == 1
        assert len(native_speech.audio_created) == 1
        assert not native_speech.created and not native_speech.provider_calls
        assert lab.phones[0].bye.is_set() and browser.closed
        assert_call_resources_released(lab)
    finally:
        await account.owner.close()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
