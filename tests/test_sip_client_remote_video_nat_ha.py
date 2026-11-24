"""Remote video preparation preserves the actual media socket and its owner."""
import asyncio
from dataclasses import replace
import socket
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.media_address import MediaEndpoint, apply_media_endpoints
from custom_components.voip_stack.sip_client import SipCallClient, SipDialog, PreparedDialogMediaUpdate
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

pytestmark = [pytest.mark.ha, pytest.mark.integration]


def bound_socket():
    result = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    result.setblocking(False)
    result.bind(('127.0.0.1', 0))
    return result


class Peer(asyncio.DatagramProtocol):
    def __init__(self):
        self.messages = asyncio.Queue()
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, raw, addr):
        message = sip.parse_message(raw)
        self.messages.put_nowait((message, addr))
        if message.method == 'BYE':
            headers = [(key, message.header(key)) for key in ('Via', 'From', 'To', 'Call-ID', 'CSeq')]
            self.transport.sendto(sip.build_response(200, 'OK', headers), addr)


async def assert_audio_socket_usable(audio, peer):
    packet = b'\x80\x60\x00\x01\x00\x00\x00\x00\x12\x34\x56\x78' + b'\x12\x34' * 320
    loop = asyncio.get_running_loop()
    await loop.sock_sendto(peer, packet, audio.getsockname())
    received, _ = await asyncio.wait_for(loop.sock_recvfrom(audio, 2048), 1)
    assert received == packet


@pytest.fixture
async def confirmed(socket_enabled, hass):
    loop = asyncio.get_running_loop()
    pcm = AudioFormat(16000, 's16le', 1, 20)
    audio, remote_audio, old_video, old_rtcp = [bound_socket() for _ in range(4)]
    client = SipCallClient(local_ip='127.0.0.1', local_name='HA', local_sip_port=0,
        local_rtp_port=audio.getsockname()[1], local_video_rtp_port=old_video.getsockname()[1],
        supported_formats=[pcm], video_formats=(sdp.DEFAULT_H264_FORMAT,))
    transport, _ = await loop.create_datagram_endpoint(lambda: SipDatagramQueueProtocol(client.queue), local_addr=('127.0.0.1', 0))
    peer_transport, peer = await loop.create_datagram_endpoint(Peer, local_addr=('127.0.0.1', 0))
    client.transport = transport
    client.local_sip_port = transport.get_extra_info('sockname')[1]
    client.dialog_ids.remote_tag = 'remote'
    client.media_endpoints = {
        'audio': MediaEndpoint(audio.getsockname()[1], '198.51.100.23', 51001),
        'video': MediaEndpoint(old_video.getsockname()[1], '198.51.100.23', 51003),
        'video_rtcp': MediaEndpoint(old_rtcp.getsockname()[1], '198.51.100.23', 51999),
    }
    initial = sdp.build_offer_directional('127.0.0.1', '127.0.0.1', audio.getsockname()[1], [pcm], [pcm],
        local_capabilities=HA_AUDIO_CAPABILITIES)
    fmt = sdp.RtpPcmFormat(96, 'L16', 16000, 1, 20, max_frame_ms=20)
    initial_answer = sdp.build_answer_directional('127.0.0.1', '127.0.0.1', remote_audio.getsockname()[1],
        fmt, fmt, remote_sdp=initial)
    negotiated = sdp.negotiate_answer_directional(initial_answer, [pcm], [pcm], local_offer_sdp=initial,
        local_capabilities=HA_AUDIO_CAPABILITIES)
    assert negotiated is not None
    current = SipDialog(target='Zoiper418', remote_host='127.0.0.1', remote_sip_port=peer_transport.get_extra_info('sockname')[1],
        remote_rtp_host='127.0.0.1', remote_rtp_port=remote_audio.getsockname()[1], local_rtp_port=audio.getsockname()[1],
        call_id=client.dialog_ids.call_id, local_uri=f'sip:HA@127.0.0.1:{client.local_sip_port}',
        remote_uri=f'sip:Zoiper418@127.0.0.1:{peer_transport.get_extra_info("sockname")[1]}', send_format=negotiated.send, recv_format=negotiated.recv,
        send_formats=negotiated.send_formats, recv_formats=negotiated.recv_formats,
        remote_tag='remote', remote_target_uri=f'sip:Zoiper418@127.0.0.1:{peer_transport.get_extra_info("sockname")[1]}',
        local_sdp_session_id=4242, local_sdp_body=apply_media_endpoints(initial, client.media_endpoints))
    client.dialog = current
    stage = SimpleNamespace(sockets={}, rollbacks=0, commits=0)
    try:
        yield client, current, peer, audio, remote_audio, stage
    finally:
        for sock in stage.sockets.values():
            sock.close()
        client.dialog = None
        await client.close()
        peer_transport.close()
        for sock in (audio, remote_audio, old_video, old_rtcp):
            sock.close()


