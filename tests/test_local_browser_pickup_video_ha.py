"""Casa -> ringing browser -> JPEG receive-only native pickup, real HA/SIP owners."""
import asyncio
import socket
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot
from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND, CONF_VIDEO_TRANSCODING
from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core import rtp, video_rtp
from custom_components.voip_stack.core.sip_stun import is_stun_message
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.store import config_entry
from tests.test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize('caller_video,picker_video,answer_race', [
    (True, True, False), (False, True, False), (True, False, False), (True, True, True)])
async def test_ringing_local_browser_camera_reaches_jpeg_picker(
    lab, monkeypatch, tmp_path, caller_video, picker_video, answer_race,
):
    from aiohttp import WSMsgType
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.local_softphone_runtime import async_setup_local_softphone_bridge
    from custom_components.voip_stack.websocket_api import _ha_softphone_store
    from custom_components.voip_stack import audio_ws_view, video_ws_view
    from custom_components.voip_stack import audio_ws
    from custom_components.voip_stack.media_ws_session import async_media_websocket_session
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner

    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, 'parking': {'enabled': True}})
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(('127.0.0.1', 0))
        sip_port = probe.getsockname()[1]
    lab.runtime.transport_config.update(sip_port=sip_port, **{
        CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: True, CONF_VIDEO_TRANSCODING: True})
    casa = PhoneEndpoint(endpoint_id='browser:casa-pickup', name='Casa pickup camera', device_id='casa-pickup-device',
        kind=EndpointKind.BROWSER, availability='available',
        capabilities=frozenset({'audio', 'dtmf'} | ({'video'} if caller_video else set())))
    target = PhoneEndpoint(endpoint_id='browser:test-ringing', name='Test ringing', device_id='test-ringing-device',
        kind=EndpointKind.BROWSER, availability='available', capabilities=frozenset({'audio', 'video', 'dtmf'}))
    picker_endpoint = PhoneEndpoint(endpoint_id='ws3-jpeg-picker', name='ws3-jpeg-picker', device_id='ws3-picker-device',
        kind=EndpointKind.ESPHOME, availability='available', capabilities=frozenset({'audio', 'video', 'dtmf'}))
    for endpoint in (casa, target, picker_endpoint):
        lab.runtime.endpoints.register(endpoint)
    directory = await async_refresh_directory(lab.hass)
    lab.roster[:] = [item for item in directory if item.metadata.get('endpoint_id') != picker_endpoint.endpoint_id]
    lab.roster.append(RosterEntry('ws3-jpeg-picker', name='ws3-jpeg-picker', address='127.0.0.1',
        metadata={'endpoint_id': picker_endpoint.endpoint_id, 'endpoint_kind': 'esphome', 'sip_video_codec': 'JPEG'}))
    lab.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(lab.roster)})
    monkeypatch.setattr(peer_snapshot, 'async_advertise_host', AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(peer_snapshot, 'async_build_peer_snapshot', AsyncMock(return_value=[]))
    monkeypatch.setattr(endpoint_runtime, '_roster_from_peers', lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, '_roster_from_peers', lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    async_setup_local_softphone_bridge(lab.hass)
    video_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    video_socket.bind(('127.0.0.2', 0))
    video_socket.setblocking(False)
    audio_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio_socket.bind(('127.0.0.2', 0))
    audio_socket.setblocking(False)
    picker = SipCallClient(local_ip='127.0.0.2', local_name='ws3-jpeg-picker', local_uri_user='ws3-jpeg-picker',
        local_sip_port=0, local_rtp_port=audio_socket.getsockname()[1], supported_formats=[AudioFormat(16000, frame_ms=20)],
        local_video_rtp_port=video_socket.getsockname()[1] if picker_video else 0,
        # Archived WS3 normal03: JPEG receive-only,320x240,10fps; no H264 offer.
        video_formats=(sdp.RtpVideoFormat(payload_type=26, encoding='JPEG', max_framerate=10,
            image_attributes=sdp.parse_imageattr('26 recv [x=[8:8:320],y=[8:8:240],sar=1.0,q=1.0]')[1]),) if picker_video else (),
        video_direction='recvonly')
    picker.audio_rtp_socket = audio_socket
    call_id = ''
    tasks = []

    class Browser:
        def __init__(self):
            self.messages, self.frames = asyncio.Queue(), asyncio.Queue()
            self.ready = asyncio.Event()
            self.closed = False
            self.negotiated = None
        def force_close(self):
            self.closed = True
            self.messages.put_nowait(SimpleNamespace(type=WSMsgType.CLOSE))
        async def close(self, **_kwargs): self.force_close()
        def __aiter__(self): return self
        async def __anext__(self): return await self.messages.get()
        async def send_json(self, value):
            self.negotiated = value
            self.ready.set()
        async def send_bytes(self, value): self.frames.put_nowait(value)
    try:
        await lab.hass.services.async_call('voip_stack', 'call', {'device_id': casa.device_id,
            'destination': target.name, 'media_client_id': 'casa-camera-owner', 'send_video': True}, blocking=True)
        call_id = _ha_softphone_store(lab.hass, casa.endpoint_id)['call_id']
        assert _ha_softphone_store(lab.hass, target.endpoint_id)['state'] == 'ringing'
        if answer_race:
            from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
            start = SipRtpRelay.start
            answered = False
            async def original_phone_answers(relay):
                nonlocal answered
                if not answered:
                    answered = True
                    await lab.hass.services.async_call('voip_stack', 'answer', {
                        'device_id': target.device_id, 'call_id': call_id,
                        'media_client_id': 'original-test-answer-owner', 'send_video': False}, blocking=True)
                await start(relay)
            monkeypatch.setattr(SipRtpRelay, 'start', original_phone_answers)
        outcome = await picker.invite(target='pickup.Test ringing', remote_host='127.0.0.1', remote_sip_port=sip_port, timeout=3)
        if outcome == 'ringing':
            outcome = await picker.wait_for_final(timeout=3)
        if answer_race:
            assert outcome != 'in_call'
            session = lab.calls.get_session(call_id)
            assert session.live and session.owner == 'local_bridge' and session.state == 'in_call'
            assert _ha_softphone_store(lab.hass, target.endpoint_id)['state'] == 'in_call'
            assert lab.runtime.endpoints.get(casa.endpoint_id).active_call_id == call_id
            assert lab.runtime.endpoints.get(target.endpoint_id).active_call_id == call_id
            assert not lab.runtime.endpoints.get(picker_endpoint.endpoint_id).active_call_id
            assert session.endpoint_claims == {casa.endpoint_id: 'source', target.endpoint_id: 'destination'}
            assert not lab.runtime.rtp_port_pool.get('used'), 'Rollback must release both relay and browser video reservations'
            from custom_components.voip_stack.local_softphone_runtime import local_softphone_bridge, LOCAL_AUDIO_FORMAT
            winner = local_softphone_bridge(lab.hass)
            caller_lease = winner.acquire_media(call_id, casa.endpoint_id, 'casa-camera-owner')
            callee_lease = winner.acquire_media(call_id, target.endpoint_id, 'original-test-answer-owner')
            for sender, receiver, level in ((caller_lease, callee_lease, 6000), (callee_lease, caller_lease, 9000)):
                pcm = struct.pack('<h', level) * LOCAL_AUDIO_FORMAT.nominal_frame_samples
                winner.send_audio(call_id, sender.endpoint_id, sender.token, pcm)
                assert winner.receive_audio_nowait(call_id, receiver.endpoint_id, receiver.token) == pcm
            return
        assert outcome == 'in_call'
        session = lab.calls.get_session(call_id)
        assert session.state == 'in_call' and session.owner == 'bridge'
        assert lab.runtime.endpoints.get(casa.endpoint_id).active_call_id == call_id
        assert lab.runtime.endpoints.get(picker_endpoint.endpoint_id).active_call_id == call_id
        assert not lab.runtime.endpoints.get(target.endpoint_id).active_call_id
        audio = audio_ws_view._active_softphone_media_session(lab.hass, casa.endpoint_id)
        assert audio is not None
        audio_browser = Browser()
        tasks.append(asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, audio_browser, audio,
            endpoint_id=casa.endpoint_id)))
        await asyncio.wait_for(audio_browser.ready.wait(), 2)
        pcm = struct.pack('<h', 7000) * audio.send_format.audio_format.nominal_frame_samples
        async def feed_browser_audio():
            for _ in range(20):
                audio_browser.messages.put_nowait(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(pcm)))
                await asyncio.sleep(.01)
        tasks.append(asyncio.create_task(feed_browser_audio()))
        # Startup playout may emit silence; the oracle requires the new nonzero
        # source, not the first packet or a mere counter increase.
        async with asyncio.timeout(3):
            while True:
                received = rtp.parse_packet(await lab.hass.loop.sock_recv(audio_socket, 4096))
                if any(received.payload):
                    break
        for sequence in range(1, 5):
            outgoing = rtp.RtpPacket(payload_type=picker.dialog.send_format.payload_type, sequence=sequence,
                timestamp=sequence * picker.dialog.send_format.audio_format.nominal_frame_samples,
                ssrc=1234, marker=False, payload=struct.pack('>h', 5000) * picker.dialog.send_format.audio_format.nominal_frame_samples)
            await lab.hass.loop.sock_sendto(audio_socket, rtp.build_packet(outgoing),
                (picker.dialog.remote_rtp_host, picker.dialog.remote_rtp_port))
            await asyncio.sleep(.02)
        async with asyncio.timeout(3):
            while not any(audio_ws.decode_audio_frame(await audio_browser.frames.get())):
                pass
        if not (caller_video and picker_video):
            assert picker.dialog.video_format is None
            assert video_ws_view._active_video_session(lab.hass, casa.endpoint_id) is None
            return
        assert picker.dialog.video_format is not None, 'Pickup lost the browser camera before SDP/JPEG admission'
        assert picker.dialog.recv_video_format.encoding == 'JPEG'
        assert picker.dialog.local_video_direction == 'recvonly'
        video = video_ws_view._active_video_session(lab.hass, casa.endpoint_id)
        assert video is not None and video.can_send and not video.can_receive
        assert video.send_video_format.max_framerate == 10
        limits = video.send_video_format.image_attributes.recv.sets[0]
        assert limits.x.maximum == 320 and limits.y.maximum == 240
        video_ws_view._detach_video_socket(lab.hass, video)
        video_browser = Browser()
        async def run_video():
            owner = MediaWebSocketOwner(websocket=video_browser, client_id='casa-camera-owner')
            async with async_media_websocket_session(lab.runtime.media, lab.calls, call_id, casa.endpoint_id,
                    owner, channel='video', timeout=2, shutdown_event=None, pin_client_identity=True,
                    local_bridge=None, publish_state=lambda: None):
                await video_ws_view._run_video_session(lab.hass, video_browser, video, endpoint_id=casa.endpoint_id)
        tasks.append(asyncio.create_task(run_video()))
        await asyncio.wait_for(video_browser.ready.wait(), 2)
        assert video_browser.negotiated['send']['encoding'] == 'JPEG'
        assert video_browser.negotiated['can_send'] is True
        encoder = await asyncio.create_subprocess_exec('ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'color=red:size=160x120', '-frames:v', '1', '-c:v', 'mjpeg', '-huffman', 'default',
            '-force_duplicated_matrix', '1', '-pix_fmt', 'yuvj420p', '-threads', '1', '-f', 'image2pipe', 'pipe:1',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        jpeg, error = await asyncio.wait_for(encoder.communicate(), 5)
        assert encoder.returncode == 0, error.decode()
        video_browser.messages.put_nowait(SimpleNamespace(type=WSMsgType.BINARY,
            data=struct.pack('!BBI', 1, 1, 1000000) + jpeg))
        depacketizer = video_rtp.JpegDepacketizer()
        picture = None
        async with asyncio.timeout(4):
            while picture is None:
                raw = await lab.hass.loop.sock_recv(video_socket, 4096)
                if is_stun_message(raw):
                    continue
                packet = rtp.parse_packet(raw)
                assert packet.payload_type == 26
                picture = depacketizer.push(packet)
        assert video_rtp.jpeg_dimensions(picture.data) == (160, 120)
        decoder = await asyncio.create_subprocess_exec('ffmpeg', '-nostdin', '-v', 'error', '-f', 'image2pipe', '-i',
            'pipe:0', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        rgb, error = await asyncio.wait_for(decoder.communicate(picture.data), 5)
        assert decoder.returncode == 0 and len(rgb) == 160 * 120 * 3, error.decode()
        assert sum(rgb[0::3]) > 10 * max(1, sum(rgb[2::3])), 'Decoded picker JPEG must contain the newly sent red image'
        (tmp_path / 'picker-new-frame.jpg').write_bytes(picture.data)
    finally:
        await picker.close()
        video_socket.close()
        audio_socket.close()
        if call_id and lab.calls.get_session(call_id):
            await lab.calls.request_termination(call_id, TerminationIntent('test_done'))
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get('used')
        assert not lab.runtime.media.sessions_for('video')
        assert all(not lab.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
                   for endpoint in (casa, target, picker_endpoint))


@pytest.mark.parametrize('invalid_owner', ['stale_generation', 'ended', 'different_owner'])
def test_losing_pickup_does_not_adopt_stale_or_changed_owner(invalid_owner):
    from unittest.mock import Mock
    from custom_components.voip_stack.local_softphone_runtime import PendingLocalSource, LocalCallState

    session = SimpleNamespace(call_id='original', generation=7, live=True, owner='local_bridge', state='in_call')
    if invalid_owner == 'ended':
        session.live = False
    if invalid_owner == 'different_owner':
        session.owner = 'bridge'
    registry = SimpleNamespace(get_session=Mock(return_value=None if invalid_owner == 'stale_generation' else session),
                               claim_endpoint=Mock())
    current = SimpleNamespace(caller_state=LocalCallState.IN_CALL, callee_state=LocalCallState.IN_CALL)
    source = SimpleNamespace(outgoing=False, committed=False, registry=registry,
        token=SimpleNamespace(call_id='original', generation=7), bridge=SimpleNamespace(get_call=lambda _: current))
    PendingLocalSource.preserve_answered_local_claims(source)
    registry.get_session.assert_called_once_with('original', generation=7)
    registry.claim_endpoint.assert_not_called()
