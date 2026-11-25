"""A remote PBX can reach a trunk call through HA's advertised TCP Contact."""

import asyncio

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_runtime import configure_local_contact
from custom_components.voip_stack.sip_tcp_io import read_sip_stream_message
from custom_components.voip_stack.sip_trunk import SipTrunkClient, SipTrunkConfig
from custom_components.voip_stack.trunk_signaling import reuse_registered_trunk_flow
from .test_trunk_calls_ha import trunks, lab  # noqa: F401

pytestmark = [pytest.mark.ha, pytest.mark.integration]


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_static_tcp_trunk_bye_arrives_on_a_new_connection_to_stable_contact(trunks):
    lab = trunks
    requests, writers, tasks = [], [], set()
    async def remote_pbx(reader, writer):
        writers.append(writer)
        tasks.add(asyncio.current_task())
        try:
            while raw := await read_sip_stream_message(reader):
                request = sip.parse_message(raw)
                requests.append(request)
                if request.method == "ACK":
                    continue
                writer.write(sip.build_uas_response(request, 200, "OK", to_tag="remote-pbx",
                    contact_uri=f"sip:remote@127.0.0.1:{writer.get_extra_info('sockname')[1]};transport=tcp",
                    body=request.body if request.method == "INVITE" else b""))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())
    server = await asyncio.start_server(remote_pbx, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    manager = lab.calls.component("udp_listener")
    trunk = SipTrunkClient(config=SipTrunkConfig(enabled=True, register=False, transport="tcp",
        server="127.0.0.1", port=port, domain="test", username="line", auth_username="", password="",
        expires=300, allowed_ips=("127.0.0.1",)), local_ip="127.0.0.1", local_sip_port=manager.port)
    client = SipCallClient(local_ip="127.0.0.1", local_name="line", local_sip_port=manager.port,
        local_rtp_port=41000, signaling_transport="TCP")
    configure_local_contact(lab.hass, client)
    reuse_registered_trunk_flow(trunk, client)
    ending = None
    try:
        assert await client.invite(target="remote", remote_host="127.0.0.1", remote_sip_port=port, timeout=2) == "in_call"
        invite = next(request for request in requests if request.method == "INVITE")
        contact = sip.parse_sip_uri(invite.header("Contact"))
        ending = asyncio.create_task(client.wait_for_dialog_termination(timeout=2))
        # This connection belongs to the peer, not to SipCallClient's outgoing
        # stream. HA's normal listener must find the existing dialog owner.
        reader, writer = await asyncio.open_connection(contact.host, contact.port)
        writers.append(writer)
        writer.write(sip.build_request("BYE", str(contact), [
            ("Via", f"SIP/2.0/TCP 127.0.0.1:{port};branch=z9hG4bK-new-flow"),
            ("From", invite.header("To") + ";tag=remote-pbx"), ("To", invite.header("From")),
            ("Call-ID", invite.header("Call-ID")), ("CSeq", "5 BYE")]))
        await writer.drain()
        response = sip.parse_message(await asyncio.wait_for(read_sip_stream_message(reader), 2))
        assert response.status_code == 200 and response.header("CSeq") == "5 BYE"
        assert await ending == "remote_hangup"
        assert contact.port == manager.port
    finally:
        await client.close()
        if ending is not None:
            ending.cancel()
            await asyncio.gather(ending, return_exceptions=True)
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        if tasks:
            await asyncio.gather(*tuple(tasks))
        server.close()
        await server.wait_closed()