async def remote_video_offer(client, peer, *, cseq=2, video=True):
    pcm = AudioFormat(16000, 's16le', 1, 20)
    description = sdp.build_offer_directional('127.0.0.1', '127.0.0.1', client.dialog.remote_rtp_port, [pcm], [pcm],
        video_port=44002, video_formats=(sdp.DEFAULT_H264_FORMAT,))
    if not video:
        description = sdp.build_video_reoffer(description, video_port=0, video_formats=(sdp.DEFAULT_H264_FORMAT,), video_direction='inactive')
    request = sip.build_request('INVITE', client.dialog.local_uri, [
        ('Via', f'SIP/2.0/UDP 127.0.0.1:{peer.transport.get_extra_info("sockname")[1]};branch=z9hG4bK-remote-video;rport'),
        ('From', f'<{client.dialog.remote_uri}>;tag=remote'),
        ('To', f'<{client.dialog.local_uri}>;tag={client.dialog_ids.local_tag}'),
        ('Contact', f'<{client.dialog.remote_target_uri}>'), ('Call-ID', client.dialog_ids.call_id),
        ('CSeq', f'{cseq} INVITE'), ('Content-Type', 'application/sdp')], description.encode())
    peer.transport.sendto(request, ('127.0.0.1', client.local_sip_port))
    raw, address = await asyncio.wait_for(client.queue.get(), 1)
    outcome = await client._handle_dialog_media_request(sip.parse_message(raw), *address)
    responses = []
    while True:
        message, _ = await asyncio.wait_for(peer.messages.get(), 1)
        responses.append(message)
        if message.is_response and message.status_code >= 200:
            if message.status_code < 300:
                ack = sip.build_request('ACK', client.dialog.local_uri, [
                    ('Via', f'SIP/2.0/UDP 127.0.0.1:{peer.transport.get_extra_info("sockname")[1]};branch=z9hG4bK-ack-{cseq};rport'),
                    ('From', message.header('From')), ('To', message.header('To')),
                    ('Call-ID', message.header('Call-ID')), ('CSeq', f'{cseq} ACK')])
                peer.transport.sendto(ack, ('127.0.0.1', client.local_sip_port))
                raw, address = await asyncio.wait_for(client.queue.get(), 1)
                await client._dispatch_in_dialog_request(sip.parse_message(raw), address)
            return outcome, message, responses


async def test_unadvertised_new_video_socket_rolls_back_before_failure_response(confirmed, caplog):
    client, previous, peer, audio, remote_audio, stage = confirmed
    old_endpoints = client.media_endpoints
    descriptor = audio.fileno()
    await assert_audio_socket_usable(audio, remote_audio)
    async def prepare(_old, _new, _method):
        stage.sockets = {'video': bound_socket(), 'video_rtcp': bound_socket()}
        async def commit():
            stage.commits += 1
        async def rollback():
            stage.rollbacks += 1
            for sock in stage.sockets.values():
                sock.close()
        return PreparedDialogMediaUpdate(commit=commit, rollback=rollback,
            answer_video_rtp_port=stage.sockets['video'].getsockname()[1])
    client.on_media_update = prepare
    _, response, _ = await remote_video_offer(client, peer)
    assert response.status_code == 500
    assert 'Changed media socket requires a fresh NAT binding' in caplog.text
    assert client.dialog is previous and client.media_endpoints is old_endpoints
    assert audio.fileno() == descriptor
    await assert_audio_socket_usable(audio, remote_audio)
    assert stage.rollbacks == 1 and stage.commits == 0
    assert all(sock.fileno() == -1 for sock in stage.sockets.values())


