"""Decode real preanswer PCMA RTP through the authenticated production audio WS."""
import asyncio
import json
import struct
from unittest.mock import AsyncMock

from aiohttp import WSMsgType, WSServerHandshakeError, web
import pytest
from homeassistant.core import Context

from custom_components.voip_stack.core import sip
from tests.test_automation_originate_ha import lab as lab, assert_call_resources_released
from tests.test_trunk_calls_ha import trunks as trunks
from tests.test_sip_phone_transfers_ha import registered_phones as registered_phones

pytestmark = pytest.mark.ha


@pytest.mark.parametrize('final_answer', [False, True])
async def test_pcma_rtp_is_decoded_on_audio_ws_before_any_200(
        lab, monkeypatch, aiohttp_client, hass_admin_user, final_answer):
    from custom_components.voip_stack import audio_ws_view, softphone_originate
    from custom_components.voip_stack.audio_ws import decode_audio_frame
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.roster import dump_roster_json
    from custom_components.voip_stack.websocket_api import _ha_softphone_state

    from homeassistant.helpers import entity_registry as er
    entity = er.async_get(lab.hass).async_get_or_create('event','voip_stack','early-pcm-phone')
    endpoint = PhoneEndpoint(endpoint_id='early-pcm',name='Early PCM',username='EarlyPCM',
        device_id='early-pcm-device',kind='browser',availability='available',capabilities={'audio','dtmf'},entity_ids={entity.entity_id})
    lab.runtime.endpoints.register(endpoint)
    lab.hass.states.async_set('sensor.voip_phonebook','ready',{'roster_json':dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate,'_ha_advertise_host',AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate,'_resolve_target_device',AsyncMock(return_value=None))
    phone = lab.phones[0]
    phone.auto_answer = False
    media_client = 'early-pcm-browser-owner'
    call_id = ''
    ws = None

    @web.middleware
    async def authenticated(request, handler):
        request['hass_user'] = hass_admin_user
        return await handler(request)

    app = web.Application(middlewares=[authenticated])
    app['hass'] = lab.hass
    app.router.add_get('/api/voip_stack/ws',audio_ws_view.VoipAudioWebSocketView().get)
    http = await aiohttp_client(app)

    def response(status, *, direction='sendonly'):
        request = phone.last_request
        headers = [(name,request.header(name)) for name in ('Via','From','Call-ID','CSeq')]
        headers += [('To',request.header('To')+';tag=bedroom'),('Content-Type','application/sdp'),
            ('Contact',f'<sip:bedroom@127.0.0.1:{phone.transport.get_extra_info("sockname")[1]}>')]
        body = ('v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=PCMA peer\r\n'
            'c=IN IP4 127.0.0.1\r\nt=0 0\r\n'
            f'm=audio {phone.rtp_socket.getsockname()[1]} RTP/AVP 8\r\n'
            f'a=rtpmap:8 PCMA/8000\r\na=ptime:20\r\na={direction}\r\n').encode()
        phone.transport.sendto(sip.build_response(status,'Session Progress' if status==183 else 'OK',headers,body),phone.remote)

    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.01)

    async def control():
        async with asyncio.timeout(3):
            while True:
                message = await ws.receive()
                assert message.type not in {WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR}, message
                if message.type == WSMsgType.TEXT:
                    payload = json.loads(message.data)
                    if 'rx_format' in payload:
                        return payload

    try:
        await lab.hass.services.async_call('voip_stack','call',{'device_id':endpoint.device_id,
            'destination':'bedroom0','media_client_id':media_client},blocking=True,
            context=Context(user_id=hass_admin_user.id))
        call_id = lab.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
        client = lab.calls.sip_client_for(call_id)
        await asyncio.wait_for(phone.invited.wait(),2)
        response(183)
        await wait_for(lambda: _ha_softphone_state(lab.hass,endpoint.endpoint_id).get('early_media'))
        assert client.dialog is None and not lab.calls.get_session(call_id).answer_committed
        rtp_socket = client.audio_rtp_socket
        assert rtp_socket is not None
        rtp_fd = rtp_socket.fileno()
        ws = await http.ws_connect('/api/voip_stack/ws?endpoint_id='+endpoint.endpoint_id+
            '&call_id='+call_id+'&client_id='+media_client)
        initial = await control()
        assert initial['state']=='remote_ringing' and initial['audio_direction']=='recvonly'
        await ws.send_json({'type':'audio_ready'})
        await wait_for(lambda: (call_id,endpoint.endpoint_id) in lab.runtime.media.sessions_for('audio'))
        owned = lab.runtime.media.sessions_for('audio')[(call_id,endpoint.endpoint_id)]
        owner = lab.runtime.media.owners_for('audio')[endpoint.endpoint_id+'|'+call_id]
        selected = next(dialog for dialog in client.early_dialogs.values() if dialog is not None)

        async def send_golden_audio():
            # Independent G.711 A-law codewords: AA = +32256, 2A = -32256.
            # No production encoder or decoder constructs the expected samples.
            payload = b'\xaa'*80+b'\x2a'*80
            for sequence in range(12):
                raw = struct.pack('!BBHII',0x80,8,sequence,sequence*160,0x11223344)+payload
                await lab.hass.loop.sock_sendto(phone.rtp_socket,raw,('127.0.0.1',selected.local_rtp_port))
                await asyncio.sleep(.02)

        sending = asyncio.create_task(send_golden_audio())
        samples = []
        binary_frames = 0
        try:
            async with asyncio.timeout(3):
                while binary_frames < 6:
                    message = await ws.receive()
                    assert message.type not in {WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR}, message
                    if message.type != WSMsgType.BINARY:
                        continue
                    pcm = decode_audio_frame(message.data)
                    samples.extend(struct.unpack('<'+'h'*(len(pcm)//2),pcm))
                    binary_frames += 1
        finally:
            await sending
        assert binary_frames >= 6
        assert sum(abs(value-32256)<=32 for value in samples)>500
        assert sum(abs(value+32256)<=32 for value in samples)>500
        assert client.dialog is None, 'The decoded audio must arrive before any final answer'
        state = _ha_softphone_state(lab.hass,endpoint.endpoint_id)
        assert state['state']=='remote_ringing' and state['connected_elapsed']==0
        assert not lab.calls.get_session(call_id).answer_committed

        if final_answer:
            response(200,direction='sendrecv')
            await wait_for(lambda: _ha_softphone_state(lab.hass,endpoint.endpoint_id)['state']=='in_call')
            final = await control()
            assert final['state']=='in_call' and final['audio_direction']=='sendrecv'
            assert lab.runtime.media.sessions_for('audio')[(call_id,endpoint.endpoint_id)] is owned
            assert rtp_socket.fileno()==rtp_fd
            assert rtp_socket.getsockname()[1]==owned.local_rtp_port
            assert client.audio_rtp_socket is None, 'The existing WS transport retains the original socket'
            assert lab.runtime.media.owners_for('audio')[endpoint.endpoint_id+'|'+call_id] is owner
            assert owned.media_generation==1
    finally:
        if call_id:
            await lab.hass.services.async_call('voip_stack','hangup',{'device_id':endpoint.device_id,
                'call_id':call_id},blocking=True,context=Context(user_id=hass_admin_user.id))
        if ws is not None:
            await ws.close()
        await http.close()
        await lab.hass.async_block_till_done()
    assert sum(request.method=='BYE' for request in phone.requests)==int(final_answer)
    assert any(request.method=='CANCEL' for request in phone.requests) is (not final_answer)
    assert_call_resources_released(lab)


async def test_pcma_preanswer_witness_detects_confirmed_only_gate(
        lab, monkeypatch, aiohttp_client, hass_admin_user):
    """The exact PCM witness must reject the original answered-only media gate."""
    from custom_components.voip_stack import media_call_lifetime
    monkeypatch.setattr(media_call_lifetime, '_media_state_active',
        lambda store: str(store.get('state') or '') in {'connecting', 'in_call'})
    with pytest.raises(WSServerHandshakeError) as failure:
        await test_pcma_rtp_is_decoded_on_audio_ws_before_any_200(
            lab, monkeypatch, aiohttp_client, hass_admin_user, False)
    assert failure.value.status == 409
    assert_call_resources_released(lab)
