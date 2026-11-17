"""Actual registered phone -> HA -> UDP registered trunk upgraded to TCP."""
import asyncio

import pytest

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_endpoint import SipEndpointManager
from custom_components.voip_stack.sip_listener import SipInviteResult
from custom_components.voip_stack.runtime_data import sip_trunks
from custom_components.voip_stack.trunk_runtime import async_start_sip_trunk, async_stop_sip_trunk
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_registered_phone_trunk_auth_growth_remote_tcp_bye(trunks, registered_phones, monkeypatch):
    runtime = trunks
    provider = runtime.phones[1]
    port = provider.transport.get_extra_info('sockname')[1]
    audio_port = provider.rtp_socket.getsockname()[1]
    challenges, invites = [], []
    original = provider.datagram_received

    def receive(raw, address):
        request = sip.parse_message(raw)
        if request.method != 'INVITE':
            return original(raw, address)
        challenges.append(request)
        provider.transport.sendto(sip.build_uas_response(request, 401, 'Unauthorized', to_tag='provider',
            extra_headers=(('WWW-Authenticate', 'Digest realm="test", nonce="' + 'n' * 400 + '", qop="auth"'),)), address)

    monkeypatch.setattr(provider, 'datagram_received', receive)

    async def accept(invite):
        invites.append(invite)
        answer = sdp.build_answer_directional('127.0.0.1', '127.0.0.1', audio_port,
            invite.send_format, invite.recv_format, remote_sdp=invite.remote_sdp)
        return SipInviteResult(200, 'OK', answer_sdp=answer)

    server = SipEndpointManager(host='127.0.0.1', port=port, local_ip='127.0.0.1',
        local_rtp_port=audio_port, supported_formats=[AudioFormat(16000, 's16le', 1, 20)],
        on_invite=accept, udp_enabled=False, tcp_enabled=True)
    assert await server.start()
    runtime.runtime.trunk_configs = {'work': normalized_trunk_config({'trunk_enabled': True,
        'trunk_register': True, 'trunk_server': '127.0.0.1', 'trunk_port': port,
        'trunk_username': 'work', 'trunk_password': 'test-only'})}
    runtime.runtime.trunk_config = runtime.runtime.trunk_configs['work']
    runtime.runtime.default_trunk_id = 'work'
    runtime.runtime.endpoints.update('transfer_a', policy=PhonePolicy(outbound_trunk_id='work'))
    runtime.roster.append(RosterEntry('External', number='12345'))
    caller = registered_phones[0]
    try:
        await async_start_sip_trunk(runtime.hass, local_ip='127.0.0.1')
        assert await sip_trunks(runtime.hass)['work'].wait_for_registration(timeout=2)
        await asyncio.to_thread(caller.command, '/dial External')
        await asyncio.to_thread(caller.wait_for, 'Call established', 5)
        async with asyncio.timeout(3):
            while not invites or not server.active_dialog_count():
                await asyncio.sleep(0)
        assert len(challenges) == 1 and challenges[0].header('CSeq') == '1 INVITE'
        assert len(invites) == 1 and invites[0].signaling_transport == 'TCP' and invites[0].cseq == '2 INVITE'
        assert challenges[0].header('Call-ID') == invites[0].call_id
        session = next(iter(runtime.calls.sessions.values()))
        assert session.metadata['outbound_trunk_id'] == 'work'
        packet, _ = await asyncio.wait_for(provider.audio.get(), 3)
        assert any(rtp.parse_packet(packet).payload)
        before = caller.read()
        assert await server.async_send_bye(invites[0].call_id)
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = runtime.calls.request_termination(session.call_id, session.termination_intent)
        if barrier:
            await asyncio.wait_for(barrier, 3)
        async with asyncio.timeout(3):
            while 'session closed' not in caller.read()[len(before):].lower():
                await asyncio.sleep(.01)
        assert not runtime.calls.sessions and not runtime.runtime.rtp_port_pool['used']
        assert server.active_dialog_count() == 0
        assert sip_trunks(runtime.hass)['work'].ready
    finally:
        await async_stop_sip_trunk(runtime.hass)
        await server.stop()
