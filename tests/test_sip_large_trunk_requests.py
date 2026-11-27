"""Real UDP registration and TCP request growth follow RFC 3261 section 18.1.1."""

import asyncio
import hashlib
import re

import pytest
from .voip_phase1_support import sip,sip_client,sip_trunk,sdp,audio_format


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario',['large','auth_growth','refused','small'])
async def test_request_size_switch_preserves_registration_and_digest(scenario):
    wire=[]
    tasks=set()
    writers=set()
    nonce='n'*(400 if scenario=='auth_growth' else 12)
    failures=[]
    def reply(raw,transport):
        message=sip.parse_message(raw)
        wire.append((transport,message,len(raw)))
        if message.method=='ACK':
            return None
        challenge=message.method=='INVITE' and not message.header('Authorization')
        if message.method=='INVITE' and not challenge:
            auth=dict((key,value.strip('"')) for key,value in re.findall(r'(\w+)=("[^"]*"|[^, ]+)',message.header('Authorization')))
            def md5(value):
                return hashlib.md5(value.encode()).hexdigest()
            expected=md5(f'{md5("line:test:secret")}:{nonce}:{auth["nc"]}:{auth["cnonce"]}:auth:{md5("INVITE:"+message.uri)}')
            assert auth['response']==expected
            assert auth['uri']==message.uri
            assert auth['nc']=='00000001'
        response=sip.build_uas_response(message,401 if challenge else 200,'Unauthorized' if challenge else 'OK',
            to_tag='provider',contact_uri=f'sip:peer@127.0.0.1:{port};transport={transport.lower()}',
            body=message.body if message.method=='INVITE' and not challenge else b'')
        if challenge:
            response=response.replace(b'Content-Length:',f'WWW-Authenticate: Digest realm="test", nonce="{nonce}", qop="auth"\r\nContent-Length:'.encode())
        return response
    class Provider(asyncio.DatagramProtocol):
        def connection_made(self,transport):self.transport=transport
        def datagram_received(self,raw,address):
            try:
                response=reply(raw,'UDP')
                if response:
                    self.transport.sendto(response,address)
            except BaseException as error:
                failures.append(error)
    loop=asyncio.get_running_loop()
    peer,_=await loop.create_datagram_endpoint(Provider,local_addr=('127.0.0.1',0))
    port=peer.get_extra_info('sockname')[1]
    async def serve(reader,writer):
        tasks.add(asyncio.current_task())
        writers.add(writer)
        try:
            while True:
                head=await reader.readuntil(b'\r\n\r\n')
                length=int(re.search(rb'(?im)^Content-Length:\s*(\d+)',head)[1])
                raw=head+await reader.readexactly(length)
                response=reply(raw,'TCP')
                if response:
                    writer.write(response)
                    await writer.drain()
        except (asyncio.IncompleteReadError,ConnectionError):
            pass
        except BaseException as error:
            failures.append(error)
        finally:
            writer.close()
            writers.discard(writer)
            tasks.discard(asyncio.current_task())
    server=None if scenario=='refused' else await asyncio.start_server(serve,'127.0.0.1',port)
    trunk=sip_trunk.SipTrunkClient(config=sip_trunk.SipTrunkConfig(options_interval=30, enabled=True,transport='udp',server='127.0.0.1',port=port,
        domain='test',username='line',auth_username='line',password='secret',expires=300),local_ip='127.0.0.1',local_sip_port=5060)
    client=None
    try:
        await trunk._connect_udp()
        trunk._ensure_receive_task()
        assert await trunk.register(timeout=2)=='registered'
        registration_transport=trunk.transport
        large=scenario in {'large','refused'}
        client=sip_client.SipCallClient(local_ip='127.0.0.1',local_name='HA',local_sip_port=trunk._udp_local_port,
            local_rtp_port=41000,supported_formats=[audio_format.AudioFormat(16000,'s16le',1,20)],
            username='line',password='secret',include_common_codecs=large,
            local_video_rtp_port=41002 if large else 0,video_formats=sdp.DEFAULT_VIDEO_FORMATS if large else ())
        identity=client.dialog_ids.call_id
        send,responses=trunk.open_outbound_dialog(identity)
        client.use_reused_signaling_flow(send=send,responses=responses,close=lambda:trunk.close_outbound_dialog(identity))
        uri=f'sip:peer@127.0.0.1:{port};transport=udp'
        assert await client.invite(target='peer',remote_host='127.0.0.1',remote_sip_port=port,request_uri=uri,timeout=2)=='in_call'
        assert not failures,failures
        invites=[item for item in wire if item[1].method=='INVITE']
        expected=['TCP','TCP'] if scenario=='large' else ['UDP','TCP'] if scenario=='auth_growth' else ['UDP','UDP']
        assert [item[0] for item in invites]==expected
        assert all(item[1].uri==uri for item in invites)
        assert invites[0][1].header('From')==invites[1][1].header('From')
        assert invites[0][1].header('To')==invites[1][1].header('To')
        assert all(f'SIP/2.0/{transport}' in message.header('Via') for transport,message,_ in invites)
        if scenario=='auth_growth':
            assert invites[0][2]<=1300<invites[1][2]
        assert await client.terminate(timeout=2) == 'remote_hangup'
        byes = [item for item in wire if item[1].method == 'BYE']
        assert len(byes) == 1
        assert byes[0][0] == expected[-1]
        await client.close()
        assert not trunk._outbound_dialogs
        assert trunk.transport is registration_transport and trunk.ready
        assert await trunk.register(timeout=2)=='registered'
    finally:
        if client is not None:
            await client.close()
        await trunk.stop()
        peer.close()
        if server is not None:
            server.close()
            await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.gather(*tuple(tasks),return_exceptions=True)
    assert not failures,failures
