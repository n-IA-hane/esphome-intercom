"""A registered trunk's20ms packet survives the public registered-phone bridge."""
import asyncio
from dataclasses import asdict
import json
import re
import socket
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_routing import roster_from_peers
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.runtime_data import sip_trunks
from .test_contact_audio_routing_ha import ContactPeer
from .test_sip_capabilities_ha import register_peer, wait_until
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_inbound_receive_packet_budget import AST_OFFER

pytestmark = pytest.mark.ha


class DirectionalPhone(ContactPeer):
    def answer(self, request, address, status=200, body=b'', content_type='application/sdp'):
        port = self.transport.get_extra_info('sockname')[1]
        self.transport.sendto(sip.build_uas_response(request, status, 'OK' if status==200 else 'Ringing',
            to_tag='bedroom', contact_uri=f'sip:caps@127.0.0.1:{port}', body=body,
            extra_headers=(('Content-Type', content_type),) if body else ()), address)

    def finish(self):
        request, address = self.offers[0]
        rx = re.search(rb'a=rtpmap:(\d+) L16/48000/1', request.body)[1].decode()
        tx = re.search(rb'a=rtpmap:(\d+) L16/16000/1', request.body)[1].decode()
        self.payload_type = int(rx)
        body = ('v=0\r\no=- 4 4 IN IP4 127.0.0.1\r\ns=D1phone\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n'
            f'm=audio {self.rtp_socket.getsockname()[1]} RTP/AVP {rx} {tx}\r\n'
            f'a=rtpmap:{rx} L16/48000/1\r\na=x-voip-stack-flow:{rx} recv\r\n'
            f'a=rtpmap:{tx} L16/16000/1\r\na=x-voip-stack-flow:{tx} send\r\n'
            'a=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\n').encode()
        self.answer(request,address,body=body)


