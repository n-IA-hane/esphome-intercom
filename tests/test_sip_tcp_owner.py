"""TCP composition and stream behavior through the production owner APIs."""

import ast
import importlib
import inspect
import json
from pathlib import Path
import sys
import types

from .voip_phase1_support import asyncio, audio_format, sip, sip_endpoint, sip_listener


owner_module = importlib.import_module(sip_listener.__package__ + ".sip_tcp_server")


def test_transport_owner_import_does_not_load_the_request_protocol():
    name = "_isolated_sip_tcp_owner"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).parents[1] / "custom_components/voip_stack")]
    sys.modules[name] = package
    try:
        module = importlib.import_module(name + ".sip_tcp_server")
        assert module.SipTcpServer.__module__ == name + ".sip_tcp_server"
        assert name + ".sip_listener" not in sys.modules
        assert name + ".sip_client" not in sys.modules
        assert name + ".sip_endpoint" not in sys.modules
    finally:
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key)


def test_legacy_constructor_keeps_every_frozen_keyword_and_default():
    baseline = json.loads((Path(__file__).parent / "fixtures/sip_tcp_server_constructor.json").read_text())
    public = inspect.signature(sip_listener.SipTcpServer)
    assert list(public.parameters) == [field["name"] for field in baseline]
    for field in baseline:
        parameter = public.parameters[field["name"]]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        expected = inspect.Parameter.empty if field["default"] is None else ast.literal_eval(field["default"])
        assert parameter.default == expected
    owned = inspect.signature(owner_module.SipTcpServer)
    assert owned.parameters["endpoint_factory"].default is inspect.Parameter.empty
    assert list(owned.parameters) == [*public.parameters, "endpoint_factory"]
    assert issubclass(sip_listener.SipTcpServer, owner_module.SipTcpServer)


def configuration():
    async def invite(_request):
        return sip_listener.SipInviteResult(486, "Busy Here")

    return dict(host="127.0.0.1", port=0, local_ip="127.0.0.1", local_rtp_port=41000,
                supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)], on_invite=invite)


async def test_manager_composes_the_transport_owner_directly():
    manager = sip_endpoint.SipEndpointManager(**configuration(), udp_enabled=False)
    try:
        assert await manager.start()
        assert type(manager.tcp_server) is owner_module.SipTcpServer
        assert isinstance(manager.tcp_server.endpoint, sip_listener.SipUdpEndpoint)
        assert len(manager.tcp_server.endpoints) == 1
    finally:
        await manager.stop()


async def test_one_protocol_handles_fragmented_and_pipelined_requests_on_two_streams():
    created = []

    def factory(**values):
        protocol = sip_listener.SipUdpEndpoint(**values)
        created.append(protocol)
        return protocol

    server = owner_module.SipTcpServer(**configuration(), endpoint_factory=factory)
    assert await server.start()
    port = server.server.sockets[0].getsockname()[1]
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)

    def request(identity):
        return sip.build_request("OPTIONS", "sip:HA@127.0.0.1", [
            ("Via", f"SIP/2.0/TCP 127.0.0.1;branch=z9hG4bK{identity}"),
            ("From", "<sip:probe@127.0.0.1>;tag=probe"), ("To", "<sip:HA@127.0.0.1>"),
            ("Call-ID", identity), ("CSeq", "1 OPTIONS"), ("Max-Forwards", "70"),
        ])

    async def read(reader):
        record = await asyncio.wait_for(sip_listener._read_sip_stream_message(reader), 1)
        assert record is not None
        return sip.parse_message(record)

    try:
        initial = request("split-first")
        first_writer.write(initial[:13])
        await first_writer.drain()
        first_writer.write(initial[13:] + request("pipelined-second"))
        second_writer.write(request("other-stream"))
        await asyncio.gather(first_writer.drain(), second_writer.drain())
        first = [await read(first_reader), await read(first_reader)]
        second = await read(second_reader)
        assert {reply.header("Call-ID") for reply in first} == {"split-first", "pipelined-second"}
        assert second.header("Call-ID") == "other-stream"
        assert all(reply.status_code == 200 for reply in (*first, second))
        assert created == [server.endpoint] and server.endpoints == {server.endpoint}
        assert len(server._client_tasks) == 2
        assert len(server._tcp_writers) == 2
        await asyncio.wait_for(server.stop(), 1)
        assert await asyncio.wait_for(first_reader.read(), 1) == b""
        assert await asyncio.wait_for(second_reader.read(), 1) == b""
        assert not server._client_tasks and not server._writers and not server._tcp_writers
        assert not server._connections_by_host and not server.endpoints and server.endpoint is None
    finally:
        for writer in (first_writer, second_writer):
            writer.close()
        await asyncio.gather(first_writer.wait_closed(), second_writer.wait_closed())
        await server.stop()
