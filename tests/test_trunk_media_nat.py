"""Wire SDP witnesses for actual-socket media discovery and local routing."""

from __future__ import annotations

import asyncio
import importlib
import socket
from types import SimpleNamespace

import pytest

from .voip_phase1_support import sip, sdp, sip_client, _load_intercom_module
from .test_media_stun import _Server, _response

addressing = importlib.import_module("custom_components.voip_stack.core.media_address")
trunk_media = _load_intercom_module("trunk_media")


def test_sdp_mapping_retains_contract_and_rejected_sections():
    original = ("v=0\r\no=- 123 7 IN IP4 192.168.1.2\r\ns=phone\r\n"
                "c=IN IP4 192.168.1.2\r\nt=0 0\r\n"
                "m=audio 41000 RTP/AVP 9\r\na=rtpmap:9 G722/8000\r\na=ptime:20\r\n"
                "m=video 41002 RTP/AVP 105\r\na=rtcp:41003\r\na=recvonly\r\n"
                "m=video 0 RTP/AVP 26\r\na=inactive\r\n")
    endpoints = {key: addressing.MediaEndpoint(local, "198.51.100.7", public)
                 for key, local, public in (("audio", 41000, 51001), ("video", 41002, 51003),
                                           ("video_rtcp", 41003, 51999))}
    mapped = addressing.apply_media_endpoints(original, endpoints)
    assert "o=- 123 7 IN IP4 192.168.1.2" in mapped
    assert "m=audio 51001 RTP/AVP 9\r\nc=IN IP4 198.51.100.7" in mapped
    assert "m=video 51003 RTP/AVP 105\r\nc=IN IP4 198.51.100.7" in mapped
    assert "a=rtcp:51999 IN IP4 198.51.100.7" in mapped
    assert "m=video 0 RTP/AVP 26\r\na=inactive" in mapped
    assert "a=rtpmap:9 G722/8000\r\na=ptime:20" in mapped
    assert addressing.apply_media_endpoints(mapped, endpoints) == mapped
    with pytest.raises(ValueError, match="fresh NAT binding"):
        addressing.apply_media_endpoints(original.replace("video 41002", "video 42002"), endpoints)