@pytest.mark.parametrize('trunks',[True],indirect=True)
async def test_registered_trunk20ms_reframes_to_registered_phone10ms(trunks,monkeypatch,tmp_path):
    from custom_components.voip_stack.inbound_routing import bridge
    runtime = trunks
    registrar = runtime.calls.component('registrar')
    registrar.enabled = True
    registrar.update_accounts([SipAccount('caps','Caps','fixture-only')])
    runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id='caps',name='Caps',username='caps',kind=EndpointKind.SIP_ACCOUNT))
    await sip_trunks(runtime.hass)['work'].stop()
    runtime.runtime.trunk_configs.pop('work')
    runtime.runtime.trunk_configs['home'].update(trunk_inbound_mode='direct',trunk_inbound_default_target='caps')
    target_audio=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    target_audio.bind(('127.0.0.1',0))
    target_audio.setblocking(False)
    transport,phone=await runtime.hass.loop.create_datagram_endpoint(lambda:DirectionalPhone('known',target_audio),local_addr=('127.0.0.1',0))
    provider=runtime.phones[0]
    caller=SipCallClient(local_ip='127.0.0.1',local_name='AstPeer',local_sip_port=provider.transport.get_extra_info('sockname')[1],
        local_rtp_port=provider.rtp_socket.getsockname()[1],supported_formats=[AudioFormat(48000,'s16le',1,10)])
    observed={}
    responses=asyncio.Queue()
    original_receive=provider.datagram_received
    original_build=caller._build_pending_invite
    original_select=bridge.bridge_source_audio
    def receive(raw,address):
        message=sip.parse_message(raw)
        if message.header('Call-ID')==caller.dialog_ids.call_id:
            if message.status_code==200 and message.body:
                observed['answer']=message.body.decode()
            responses.put_nowait(raw)
        else:
            original_receive(raw,address)
    monkeypatch.setattr(provider,'datagram_received',receive)
    def build():
        body=AST_OFFER.replace('192.0.2.231','127.0.0.1').replace('m=audio 18030 ',f'm=audio {provider.rtp_socket.getsockname()[1]} ')
        caller._pending_invite_body=body.encode()
        caller._local_sdp_body=body
        return original_build()
    monkeypatch.setattr(caller,'_build_pending_invite',build)
    def select(invite,dialog):
        observed['source_before']=asdict(invite.recv_format)
        result=original_select(invite,dialog)
        observed['source_after']=asdict(result.recv_format)
        return result
    monkeypatch.setattr(bridge,'bridge_source_audio',select)
    caller.use_reused_signaling_flow(send=lambda raw:provider.transport.sendto(raw,('127.0.0.1',runtime.runtime.transport_config['sip_port'])),
                                    responses=responses,close=lambda:None)
    session=None
    try:
        await register_peer(runtime,phone)
        await asyncio.wait_for(phone.options.get(),2)
        await wait_until(lambda:registrar.registered_contacts('caps')[0].capabilities is not None)
        runtime.roster.extend(roster_from_peers(runtime.hass,[],registrar.registered_roster_entries()))
        assert sip_trunks(runtime.hass)['home'].ready
        assert await caller.invite(target='caps',remote_host='127.0.0.1',remote_sip_port=runtime.runtime.transport_config['sip_port'],timeout=3)=='ringing'
        await wait_until(lambda:bool(phone.offers))
        phone.finish()
        assert await caller.wait_for_final(timeout=3)=='in_call'
        session=runtime.calls.get_session(caller.dialog_ids.call_id)
        assert session.metadata['inbound_trunk_id']=='home'
        relay=runtime.calls.resource_for(session.call_id,'relay')
        observed['relay_receiver']=asdict(relay.left.inbound_rtp_format)
        receive_limit=HA_AUDIO_CAPABILITIES.max_receive_packet_ms(relay.left.inbound_rtp_format)
        assert receive_limit==128
        values=tuple(range(-480,480))
        payload=struct.pack('!960h',*values)
        packet=rtp.build_packet(rtp.RtpPacket(126,123,24000,9876,payload[:960]))[:12]+payload
        await runtime.hass.loop.sock_sendto(provider.rtp_socket,packet,(caller.dialog.remote_rtp_host,caller.dialog.remote_rtp_port))
        first=rtp.parse_packet(await asyncio.wait_for(runtime.hass.loop.sock_recv(target_audio,4096),1))
        second=rtp.parse_packet(await asyncio.wait_for(runtime.hass.loop.sock_recv(target_audio,4096),1))
        assert len(first.payload)==len(second.payload)==960
        assert first.payload_type==second.payload_type==phone.payload_type
        assert (second.sequence-first.sequence)&65535==1
        assert (second.timestamp-first.timestamp)&0xffffffff==480
        assert first.payload+second.payload==payload
        assert sdp.parse_sdp(observed['answer'])['maxptime']==receive_limit
        assert observed['source_before']['max_frame_ms']==observed['source_after']['max_frame_ms']==receive_limit
        assert observed['relay_receiver']['max_frame_ms']==receive_limit
        from custom_components.voip_stack.runtime_data import sip_endpoint_manager
        from custom_components.voip_stack.core.sip_dialog import build_dialog_request
        manager=sip_endpoint_manager(runtime.hass)
        owner=next(endpoint for endpoint in manager._dialog_endpoints() if session.call_id in endpoint.active_dialogs)
        await wait_until(lambda:not owner.active_dialogs[session.call_id].invite_2xx.active)
        original_capabilities=owner.invite_for_call(session.call_id).local_recv_capabilities
        reoffer=sdp.rewrite_sdp_origin(caller._local_sdp_body,1725022972,1725022973).encode()
        response=await caller._send_in_dialog_request('INVITE',body=reoffer,content_type='application/sdp',timeout=2)
        assert response.status_code==200
        dialog=caller.dialog
        ack=build_dialog_request('ACK',call_id=dialog.call_id,local_tag=caller.dialog_ids.local_tag,
            remote_tag=dialog.remote_tag,cseq=sip.parse_cseq(response.header('CSeq')).number,
            local_uri=dialog.local_uri,remote_uri=dialog.remote_uri,remote_target_uri=dialog.remote_target_uri)
        provider.transport.sendto(ack.raw,('127.0.0.1',runtime.runtime.transport_config['sip_port']))
        await wait_until(lambda:not owner.active_dialogs[session.call_id].invite_2xx.active)
        updated=owner.invite_for_call(session.call_id)
        assert updated.local_recv_capabilities==original_capabilities and updated.recv_format.max_frame_ms==receive_limit
        assert relay.left.inbound_rtp_format.max_frame_ms==receive_limit
        assert relay.right.outbound_rtp_format.frame_ms==10 and relay.right.outbound_rtp_format.max_frame_ms==10
        again=rtp.build_packet(rtp.RtpPacket(126,124,24960,9876,payload[:960]))[:12]+payload
        await runtime.hass.loop.sock_sendto(provider.rtp_socket,again,(caller.dialog.remote_rtp_host,caller.dialog.remote_rtp_port))
        resumed=[rtp.parse_packet(await asyncio.wait_for(runtime.hass.loop.sock_recv(target_audio,4096),1)) for _ in range(2)]
        assert [len(item.payload) for item in resumed]==[960,960]
        assert b''.join(item.payload for item in resumed)==payload
        from custom_components.voip_stack.parking import park_established_sip_call,get_parking_lot
        from custom_components.voip_stack.store import config_entry
        entry=config_entry(runtime.hass)
        runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
        parked=await park_established_sip_call(runtime.hass,session.call_id,'caps')
        assert parked.invite.local_recv_capabilities==original_capabilities
        assert parked.invite.recv_format.max_frame_ms==receive_limit
        assert get_parking_lot(runtime.hass).calls
        await caller.terminate()
        await wait_until(lambda:not runtime.calls.sessions)
        assert not get_parking_lot(runtime.hass).calls
        assert not runtime.runtime.rtp_port_pool['used']
    finally:
        (tmp_path/'consumer-contract.json').write_text(json.dumps(observed,indent=2))
        await caller.terminate()
        await caller.close()
        transport.close()
        target_audio.close()


