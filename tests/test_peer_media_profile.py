"""Contact compatibility choices preserve real SIP identity and offer mappings."""

import asyncio

import pytest

from .voip_phase1_support import _load_intercom_module, audio_format, sdp, sip, sip_client

resolve = _load_intercom_module('peer_media_profile').resolve_peer_media_profile


@pytest.mark.parametrize('metadata,override,expected', [
    ({}, None, (False, False, False)),
    ({'user_agent': 'Dahua UAC/1.0'}, None, (True, True, False)),
    ({'sip_profile': 'dahua'}, None, (True, False, True)),
    ({'sip_profile': 'dahua', 'dahua_audio': 'pcm'}, None, (True, True, True)),
    ({'sip_profile': 'dahua', 'dahua_audio': 'standard', 'user_agent': 'Dahua UAC/1.0'}, None, (True, False, True)),
    ({'registered': True, 'sip_profile': 'dahua'}, None, (True, False, False)),
    ({'registered': True, 'sip_profile': 'dahua', 'user_agent': 'Dahua UAC/1.0'}, 'Zoiper', (False, False, False)),
    ({'registered': True, 'sip_profile': 'dahua'}, '', (False, False, False)),
    ({'registered': True, 'user_agent': 'Zoiper'}, 'Dahua UAC/2.0', (True, True, False)),
])
def test_profile_and_pcm_choices(metadata, override, expected):
    profile = resolve(metadata, override)
    assert (profile.is_dahua, profile.include_dahua_pcm, profile.explicit_dahua) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('user_agent,override,vendor', [
    ('Dahua UAC/1.0', None, True),
    ('Dahua UAC/1.0', False, False),
    ('', True, True),
    ('', None, False),
])
async def test_client_wire_offer_explicit_pcm_without_forging_identity(user_agent, override, vendor):
    offers = []
    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
        def datagram_received(self, raw, addr):
            request = sip.parse_message(raw)
            if request.method != 'INVITE':
                return
            offers.append(request.body)
            headers = [(key, request.header(key)) for key in ('Via', 'From', 'Call-ID', 'CSeq')]
            headers.append(('To', request.header('To') + ';tag=peer'))
            self.transport.sendto(sip.build_response(486, 'Busy Here', headers), addr)
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(Peer, local_addr=('127.0.0.1', 0))
    client = sip_client.SipCallClient(
        local_ip='127.0.0.1', local_name='HA', local_sip_port=0, local_rtp_port=40000,
        supported_formats=list(audio_format.HA_TRUNK_AUDIO_FORMATS), include_common_codecs=True,
        peer_user_agent=user_agent, include_dahua_pcm=override,
    )
    try:
        await client.invite(target='door', remote_host='127.0.0.1', remote_sip_port=transport.get_extra_info('sockname')[1], timeout=1)
        assert client.peer_user_agent == user_agent
        formats = sdp.offered_pcm_formats(offers[0], allow_dahua_pcm=True)
        assert any(f.encoding == 'PCM' for f in formats) == vendor
        assert any(f.encoding == 'PCMU' for f in formats)
        assert all(f.frame_ms == 20 for f in formats)
        if not vendor:
            assert all(f.encoding != 'PCM' for f in formats)
    finally:
        await client.close()
        transport.close()
