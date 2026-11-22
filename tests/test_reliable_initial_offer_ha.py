"""Real UDP witness for RFC 3262 offers and answers in reliable 183 responses."""

import asyncio
import json

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.sip_client import SipCallClient


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("delayed_offer", [False, True], ids=["invite-offer", "invite-empty"])
async def test_reliable_183_correlates_offer_and_answer_on_real_udp(hass, socket_enabled, tmp_path, delayed_offer):
    pracked = asyncio.Event()
    peer_closed = asyncio.Event()
    frames = []
    received_prack = []
    invite_request = []
    evidence = {"delayed_offer": delayed_offer, "frames": frames, "cleanup": {}}
    remote_sdp = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Reliable peer\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 42000 RTP/AVP 110\r\n"
        "a=rtpmap:110 L16/16000/1\r\na=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n")

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def connection_lost(self, exc):
            peer_closed.set()

        def send(self, raw, address):
            frames.append({"direction": "peer_to_client", "raw": raw.decode()})
            self.transport.sendto(raw, address)

        def datagram_received(self, raw, address):
            frames.append({"direction": "client_to_peer", "raw": raw.decode()})
            request = sip.parse_message(raw)
            port = self.transport.get_extra_info("sockname")[1]
            contact = f"sip:reliable@127.0.0.1:{port}"
            if request.method == "INVITE":
                invite_request.append(request)
                self.send(sip.build_uas_response(request, 183, "Session Progress", to_tag="rel-offer",
                    body=remote_sdp.encode(), extra_headers=(("Contact", f"<{contact}>"),
                        ("Require", "100rel"), ("RSeq", "41"))), address)
            elif request.method == "PRACK":
                received_prack.append(request)
                pracked.set()
                accepted = not delayed_offer or bool(request.body)
                self.send(sip.build_uas_response(request, 200 if accepted else 488,
                    "OK" if accepted else "Not Acceptable Here", to_tag="rel-offer"), address)
                self.send(sip.build_uas_response(invite_request[0], 200 if accepted else 487,
                    "OK" if accepted else "Request Terminated", to_tag="rel-offer",
                    contact_uri=contact), address)
            elif request.method in {"BYE", "CANCEL"}:
                self.send(sip.build_uas_response(request, 200, "OK", to_tag="rel-offer"), address)
                if request.method == "CANCEL":
                    self.send(sip.build_uas_response(invite_request[0], 487, "Request Terminated",
                        to_tag="rel-offer"), address)

    server, _ = await hass.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    peer_port = server.get_extra_info("sockname")[1]
    pcm = AudioFormat(16000, "s16le", 1, 20)
    client = SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0,
        local_rtp_port=41000, supported_send_formats=[pcm], supported_recv_formats=[pcm],
        codec_policy=CodecPolicy(audio=("L16",)))
    invite_task = None
    try:
        await client.start()
        invite_task = asyncio.create_task(client.invite(target="Reliable", remote_host="127.0.0.1",
            remote_sip_port=peer_port, delayed_offer=delayed_offer, timeout=3))
        await asyncio.wait_for(pracked.wait(), 3)
        invite = invite_request[0]
        prack = received_prack[0]
        assert bool(invite.body) is not delayed_offer
        assert "100rel" in sip.option_tags(invite, "Supported")
        assert prack.header("Call-ID") == invite.header("Call-ID")
        assert sip.extract_tag(prack.header("To")) == "rel-offer"
        assert prack.header("RAck") == f"41 {sip.parse_cseq(invite.header('CSeq')).number} INVITE"
        if delayed_offer:
            assert prack.body, "The reliable 183 contains the initial offer, so PRACK must carry its SDP answer"
            assert prack.header("Content-Type").split(";", 1)[0] == "application/sdp"
            sdp.validate_sdp_answer(remote_sdp, prack.body)
            assert b"a=rtpmap:110 L16/16000/1" in prack.body
        else:
            sdp.validate_sdp_answer(invite.body, remote_sdp)
            assert not prack.body
        result = await asyncio.wait_for(invite_task, 3)
        evidence["invite_result"] = result
        if result == "ringing":
            result = await client.wait_for_final(timeout=3)
        evidence["final_result"] = result
        assert result == "in_call"
        assert client.dialog is not None
        assert client.dialog.remote_rtp_port == 42000
        assert client.dialog.send_format.sample_rate == client.dialog.recv_format.sample_rate == 16000
    finally:
        await client.close()
        if invite_task is not None:
            await asyncio.gather(invite_task, return_exceptions=True)
        server.close()
        await asyncio.wait_for(peer_closed.wait(), 2)
        evidence["cleanup"] = {"client_closed": client._closed,
            "transport_released": client.transport is None,
            "invite_owner_done": client._invite_task is None or client._invite_task.done(),
            "peer_closed": peer_closed.is_set()}
        (tmp_path / f"wire-{'invite-empty' if delayed_offer else 'invite-offer'}.json").write_text(
            json.dumps(evidence, indent=2) + "\n")
        assert all(evidence["cleanup"].values())


