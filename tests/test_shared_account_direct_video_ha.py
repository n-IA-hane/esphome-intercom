"""Direct trunk video retains its explicit media contract when the HA fork wins."""
import asyncio
import secrets

import pytest

from custom_components.voip_stack.core import sip, sdp
from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.trunk_runtime import trunk_client_config
from custom_components.voip_stack.store import config_entry
from tests.test_shared_account_fork_ha import (
    shared_account as shared_account, canonical_browser as canonical_browser,
    logical_browser as logical_browser, registered_phones as registered_phones,
    trunks as trunks, lab as lab, assert_no_active_calls,
)
from tests.test_browser_logical_availability_ha import open_browser_ws, browser_snapshot, resources

pytestmark = pytest.mark.ha


class Provider(asyncio.DatagramProtocol):
    def __init__(self):
        self.responses = asyncio.Queue()
        self.requests = []
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, data, address):
        message = sip.parse_message(data)
        if message.is_response:
            self.responses.put_nowait(message)
        else:
            self.requests.append(message)
            headers = [(name,message.header(name)) for name in ('Via','From','To','Call-ID','CSeq')]
            self.transport.sendto(sip.build_response(200,'OK',headers),address)


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize('logical_browser',[{'ringing_timeout':8}],indirect=True)
@pytest.mark.parametrize(('send_video','failure'),[(False,None),(True,None),(True,'build'),(True,'contract')])
async def test_direct_trunk_browser_fork_answer_video(shared_account,hass_ws_client,monkeypatch,send_video,failure):
    runtime, _, peers = shared_account
    endpoint_id = runtime.browser_endpoint_id
    entry=config_entry(runtime.hass)
    settings={'experimental_sip_video':True,'video_camera_send_enabled':True}
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,**settings})
    runtime.runtime.transport_config.update(settings)
    # This fixture starts audio-only; expose the enabled capability as a real
    # config refresh does before testing an offered video contract.
    endpoint = runtime.runtime.endpoints.require(endpoint_id)
    runtime.runtime.endpoints.update(endpoint_id, capabilities=endpoint.capabilities | {'video'})
    for endpoint in runtime.calls.component("udp_listener")._dialog_endpoints():
        endpoint.enable_video = True
    cfg=normalized_trunk_config({"trunk_options_interval": 0, 'trunk_enabled':True,'trunk_register':False,
        'trunk_server':'127.0.0.2','trunk_allowed_ips':['127.0.0.2/32'],
        'trunk_inbound_mode':'direct','trunk_inbound_default_target':'Casa'})
    runtime.runtime.trunk_configs['provider-video']=cfg
    port=runtime.runtime.transport_config['sip_port']
    trunk=SipTrunkClient(config=trunk_client_config(cfg),local_ip='127.0.0.1',local_sip_port=port,trunk_id='provider-video')
    runtime.calls.adopt_component('trunk:provider-video',trunk,closer=trunk.stop)
    from custom_components.voip_stack import media_ports
    prepared = []
    original_reserve = media_ports.reserve_sip_video_media
    def reserve(*args, **kwargs):
        owned = original_reserve(*args, **kwargs)
        prepared.append(owned)
        return owned
    monkeypatch.setattr(media_ports, 'reserve_sip_video_media', reserve)
    observer=await open_browser_ws(runtime,hass_ws_client)
    transport,provider=await runtime.hass.loop.create_datagram_endpoint(Provider,local_addr=('127.0.0.2',0))
    provider_port=transport.get_extra_info('sockname')[1]
    identity='direct-video-'+secrets.token_hex(6)
    target=f'sip:450@127.0.0.1:{port}'
    headers=[('Via',f'SIP/2.0/UDP 127.0.0.2:{provider_port};branch=z9hG4bK-{identity};rport'),
        ('From','<sip:427@127.0.0.2>;tag=provider'),('To','<sip:450@127.0.0.1>'),
        ('Contact',f'<sip:427@127.0.0.2:{provider_port}>'),('Call-ID',identity),('CSeq','1 INVITE'),('Content-Type','application/sdp')]
    body=('v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Provider\r\nc=IN IP4 127.0.0.2\r\nt=0 0\r\n'
        'm=audio 24000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=sendrecv\r\n'
        'm=video 24002 RTP/AVP 103\r\na=rtpmap:103 H264/90000\r\na=fmtp:103 profile-level-id=42801f;packetization-mode=1\r\na=sendrecv\r\n').encode()
    transport.sendto(sip.build_request('INVITE',target,headers,body),('127.0.0.1',port))
    try:
        async with asyncio.timeout(4):
            while (await browser_snapshot(observer))['state']!='ringing':
                await asyncio.sleep(.01)
        for peer in peers:
            await asyncio.to_thread(peer.wait_for,'Incoming call',3)
        if failure:
            def rejected(*_args, **_kwargs):
                raise ValueError('injected answer preparation failure')
            from custom_components.voip_stack import ring_group_orchestrator, sip_server_state
            monkeypatch.setattr(ring_group_orchestrator if failure == 'build' else sip_server_state,
                'build_answer_directional' if failure == 'build' else 'answered_media_contract', rejected)
        await runtime.hass.services.async_call('voip_stack','answer',{'device_id':'browser-casa',
            'call_id':identity,'media_client_id':'video-card','send_video':send_video},blocking=True)
        async with asyncio.timeout(4):
            while True:
                response=await provider.responses.get()
                if response.status_code>=200:
                    break
        if failure:
            assert response.status_code >= 300, response
            async with asyncio.timeout(3):
                while not resources(runtime)['call_scoped_quiescent']:
                    await asyncio.sleep(.01)
            await assert_no_active_calls(peers)
            assert prepared, 'Failure injection must occur after real media reservation'
            for reservation, video_socket, rtcp_socket in prepared:
                assert video_socket.fileno() == rtcp_socket.fileno() == -1
                assert not set(reservation.ports) & set(runtime.runtime.rtp_port_pool.get('used', ()))
            return
        assert response.status_code==200, response
        _connection,_direction,sections=sdp.parse_media_sections(response.body)
        video=next(section for section in sections if section['media']=='video')
        assert video['port']>0 and video['direction']==('sendrecv' if send_video else 'recvonly')
        ack=[(name,response.header(name)) for name in ('From','To','Call-ID')]
        ack += [('Via',f'SIP/2.0/UDP 127.0.0.2:{provider_port};branch=z9hG4bK-ack-{identity}'),('CSeq','1 ACK')]
        transport.sendto(sip.build_request('ACK',target,ack),('127.0.0.1',port))
        async with asyncio.timeout(3):
            while (state:=await browser_snapshot(observer))['state']!='in_call':
                await asyncio.sleep(.01)
        assert state['video_active'] is True
        assert state['video_direction']==video['direction']
        media=runtime.calls.resource_for(identity,'softphone_media')
        assert media['local_video_rtp_port']==video['port']
        assert media['camera_send_authorized'] is send_video
        from custom_components.voip_stack.video_ws_view import _active_video_session
        resolved = _active_video_session(runtime.hass, endpoint_id)
        assert resolved is not None and resolved.can_receive
        assert resolved.can_send is send_video
        assert resolved.rtp_socket is media['video_rtp_socket']
        assert resolved.recv_video_format.encoding == 'H264'
        await assert_no_active_calls(peers)
    finally:
        session = runtime.calls.get_session(identity)
        if session is not None and session.live:
            await runtime.hass.services.async_call('voip_stack','hangup',{
                'device_id':'browser-casa','call_id':identity},blocking=True)
        await observer.close()
        transport.close()
    assert resources(runtime)['call_scoped_quiescent']
