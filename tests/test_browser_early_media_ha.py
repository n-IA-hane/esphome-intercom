"""A real 183/SDP opens browser media while final answer is withheld."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.core import sip
from tests.test_automation_originate_ha import lab as lab, assert_call_resources_released
from tests.test_trunk_calls_ha import trunks as trunks
from tests.test_sip_phone_transfers_ha import registered_phones as registered_phones

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("final_answer", [False, True])
async def test_browser_183_keeps_media_owner_and_releases(lab, monkeypatch, final_answer):
    from custom_components.voip_stack import softphone_originate, audio_ws_view
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.roster import dump_roster_json
    from custom_components.voip_stack.websocket_api import _ha_softphone_state
    source = PhoneEndpoint(endpoint_id='browser-early',name='Early',username='Early',
        device_id='browser-early-device',kind='browser',availability='available')
    lab.runtime.endpoints.register(source)
    lab.hass.states.async_set('sensor.voip_phonebook','ready',{'roster_json':dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate,'_ha_advertise_host',AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate,'_resolve_target_device',AsyncMock(return_value=None))
    phone=lab.phones[0]
    original=phone.respond
    def provisional(request,address,status,body=b''):
        if status==200 and body:
            headers=[(name,request.header(name)) for name in ('Via','From','Call-ID','CSeq')]
            headers += [('To',request.header('To')+';tag=bedroom'),('Content-Type','application/sdp'),
                ('Contact',f'<sip:bedroom@127.0.0.1:{phone.transport.get_extra_info("sockname")[1]}>')]
            phone.transport.sendto(sip.build_response(183,'Session Progress',headers,body),address)
        else:
            original(request,address,status,body)
    monkeypatch.setattr(phone,'respond',provisional)
    call_id=''
    remove_listener = None
    ended = None
    active_key = None
    try:
        await lab.hass.services.async_call('voip_stack','call',{
            'device_id':source.device_id,'destination':'bedroom0'},blocking=True)
        call_id=lab.runtime.endpoints.get(source.endpoint_id).active_call_id
        client=lab.calls.sip_client_for(call_id)
        async with asyncio.timeout(2):
            while not any(dialog is not None for dialog in client.early_dialogs.values()):
                await asyncio.sleep(.01)
        assert client.dialog is None, '183 must remain an early SIP dialog'
        state=_ha_softphone_state(lab.hass,source.endpoint_id)
        assert state['state']=='remote_ringing' and not lab.calls.get_session(call_id).answer_committed
        assert state.get('early_media') is True, state
        assert state['can_attach_media'] is True
        assert state['connected_elapsed'] == 0.0
        media=audio_ws_view._active_softphone_media_session(lab.hass,source.endpoint_id)
        assert media is not None, 'Actual provisional SDP must resolve the same browser RTP owner'
        assert media.remote_rtp_port==phone.rtp_socket.getsockname()[1]
        assert media.rtp_socket is client.audio_rtp_socket
        from custom_components.voip_stack.media_call_lifetime import listen_for_media_call_end
        ended, remove_listener = listen_for_media_call_end(lab.hass, call_id, source.endpoint_id)
        assert not ended.is_set(), 'Ringing with negotiated SDP must preserve the media lifecycle'
        from dataclasses import replace
        candidate = next(dialog for dialog in client.early_dialogs.values() if dialog is not None)
        other = replace(candidate, remote_tag='other-fork', remote_rtp_port=9)
        client.early_dialogs[other.remote_tag] = other
        assert await client.on_provisional_media(other) is None
        assert audio_ws_view._active_softphone_media_session(lab.hass,source.endpoint_id).remote_rtp_port == media.remote_rtp_port
        assert client.dialog is None
        if final_answer:
            # Install the exact production session as the already attached owner.
            active_key = (call_id, source.endpoint_id)
            lab.runtime.media.sessions_for('audio')[active_key] = media
            monkeypatch.setattr(phone, 'respond', original)
            phone.direction = 'sendonly'
            phone.answer()
            async with asyncio.timeout(2):
                while _ha_softphone_state(lab.hass,source.endpoint_id)['state'] != 'in_call':
                    await asyncio.sleep(.01)
            assert client.dialog is not None and not ended.is_set()
            assert _ha_softphone_state(lab.hass,source.endpoint_id)['early_media'] is False
            answered_state = _ha_softphone_state(lab.hass,source.endpoint_id)
            context = lab.calls.event_context(call_id)
            assert answered_state['connected_elapsed'] == pytest.approx(
                time.monotonic() - context.connected_at, abs=0.1)
            assert lab.runtime.media.sessions_for('audio')[active_key] is media
            assert media.rtp_socket is client.audio_rtp_socket
            assert media.local_audio_direction == 'recvonly'
            assert media.media_generation == 1 and media.update_event.is_set()
    finally:
        if call_id:
            await lab.hass.services.async_call('voip_stack','hangup',{
                'device_id':source.device_id,'call_id':call_id},blocking=True)
            await lab.hass.async_block_till_done()
        if active_key is not None:
            lab.runtime.media.sessions_for('audio').pop(active_key, None)
        if remove_listener is not None:
            remove_listener()
    assert ended is not None and ended.is_set()
    assert any(request.method=='CANCEL' for request in phone.requests) is (not final_answer)
    assert sum(request.method=='BYE' for request in phone.requests) == int(final_answer)
    assert len({request.header('Call-ID') for request in phone.requests if request.method=='INVITE'}) == 1
    assert_call_resources_released(lab)