@pytest.mark.parametrize("protocol", ["UDP", "TCP"])
@pytest.mark.parametrize("cancel", [False, True], ids=["answer-flow", "cancel-flow"])
async def test_reliable_offer_retransmission_and_cancel_reuse_the_dialog_flow(hass, socket_enabled, protocol, cancel):
    from custom_components.voip_stack.sip_tcp_io import read_sip_stream_message

    remote_sdp = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Reliable peer\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 42000 RTP/AVP 110\r\n"
        "a=rtpmap:110 L16/16000/1\r\na=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n")
    invites, pracks, progress = {}, {}, {}
    connections, peer_tasks, provisional, sources = [], [], [], set()
    peer_closed = asyncio.Event()
    port = 0

    def receive(raw, send):
        request = sip.parse_message(raw)
        key = request.header("Call-ID")
        contact = f"sip:reliable@127.0.0.1:{port};transport={protocol.lower()}"
        if request.method == "INVITE":
            assert not request.body
            invites[key] = request
            pracks[key] = []
            progress[key] = sip.build_uas_response(request, 183, "Session Progress", to_tag="rel-offer",
                body=remote_sdp.encode(), extra_headers=(("Contact", f"<{contact}>"),
                    ("Require", "100rel"), ("RSeq", "41")))
            send(progress[key])
        elif request.method == "PRACK":
            pracks[key].append(raw)
            assert request.body
            assert request.header("RAck") == f"41 {sip.parse_cseq(invites[key].header('CSeq')).number} INVITE"
            sdp.validate_sdp_answer(remote_sdp, request.body)
            if len(pracks[key]) == 1:
                # The same provisional response is not another offer exchange.
                send(progress[key])
                if protocol == "UDP":
                    return  # The ordinary PRACK transaction must retry identical bytes.
            send(sip.build_uas_response(request, 200, "OK", to_tag="rel-offer"))
            if not cancel:
                send(sip.build_uas_response(invites[key], 200, "OK", to_tag="rel-offer", contact_uri=contact))
        elif request.method == "CANCEL":
            send(sip.build_uas_response(request, 200, "OK", to_tag="rel-offer"))
            send(sip.build_uas_response(invites[key], 487, "Request Terminated", to_tag="rel-offer"))
        elif request.method == "BYE":
            send(sip.build_uas_response(request, 200, "OK", to_tag="rel-offer"))

    class DatagramPeer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            sources.add(address)
            receive(raw, lambda response: self.transport.sendto(response, address))

        def connection_lost(self, exc):
            peer_closed.set()

    async def stream_peer(reader, writer):
        connections.append(writer)
        peer_tasks.append(asyncio.current_task())
        try:
            while (raw := await read_sip_stream_message(reader, writer=writer)) is not None:
                if raw != b"\r\n":
                    receive(raw, writer.write)
                    await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    if protocol == "UDP":
        server, _ = await hass.loop.create_datagram_endpoint(DatagramPeer, local_addr=("127.0.0.1", 0))
        port = server.get_extra_info("sockname")[1]
    else:
        server = await asyncio.start_server(stream_peer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
    pcm = AudioFormat(16000, "s16le", 1, 20)
    client = SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0,
        local_rtp_port=41000, supported_send_formats=[pcm], supported_recv_formats=[pcm],
        codec_policy=CodecPolicy(audio=("L16",)), signaling_transport=protocol)

    async def record_provisional(candidate):
        provisional.append(candidate)

    client.on_provisional_media = record_provisional
    try:
        await client.start()
        result = await client.invite(target="Reliable", remote_host="127.0.0.1",
            remote_sip_port=port, delayed_offer=True, timeout=3)
        signaling = client.transport if protocol == "UDP" else client.writer
        if cancel:
            assert result == "ringing"
            assert client.request_cancel()
            assert await client.wait_for_final(timeout=3) == "cancelled"
            assert client.dialog is None
        else:
            if result == "ringing":
                result = await client.wait_for_final(timeout=3)
            assert result == "in_call"
            assert client.dialog is not None and client.dialog.remote_rtp_port == 42000
            assert client._local_sdp_body == client.dialog.local_sdp_body
            assert client.bye()
            await asyncio.wait_for(asyncio.shield(client._dialog_termination_task), 3)
            assert client.dialog is None
        assert (client.transport if protocol == "UDP" else client.writer) is signaling
        assert len(invites) == len(provisional) == 1
        for copies in pracks.values():
            assert len(copies) == (2 if protocol == "UDP" else 1)
            assert len(set(copies)) == 1
        if protocol == "TCP":
            assert len(connections) == 1
        else:
            assert len(sources) == 1
    finally:
        await client.close()
        server.close()
        if protocol == "UDP":
            await asyncio.wait_for(peer_closed.wait(), 2)
        else:
            await server.wait_closed()
            if peer_tasks:
                await asyncio.wait_for(asyncio.gather(*peer_tasks), 2)
        assert client._closed and client.transport is None and client.writer is None
        assert client._invite_task is None or client._invite_task.done()
