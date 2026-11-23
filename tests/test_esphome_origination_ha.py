"""Native dial commands originate ordinary registered SIP without HA prestate."""

import asyncio
import re
import socket
import struct
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack.core import sip, rtp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from custom_components.voip_stack.runtime_data import sip_trunks
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.trunk_config import trunk_subentries
from .test_trunk_calls_ha import sip_test_account, trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


async def idle(runtime):
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(.01)
    assert not runtime.runtime.rtp_port_pool.get('used', set())


async def connected(phone):
    result = await phone.tasks[-1]
    if result == 'ringing':
        result = await phone.client.wait_for_final(timeout=3)
    assert result == 'in_call'
    return phone.client


@pytest.fixture
async def esp_phones(trunks, monkeypatch):
    from custom_components.voip_stack import phone_control
    runtime = trunks
    entry = config_entry(runtime.hass)
    existing = {str(item.data.get('trunk_id') or item.subentry_id) for item in trunk_subentries(entry)}
    for identity, config in runtime.runtime.trunk_configs.items():
        if identity not in existing:
            runtime.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
                subentry_type='trunk', title=identity, unique_id='trunk:'+identity,
                data={**config, 'trunk_id': identity},
            ))
    phones = []
    for index in range(2):
        identity = f'esp_{index}'
        host = f'127.0.0.{index+2}'
        route_id = 'node_'+identity
        endpoint = PhoneEndpoint(endpoint_id=identity, name=f'Native phone {index}', username=identity,
            device_id='device-'+identity, account_device_id='account-'+identity, kind=EndpointKind.SIP_ACCOUNT, availability='available',
            policy=PhonePolicy(outbound_trunk_id='work'))
        runtime.runtime.endpoints.register(endpoint)
        account = sip_test_account(runtime, identity)
        audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        audio.bind((host, 0))
        audio.setblocking(False)
        phone = SimpleNamespace(identity=identity, route_id=route_id, endpoint=endpoint, account=account, audio=audio, host=host,
            client=None, tasks=[], monitors=[], workers=[], gates=[], stopped=[], delivered=[], rejected=[],
            emitted_ids=[], ordinary_args=[], send_invite=True, calling=False, pending=None, device={
                'endpoint_id': identity, 'device_id': endpoint.device_id, 'name': endpoint.name,
                'route_id': route_id, 'entities': {},
            })

        async def ensure_client(phone=phone):
            if phone.client is not None and not phone.client._closed:
                return phone.client
            client = SipCallClient(local_ip=phone.host, local_name=phone.endpoint.name,
                local_uri_user=phone.identity, username=phone.account.username, password=phone.account.password,
                local_sip_port=0, local_rtp_port=phone.audio.getsockname()[1],
                supported_formats=[AudioFormat(16000, frame_ms=20)], codec_policy=CodecPolicy(audio=('L16',)))
            client.transport, client.protocol = await runtime.hass.loop.create_datagram_endpoint(
                lambda: SipDatagramQueueProtocol(client.queue), local_addr=(phone.host, 0))
            client.local_sip_port = client.transport.get_extra_info('sockname')[1]
            phone.client = client
            listener = ('127.0.0.1', runtime.runtime.transport_config['sip_port'])
            uri = f'sip:127.0.0.1:{listener[1]}'
            contact = str(sip.SipUri(phone.identity, phone.host, client.local_sip_port))

            def registration(sequence, authorization=''):
                headers = [('Via', f'SIP/2.0/UDP {phone.host}:{client.local_sip_port};branch=z9hG4bK-native-register-{sequence};rport'),
                    ('From', f'<sip:{phone.identity}@127.0.0.1>;tag=native-registration'),
                    ('To', f'<sip:{phone.identity}@127.0.0.1>'), ('Call-ID', 'register-'+phone.identity),
                    ('CSeq', f'{sequence} REGISTER'), ('Contact', '<'+contact+'>'), ('Expires', '120')]
                if authorization:
                    headers.append(('Authorization', authorization))
                client.transport.sendto(sip.build_request('REGISTER', uri, headers), listener)

            async def response(status):
                async with asyncio.timeout(2):
                    while True:
                        raw, address = await client.queue.get()
                        message = sip.parse_message(raw)
                        if message.method == 'OPTIONS':
                            client._send_response_to_request(message, *address, 200, 'OK')
                        elif message.status_code == status and message.header('CSeq').endswith(' REGISTER'):
                            return message

            registration(1)
            challenge = await response(401)
            authorization = build_digest_authorization(challenge_header=challenge.header('WWW-Authenticate'),
                username=phone.account.username, password=phone.account.password, method='REGISTER', uri=uri)
            registration(2, authorization)
            await response(200)
            registrar = runtime.calls.component('registrar')
            assert any(binding.source_host == phone.host and binding.source_port == client.local_sip_port
                       for binding in registrar.registered_contacts(phone.identity))
            row = next(row for row in registrar.roster_entries() if row.id == phone.identity)
            runtime.roster[:] = [item for item in runtime.roster if item.id != phone.identity] + [row]
            runtime.runtime.directory.replace(runtime.roster)
            return client

        phone.ensure_client = ensure_client

        async def emit(data, phone=phone):
            assert set(data) == {'dest', 'line', 'send_video', 'receive_video'}
            assert data['line'] == 'ha'
            phone.calling = True
            phone.pending = dict(data)
            if not phone.send_invite:
                return
            client = await phone.ensure_client()
            phone.pending = None

            async def dial():
                state = await client.invite(target=data['dest'], remote_host='127.0.0.1',
                    remote_sip_port=runtime.runtime.transport_config['sip_port'], timeout=3)
                phone.emitted_ids.append(client.dialog_ids.call_id)

                async def monitor():
                    try:
                        final = await client.wait_for_final() if state == 'ringing' else state
                        if final == 'in_call':
                            await client.wait_for_dialog_termination()
                    finally:
                        if phone.client is client:
                            phone.calling = False
                phone.monitors.append(asyncio.create_task(monitor()))
                return state

            phone.tasks.append(asyncio.create_task(dial()))

        phone.emit = emit

        async def originate(call, phone=phone):
            data = dict(call.data)
            phone.delivered.append(data)
            if phone.calling:
                phone.rejected.append(data)
                return
            assert not runtime.runtime.endpoints.require(phone.identity).active_call_id
            await phone.emit(data)

        async def stop(call, phone=phone):
            call_id = call.data.get('call_id') or (phone.client.dialog_ids.call_id if phone.client else '')
            if call.data.get('call_id') and (phone.client is None or call_id != phone.client.dialog_ids.call_id):
                return
            if phone.client is not None and call_id == phone.client.dialog_ids.call_id:
                phone.stopped.append(call_id)
                await phone.client.terminate()
            phone.pending = None
            phone.calling = False

        phone.start = originate
        phone.stop = stop

        async def ordinary(call, phone=phone):
            phone.ordinary_args.append(dict(call.data))
            await phone.start(SimpleNamespace(data={'dest': call.data['dest'], 'line': 'ha',
                'send_video': False, 'receive_video': False}))

        runtime.hass.services.async_register('esphome', route_id+'_start_call', ordinary)
        runtime.hass.services.async_register('esphome', route_id+'_start_call_on_line', originate)
        runtime.hass.services.async_register('esphome', route_id+'_hangup_call_if_id', stop)
        runtime.hass.services.async_register('esphome', route_id+'_hangup_call', stop)
        phones.append(phone)
        await phone.ensure_client()

    async def resolve(_hass, _call, *, selector):
        return next((phone.device for phone in phones if phone.endpoint.device_id == selector), None)
    monkeypatch.setattr(phone_control, 'async_resolve_source_device', resolve)
    yield phones
    for phone in phones:
        for gate in phone.gates:
            gate.set()
        if phone.client is not None:
            await phone.client.terminate()
            await phone.client.close()
        for task in (*phone.tasks, *phone.monitors, *phone.workers):
            if not task.done():
                task.cancel()
        await asyncio.gather(*phone.tasks, *phone.monitors, *phone.workers, return_exceptions=True)
        phone.audio.close()


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_two_esp_actions_use_independent_lines_and_real_duplex_rtp(trunks, esp_phones):
    runtime = trunks
    responses = await asyncio.gather(*(
        runtime.hass.services.async_call('voip_stack', 'call', {
            'device_id': phone.endpoint.device_id, 'destination': 'External', 'trunk_id': identity},
            blocking=True, return_response=True)
        for phone, identity in zip(esp_phones, ('home', 'work'), strict=True)))
    for index, (phone, response) in enumerate(zip(esp_phones, responses, strict=True)):
        client = await connected(phone)
        assert not (response.get('call') or {}).get('call_id')
        assert set(phone.delivered[0]) == {'dest', 'line', 'send_video', 'receive_video'}
        assert phone.delivered[0]['dest'] == f'trunk.{("home", "work")[index]}.External'
        assert phone.delivered[0]['line'] == 'ha'
        assert phone.delivered[0]['send_video'] is phone.delivered[0]['receive_video'] is False
        assert client.dialog_ids.call_id == phone.emitted_ids[0]
        session = runtime.calls.get_session(client.dialog_ids.call_id)
        assert session.metadata['source_endpoint_id'] == phone.identity
        assert session.metadata['outbound_trunk_id'] == ('home' if index == 0 else 'work')
        assert runtime.runtime.endpoints.require(phone.identity).policy.outbound_trunk_id == 'work'
        dialog = client.dialog
        packet = rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type, 1, 320, 901+index,
            struct.pack('!320h', *([2000+index*500]*320))))
        await runtime.hass.loop.sock_sendto(phone.audio, packet, (dialog.remote_rtp_host, dialog.remote_rtp_port))
        received, _ = await asyncio.wait_for(runtime.phones[index].audio.get(), 3)
        assert set(struct.unpack('!320h', rtp.parse_packet(received).payload)) == {2000+index*500}
        peer_port = int(re.search(rb'm=audio (\d+)', runtime.phones[index].invites[0].body)[1])
        packet = rtp.build_packet(rtp.RtpPacket(runtime.phones[index].payload_type, 1, 320, 951+index,
            struct.pack('!320h', *([3000+index*500]*320))))
        await runtime.hass.loop.sock_sendto(runtime.phones[index].rtp_socket, packet, ('127.0.0.1', peer_port))
        received = rtp.parse_packet(await asyncio.wait_for(runtime.hass.loop.sock_recv(phone.audio, 4096), 3))
        assert set(struct.unpack('!320h', received.payload)) == {3000+index*500}
    assert esp_phones[0].emitted_ids[0] != esp_phones[1].emitted_ids[0]
    for phone in esp_phones:
        await phone.client.terminate()
    await idle(runtime)
    assert all(client.ready for client in sip_trunks(runtime.hass).values())
    phone = esp_phones[0]
    await phone.client.close()
    await runtime.hass.services.async_call('voip_stack', 'call', {
        'device_id': phone.endpoint.device_id, 'destination': 'External'}, blocking=True)
    await connected(phone)
    assert len(runtime.phones[0].invites) == 1 and len(runtime.phones[1].invites) == 2
    assert runtime.calls.get_session(phone.client.dialog_ids.call_id).metadata['outbound_trunk_id'] == 'work'
    await phone.client.terminate()
    await idle(runtime)


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('failure', ['timeout', 'cancel'])
async def test_cancelled_command_never_reserves_or_rejects_a_late_registered_invite(trunks, esp_phones, failure):
    phone = esp_phones[0]
    entered, release = asyncio.Event(), asyncio.Event()
    phone.gates.append(release)
    async def delivered(call):
        phone.delivered.append(dict(call.data))
        entered.set()
        await release.wait()
    trunks.hass.services.async_register('esphome', phone.route_id+'_start_call_on_line', delivered)
    task = asyncio.create_task(trunks.hass.services.async_call('voip_stack', 'call', {
        'device_id': phone.endpoint.device_id, 'destination': 'External', 'trunk_id': 'home'}, blocking=True))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if failure == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, .1)
        assert not phone.stopped and not trunks.calls.sessions
        assert not trunks.runtime.endpoints.require(phone.identity).active_call_id
        release.set()
        await phone.emit(phone.delivered[0])
        client = await connected(phone)
        assert trunks.calls.get_session(client.dialog_ids.call_id).metadata['source_endpoint_id'] == phone.identity
        await client.terminate()
        await idle(trunks)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('failure', ['old_firmware', 'unavailable_line', 'invalid_target'])