@pytest.mark.parametrize('delayed_proposal', ['same', 'new_port', 'new_binding', 'reject_video', 'rollback_current', 'rollback_replaced', 'commit_rollback_current', 'commit_rollback_replaced'])
async def test_new_video_answer_uses_fresh_socket_bindings_and_keeps_audio(confirmed, delayed_proposal):
    from custom_components.voip_stack.trunk_media import discover_pending_media
    from tests.test_media_stun import _Server, _response
    client, previous, peer, audio, remote_audio, stage = confirmed
    old_endpoints = client.media_endpoints
    descriptor = audio.fileno()
    mapped = {}
    def respond(server, raw, address):
        mapped.setdefault(address[1], 53001 + len(mapped) * 17)
        server.transport.sendto(_response(raw, ip='198.51.100.77', port=mapped[address[1]]), address)
    stun = _Server(respond)
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: stun, local_addr=('127.0.0.1', 0))
    async def prepare(_old, _new, _method):
        assert client.dialog is previous and client.media_endpoints is old_endpoints
        stage.sockets = {'video': bound_socket(), 'video_rtcp': bound_socket()}
        discovered = await discover_pending_media(stage.sockets, lambda: (stun.uri,))
        async def commit():
            assert client.dialog is previous and client.media_endpoints is old_endpoints
            stage.commits += 1
        async def rollback():
            stage.rollbacks += 1
            for sock in stage.sockets.values():
                sock.close()
        return PreparedDialogMediaUpdate(commit=commit, rollback=rollback,
            answer_video_rtp_port=stage.sockets['video'].getsockname()[1],
            answer_media_endpoints={**old_endpoints, **discovered})
    client.on_media_update = prepare
    try:
        _, response, _ = await remote_video_offer(client, peer)
        assert response.status_code == 200
        video = sdp.parse_video_sdp(response.body)
        assert video['media_port'] == mapped[stage.sockets['video'].getsockname()[1]]
        assert video['connection_ip'] == '198.51.100.77'
        assert f"a=rtcp:{mapped[stage.sockets['video_rtcp'].getsockname()[1]]} IN IP4 198.51.100.77".encode() in response.body
        assert client.media_endpoints['audio'] is old_endpoints['audio']
        assert client.media_endpoints['video'].local_port == stage.sockets['video'].getsockname()[1]
        assert client.media_endpoints['video'].port != old_endpoints['video'].port
        assert client.dialog is not previous and client.dialog.local_rtp_port == previous.local_rtp_port
        assert client.dialog.send_format == previous.send_format and client.dialog.recv_format == previous.recv_format
        assert stage.commits == 1 and stage.rollbacks == 0 and audio.fileno() == descriptor
        await assert_audio_socket_usable(audio, remote_audio)
        assert {request[2][1] for request in stun.requests} == {sock.getsockname()[1] for sock in stage.sockets.values()}
        adopted = client.dialog
        async def unchanged_video(_old, _new, _method):
            async def commit():
                stage.commits += 1
            return PreparedDialogMediaUpdate(commit=commit,
                answer_video_rtp_port=stage.sockets['video'].getsockname()[1],
                answer_media_endpoints=client.media_endpoints)
        client.on_media_update = unchanged_video
        _, second, _ = await remote_video_offer(client, peer, cseq=3)
        assert second.status_code == 200
        assert client.dialog.local_video_rtp_port == adopted.local_video_rtp_port
        assert len(stun.requests) == 2
        await assert_audio_socket_usable(audio, remote_audio)
        first_video_sockets = tuple(stage.sockets.values())
        async def remove_video(_old, _new, _method):
            async def commit():
                stage.commits += 1
                for sock in first_video_sockets:
                    sock.close()
            return PreparedDialogMediaUpdate(commit=commit, answer_video_rtp_port=0,
                answer_media_endpoints={'audio': client.media_endpoints['audio']})
        client.on_media_update = remove_video
        _, removed, _ = await remote_video_offer(client, peer, cseq=4, video=False)
        assert removed.status_code == 200 and b'\r\nm=video 0 RTP/AVP ' in removed.body
        assert sdp.parse_video_sdp(removed.body) is None
        assert all(sock.fileno() == -1 for sock in first_video_sockets)
        previous, old_endpoints = client.dialog, client.media_endpoints
        client.on_media_update = prepare
        _, restarted, _ = await remote_video_offer(client, peer, cseq=5)
        assert restarted.status_code == 200 and client.dialog.video_format is not None
        assert len(stun.requests) == 4 and stage.rollbacks == 0 and stage.commits == 4
        await assert_audio_socket_usable(audio, remote_audio)

        # A subsequent200offerless must keep the same actual local socket that
        # the preceding remote offer adopted and advertised through NAT.
        announced = client.dialog.local_video_rtp_port
        advertised = dict(client.media_endpoints)
        assert announced != client.local_video_rtp_port
        async def after_delayed(_old, _new, _method):
            async def commit():
                if delayed_proposal.startswith('commit_rollback'):
                    raise RuntimeError('injected media generation commit failure')
                stage.commits += 1
                if delayed_proposal == 'reject_video':
                    for sock in stage.sockets.values():
                        sock.close()
            async def rollback():
                stage.rollbacks += 1
                if 'rollback_' in delayed_proposal:
                    from custom_components.voip_stack.sip_video_relay import VideoTranscodeRestoreError
                    if delayed_proposal.endswith('replaced'):
                        stage.replacement = replace(client.dialog,
                            local_sdp_session_version=client.dialog.local_sdp_session_version + 1)
                        client.dialog = stage.replacement
                    raise VideoTranscodeRestoreError('injected previous converter restoration failure')
            proposed_port = client.local_video_rtp_port if delayed_proposal in {'new_port', 'rollback_current', 'rollback_replaced'} else (0 if delayed_proposal == 'reject_video' else announced)
            endpoints = dict(advertised)
            if delayed_proposal == 'new_binding':
                old = endpoints['video']
                endpoints['video'] = MediaEndpoint(old.local_port, old.host, old.port + 7)
            return PreparedDialogMediaUpdate(commit, rollback, answer_video_rtp_port=proposed_port,
                answer_media_endpoints=endpoints)
        client.on_media_update = after_delayed
        if 'rollback_' in delayed_proposal:
            from custom_components.voip_stack.sip_video_relay import VideoTranscodeRestoreError
            with pytest.raises(VideoTranscodeRestoreError, match='previous converter restoration failure'):
                await delayed_offer_answer(client, peer, cseq=6)
            requests = []
            while not peer.messages.empty():
                message, _ = peer.messages.get_nowait()
                if message.is_request:
                    requests.append(message)
            assert stage.rollbacks == 1 and stage.commits == 4
            if delayed_proposal.endswith('replaced'):
                assert client.dialog is stage.replacement
                assert not any(message.method == 'BYE' for message in requests)
                assert client._dialog_termination_task is None
            else:
                assert any(message.method == 'BYE' for message in requests)
                assert client.dialog is None
                assert client._dialog_termination_task.done()
                assert client._dialog_termination_task.result() == 'remote_hangup'
            return
        outcome, offered = await delayed_offer_answer(client, peer, cseq=6, reject_video=delayed_proposal == 'reject_video')
        assert sdp.parse_video_sdp(offered.body)['media_port'] == advertised['video'].port
        if delayed_proposal in {'new_port', 'new_binding'}:
            assert outcome == 'media_incompatible' and stage.rollbacks == 1 and stage.commits == 4
            assert client.dialog is None
            await assert_audio_socket_usable(audio, remote_audio)
            return
        assert outcome is None and stage.rollbacks == 0 and stage.commits == 5
        if delayed_proposal == 'reject_video':
            assert client.dialog.video_format is None and client.dialog.local_video_rtp_port == 0
            await assert_audio_socket_usable(audio, remote_audio)
            return
        assert client.dialog.local_video_rtp_port == announced, {
            'constructor': client.local_video_rtp_port, 'announced': announced,
            'after_ack': client.dialog.local_video_rtp_port, 'binding': client.media_endpoints['video']}
        candidate = await local_video_reinvite(client, peer, local_port=announced)
        assert candidate is not None and client.commit_prepared_reinvite(client.dialog, candidate)
        assert client.dialog.local_video_rtp_port == announced and client.media_endpoints == advertised
        assert len(stun.requests) == 4
        await assert_audio_socket_usable(audio, remote_audio)

    finally:
        transport.close()
        await stun.closed


