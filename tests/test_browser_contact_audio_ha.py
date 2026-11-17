"""Audio negotiation is capability driven for an ordinary registered SIP phone."""

import asyncio
import secrets
import re
import socket
from unittest.mock import AsyncMock

import pytest

from custom_components import voip_stack
from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot
from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.sip_registrar import SipAccount
from tests.test_automation_originate_ha import lab as lab
from tests.test_sip_capabilities_ha import CAPS as BASE_CAPS

# This independent peer explicitly supports the concrete D1 quantum it answers.
CAPS = BASE_CAPS + b"a=ptime:10\r\n"

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("profile", ["d1", "standard", "changed", "unknown"])
async def test_generic_registered_phone_negotiates_declared_audio_and_answer(lab, monkeypatch, profile):
    """Public browser call to a generic SIP account, without ESP identity rules."""
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        # Stay outside the host client ephemeral range: releasing a successful
        # bind(0) probe otherwise races concurrent outgoing test connections.
        udp.bind(("127.0.0.1", 20000 + secrets.randbelow(10000)))
        listener_port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", listener_port))
    lab.runtime.transport_config.update({
        "sip_port": listener_port, "sip_registrar_enabled": True,
        CONF_SIP_VIDEO: False, CONF_VIDEO_CAMERA_SEND: False,
    })
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)

    source = PhoneEndpoint(endpoint_id="browser:camera-wire", name="Camera caller",
        device_id="camera-wire-source", kind=EndpointKind.BROWSER,
        capabilities=frozenset({"audio", "dtmf", "video"}))
    receiver = PhoneEndpoint(endpoint_id="sip:camera-wire", name="Wire receiver",
        username="camera-wire-receiver", kind=EndpointKind.SIP_ACCOUNT)
    lab.runtime.endpoints.register(source)
    lab.runtime.endpoints.register(receiver)
    assert not any(item.kind is EndpointKind.ESPHOME for item in lab.runtime.endpoints.endpoints)
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount(receiver.username, receiver.name, "fixture-only")])
    lab.hass.services.async_register("voip_stack", "set_send_video", voip_stack._handle_set_send_video_service)

    replies, acks = asyncio.Queue(), asyncio.Queue()
    options = asyncio.Event()
    requests = []
    peer_errors = []

    class Receiver(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            try:
                message = sip.parse_message(raw)
                if message.is_response:
                    replies.put_nowait(message)
                    return
                headers = [(key, message.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
                to = message.header("To")
                headers.append(("To", to if ";tag=" in to else to + ";tag=wire-receiver"))
                body = b""
                if message.method == "OPTIONS":
                    options.set()
                    if profile == "unknown":
                        return  # No OPTIONS answer must not block an ordinary call.
                    body = CAPS if profile in {"d1", "changed"} else (
                        b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Generic phone\r\n"
                        b"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 0 RTP/AVP 0\r\n"
                        b"a=rtpmap:0 PCMU/8000/1\r\na=ptime:20\r\na=sendrecv\r\n")
                elif message.method in {"INVITE", "ACK", "BYE"}:
                    requests.append((message, address))
                    if message.method == "ACK":
                        acks.put_nowait(message)
                        return
                    if message.method == "INVITE":
                        if profile == "d1":
                            receive = re.search(rb"a=rtpmap:(\d+) L16/48000(?:/1)?\r", message.body)[1].decode()
                            send = re.search(rb"a=rtpmap:(\d+) L16/16000(?:/1)?\r", message.body)[1].decode()
                            assert b"a=ptime:10\r\n" in message.body
                            assert b"a=x-voip-stack-flow:" in message.body
                            audio = (f"m=audio 35000 RTP/AVP {receive} {send}\r\n"
                                f"a=rtpmap:{receive} L16/48000/1\r\na=rtpmap:{send} L16/16000/1\r\n"
                                f"a=x-voip-stack-flow:{receive} recv\r\na=x-voip-stack-flow:{send} send\r\n"
                                "a=ptime:10\r\na=sendrecv\r\n")
                        else:
                            payload = re.search(rb"a=rtpmap:(\d+) PCMU/8000(?:/1)?\r", message.body)[1].decode()
                            audio = (f"m=audio 35000 RTP/AVP {payload}\r\n"
                                f"a=rtpmap:{payload} PCMU/8000/1\r\na=ptime:20\r\na=sendrecv\r\n")
                        body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Generic receiver\r\n"
                            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n" + audio).encode()
                        port = self.transport.get_extra_info("sockname")[1]
                        headers.append(("Contact", f"<sip:{receiver.username}@127.0.0.1:{port};transport=udp>"))
                else:
                    return
                if body:
                    headers.append(("Content-Type", "application/sdp"))
                self.transport.sendto(sip.build_response(200, "OK", headers, body), address)
            except Exception as error:
                peer_errors.append(error)

    transport, _peer = await lab.hass.loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
    peer_port = transport.get_extra_info("sockname")[1]
    registration_uri = f"sip:127.0.0.1:{listener_port}"

    async def register(sequence, authorization=None):
        headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_port};branch=z9hG4bK-camera-{sequence};rport"),
            ("From", f"<sip:{receiver.username}@127.0.0.1>;tag=camera-registration"),
            ("To", f"<sip:{receiver.username}@127.0.0.1>"), ("Call-ID", "camera-registration"),
            ("CSeq", f"{sequence} REGISTER"),
            ("Contact", f"<sip:{receiver.username}@127.0.0.1:{peer_port}>"),
            ("Expires", "60"), ("User-Agent", "Ordinary receiving phone")]
        if authorization:
            headers.append(("Authorization", authorization))
        transport.sendto(sip.build_request("REGISTER", registration_uri, headers), ("127.0.0.1", listener_port))
        async with asyncio.timeout(2):
            while True:
                reply = await replies.get()
                if reply.header("CSeq").endswith(" REGISTER"):
                    return reply

    async def action(name, **data):
        await lab.hass.services.async_call("voip_stack", name, {"device_id": source.device_id, **data}, blocking=True)

    try:
        challenge = await register(1)
        assert challenge.status_code == 401
        authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username=receiver.username, password="fixture-only", method="REGISTER", uri=registration_uri)
        assert (await register(2, authorization)).status_code == 200
        await asyncio.wait_for(options.wait(), 2)
        if profile != "unknown":
            async with asyncio.timeout(2):
                while registrar.registered_contacts(receiver.username)[0].capabilities is None:
                    await asyncio.sleep(.01)
        from custom_components.voip_stack.directory_runtime import async_refresh_directory
        from custom_components.voip_stack.roster import dump_roster_json
        directory = await async_refresh_directory(lab.hass)
        # Publish the entity platform's canonical directory result in this socket fixture.
        lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(directory)})
        call_ids = []
        for cycle in range(2):
            await action("call", destination=receiver.name, send_video=False)
            initial_ack = await asyncio.wait_for(acks.get(), 2)
            call_id = initial_ack.header("Call-ID")
            call_ids.append(call_id)
            client = lab.calls.sip_client_for(call_id)
            assert client.dialog is not None
            if profile == "d1":
                assert client.dialog.send_format.sample_rate == 48000 and client.dialog.recv_format.sample_rate == 16000
                assert client.dialog.send_format.frame_ms == client.dialog.recv_format.frame_ms == 10
            else:
                assert client.dialog.send_format.encoding == client.dialog.recv_format.encoding == "PCMU"
                assert client.dialog.send_format.sample_rate == client.dialog.recv_format.sample_rate == 8000
                assert client.dialog.send_format.frame_ms == 20
                # The local receive recommendation remains 10 ms when no
                # usable discovery exists, independently of peer RX20.
                assert client.dialog.recv_format.frame_ms == (20 if profile == "standard" else 10)
            await action("hangup", call_id=call_id)
            assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
            assert not peer_errors
        assert call_ids[0] != call_ids[1]
        observed = [(request.method, request.header("Call-ID")) for request, _address in requests]
        assert [method for method, _call in observed] == ["INVITE", "ACK", "BYE"] * 2
        # RFC 3581 permits sent-by and UDP source to use different ports.
        # Ordinary calls own their dialog socket; RFC 5626 flow reuse is
        # qualified separately for Contacts that actually request it.
        for call_id in call_ids:
            assert len({address for request, address in requests
                if request.header("Call-ID") == call_id}) == 1
        first = [request for request, _address in requests if request.method == "INVITE" and request.header("Call-ID") == call_ids[0]]
        assert [int(request.header("CSeq").split()[0]) for request in first] == sorted({int(request.header("CSeq").split()[0]) for request in first})
        assert len({re.search(rb"m=audio (\d+)", request.body)[1] for request in first}) == 1
    finally:
        from custom_components.voip_stack.endpoint_session import TerminationIntent
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        transport.close()
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