async def test_invalid_esp_override_does_not_start_any_call(trunks, esp_phones, failure):
    phone = esp_phones[0]
    destination = 'External'
    if failure == 'old_firmware':
        trunks.hass.services.async_remove('esphome', phone.route_id+'_start_call_on_line')
    elif failure == 'unavailable_line':
        await sip_trunks(trunks.hass)['home'].stop()
    else:
        destination = 'bad\r\nHeader'
    with pytest.raises(ServiceValidationError):
        await trunks.hass.services.async_call('voip_stack', 'call', {
            'device_id': phone.endpoint.device_id, 'destination': destination, 'trunk_id': 'home'}, blocking=True)
    assert not phone.delivered and not trunks.calls.sessions
    assert not any(peer.invites for peer in trunks.phones)
    if failure == 'old_firmware':
        await trunks.hass.services.async_call('esphome', phone.route_id+'_start_call', {'dest': 'External'}, blocking=True)
        await connected(phone)
        assert phone.ordinary_args == [{'dest': 'External'}]
        assert len(trunks.phones[1].invites) == 1 and not trunks.phones[0].invites
        await phone.client.terminate()
        await idle(trunks)


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('mismatch', ['source', 'destination'])
async def test_actual_registered_invite_identity_and_destination_own_routing(trunks, esp_phones, mismatch):
    selected = esp_phones[0]
    sender = esp_phones[1] if mismatch == 'source' else selected
    async def changed(call):
        await sender.start(SimpleNamespace(data={**call.data,
            'dest': 'Other' if mismatch == 'destination' else call.data['dest']}))
    trunks.hass.services.async_register('esphome', selected.route_id+'_start_call_on_line', changed)
    await trunks.hass.services.async_call('voip_stack', 'call', {
        'device_id': selected.endpoint.device_id, 'destination': 'External', 'trunk_id': 'home'}, blocking=True)
    client = await connected(sender)
    session = trunks.calls.get_session(client.dialog_ids.call_id)
    assert session.metadata['source_endpoint_id'] == sender.identity
    assert session.metadata['outbound_trunk_id'] == ('work' if mismatch == 'destination' else 'home')
    if sender is not selected:
        assert not trunks.runtime.endpoints.require(selected.identity).active_call_id
    assert all(phone.endpoint.policy.outbound_trunk_id == 'work' for phone in esp_phones)
    await client.terminate()
    await idle(trunks)


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_cancelled_command_waiter_cannot_cancel_the_independent_sip_owner(trunks, esp_phones):
    phone, peer = esp_phones[0], trunks.phones[0]
    peer.auto_answer = False
    peer.answer_on_cancel = True
    entered, release = asyncio.Event(), asyncio.Event()
    phone.gates.append(release)
    async def delivered_but_pending(call):
        await phone.start(call)
        entered.set()
        await release.wait()
    trunks.hass.services.async_register('esphome', phone.route_id+'_start_call_on_line', delivered_but_pending)
    task = asyncio.create_task(trunks.hass.services.async_call('voip_stack', 'call', {
        'device_id': phone.endpoint.device_id, 'destination': 'External', 'trunk_id': 'home'}, blocking=True))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await phone.tasks[-1] == 'ringing'
        assert len(peer.invites) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not phone.stopped and trunks.calls.get_session(phone.client.dialog_ids.call_id).live
        await phone.client.terminate()
        await idle(trunks)
        methods = [request.method for request in peer.requests]
        assert 'CANCEL' in methods and 'ACK' in methods and 'BYE' in methods
        assert all(client.ready for client in sip_trunks(trunks.hass).values())
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_repeated_waiter_cancellation_preserves_scoped_native_stop(trunks, esp_phones):
    phone = esp_phones[0]
    await trunks.hass.services.async_call('voip_stack', 'call', {
        'device_id': phone.endpoint.device_id, 'destination': 'External', 'trunk_id': 'home'}, blocking=True)
    client = await connected(phone)
    call_id = client.dialog_ids.call_id
    entered, release = asyncio.Event(), asyncio.Event()
    phone.gates.append(release)
    async def gated_stop(call):
        assert call.data['call_id'] == call_id
        phone.stopped.append(call_id)
        async def firmware_stop():
            entered.set()
            await release.wait()
            await client.terminate()
        worker = asyncio.create_task(firmware_stop())
        phone.workers.append(worker)
        await asyncio.shield(worker)
    trunks.hass.services.async_register('esphome', phone.route_id+'_hangup_call_if_id', gated_stop)
    task = asyncio.create_task(trunks.hass.services.async_call('esphome', phone.route_id+'_hangup_call_if_id', {
        'call_id': call_id}, blocking=True))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert len(phone.workers) == 1 and not phone.workers[0].done()
        assert phone.stopped == [call_id]
        release.set()
        await asyncio.wait_for(phone.workers[0], 3)
        await asyncio.gather(task, return_exceptions=True)
        await idle(trunks)
        assert not trunks.runtime.endpoints.require(phone.identity).active_call_id
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_second_native_command_cannot_replace_the_firmware_pending_intent(trunks, esp_phones):
    phone = esp_phones[0]
    phone.send_invite = False
    await trunks.hass.services.async_call('voip_stack', 'call', {
        'device_id': phone.endpoint.device_id, 'destination': 'External', 'trunk_id': 'home'}, blocking=True)
    first = dict(phone.pending)
    assert not trunks.calls.sessions and not trunks.runtime.endpoints.require(phone.identity).active_call_id
    await trunks.hass.services.async_call('voip_stack', 'call', {
        'device_id': phone.endpoint.device_id, 'destination': 'External', 'trunk_id': 'work'}, blocking=True)
    assert len(phone.delivered) == 2 and phone.rejected == [phone.delivered[1]]
    assert phone.pending == first and not phone.stopped
    assert not trunks.calls.sessions
    phone.send_invite = True
    await phone.emit(first)
    client = await connected(phone)
    assert len(trunks.phones[0].invites) == 1 and not trunks.phones[1].invites
    assert trunks.calls.get_session(client.dialog_ids.call_id).metadata['outbound_trunk_id'] == 'home'
    await client.terminate()
    await idle(trunks)
