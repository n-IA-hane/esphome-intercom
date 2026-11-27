"""Linphone-style ordinary REGISTER over real UDP/TCP with independent Digest."""
from __future__ import annotations

from .support.sip_policy import configured_registrar_policy

import asyncio
import hashlib
import re
import socket
from urllib.parse import quote

import pytest

from .voip_phase1_support import (
    _reserved_udp_ports,
    audio_format,
    sip_endpoint,
    sip_listener,
    sip_registrar,
)


def _response(raw: bytes) -> tuple[int, dict[str, str]]:
    lines = raw.decode().split('\r\n')
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        key, value = line.split(':', 1)
        headers[key.lower()] = value.strip()
    return int(lines[0].split()[1]), headers


def _digest(challenge: str, uri: str, username: str, password: str, *, nonce_count: int = 1) -> str:
    # The peer deliberately does not use the component's Digest implementation.
    params = {key.lower(): quoted or token for key, quoted, token in
              re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', challenge)}
    assert params.get('algorithm', 'MD5').upper() == 'MD5'
    realm, nonce = params['realm'], params['nonce']
    def md5(value: str) -> str:
        return hashlib.md5(value.encode()).hexdigest()

    ha1, ha2 = md5(f'{username}:{realm}:{password}'), md5(f'REGISTER:{uri}')
    count = f'{nonce_count:08x}'
    value = md5(f'{ha1}:{nonce}:{count}:independent:auth:{ha2}')
    assert 'auth' in params.get('qop', 'auth').split(',')
    escaped = username.replace('\\', '\\\\').replace('"', '\\"')
    return (f'Digest username="{escaped}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{value}", algorithm=MD5, '
            f'qop=auth, nc={count}, cnonce="independent"')


@pytest.mark.parametrize('transport', ['UDP', 'TCP'])
@pytest.mark.parametrize('supported', ['outbound', 'outbound, gruu'])
def test_instance_without_reg_id_registers_on_real_flow(transport, supported):
    account = sip_registrar.SipAccount('Phone', 'Phone', 'wire-test-password')
    asyncio.run(check_account_registration(transport, supported, account))


async def check_account_registration(transport: str, supported: str, account) -> None:
    local = '127.0.0.1'
    with _reserved_udp_ports(2) as ports:
        sip_port, rtp_port = ports
    registrar = sip_registrar.SipRegistrar(policy=configured_registrar_policy(),
        enabled=True,
        accounts=[account],
        local_ip=local, local_sip_port=sip_port,
    )

    async def unexpected_invite(_invite):
        raise AssertionError('REGISTER must not create an INVITE')

    manager = sip_endpoint.SipEndpointManager(
        host=local, port=sip_port, local_ip=local, local_rtp_port=rtp_port,
        supported_formats=[audio_format.AudioFormat(16000, 's16le', 1, 20)],
        on_invite=unexpected_invite, on_register=registrar.handle_register,
        udp_enabled=transport == 'UDP', tcp_enabled=transport == 'TCP',
    )
    assert await manager.start()
    writer = None
    sock = None
    try:
        if transport == 'TCP':
            reader, writer = await asyncio.open_connection(local, sip_port)
            source_port = writer.get_extra_info('sockname')[1]
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.bind((local, 0))
            source_port = sock.getsockname()[1]
        uri = f'sip:{local}:{sip_port}'
        user = quote(account.username, safe='')
        instance = 'urn:uuid:12345678-1234-5678-1234-567812345678'

        async def exchange(cseq: int, authorization: str = ''):
            # Raw SIP text is independent of sip.build_request and parse_message.
            lines = [
                f'REGISTER {uri} SIP/2.0',
                f'Via: SIP/2.0/{transport} {local}:{source_port};branch=z9hG4bK-wire-{cseq};rport',
                f'From: <sip:{user}@{local}>;tag=independent',
                f'To: <sip:{user}@{local}>',
                'Call-ID: linphone-independent-registration',
                f'CSeq: {cseq} REGISTER',
                f'Contact: <sip:{user}@10.55.0.5:5060;transport={transport.lower()}>;expires=120;+sip.instance="<{instance}>"',
                f'Supported: {supported}',
                'User-Agent: Independent Linphone-style test peer',
            ]
            if authorization:
                lines.append('Authorization: '+authorization)
            raw = ('\r\n'.join(lines)+'\r\nContent-Length: 0\r\n\r\n').encode()
            if writer is not None:
                writer.write(raw)
                await writer.drain()
                reply = await asyncio.wait_for(sip_listener._read_sip_stream_message(reader), 2)
                assert reply is not None
            else:
                loop = asyncio.get_running_loop()
                await loop.sock_sendto(sock, raw, (local, sip_port))
                reply = await asyncio.wait_for(loop.sock_recv(sock, 8192), 2)
            return _response(reply)

        status, headers = await exchange(1)
        assert status == 401
        authorization = _digest(headers['www-authenticate'], uri, account.username, account.password)
        status, headers = await exchange(2, authorization)
        assert status == 200
        binding, = registrar.registered_contacts(account.username)
        assert binding.username == account.username
        assert not binding.outbound and binding.reg_id == 0
        assert (binding.source_host, binding.source_port, binding.transport) == (local, source_port, transport)
        assert f'{local}:{source_port}' in binding.contact_uri
        roster = registrar.roster_entries()
        assert len(roster) == 1 and roster[0].sip_uri == binding.contact_uri
        assert 'outbound' not in headers.get('require', '').lower()
        assert 'flow-timer' not in headers
        if 'gruu' in supported:
            assert binding.instance_id == instance and binding.public_gruu and binding.temporary_gruu
            assert 'pub-gruu=' in headers['contact'] and 'temp-gruu=' in headers['contact']
        else:
            assert binding.instance_id == '' and not binding.public_gruu
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        if sock is not None:
            sock.close()
        await manager.stop()
