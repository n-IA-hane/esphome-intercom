"""Real HA media resolution, FFmpeg decoding and SIP/RTP announcement delivery."""
import asyncio
from contextlib import aclosing
import io
import math
from pathlib import Path
import struct
import wave
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from homeassistant.components import ffmpeg, media_source
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers import config_validation as cv
from homeassistant.setup import async_setup_component

from custom_components.voip_stack.announcement_media import async_audio_stream
from custom_components.voip_stack.core import rtp
from .test_automation_originate_ha import lab, dial, sequence

pytestmark = pytest.mark.ha


def wav_audio(seconds=0.2):
    pcm = b"".join(struct.pack('<h', int(10000 * math.sin(2 * math.pi * 730 * n / 16000))) for n in range(int(16000*seconds)))
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm)
    return out.getvalue(), pcm


@pytest.fixture
async def media_file(hass, tmp_path):
    data, pcm = wav_audio()
    (tmp_path/'notice.wav').write_bytes(data)
    hass.config.media_dirs = {'local': str(tmp_path)}
    assert await async_setup_component(hass, 'media_source', {})
    # Real manager, real installed decoder; no mocked PCM provider.
    hass.data['ffmpeg'] = ffmpeg.FFmpegManager(hass, '/usr/bin/ffmpeg')
    return 'media-source://media_source/local/notice.wav', pcm


@pytest.mark.parametrize('delayed', [False, True])
async def test_file_after_answer_then_bye_and_redial(lab, media_file, delayed):
    media_id, expected = media_file
    phone = lab.phones[0]
    for cycle in range(2):
        phone.invited.clear()
        phone.bye.clear()
        phone.auto_answer = not delayed
        actions = sequence()
        actions[1] = {'action':'voip_stack.play_media', 'data':{
            'call_id':'{{ dialed.call_id }}', 'expected_generation':'{{ dialed.generation }}',
            'media': {'media_content_id':media_id, 'media_content_type':'audio/wav'}}}
        script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(actions)), 'File call', 'automation')
        task = asyncio.create_task(script.async_run(context=Context()))
        await asyncio.wait_for(phone.invited.wait(), 2)
        if delayed:
            assert phone.audio.empty()
            assert not task.done()
            phone.answer()
        await asyncio.wait_for(task, 5)
        received = bytearray()
        last = 0
        while not phone.audio.empty():
            raw, when = phone.audio.get_nowait()
            packet = rtp.parse_packet(raw)
            assert packet.payload_type == phone.payload_type
            if any(packet.payload):
                swapped = bytearray(len(packet.payload))
                swapped[::2], swapped[1::2] = packet.payload[1::2], packet.payload[::2]
                received.extend(swapped)
                last = when
        assert bytes(received) == expected
        assert phone.bye_at - last >= .18
        assert not lab.calls.sessions
        assert not lab.runtime.softphones
        assert all(not ep.active_call_id for ep in lab.runtime.endpoints.endpoints)


