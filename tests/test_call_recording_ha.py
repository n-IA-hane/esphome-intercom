"""Conversation capture taps real SIP audio without owning or ending the call."""

import asyncio
import math
from pathlib import Path
import wave

import numpy as np
import pytest
from homeassistant.exceptions import ServiceValidationError


from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_browser_phone_policy_ha import browser_lab as browser_lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('ending', ['stop', 'remote', 'limit', 'disk_failure'])
async def test_sip_conversation_recording_keeps_audio_and_owns_one_file(
    registered_phones, trunks, tmp_path, monkeypatch, ending,
):
    from custom_components.voip_stack.mailbox import get_archive
    runtime = trunks
    media = tmp_path / 'media'
    media.mkdir()
    runtime.hass.config.media_dirs = {'local': str(media)}
    runtime.runtime.endpoints.update('transfer_a', device_id='caller-phone')
    runtime.runtime.endpoints.update('transfer_b', device_id='callee-phone')
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    events = asyncio.Queue()
    async def finished(event):
        await events.put(event.data)
    remove = runtime.hass.bus.async_listen('voip_stack.recording_finished', finished)
    await asyncio.to_thread(registered_phones[0].dial, 'transfer_b')
    await wait_for(lambda: any(s.state == 'in_call' for s in runtime.calls.sessions.values()))
    session = next(iter(runtime.calls.sessions.values()))
    relay = runtime.calls.resource_for(session.call_id, 'relay')
    await wait_for(lambda: relay.left_rx_packets > 5 and relay.right_rx_packets > 5)
    previous_route = (relay.left_to_right_passthrough, relay.right_to_left_passthrough)
    first, second = await asyncio.gather(*(
        runtime.hass.services.async_call('voip_stack', 'start_recording',
            {'device_id': 'caller-phone', 'max_seconds': 1 if ending == 'limit' else 30},
            blocking=True, return_response=True) for _ in range(2)
    ), return_exceptions=True)
    successful = [item for item in (first, second) if isinstance(item, dict)]
    assert len(successful) == 1
    assert any(isinstance(item, ServiceValidationError) for item in (first, second))
    response = successful[0]
    recording = runtime.calls.resource_for(session.call_id, 'recording')
    archive = await get_archive(runtime.hass)
    assert len(archive._active) == 1 and archive.counts('transfer_a') == (0, 0)
    assert (relay.left_to_right_passthrough, relay.right_to_left_passthrough) == previous_route
    with pytest.raises(ServiceValidationError, match='no active recording'):
        await runtime.hass.services.async_call('voip_stack', 'stop_recording',
            {'device_id': 'callee-phone'}, blocking=True)
    if ending == 'disk_failure':
        def broken_write(_frames):
            raise OSError('injected disk failure')
        monkeypatch.setattr(recording.writer.disk, 'write', broken_write)
        result = await asyncio.wait_for(events.get(), 5)
        assert result['error'] == 'storage_error'
        assert session.live
        before = relay.left_tx_packets
        await wait_for(lambda: relay.left_tx_packets > before + 20)
        # Failure affects only this writer. A clean retry uses the same call.
        response = await runtime.hass.services.async_call('voip_stack', 'start_recording',
            {'device_id': 'caller-phone'}, blocking=True, return_response=True)
        recording = runtime.calls.resource_for(session.call_id, 'recording')
    before = relay.left_rx_packets
    await wait_for(lambda: relay.left_rx_packets > before + 25 and relay.right_rx_packets > 25)
    if ending == 'remote':
        await asyncio.to_thread(registered_phones[1].hangup)
    elif ending != 'limit':
        stopped = await runtime.hass.services.async_call('voip_stack', 'stop_recording',
            {'device_id': 'caller-phone'}, blocking=True, return_response=True)
        assert stopped['recording_id'] == response['recording_id']
        assert Path(stopped['file_path']).is_file()
    await asyncio.wait_for(asyncio.shield(recording.task), 5)
    event = await asyncio.wait_for(events.get(), 5)
    assert event['recording_id'] == response['recording_id'] and not event['error']
    if ending == 'remote':
        await wait_for(lambda: not runtime.calls.sessions)
    assert runtime.calls.resource_for(session.call_id, 'recording') is None
    with wave.open(event['file_path'], 'rb') as wav:
        rate = wav.getframerate()
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(float)
    spectrum = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
    frequencies = np.fft.rfftfreq(len(pcm), 1 / rate)
    for tone in (450, 550):
        assert spectrum[np.abs(frequencies - tone) < 5].max() > 100_000
    assert len(archive.message(response['recording_id'], 'transfer_a')['segments']) == 1
    assert archive.counts('transfer_a') == (0, 0)
    result = await runtime.hass.services.async_call('voip_stack', 'get_recording',
        {'device_id': 'caller-phone', 'recording_id': response['recording_id']}, blocking=True, return_response=True)
    assert result['file_path'] == event['file_path']
    with pytest.raises(ServiceValidationError):
        await runtime.hass.services.async_call('voip_stack', 'get_recording',
            {'device_id': 'callee-phone', 'recording_id': response['recording_id']}, blocking=True, return_response=True)
    if ending != 'remote':
        assert session.live
        before = relay.right_tx_packets
        await wait_for(lambda: relay.right_tx_packets > before + 20)
        await asyncio.to_thread(registered_phones[0].hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get('used', set()) and not archive._active and archive._reserved == 0
    remove()
    deleted = await runtime.hass.services.async_call('voip_stack', 'delete_recording',
        {'device_id': 'caller-phone', 'recording_id': response['recording_id']}, blocking=True, return_response=True)
    assert deleted['deleted'] and not Path(event['file_path']).exists()
    assert not (archive.root / (response['recording_id'] + '.json')).exists()




@pytest.mark.parametrize('ending', ['stop', 'remote', 'capture_error'])
@pytest.mark.parametrize('maximum', ['default', 'configured_zero', 0])
async def test_local_browser_recording_reuses_frames_and_preserves_call(browser_lab, tmp_path, monkeypatch, ending, maximum):
    import struct
    from custom_components.voip_stack.local_softphone_runtime import start_local_softphone_call
    runtime = browser_lab
    runtime.runtime.endpoints.update("browser", name="Reception", extension="204")
    directory = tmp_path / 'media'
    directory.mkdir()
    runtime.hass.config.media_dirs = {'local': str(directory)}
    call = start_local_softphone_call(runtime.hass, 'browser', 'kitchen', caller_owner_id='first')
    runtime.bridge.answer(call.call_id, 'kitchen', 'second')
    first = runtime.bridge.acquire_media(call.call_id, 'browser', 'first')
    second = runtime.bridge.acquire_media(call.call_id, 'kitchen', 'second')
    if maximum == 'configured_zero':
        from custom_components.voip_stack.store import config_entry
        entry = config_entry(runtime.hass)
        runtime.hass.config_entries.async_update_entry(entry, data={**entry.data,
            'service_timeouts': {**entry.data['service_timeouts'], 'recording_timeout': 0}})
    response = await runtime.hass.services.async_call('voip_stack', 'start_recording',
        {'device_id': 'Reception', **({'max_seconds': maximum} if maximum == 0 else {})},
        blocking=True, return_response=True)
    recording = runtime.calls.resource_for(call.call_id, 'recording')
    assert recording.max_seconds == (3600 if maximum == 'default' else None)
    if ending == 'capture_error':
        def fail_capture(*_args, **_kwargs):
            raise ValueError('injected capture failure')
        monkeypatch.setattr(recording.writer, 'offer', fail_capture)
    left = struct.pack('<1920h', *([1000] * 1920))
    right = struct.pack('<1920h', *([2000] * 1920))
    for _ in range(12):
        runtime.bridge.send_audio(call.call_id, 'browser', first.token, left)
        runtime.bridge.send_audio(call.call_id, 'kitchen', second.token, right)
        assert runtime.bridge.receive_audio_nowait(call.call_id, 'kitchen', second.token) == left
        assert runtime.bridge.receive_audio_nowait(call.call_id, 'browser', first.token) == right
        await asyncio.sleep(.02)
    if ending == 'capture_error':
        await asyncio.wait_for(recording.task, 2)
        assert recording.error == 'capture_error'
        assert runtime.bridge.get_call(call.call_id) is not None
    else:
        if ending == 'remote':
            runtime.bridge.hangup(call.call_id, 'kitchen')
            await asyncio.wait_for(recording.task, 2)
        else:
            stopped = await runtime.hass.services.async_call('voip_stack', 'stop_recording',
                {'device_id': '204'}, blocking=True, return_response=True)
            assert stopped['recording_id'] == response['recording_id']
            assert runtime.bridge.get_call(call.call_id) is not None
        result = await runtime.hass.services.async_call('voip_stack', 'get_recording',
            {'device_id': 'device-browser', 'recording_id': response['recording_id']}, blocking=True, return_response=True)
        with wave.open(result['file_path'], 'rb') as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (48000, 2, 2)
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
        assert np.count_nonzero(pcm == 3000) > 1000
        from custom_components.voip_stack.mailbox import get_archive
        from custom_components.voip_stack.core.media_archive import MediaArchive
        archive = await get_archive(runtime.hass)
        restored = MediaArchive(archive.root, archive.run_io)
        await restored.load()
        assert restored.message(response['recording_id'], 'browser')['status'] == 'complete'
        assert not archive._active and archive._reserved == 0
    if runtime.bridge.get_call(call.call_id) is not None:
        runtime.bridge.hangup(call.call_id, 'browser')


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('binding_failure', [False, True])
async def test_browser_to_sip_recording_captures_both_existing_media_paths(registered_phones, trunks, tmp_path, monkeypatch, binding_failure):
    import struct
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from aiohttp import WSMsgType
    from custom_components.voip_stack import audio_ws_view, audio_ws, softphone_originate
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import dump_roster_json
    runtime = trunks
    directory = tmp_path / 'media'
    directory.mkdir()
    runtime.hass.config.media_dirs = {'local': str(directory)}
    endpoint = PhoneEndpoint(endpoint_id='recording-browser', name='Recording Browser', device_id='recording-device',
        kind=EndpointKind.BROWSER, availability='available', capabilities=frozenset({'audio', 'dtmf'}))
    runtime.runtime.endpoints.register(endpoint)
    runtime.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(runtime.roster)})
    monkeypatch.setattr(softphone_originate, '_ha_advertise_host', AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate, '_resolve_target_device', AsyncMock(return_value=None))
    await runtime.hass.services.async_call('voip_stack', 'call',
        {'device_id': endpoint.device_id, 'destination': 'transfer_b', 'media_client_id': 'recording-owner'}, blocking=True)
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)
    await wait_for(lambda: runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id and
        runtime.calls.get_session(runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id).state == 'in_call')
    media = audio_ws_view._active_softphone_media_session(runtime.hass, endpoint.endpoint_id)
    class Browser:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.ready = asyncio.Event()
        def force_close(self):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            return await self.messages.get()
        async def send_json(self, _value):
            if "tx_format" in _value:
                self.contract = _value
            self.ready.set()
        async def send_bytes(self, _value):
            pass
    browser = Browser()
    # The call-connected automation can run before the card opens its media socket.
    await runtime.hass.services.async_call('voip_stack', 'start_recording',
        {'device_id': endpoint.device_id}, blocking=True)
    if binding_failure:
        from custom_components.voip_stack.call_recording import CallRecording
        original_bind = CallRecording.bind_media
        def fail_bind(_recording, _observe):
            raise ValueError('injected observer binding failure')
        monkeypatch.setattr(CallRecording, 'bind_media', fail_bind)
    worker = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser, media, endpoint_id=endpoint.endpoint_id))
    try:
        await asyncio.wait_for(browser.ready.wait(), 2)
        await wait_for(lambda: media.observe_audio is not None)
        if binding_failure:
            await wait_for(lambda: runtime.calls.resource_for(media.call_id, 'recording') is None)
            assert not worker.done()
            monkeypatch.setattr(CallRecording, 'bind_media', original_bind)
            await runtime.hass.services.async_call('voip_stack', 'start_recording',
                {'device_id': endpoint.device_id}, blocking=True)
        from custom_components.voip_stack.core.audio_format import parse_audio_format_token
        fmt = parse_audio_format_token(browser.contract['tx_format'])
        for index in range(round(800 / fmt.frame_ms)):
            frame = struct.pack(f'<{fmt.nominal_frame_samples}h',
                *(int(5000 * math.sin(2 * math.pi * 650 * (i + index * fmt.nominal_frame_samples) / fmt.sample_rate))
                  for i in range(fmt.nominal_frame_samples)))
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(frame)))
            await asyncio.sleep(fmt.frame_ms / 1000)
        result = await runtime.hass.services.async_call('voip_stack', 'stop_recording',
            {'device_id': endpoint.device_id}, blocking=True, return_response=True)
        with wave.open(result['file_path'], 'rb') as wav:
            rate = wav.getframerate()
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(float)
        spectrum = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
        frequencies = np.fft.rfftfreq(len(pcm), 1 / rate)
        for tone in (550, 650):
            assert spectrum[np.abs(frequencies - tone) < 5].max() > 100_000
        assert runtime.calls.get_session(media.call_id).state == 'in_call'
        await runtime.hass.services.async_call('voip_stack', 'hangup', {'device_id': endpoint.device_id}, blocking=True)
        await asyncio.wait_for(worker, 2)
        assert not runtime.calls.sessions and not runtime.runtime.rtp_port_pool.get('used', set())
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_hangup_while_recording_storage_opens_leaves_no_capture(registered_phones, trunks, tmp_path, monkeypatch):
    from custom_components.voip_stack.mailbox import get_archive
    runtime = trunks
    directory = tmp_path / 'media'
    directory.mkdir()
    runtime.hass.config.media_dirs = {'local': str(directory)}
    runtime.runtime.endpoints.update('transfer_a', device_id='caller-phone')
    await asyncio.to_thread(registered_phones[0].dial, 'transfer_b')
    async with asyncio.timeout(3):
        while not any(s.state == 'in_call' for s in runtime.calls.sessions.values()):
            await asyncio.sleep(.01)
    archive = await get_archive(runtime.hass)
    opened, release = asyncio.Event(), asyncio.Event()
    original = archive.begin
    async def delayed(*args, **kwargs):
        writer = await original(*args, **kwargs)
        opened.set()
        await release.wait()
        return writer
    monkeypatch.setattr(archive, 'begin', delayed)
    starting = asyncio.create_task(runtime.hass.services.async_call('voip_stack', 'start_recording',
        {'device_id': 'caller-phone'}, blocking=True, return_response=True))
    try:
        await asyncio.wait_for(opened.wait(), 2)
        await asyncio.to_thread(registered_phones[0].hangup)
        async with asyncio.timeout(3):
            while runtime.calls.sessions:
                await asyncio.sleep(.01)
        release.set()
        with pytest.raises(ServiceValidationError, match='changed'):
            await starting
        assert not archive._active and archive._reserved == 0
        assert not list(archive.root.iterdir())
        assert not runtime.runtime.rtp_port_pool.get('used', set())
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
