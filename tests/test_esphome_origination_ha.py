"""Native action correlation selects a line for one real SIP call only."""

import asyncio
from dataclasses import replace
import re
import socket
import struct
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack.core import sip, rtp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from custom_components.voip_stack.runtime_data import sip_trunks
from .test_trunk_calls_ha import sip_test_account, trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.fixture
async def esp_phones(trunks, monkeypatch):
    from custom_components.voip_stack import phone_control
    runtime = trunks
    phones = []
    for index in range(2):
        identity = f'esp_{index}'
        host = f'127.0.0.{index+2}'
        endpoint = PhoneEndpoint(endpoint_id=identity, name=identity, username=identity, device_id='device-'+identity,
            kind=EndpointKind.ESPHOME, availability='available', policy=PhonePolicy(outbound_trunk_id='work'))
        runtime.runtime.endpoints.register(endpoint)
        account = sip_test_account(runtime, identity)
        runtime.roster.append(RosterEntry(identity, address=host, metadata={'endpoint_id':identity}))
        audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        audio.bind((host, 0))
        audio.setblocking(False)
        phone = SimpleNamespace(identity=identity, endpoint=endpoint, account=account, audio=audio, host=host, client=None,
            tasks=[], stopped=[], delivered=[], ordinary_args=[], send_invite=True, device={
                'endpoint_id':identity, 'device_id':endpoint.device_id, 'name':identity,
                'route_id':identity, 'entities':{},
            })
        async def originate(call, phone=phone):
            call_id = call.data['call_id']
            phone.delivered.append(call_id)
            if not phone.send_invite:
                return
            client = SipCallClient(local_ip=phone.host, local_name=phone.identity, local_uri_user=phone.identity,
                username=phone.account.username, password=phone.account.password,
                local_sip_port=5060, local_rtp_port=phone.audio.getsockname()[1],
                supported_formats=[AudioFormat(16000, frame_ms=20)])
            client.dialog_ids = replace(client.dialog_ids, call_id=call_id)
            client.transport, client.protocol = await runtime.hass.loop.create_datagram_endpoint(
                lambda: SipDatagramQueueProtocol(client.queue), local_addr=(phone.host,0))
            client.local_sip_port = client.transport.get_extra_info('sockname')[1]
            phone.client = client
            phone.tasks.append(asyncio.create_task(client.invite(target=call.data['dest'], remote_host='127.0.0.1',
                remote_sip_port=runtime.runtime.transport_config['sip_port'], timeout=3)))
        async def stop(call, phone=phone):
            phone.stopped.append(call.data['call_id'])
            if phone.client is not None and phone.client.dialog_ids.call_id == call.data['call_id']:
                await phone.client.terminate()
        phone.start = originate
        async def ordinary(call, originate=originate, phone=phone):
            phone.ordinary_args.append(dict(call.data))
            await originate(SimpleNamespace(data={'call_id':sip.make_call_id(phone.identity),'dest':call.data['dest']}))
        runtime.hass.services.async_register('esphome', identity+'_start_call', ordinary)
        runtime.hass.services.async_register('esphome', identity+'_start_call_via_ha', originate)
        runtime.hass.services.async_register('esphome', identity+'_hangup_call_if_id', stop)
        phones.append(phone)
    async def resolve(_hass, _call, *, selector):
        return next((p.device for p in phones if p.endpoint.device_id==selector), None)
    monkeypatch.setattr(phone_control, 'async_resolve_source_device', resolve)
    yield phones
    for phone in phones:
        if phone.client is not None:
            await phone.client.terminate()
            await phone.client.close()
        for task in phone.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*phone.tasks, return_exceptions=True)
        phone.audio.close()


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_two_esp_actions_use_independent_lines_and_real_duplex_rtp(trunks, esp_phones):
    runtime = trunks
    responses = await asyncio.gather(*(
        runtime.hass.services.async_call('voip_stack','call',{
            'device_id':phone.endpoint.device_id,'destination':'External','trunk_id':identity},
            blocking=True,return_response=True)
        for phone,identity in zip(esp_phones,('home','work'),strict=True)))
    for index,(phone,response) in enumerate(zip(esp_phones,responses,strict=True)):
        client = phone.client
        result = await phone.tasks[-1]
        if result=='ringing':
            result=await client.wait_for_final(timeout=3)
        assert result=='in_call'
        assert response['call']['call_id']==client.dialog_ids.call_id==phone.delivered[0]
        session=runtime.calls.get_session(client.dialog_ids.call_id)
        assert session.metadata['outbound_trunk_id']==('home' if index==0 else 'work')
        assert runtime.runtime.endpoints.get(phone.identity).policy.outbound_trunk_id=='work'
        dialog=client.dialog
        packet=rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type,1,320,901+index,
            struct.pack('!320h',*([2000+index*500]*320))))
        await runtime.hass.loop.sock_sendto(phone.audio,packet,(dialog.remote_rtp_host,dialog.remote_rtp_port))
        received,_=await asyncio.wait_for(runtime.phones[index].audio.get(),3)
        assert set(struct.unpack('!320h',rtp.parse_packet(received).payload))=={2000+index*500}
        peer_port=int(re.search(rb'm=audio (\d+)',runtime.phones[index].invites[0].body)[1])
        packet=rtp.build_packet(rtp.RtpPacket(runtime.phones[index].payload_type,1,320,951+index,
            struct.pack('!320h',*([3000+index*500]*320))))
        await runtime.hass.loop.sock_sendto(runtime.phones[index].rtp_socket,packet,('127.0.0.1',peer_port))
        received=rtp.parse_packet(await asyncio.wait_for(runtime.hass.loop.sock_recv(phone.audio,4096),3))
        assert set(struct.unpack('!320h',received.payload))=={3000+index*500}
    for phone in esp_phones:
        await phone.client.terminate()
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(.01)
    assert not runtime.runtime.rtp_port_pool['used']
    assert all(client.ready for client in sip_trunks(runtime.hass).values())
    phone=esp_phones[0]
    await phone.client.close()
    await runtime.hass.services.async_call('voip_stack','call',{
        'device_id':phone.endpoint.device_id,'destination':'External'},blocking=True)
    result=await phone.tasks[-1]
    if result=='ringing':
        result=await phone.client.wait_for_final(timeout=3)
    assert result=='in_call'
    assert len(runtime.phones[0].invites)==1 and len(runtime.phones[1].invites)==2
    assert runtime.calls.get_session(phone.client.dialog_ids.call_id).metadata['outbound_trunk_id']=='work'
    await phone.client.terminate()
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(.01)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('failure', ['timeout', 'cancel'])
async def test_cancelled_action_rejects_late_invite_and_releases_claim(trunks, esp_phones, monkeypatch, failure):
    from custom_components.voip_stack import esphome_origination
    monkeypatch.setattr(esphome_origination, 'ACTION_TIMEOUT', .1)
    phone=esp_phones[0]
    phone.send_invite=False
    task=asyncio.create_task(trunks.hass.services.async_call('voip_stack','call',{
        'device_id':phone.endpoint.device_id,'destination':'External','trunk_id':'home'},blocking=True))
    async with asyncio.timeout(2):
        while not phone.delivered:
            await asyncio.sleep(.001)
    if failure=='cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ServiceValidationError, match='did not start'):
            await task
    identity=phone.delivered[0]
    assert phone.stopped==[identity]
    assert not trunks.calls.sessions
    assert not trunks.runtime.endpoints.get(phone.identity).active_call_id
    phone.send_invite=True
    await phone.start(SimpleNamespace(data={'call_id':identity,'dest':'External'}))
    await phone.tasks[-1]
    assert phone.client.last_sip_status_code==481
    assert not any(peer.invites for peer in trunks.phones)
    assert not trunks.calls.sessions


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('failure', ['old_firmware', 'unavailable_line', 'invalid_target'])
async def test_invalid_esp_override_does_not_start_any_call(trunks, esp_phones, failure):
    phone=esp_phones[0]
    destination='External'
    if failure=='old_firmware':
        trunks.hass.services.async_remove('esphome',phone.identity+'_start_call_via_ha')
    elif failure=='unavailable_line':
        await sip_trunks(trunks.hass)['home'].stop()
    else:
        destination='bad\r\nHeader'
    with pytest.raises(ServiceValidationError):
        await trunks.hass.services.async_call('voip_stack','call',{
            'device_id':phone.endpoint.device_id,'destination':destination,'trunk_id':'home'},blocking=True)
    assert not phone.delivered and not trunks.calls.sessions
    assert not any(peer.invites for peer in trunks.phones)
    if failure == 'old_firmware':
        await trunks.hass.services.async_call('voip_stack', 'call',
            {'device_id': phone.endpoint.device_id, 'destination': 'External'}, blocking=True)
        result = await phone.tasks[-1]
        if result == 'ringing':
            result = await phone.client.wait_for_final(timeout=3)
        assert result == 'in_call'
        assert phone.ordinary_args == [{'dest': 'External'}]
        assert len(trunks.phones[1].invites) == 1 and not trunks.phones[0].invites
        await phone.client.terminate()


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('mismatch', ['source', 'destination'])
async def test_action_correlation_checks_actual_sip_source_and_destination(trunks, esp_phones, monkeypatch, mismatch):
    from custom_components.voip_stack import esphome_origination
    monkeypatch.setattr(esphome_origination, 'ACTION_TIMEOUT', .2)
    selected=esp_phones[0]
    sender=esp_phones[1] if mismatch=='source' else selected
    async def incorrect(call):
        await sender.start(SimpleNamespace(data={**call.data,
            'dest':'Other' if mismatch=='destination' else call.data['dest']}))
    trunks.hass.services.async_register('esphome',selected.identity+'_start_call_via_ha',incorrect)
    with pytest.raises(ServiceValidationError, match='did not start|could be routed'):
        await trunks.hass.services.async_call('voip_stack','call',{
            'device_id':selected.endpoint.device_id,'destination':'External','trunk_id':'home'},blocking=True)
    await sender.tasks[-1]
    assert sender.client.last_sip_status_code==403
    assert not any(peer.invites for peer in trunks.phones)
    assert not trunks.calls.sessions


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_action_cancel_after_invite_drains_late_answer(trunks, esp_phones):
    phone=esp_phones[0]
    peer=trunks.phones[0]
    peer.auto_answer=False
    peer.answer_on_cancel=True
    never=asyncio.Event()
    async def delivered_but_api_pending(call):
        await phone.start(call)
        await never.wait()
    trunks.hass.services.async_register('esphome',phone.identity+'_start_call_via_ha',delivered_but_api_pending)
    task=asyncio.create_task(trunks.hass.services.async_call('voip_stack','call',{
        'device_id':phone.endpoint.device_id,'destination':'External','trunk_id':'home'},blocking=True))
    try:
        async with asyncio.timeout(3):
            while not phone.tasks:
                await asyncio.sleep(.001)
        assert await phone.tasks[-1]=='ringing'
        assert len(peer.invites)==1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(3):
            while trunks.calls.sessions:
                await asyncio.sleep(.01)
        methods=[request.method for request in peer.requests]
        assert 'CANCEL' in methods and 'ACK' in methods and 'BYE' in methods
        assert phone.stopped==phone.delivered
        assert not trunks.runtime.rtp_port_pool.get('used',set())
        assert all(client.ready for client in sip_trunks(trunks.hass).values())
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_repeated_cancellation_waits_for_scoped_firmware_cleanup(trunks, esp_phones):
    phone=esp_phones[0]
    phone.send_invite=False
    entered,release=asyncio.Event(),asyncio.Event()
    async def gated_stop(call):
        phone.stopped.append(call.data['call_id'])
        entered.set()
        await release.wait()
    trunks.hass.services.async_register('esphome',phone.identity+'_hangup_call_if_id',gated_stop)
    task=asyncio.create_task(trunks.hass.services.async_call('voip_stack','call',{
        'device_id':phone.endpoint.device_id,'destination':'External','trunk_id':'home'},blocking=True))
    try:
        async with asyncio.timeout(2):
            while not phone.delivered:
                await asyncio.sleep(.001)
        task.cancel()
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), 'A second cancellation bypassed owned cleanup'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert phone.stopped==phone.delivered
        assert not trunks.calls.sessions and not trunks.runtime.endpoints.get(phone.identity).active_call_id
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_second_action_cannot_replace_pending_phone_request(trunks, esp_phones):
    phone=esp_phones[0]
    phone.send_invite=False
    first=asyncio.create_task(trunks.hass.services.async_call('voip_stack','call',{
        'device_id':phone.endpoint.device_id,'destination':'External','trunk_id':'home'},blocking=True))
    try:
        async with asyncio.timeout(2):
            while not phone.delivered:
                await asyncio.sleep(.001)
        identity=phone.delivered[0]
        with pytest.raises(ServiceValidationError,match='busy'):
            await trunks.hass.services.async_call('voip_stack','call',{
                'device_id':phone.endpoint.device_id,'destination':'External','trunk_id':'work'},blocking=True)
        assert phone.delivered==[identity] and not phone.stopped
        assert trunks.runtime.endpoints.get(phone.identity).active_call_id==identity
        assert trunks.calls.get_session(identity).metadata['outbound_trunk_id']=='home'
    finally:
        first.cancel()
        await asyncio.gather(first,return_exceptions=True)
    assert not trunks.calls.sessions
