"""Complete authenticated INVITEs keep the registrar-selected UDP Outbound flow."""

import asyncio
import errno
import socket

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_auth import verify_digest_authorization
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack import sip_runtime
from .test_udp_outbound_flow_ha import (
    PASSWORD, wire_lab as wire_lab, register_phone, reserve_udp_tcp_sockets,
    trunks as trunks, lab as lab,
)
from .test_notify_transport_ha import read_record

pytestmark = [pytest.mark.ha, pytest.mark.parametrize("trunks", [True], indirect=True),
              pytest.mark.parametrize("wire_lab", [True], indirect=True)]


async def test_rejected_second_transport_bind_releases_its_tcp_reservation(wire_lab):
    """A failed UDP bind must leave its successful TCP probe reusable."""
    wire_lab.tcp_socket.close()
    with pytest.raises(OSError) as error:
        reserve_udp_tcp_sockets(host=wire_lab.a.address[0], port=wire_lab.a.address[1])
    assert error.value.errno == errno.EADDRINUSE
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        tcp.bind(wire_lab.a.address)
    assert not wire_lab.a.transport.is_closing()


@pytest.mark.parametrize("mode", ["authenticated", "oversize", "cancel-preparation", "stale-binding"])
async def test_large_outbound_invite_never_replaces_its_selected_flow_or_another_lease(wire_lab, monkeypatch, caplog, mode):
    wire = wire_lab
    binding = await register_phone(wire, discover=False)
    uri = sip.parse_sip_uri(binding.contact_uri)
    selected = wire.registrar.select_outbound_flow(binding.username, uri)
    listener = wire.manager.udp_server.endpoint
    original_transport = listener.transport
    tcp_attempts, authenticated, first_requests = [], [], []
    handlers, writers = set(), []
    original_receive = wire.a.datagram_received

    def datagram(raw, address):
        request = sip.parse_message(raw)
        if request.method == "INVITE":
            if not request.header("Authorization"):
                first_requests.append(request)
                headers = [(key, request.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
                headers.append(("WWW-Authenticate", 'Digest realm="outbound", nonce="independent", qop="auth"'))
                wire.a.transport.sendto(sip.build_response(401, "Unauthorized", headers), address)
                return
            verify_digest_authorization(authorization_header=request.header("Authorization"),
                username=binding.username, password=PASSWORD, method="INVITE", uri=request.uri,
                realm="outbound", nonce="independent")
            authenticated.append((request, address, raw))
        original_receive(raw, address)

    monkeypatch.setattr(wire.a, "datagram_received", datagram)

    async def tcp(reader, writer):
        handlers.add(asyncio.current_task())
        writers.append(writer)
        try:
            raw = await read_record(reader)
            request = sip.parse_message(raw)
            tcp_attempts.append(request)
            writer.write(sip.build_response(486, "Busy Here", [
                (key, request.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]))
            await writer.drain()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            handlers.discard(asyncio.current_task())
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(tcp, sock=wire.tcp_socket)
    name = "Independent reception desk " * (3000 if mode == "oversize" else 48)
    client = SipCallClient(local_ip="127.0.0.1", local_name=name, local_uri_user="caller",
        local_sip_port=wire.listener[1], local_rtp_port=40000,
        supported_formats=[AudioFormat(16000, frame_ms=20)], username=binding.username, password=PASSWORD)
    other = SipCallClient(local_ip="127.0.0.1", local_name="Other lease", local_uri_user="other",
        local_sip_port=wire.listener[1], local_rtp_port=40002, supported_formats=[AudioFormat(16000, frame_ms=20)])
    for owner in (other, client):
        if mode == "stale-binding" and owner is client:
            binding.capability_generation = "replacement-binding"
            with pytest.raises(ConnectionError, match="binding"):
                sip_runtime.enable_reused_signaling_flow(wire.lab.hass, owner, uri,
                    target=binding.username, default_sip_port=5060, selected_flow=selected)
        else:
            assert sip_runtime.enable_reused_signaling_flow(wire.lab.hass, owner, uri,
                target=binding.username, default_sip_port=5060, selected_flow=selected)
    ready, release = asyncio.Event(), asyncio.Event()
    task = None
    if mode == "cancel-preparation":
        async def prepare(_host):
            ready.set()
            await release.wait()
        client.prepare_media = prepare
    try:
        result = "binding_rejected"
        if mode != "stale-binding":
            task = asyncio.create_task(client.invite(target=binding.username, remote_host=uri.host,
                remote_sip_port=uri.port, request_uri=str(uri), timeout=2))
            if mode == "cancel-preparation":
                await asyncio.wait_for(ready.wait(), 1)
                client.request_cancel()
                release.set()
            result = await asyncio.wait_for(task, 3)
        if mode == "authenticated":
            assert result == "in_call"
            assert len(first_requests) == len(authenticated) == 1
            request, source, raw = authenticated[0]
            assert len(raw) > 1300 and source == wire.listener and "SIP/2.0/UDP" in request.header("Via")
            assert request.uri == first_requests[0].uri == str(uri)
            for key in ("Call-ID", "From", "To"):
                assert request.header(key) == first_requests[0].header(key)
            assert sip.parse_cseq(request.header("CSeq")).number > sip.parse_cseq(first_requests[0].header("CSeq")).number
            assert await client.terminate(timeout=1) == "remote_hangup"
            bye, source = await wire.a.receive("BYE")
            assert source == wire.listener and not [request for request in wire.b.requests if request.method == "INVITE"]
        else:
            assert result == {"cancel-preparation": "cancelled", "oversize": "transport_unreachable",
                              "stale-binding": "binding_rejected"}[mode]
            assert not first_requests and not authenticated
            if mode == "oversize":
                assert "SIP request exceeds UDP datagram limit" in caplog.text
        assert not tcp_attempts and client.signaling_transport == "UDP"
        assert not [request for request in wire.b.requests if request.method == "INVITE"]
        await client.close()
        assert (wire.a.address, other.dialog_ids.call_id) in listener._dialog_queues
        assert not any(key[1] == client.dialog_ids.call_id for key in listener._dialog_queues)
        assert listener.transport is original_transport and wire.registrar.registered_contacts(binding.username)
        await other.close()
        assert not listener._dialog_queues and not listener._reused_dialog_keys
    finally:
        release.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await other.close()
        for writer in writers:
            writer.close()
        await asyncio.gather(*handlers, return_exceptions=True)
        server.close()
        await server.wait_closed()