@pytest.mark.parametrize('failure', ['advertise', 'encode', 'cancel'])
async def test_unpublishable_video_answer_discards_only_its_prepared_resources(confirmed, monkeypatch, failure):
    client, previous, peer, audio, remote_audio, stage = confirmed
    old_endpoints = client.media_endpoints
    descriptor = audio.fileno()
    async def prepare(_old, _new, _method):
        stage.sockets = {'video': bound_socket(), 'video_rtcp': bound_socket()}
        endpoints = {**old_endpoints, **{kind: MediaEndpoint(sock.getsockname()[1], '198.51.100.77', 54001 + index * 23)
            for index, (kind, sock) in enumerate(stage.sockets.items())}}
        async def commit():
            stage.commits += 1
        async def rollback():
            stage.rollbacks += 1
            for sock in stage.sockets.values():
                sock.close()
        return PreparedDialogMediaUpdate(commit=commit, rollback=rollback,
            answer_video_rtp_port=stage.sockets['video'].getsockname()[1], answer_media_endpoints=endpoints)
    client.on_media_update = prepare
    original_advertise = client._advertise_media
    def broken(body, endpoints=None):
        assert stage.sockets and endpoints is not None
        if failure == 'cancel':
            raise asyncio.CancelledError
        if failure == 'advertise':
            raise ValueError('injected advertised SDP failure')
        return original_advertise(body, endpoints) + '\ud800'
    monkeypatch.setattr(client, '_advertise_media', broken)
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await remote_video_offer(client, peer)
    else:
        _, response, _ = await remote_video_offer(client, peer)
        assert response.status_code == 500 and not response.body
    assert client.dialog is previous and client.media_endpoints is old_endpoints
    assert stage.rollbacks == 1 and stage.commits == 0
    assert all(sock.fileno() == -1 for sock in stage.sockets.values())
    assert audio.fileno() == descriptor
    await assert_audio_socket_usable(audio, remote_audio)


