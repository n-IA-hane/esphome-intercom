"""Camera intent uses real SIP exchanges and retains one call's media leases."""

import asyncio
import json
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


async def test_camera_intent_follows_connected_wire_dialog_and_releases_each_generation(lab, monkeypatch, tmp_path):
    """Wire and ownership proof; no browser or synthetic RTP counter is used."""
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        listener_port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", listener_port))
    lab.runtime.transport_config.update({
        "sip_port": listener_port, "sip_registrar_enabled": True,
        CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: True,
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
                via = re.sub(r";rport(?=;|$)", f";rport={address[1]}", message.header("Via"))
                via += f";received={address[0]}"
                headers = [("Via", via), *[(key, message.header(key)) for key in ("From", "Call-ID", "CSeq")]]
                to = message.header("To")
                headers.append(("To", to if ";tag=" in to else to + ";tag=wire-receiver"))
                body = b""
                if message.method == "OPTIONS":
                    body = CAPS
                    options.set()
                elif message.method in {"INVITE", "ACK", "BYE"}:
                    requests.append((message, address))
                    if message.method == "ACK":
                        acks.put_nowait(message)
                        return
                    if message.method == "INVITE":
                        receive = re.search(rb"a=rtpmap:(\d+) L16/48000(?:/1)?\r", message.body)[1].decode()
                        send = re.search(rb"a=rtpmap:(\d+) L16/16000(?:/1)?\r", message.body)[1].decode()
                        assert b"a=ptime:10\r\n" in message.body
                        video_offer = message.body.partition(b"m=video")[2]
                        video = (("m=video 35002 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\na=recvonly\r\n"
                            if b"a=sendrecv" in video_offer else
                            "m=video 0 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\na=inactive\r\n")
                            if video_offer else "")
                        body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Wire receiver\r\n"
                            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                            f"m=audio 35000 RTP/AVP {receive} {send}\r\n"
                            f"a=rtpmap:{receive} L16/48000/1\r\na=rtpmap:{send} L16/16000/1\r\n"
                            f"a=x-voip-stack-flow:{receive} recv\r\na=x-voip-stack-flow:{send} send\r\n"
                            f"a=ptime:10\r\na=sendrecv\r\n{video}").encode()
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
            assert client.dialog is not None and client.dialog.local_video_direction == "inactive"
            assert client.dialog.send_format.sample_rate == 48000 and client.dialog.recv_format.sample_rate == 16000
            assert client.dialog.send_format.frame_ms == client.dialog.recv_format.frame_ms == 10
            audio_port = client.local_rtp_port
            audio_formats = (client.dialog.send_format, client.dialog.recv_format)
            if cycle == 0:
                for enabled, direction in ((True, "sendonly"), (False, "inactive"), (True, "sendonly")):
                    # The first independent ACK was received before this public action.
                    await action("set_send_video", send_video=enabled)
                    ack = await asyncio.wait_for(acks.get(), 2)
                    assert ack.header("Call-ID") == call_id
                    assert client.dialog.call_id == call_id and client.dialog.local_video_direction == direction
                    assert client.local_rtp_port == audio_port
                    assert (client.dialog.send_format, client.dialog.recv_format) == audio_formats
                    assert client.media_reservation.ports[0] == audio_port
                    assert set(lab.runtime.rtp_port_pool["used"]) == set(client.media_reservation.ports)
            await action("hangup", call_id=call_id)
            assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
            assert not peer_errors
        assert call_ids[0] != call_ids[1]
        observed = [(request.method, request.header("Call-ID")) for request, _address in requests]
        assert [method for method, _call in observed] == ["INVITE", "ACK"] * 4 + ["BYE", "INVITE", "ACK", "BYE"]
        trace = [{"method": request.method, "call_id": request.header("Call-ID"),
                  "cseq": request.header("CSeq"), "via": request.header("Via"), "source": list(address)}
                 for request, address in requests]
        (tmp_path / "udp-dialog-trace.json").write_text(json.dumps(trace, indent=2))
        for selected_call_id in call_ids:
            dialog_requests = [(request, address) for request, address in requests
                               if request.header("Call-ID") == selected_call_id]
            assert len({address for _request, address in dialog_requests}) == 1
            assert {request.method for request, _address in dialog_requests} == {"INVITE", "ACK", "BYE"}
            for request, address in dialog_requests:
                sent_by = request.header("Via").split(";", 1)[0].split()[-1]
                # Both are live owners in this fixture: the standard HA UDP
                # listener authenticated REGISTER; this call's socket received
                # its rport responses and retained the same tuple throughout.
                assert int(sent_by.rsplit(":", 1)[1]) in {listener_port, address[1]}
                assert ";rport" in request.header("Via")
        first = [request for request, _address in requests if request.method == "INVITE" and request.header("Call-ID") == call_ids[0]]
        assert [int(request.header("CSeq").split()[0]) for request in first] == sorted({int(request.header("CSeq").split()[0]) for request in first})
        assert len({re.search(rb"m=audio (\d+)", request.body)[1] for request in first}) == 1
    finally:
        from custom_components.voip_stack.endpoint_session import TerminationIntent
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        transport.close()
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
