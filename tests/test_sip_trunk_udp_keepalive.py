"""Registered UDP flows retain NAT reachability without sharing call ownership."""

import asyncio
import pytest
from .voip_phase1_support import sip, sip_trunk


@pytest.mark.asyncio
@pytest.mark.parametrize('nat', [False, True])
async def test_registered_udp_flow_maintains_nat_and_stops_cleanly(monkeypatch, nat):
    monkeypatch.setattr(sip_trunk, '_UDP_KEEPALIVE_INTERVAL', 0.02)
    loop = asyncio.get_running_loop()
    packets = []
    errors = []
    last_activity = 0.0
    drop_options = False

    class Registrar(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            nonlocal last_activity
            try:
                request = sip.parse_message(raw)
                packets.append((request, address))
                last_activity = loop.time()
                assert request.method in {'REGISTER', 'OPTIONS'}
                if request.method == 'OPTIONS' and drop_options:
                    return
                headers = [(name, request.header(name)) for name in ('Via', 'From', 'To', 'Call-ID', 'CSeq')]
                if nat:
                    headers[0] = ('Via', headers[0][1].replace(';rport', f';rport={address[1]};received=198.51.100.20'))
                headers.append(('Expires', request.header('Expires') or '300'))
                self.transport.sendto(sip.build_response(200, 'OK', headers), address)
            except BaseException as error:
                errors.append(error)

    transport, peer = await loop.create_datagram_endpoint(Registrar, local_addr=('127.0.0.1', 0))
    client = sip_trunk.SipTrunkClient(
        config=sip_trunk.SipTrunkConfig(options_interval=30, enabled=True, transport='udp', server='127.0.0.1',
            port=transport.get_extra_info('sockname')[1], domain='test', username='line',
            auth_username='', password='', expires=300),
        local_ip='127.0.0.1', local_sip_port=5060,
    )
    received = asyncio.Queue()
    async def on_request(raw, _address):
        received.put_nowait(sip.parse_message(raw))
    client.set_request_handler(on_request)
    try:
        await client.start()
        assert await client.wait_for_registration(timeout=1)
        binding = packets[0][1]
        await asyncio.sleep(0.16)
        options = [request for request, _address in packets if request.method == 'OPTIONS']
        assert bool(options) is nat
        if nat:
            assert loop.time() - last_activity < 0.08
            assert len(options) >= 3
            assert all(address == binding for _request, address in packets)
            assert all(request.header('Call-ID') != client.call_id for request in options)
            assert not client._outbound_dialogs
            invite = sip.build_request('INVITE', 'sip:line@127.0.0.1', [
                ('Via', 'SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK-nat'),
                ('From', '<sip:peer@test>;tag=peer'), ('To', '<sip:line@test>'),
                ('Call-ID', 'incoming-after-idle'), ('CSeq', '1 INVITE'),
            ], b'')
            peer.transport.sendto(invite, binding)
            assert (await asyncio.wait_for(received.get(), 1)).method == 'INVITE'
            assert await client.register(timeout=1) == 'registered'
            drop_options = True
            await asyncio.sleep(0.03)
        else:
            assert client._keepalive_task is None
        await asyncio.wait_for(client.stop(), 1)
        assert client._keepalive_task is None
        assert client._receive_task is None
        assert client.transport is None
        assert packets[-1][0].method == 'REGISTER'
        assert packets[-1][0].header('Expires') == '0'
        count = len(packets)
        await asyncio.sleep(0.05)
        assert len(packets) == count
        assert not errors, errors
    finally:
        await client.stop()
        transport.close()