async def delayed_offer_answer(client, peer, *, cseq, reject_video=False):
    request = sip.build_request('INVITE', client.dialog.local_uri, [
        ('Via', f'SIP/2.0/UDP 127.0.0.1:{peer.transport.get_extra_info("sockname")[1]};branch=z9hG4bK-delayed-{cseq};rport'),
        ('From', f'<{client.dialog.remote_uri}>;tag=remote'),
        ('To', f'<{client.dialog.local_uri}>;tag={client.dialog_ids.local_tag}'),
        ('Call-ID', client.dialog_ids.call_id), ('CSeq', f'{cseq} INVITE')])
    peer.transport.sendto(request, ('127.0.0.1', client.local_sip_port))
    raw, address = await asyncio.wait_for(client.queue.get(), 1)
    assert await client._handle_dialog_media_request(sip.parse_message(raw), *address) is None
    while True:
        offered, _ = await asyncio.wait_for(peer.messages.get(), 1)
        if offered.is_response and offered.status_code >= 200:
            break
    assert offered.status_code == 200
    pcm = sdp.RtpPcmFormat(96, 'L16', 16000, 1, 20, max_frame_ms=20)
    video = sdp.offered_video_formats(offered.body)[0]
    answer = sdp.build_answer_directional('127.0.0.1', '127.0.0.1', client.dialog.remote_rtp_port,
        pcm, pcm, remote_sdp=offered.body, video_port=0 if reject_video else 44002,
        video_format=None if reject_video else video)
    ack = sip.build_request('ACK', client.dialog.local_uri, [
        ('Via', f'SIP/2.0/UDP 127.0.0.1:{peer.transport.get_extra_info("sockname")[1]};branch=z9hG4bK-delayed-ack;rport'),
        ('From', offered.header('From')), ('To', offered.header('To')), ('Call-ID', offered.header('Call-ID')),
        ('CSeq', f'{cseq} ACK'), ('Content-Type', 'application/sdp')], answer.encode())
    peer.transport.sendto(ack, ('127.0.0.1', client.local_sip_port))
    raw, address = await asyncio.wait_for(client.queue.get(), 1)
    return await client._dispatch_in_dialog_request(sip.parse_message(raw), address), offered


async def local_video_reinvite(client, peer, *, local_port):
    task = asyncio.create_task(client.async_prepare_video_reinvite(local_video_rtp_port=local_port,
        video_formats=(client.dialog.video_format,), timeout=1))
    try:
        while True:
            ready = asyncio.create_task(peer.messages.get())
            done, _ = await asyncio.wait((task, ready), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
                return task.result()
            request, address = ready.result()
            if request.method == 'INVITE':
                break
        assert sdp.parse_video_sdp(request.body)['media_port'] == client.media_endpoints['video'].port
        pcm = sdp.RtpPcmFormat(96, 'L16', 16000, 1, 20, max_frame_ms=20)
        answer = sdp.build_answer_directional('127.0.0.1', '127.0.0.1', client.dialog.remote_rtp_port,
            pcm, pcm, remote_sdp=request.body, video_port=44002, video_format=sdp.offered_video_formats(request.body)[0])
        headers = [(key, request.header(key)) for key in ('Via', 'From', 'To', 'Call-ID', 'CSeq')]
        headers.extend((('Contact', f'<{client.dialog.remote_target_uri}>'), ('Content-Type', 'application/sdp')))
        peer.transport.sendto(sip.build_response(200, 'OK', headers, answer.encode()), address)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
