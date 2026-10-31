"""Local calls borrow the listener's signaling flow without owning its socket."""

import asyncio
import re
import socket
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import sip_runtime
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_endpoint import SipEndpointManager
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


async def test_udp_dialogs_use_registered_listener_port_and_close_independently(hass, socket_enabled, monkeypatch):
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("ordinary", "Ordinary", "fixture-only")],
        local_ip="127.0.0.1", local_sip_port=5060)
    manager = SipEndpointManager(host="127.0.0.1", port=0, local_ip="127.0.0.1", local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=None, on_register=registrar.handle_register)
    assert await manager.start()
    monkeypatch.setattr(sip_runtime, "sip_endpoint_manager", lambda _hass: manager)
    requests, replies = [], asyncio.Queue()

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            message = sip.parse_message(raw)
            if message.is_response:
                replies.put_nowait(message)
                return
            requests.append((message, address))
            if message.method == "ACK":
                return
            headers = [(key, message.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
            headers.append(("To", message.header("To") if ";tag=" in message.header("To") else message.header("To") + ";tag=independent"))
            body = b""
            if message.method == "INVITE":
                payload = re.search(rb"a=rtpmap:(\d+) L16/16000", message.body)[1].decode()
                body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent peer\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    f"m=audio 35000 RTP/AVP {payload}\r\na=rtpmap:{payload} L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n").encode()
                headers.append(("Content-Type", "application/sdp"))
            self.transport.sendto(sip.build_response(200, "OK", headers, body), address)

    transport, _ = await hass.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    remote_port = transport.get_extra_info("sockname")[1]
    listener = manager.udp_server.transport
    listener_port = listener.get_extra_info("sockname")[1]
    uri = sip.parse_sip_uri(f"sip:ordinary@127.0.0.1:{remote_port};transport=udp")
    clients = [SipCallClient(local_ip="127.0.0.1", local_name=f"Caller{index}", local_sip_port=listener_port,
        local_rtp_port=40000 + index * 2, supported_formats=[AudioFormat(16000, frame_ms=20)]) for index in range(2)]
    try:
        for client in clients:
            sip_runtime.enable_reused_signaling_flow(hass, client, uri, target="Ordinary", default_sip_port=listener_port)
        results = await asyncio.gather(*(client.invite(target="ordinary", remote_host="127.0.0.1",
            remote_sip_port=remote_port, request_uri=str(uri)) for client in clients))
        assert results == ["in_call", "in_call"]
        assert {address[1] for request, address in requests if request.method == "INVITE"} == {listener_port}
        assert all(client.transport is None for client in clients)
        await clients[0].close()
        assert clients[1].dialog is not None and not listener.is_closing()
        response = await manager.async_options(str(uri), ("127.0.0.1", remote_port), "UDP", timeout=2)
        assert response.status_code == 200
        await clients[1].close()
        assert not listener.is_closing()
        raw = sip.build_request("REGISTER", f"sip:127.0.0.1:{listener_port}", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{remote_port};branch=z9hG4bK-still-live;rport"),
            ("From", "<sip:ordinary@127.0.0.1>;tag=register"), ("To", "<sip:ordinary@127.0.0.1>"),
            ("Call-ID", "registration-after-dialogs"), ("CSeq", "1 REGISTER"),
            ("Contact", f"<sip:ordinary@127.0.0.1:{remote_port}>")])
        transport.sendto(raw, ("127.0.0.1", listener_port))
        assert (await asyncio.wait_for(replies.get(), 2)).status_code == 401
    finally:
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
        transport.close()
        await manager.stop()


async def test_endpoint_shutdown_owns_bridge_client_before_first_provisional(lab, monkeypatch):
    from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot
    from custom_components.voip_stack.inbound_routing import bridge
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", port))
    lab.runtime.transport_config["sip_port"] = port
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    target = lab.phones[0]
    target.auto_answer = False
    respond = target.respond
    monkeypatch.setattr(target, "respond", lambda request, address, status, body=b"":
        respond(request, address, status, body) if status != 180 else None)
    created = []

    def client_factory(**kwargs):
        client = SipCallClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(bridge, "SipCallClient", client_factory)
    caller = SipCallClient(local_ip="127.0.0.1", local_name="Outside", local_sip_port=5060,
        local_rtp_port=35000, supported_formats=[AudioFormat(16000, frame_ms=20)])
    pending = asyncio.create_task(caller.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=port,
        request_uri=f"sip:bedroom0@127.0.0.1:{port}"))
    try:
        await asyncio.wait_for(target.invited.wait(), 3)
        assert len(created) == 1 and not created[0]._invite_task.done()
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        assert created[0]._closed
        assert created[0]._invite_task.done()
        assert not lab.calls.sessions
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        # These are this test's concrete handles, never a global task/port scrub.
        await asyncio.gather(*(client.close() for client in [caller, *created]), return_exceptions=True)
        await asyncio.gather(pending, return_exceptions=True)
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