@pytest.mark.asyncio
async def test_private_and_direct_routes_do_not_contact_stun():
    for mode, remote in (("auto", "192.168.99.2"), ("direct", "1.1.1.1")):
        client = SimpleNamespace(local_ip="192.168.1.2", media_ip="198.51.100.9")
        config = SimpleNamespace(media_nat_mode=mode, outbound_media_address="198.51.100.9")
        def forbidden():
            raise AssertionError("STUN consulted for direct route")
        await trunk_media.prepare_trunk_media(client, config, remote, forbidden)
        assert client.media_ip == "192.168.1.2"


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_after_commit", (False, True))
async def test_real_invite_uses_discovered_port_and_keeps_bound_socket(rollback_after_commit):
    loop = asyncio.get_running_loop()
    public_ports = {}
    def respond(server, raw, addr):
        public_ports.setdefault(addr[1], 47991 + len(public_ports) * 13)
        server.transport.sendto(_response(raw, ip="198.51.100.23", port=public_ports[addr[1]]), addr)
    stun = _Server(respond)
    st, _ = await loop.create_datagram_endpoint(lambda: stun, local_addr=("127.0.0.1", 0))
    received = []
    class SipPeer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
        def datagram_received(self, raw, addr):
            request = sip.parse_message(raw)
            received.append(request)
            if request.method not in {"INVITE", "BYE"}:
                return
            headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
            to = request.header("To")
            headers.append(("To", to if ";tag=" in to else to + ";tag=peer"))
            body = b""
            if request.method == "INVITE":
                headers.extend((("Contact", "<sip:peer@127.0.0.1:%d>" % self.transport.get_extra_info("sockname")[1]),
                                ("Content-Type", "application/sdp")))
                selected = sdp.offered_pcm_formats(request.body)[0]
                video = sdp.offered_video_formats(request.body)
                body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 45000,
                    selected, selected, remote_sdp=request.body,
                    video_port=45002 if video else 0, video_format=video[0] if video else None).encode()
            self.transport.sendto(sip.build_response(200, "OK", headers, body), addr)
    transport, _ = await loop.create_datagram_endpoint(SipPeer, local_addr=("127.0.0.1", 0))
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.1", 0))
    media.setblocking(False)
    local_port, original_fd = media.getsockname()[1], media.fileno()
    client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="test", local_sip_port=0,
                                     local_rtp_port=local_port)
    client.audio_rtp_socket = media
    config = SimpleNamespace(media_nat_mode="auto", outbound_media_address="198.51.100.99")
    client.discover_media_sockets = lambda sockets: trunk_media.discover_pending_media(sockets, lambda: (stun.uri,))
    client.prepare_media = lambda _host: trunk_media.prepare_trunk_media(
        client, config, "1.1.1.1", lambda: (stun.uri,))
    additional_sockets = {}
    try:
        assert await client.invite(target="peer", remote_host="127.0.0.1",
            remote_sip_port=transport.get_extra_info("sockname")[1], timeout=1) == "in_call"
        offer = next(item for item in received if item.method == "INVITE").body.decode()
        assert "m=audio 47991 " in offer
        assert "c=IN IP4 198.51.100.23" in offer
        assert "198.51.100.99" not in offer
        assert client.local_rtp_port == local_port and media.fileno() == original_fd
        assert stun.requests[0][2][1] == local_port
        initial_endpoints = client.media_endpoints
        for name in ("video", "video_rtcp"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            additional_sockets[name] = sock
        previous = client.dialog
        candidate = await client.async_prepare_video_reinvite(
            local_video_rtp_port=additional_sockets["video"].getsockname()[1],
            video_formats=(sdp.DEFAULT_H264_FORMAT,), media_sockets=additional_sockets, timeout=2)
        assert candidate is not None and candidate.video_format is not None
        assert client.media_endpoints is initial_endpoints and "video" not in initial_endpoints
        video_offer = next(item for item in reversed(received) if item.method == "INVITE").body.decode()
        assert "m=video 48004 " in video_offer
        assert "a=rtcp:48017 IN IP4 198.51.100.23" in video_offer
        assert client.commit_prepared_reinvite(previous, candidate)
        assert client.media_endpoints["video"].port == 48004
        previous_video = client.dialog
        previous_endpoints = dict(client.media_endpoints)
        descriptors = {name: sock.fileno() for name, sock in additional_sockets.items()}
        removal = await client.async_prepare_video_reinvite(
            local_video_rtp_port=0, video_formats=(sdp.DEFAULT_H264_FORMAT,), timeout=2)
        assert removal is not None and removal.video_format is None
        if rollback_after_commit:
            assert client.commit_prepared_reinvite(previous_video, removal)
            assert "video" not in client.media_endpoints
        requests_before_restore = len(stun.requests)
        restored = await client.restore_prepared_reinvite(previous_video, removal)
        assert restored is not None and client.dialog is restored
        assert restored.video_format is not None
        assert client.media_endpoints == previous_endpoints
        assert len(stun.requests) == requests_before_restore
        assert {name: sock.fileno() for name, sock in additional_sockets.items()} == descriptors
        restored_offer = next(item for item in reversed(received) if item.method == "INVITE").body.decode()
        assert "m=video 48004 " in restored_offer
        assert "a=rtcp:48017 IN IP4 198.51.100.23" in restored_offer
        assert not any(item.method == "BYE" for item in received)
        await client.terminate()
    finally:
        await client.close()
        transport.close()
        st.close()
        await stun.closed
        for sock in additional_sockets.values():
            sock.close()
    assert media.fileno() == -1


@pytest.mark.asyncio
async def test_close_during_nat_preparation_cancels_reader_and_sends_no_invite():
    loop = asyncio.get_running_loop()
    requested = asyncio.Event()
    stun = _Server(lambda *_args: requested.set())
    transport, _ = await loop.create_datagram_endpoint(lambda: stun, local_addr=("127.0.0.1", 0))
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.1", 0))
    media.setblocking(False)
    descriptor = media.fileno()
    client = sip_client.SipCallClient(local_ip="127.0.0.1", local_name="test", local_sip_port=0,
                                     local_rtp_port=media.getsockname()[1])
    client.audio_rtp_socket = media
    config = SimpleNamespace(media_nat_mode="auto", outbound_media_address="")
    client.prepare_media = lambda _host: trunk_media.prepare_trunk_media(client, config, "1.1.1.1", lambda: (stun.uri,))
    task = asyncio.create_task(client.invite(target="peer", remote_host="127.0.0.1", remote_sip_port=9))
    try:
        await asyncio.wait_for(requested.wait(), 1)
        await asyncio.wait_for(client.close(), 1)
        await asyncio.gather(task, return_exceptions=True)
        assert client.last_sip_event != "INVITE"
        assert media.fileno() == -1
        with pytest.raises(KeyError):
            loop._selector.get_key(descriptor)
    finally:
        await client.close()
        transport.close()
        await stun.closed
