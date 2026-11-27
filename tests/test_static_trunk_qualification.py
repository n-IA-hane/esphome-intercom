"""Static peers expose actual OPTIONS reachability over owned UDP/TCP flows."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from .voip_phase1_support import sip, sip_listener, sip_trunk


@pytest.mark.parametrize("transport_name", ["udp", "tcp"])
async def test_static_options_drives_existing_ready_state_and_cleans_up(monkeypatch, transport_name):
    monkeypatch.setattr(sip_trunk, "SIP_TIMER_F", 0.08)
    monkeypatch.setattr(sip_trunk, "SIP_T1", 0.01)
    packets = []
    status = 200
    notified = asyncio.Queue()
    connections = set()

    def response(raw):
        request = sip.parse_message(raw)
        packets.append(request)
        assert request.method == "OPTIONS"
        if status is None:
            return None
        return sip.build_response(status, "OK" if status == 200 else "Unavailable",
            [(name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")])

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
        def datagram_received(self, raw, address):
            if reply := response(raw):
                self.transport.sendto(reply, address)

    async def stream(reader, writer):
        connections.add(writer)
        try:
            while raw := await sip_listener._read_sip_stream_message(reader):
                if reply := response(raw):
                    writer.write(reply)
                    await writer.drain()
        finally:
            connections.discard(writer)
            writer.close()
            await writer.wait_closed()

    loop = asyncio.get_running_loop()
    if transport_name == "udp":
        server, _ = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
        port = server.get_extra_info("sockname")[1]
    else:
        server = await asyncio.start_server(stream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
    config = sip_trunk.SipTrunkConfig(options_interval=30, enabled=True, transport=transport_name, server="127.0.0.1", port=port,
        domain="peer.example", username="line", auth_username="", password="", expires=300,
        register=False, allowed_ips=("127.0.0.1",))
    client = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)
    client.on_state_change = lambda: notified.put_nowait((client.ready, client.status_code))

    async def next_state(code):
        async with asyncio.timeout(3):
            while True:
                state = await notified.get()
                if state[1] == code:
                    return state

    try:
        assert not client.ready
        client.config = replace(config, options_interval=1)
        await client.start()
        assert await next_state(200) == (True, 200)
        status = 503
        assert await next_state(503) == (False, 503)
        status = 200
        assert await next_state(200) == (True, 200)
        status = None
        assert await next_state(408) == (False, 408)
        assert client.snapshot()["trunk_last_sip_event"] == "OPTIONS"
        assert all(request.uri == "sip:peer.example" for request in packets)
        assert all(request.header("Call-ID") == client._keepalive_call_id for request in packets)
        assert not client.registered and client._refresh_task is None
        await asyncio.wait_for(client.stop(), 1)
        assert client._keepalive_task is client._receive_task is None
        assert client.transport is client.writer is None
        count = len(packets)
        await asyncio.sleep(0.03)
        assert len(packets) == count
        assert not client.ready
    finally:
        await client.stop()
        server.close()
        if transport_name == "tcp":
            await server.wait_closed()
        for writer in tuple(connections):
            writer.close()
            await writer.wait_closed()