async def test_url_and_mp3_use_real_decoder(hass, media_file, aiohttp_server, tmp_path, socket_enabled):
    wav, _ = wav_audio()
    src = tmp_path/'input.wav'; src.write_bytes(wav)
    out = tmp_path/'input.mp3'
    process = await asyncio.create_subprocess_exec('/usr/bin/ffmpeg', '-v', 'error', '-i', str(src), str(out))
    assert await process.wait() == 0
    app = web.Application()
    async def serve(request):
        return web.Response(body=out.read_bytes(), content_type='audio/mpeg')
    app.router.add_get('/notice.mp3', serve)
    server = await aiohttp_server(app)
    async with aclosing(async_audio_stream(hass, str(server.make_url('/notice.mp3')))) as chunks:
        pcm = b''.join([chunk async for chunk in chunks])
    values = struct.unpack('<'+'h'*(len(pcm)//2),pcm)
    assert len(values) >= 3200
    assert sum(x*x for x in values)/len(values) > 1000000


@pytest.mark.parametrize('failure', ['missing', 'invalid', 'scheme'])
async def test_bad_media_terminates_outgoing_and_allows_retry(lab, media_file, tmp_path, failure):
    media_id, _ = media_file
    if failure == 'missing': media_id = media_id.replace('notice.wav','missing.wav')
    if failure == 'invalid': (tmp_path/'notice.wav').write_bytes(b'not audio')
    if failure == 'scheme': media_id = 'file:///etc/passwd'
    context = Context()
    result = await dial(lab, context)
    with pytest.raises(ServiceValidationError):
        await lab.hass.services.async_call('voip_stack','play_media', {
            'call_id':result['call_id'], 'expected_generation':result['generation'],
            'media': {'media_content_id':media_id,'media_content_type':'audio/wav'},
        }, blocking=True, context=context)
    assert not lab.calls.sessions
    assert all(not ep.active_call_id for ep in lab.runtime.endpoints.endpoints)
    await dial(lab, Context())


async def test_browser_call_cannot_use_file_playback(lab, media_file):
    session = lab.calls.upsert('browser-existing',state='in_call',owner='browser')
    with pytest.raises(ServiceValidationError):
        await lab.hass.services.async_call('voip_stack','play_media', {
            'call_id':session.call_id, 'expected_generation':session.generation,
            'media': {'media_content_id':media_file[0],'media_content_type':'audio/wav'},
        },blocking=True)


async def test_wrong_controller_and_generation_cannot_play(lab, media_file):
    context = Context(); result = await dial(lab, context)
    for caller, generation in ((Context(),result['generation']), (context,result['generation']+1)):
        with pytest.raises(ServiceValidationError):
            await lab.hass.services.async_call('voip_stack','play_media', {
                'call_id':result['call_id'], 'expected_generation':generation,
                'media': {'media_content_id':media_file[0],'media_content_type':'audio/wav'},
            },blocking=True,context=caller)
        assert lab.calls.get_session(result['call_id']).live


@pytest.mark.parametrize('ending', ['remote', 'cancel', 'timeout'])
async def test_interrupted_playback_reaps_decoder_and_call(lab, media_file, tmp_path, monkeypatch, ending):
    (tmp_path/'notice.wav').write_bytes(wav_audio(10)[0])
    processes = []
    original = asyncio.create_subprocess_exec
    async def create(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    context = Context(); result = await dial(lab, context)
    task = asyncio.create_task(lab.hass.services.async_call('voip_stack', 'play_media', {
        'call_id':result['call_id'], 'expected_generation':result['generation'],
        'timeout': 1 if ending == 'timeout' else 10,
        'media': {'media_content_id':media_file[0],'media_content_type':'audio/wav'},
    }, blocking=True, context=context))
    async with asyncio.timeout(3):
        while True:
            raw, _ = await lab.phones[0].audio.get()
            if any(rtp.parse_packet(raw).payload): break
    if ending == 'remote': lab.phones[0].remote_hangup()
    if ending == 'cancel': task.cancel()
    await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),3)
    await lab.hass.async_block_till_done()
    assert processes and all(p.returncode is not None for p in processes)
    assert not lab.calls.sessions
    assert all(not ep.active_call_id for ep in lab.runtime.endpoints.endpoints)
    assert not any(t.get_name()=='voip-announcement-input' for t in asyncio.all_tasks())
    await dial(lab,Context())


from .test_automation_call import application, service


async def test_incoming_automation_uses_same_file_path(application, media_file):
    app, registry, peer, released = application
    call = service(app, media={'media_content_id':media_file[0], 'media_content_type':'audio/wav'})
    try:
        await app.play_media(call)
        assert app.answered
        received = bytearray()
        async with asyncio.timeout(2):
            while len(received) < len(media_file[1]):
                raw = await app.hass.loop.sock_recv(peer, 2048)
                payload = rtp.parse_packet(raw).payload
                if not any(payload):
                    continue
                converted = bytearray(len(payload))
                converted[::2], converted[1::2] = payload[1::2], payload[::2]
                received.extend(converted)
        assert received == media_file[1]
    finally:
        await registry.terminate_call_wait(app.session.call_id, reason='test_done')
    assert released


@pytest.mark.parametrize('status', [404, 200])
async def test_http_error_or_stalled_download_releases_decoder(lab, media_file, aiohttp_server, monkeypatch, status):
    release = asyncio.Event()
    app = web.Application()
    async def serve(request):
        if status == 404:
            return web.Response(status=404)
        await release.wait()
        return web.Response(body=b'')
    app.router.add_get('/sound.wav', serve)
    server = await aiohttp_server(app)
    processes = []
    original = asyncio.create_subprocess_exec
    async def create(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    context = Context(); result = await dial(lab,context)
    try:
        with pytest.raises(ServiceValidationError):
            await lab.hass.services.async_call('voip_stack','play_media', {
                'call_id':result['call_id'], 'expected_generation':result['generation'], 'timeout':1,
                'media': {'media_content_id':str(server.make_url('/sound.wav')), 'media_content_type':'audio/wav'},
            },blocking=True,context=context)
        assert processes and all(p.returncode is not None for p in processes)
        assert not lab.calls.sessions
    finally:
        release.set()