def test_invite_receive_capabilities_are_local_and_survive_replacement():
    from dataclasses import replace
    from types import SimpleNamespace
    from custom_components.voip_stack.sip_listener import SipUdpEndpoint
    from custom_components.voip_stack.sip_bridge import bridge_source_audio
    local=(AudioFormat(48000,'s16le',1,10),)
    endpoint=SipUdpEndpoint(local_ip='127.0.0.1',local_sip_port=5060,local_rtp_port=40000,
        supported_formats=list(local),supported_recv_formats=list(local),on_invite=None)
    request=sip.parse_message(sip.build_request('INVITE','sip:phone@127.0.0.1',[
        ('Via','SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bK-local'),
        ('From','<sip:peer@127.0.0.2>;tag=source'),('To','<sip:phone@127.0.0.1>'),
        ('Call-ID','local-budget'),('CSeq','1 INVITE'),('Contact','<sip:peer@127.0.0.2:5060>'),
        ('Content-Type','application/sdp')],AST_OFFER.encode()))
    invite=endpoint._parse_invite(request,('127.0.0.2',5060))
    assert invite.local_recv_capabilities==local and isinstance(invite.local_recv_capabilities,tuple)
    copied=replace(invite,cseq='2 INVITE')
    assert copied.local_recv_capabilities is invite.local_recv_capabilities
    destination=SimpleNamespace(recv_format=sdp.RtpPcmFormat(100,'L16',16000,1,10,max_frame_ms=20),
        send_format=sdp.RtpPcmFormat(96,'L16',48000,1,10,max_frame_ms=10))
    selected=bridge_source_audio(copied,destination)
    assert selected.local_recv_capabilities is invite.local_recv_capabilities
    limit=HA_AUDIO_CAPABILITIES.receive_payload_bytes
    assert rtp.audio_payload_size_limit(selected.recv_format)==limit
    from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
    receiver=RtpAudioReceiver(selected.recv_format)
    values=tuple(range(limit//2))
    maximum=struct.pack(f'!{len(values)}h',*values)
    decoded=receiver.decode(selected.recv_format.payload_type,maximum)
    tail_bytes=(-limit)%selected.recv_format.audio_format.nominal_frame_bytes
    decoded.extend(receiver.decode(selected.recv_format.payload_type,bytes(tail_bytes)))
    assert b''.join(decoded)==struct.pack(f'<{len(values)}h',*values)+bytes(tail_bytes)
    with pytest.raises(rtp.RtpError,match='too large'):
        receiver.decode(selected.recv_format.payload_type,bytes(limit+2))


@pytest.mark.parametrize('source_rate,dest_send_rate,dest_recv_rate,kept',[
    (48000,48000,16000,True),
    (8000,48000,48000,False),
])
def test_bridge_receive_geometry_changes_only_when_conversion_count_improves(
    source_rate,dest_send_rate,dest_recv_rate,kept,
):
    from types import SimpleNamespace
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_RX_FORMATS,HA_SIP_PCM_TX_FORMATS
    from custom_components.voip_stack.sip_bridge import bridge_source_audio
    from custom_components.voip_stack.sip_listener import SipInvite

    body=('v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Bridge preference\r\n'
        'c=IN IP4 127.0.0.2\r\nt=0 0\r\nm=audio 45000 RTP/AVP 96 97 98\r\n'
        'a=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\n'
        'a=rtpmap:98 L16/8000/1\r\na=ptime:10\r\na=sendrecv\r\n').encode()
    original=sdp.RtpPcmFormat(96 if source_rate==48000 else 98,'L16',source_rate,1,10)
    invite=SipInvite(source_host='127.0.0.2',source_port=5060,
        request_uri=sip.parse_sip_uri('sip:target@127.0.0.1'),
        caller_uri=sip.parse_sip_uri('sip:source@127.0.0.2'),target='target',caller='source',
        call_id='bridge-cost',cseq='1 INVITE',remote_sdp=body,
        send_format=original,recv_format=original,remote_rtp_host='127.0.0.2',remote_rtp_port=45000,
        local_send_capabilities=HA_SIP_PCM_TX_FORMATS,local_recv_capabilities=HA_SIP_PCM_RX_FORMATS)
    destination=SimpleNamespace(send_format=sdp.RtpPcmFormat(100,'L16',dest_send_rate,1,10),
        recv_format=sdp.RtpPcmFormat(101,'L16',dest_recv_rate,1,10))

    selected=bridge_source_audio(invite,destination)

    assert (selected is invite)==kept
    assert selected.send_format.sample_rate==selected.recv_format.sample_rate==48000
    assert selected.send_format.frame_ms==selected.recv_format.frame_ms==10
    assert selected.recv_format.payload_type==96
