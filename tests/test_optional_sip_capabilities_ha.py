"""OPTIONS optimizes offers, but an ordinary account needs no prior query."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND
from custom_components.voip_stack.directory_runtime import async_refresh_directory
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.roster import dump_roster_json
from custom_components.voip_stack.sip_registrar import SipAccount
from tests.test_sip_options_capabilities_ha import CAPS
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("metadata, expected", [
    ({"sip_capabilities_known": False, "capabilities": ["audio"]}, None),
    ({"sip_capabilities_known": False, "capabilities": ["video"], "sip_video_codec": "jpeg"}, None),
    ({"sip_capabilities_known": True, "sip_video_codec": ""}, ""),
    ({"sip_capabilities_known": True, "sip_video_codec": "jpeg"}, "jpeg"),
])
def test_video_capability_contract_distinguishes_unknown_and_audio_only(metadata, expected):
    from custom_components.voip_stack.endpoint_routing import peer_video_codec
    assert peer_video_codec(None, SimpleNamespace(metadata=metadata)) == expected


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("query", ["late", "empty", "405", "timeout"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_outgoing_media_does_not_wait_for_optional_options(trunks, query, legacy):
    runtime = trunks
    username = "ordinary-unqueried"
    endpoint = PhoneEndpoint(endpoint_id="sip:unqueried", name="Unqueried phone",
        username=username, kind=EndpointKind.SIP_ACCOUNT)
    browser = PhoneEndpoint(endpoint_id="browser:unqueried-caller", name="Caller",
        device_id="unqueried-caller", kind=EndpointKind.BROWSER,
        capabilities=frozenset({"audio", "dtmf", "video"}))
    runtime.runtime.endpoints.register(endpoint)
    runtime.runtime.endpoints.register(browser)
    runtime.runtime.transport_config.update({CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: True})
    registrar = runtime.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount(username, endpoint.name, "fixture-only")])
    replies, invites, held_options = asyncio.Queue(), asyncio.Queue(), []

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def answer(self, request, address, status=200, body=b""):
            headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
            headers.append(("To", request.header("To") if ";tag=" in request.header("To") else request.header("To") + ";tag=ordinary"))
            if status == 405:
                headers.append(("Allow", "INVITE, ACK, BYE, CANCEL"))
            if body:
                headers.extend((("Content-Type", "application/sdp"),
                    ("Contact", f"<sip:{username}@127.0.0.1:{self.transport.get_extra_info('sockname')[1]}>")))
            self.transport.sendto(sip.build_response(status, {200: "OK", 405: "Method Not Allowed", 488: "Not Acceptable Here"}[status], headers, body), address)

        def datagram_received(self, raw, address):
            message = sip.parse_message(raw)
            if message.is_response:
                replies.put_nowait(message)
            elif message.method == "OPTIONS":
                if query in {"late", "timeout"}:
                    held_options.append((message, address))
                else:
                    self.answer(message, address, 405 if query == "405" else 200)
            elif message.method == "INVITE":
                invites.put_nowait(message)
                formats = sdp.offered_pcm_formats(message.body)
                video = any(item.encoding == "JPEG" for item in sdp.offered_video_formats(message.body))
                wanted = next((item for item in formats if item.encoding == ("PCMA" if legacy else "L16")
                    and item.sample_rate == (8000 if legacy else 48000)), None)
                recv = wanted if legacy else next((item for item in formats if item.encoding == "L16" and item.sample_rate == 16000), None)
                if wanted is None or recv is None or not video:
                    self.answer(message, address, 488)
                    return
                payloads = str(wanted.payload_type) if legacy else f"{wanted.payload_type} {recv.payload_type}"
                body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent peer\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    f"m=audio 35000 RTP/AVP {payloads}\r\n"
                    f"a=rtpmap:{wanted.payload_type} {'PCMA/8000/1' if legacy else 'L16/48000/1'}\r\n")
                if not legacy:
                    body += (f"a=rtpmap:{recv.payload_type} L16/16000/1\r\n"
                        f"a=x-voip-stack-flow:{wanted.payload_type} recv\r\n"
                        f"a=x-voip-stack-flow:{recv.payload_type} send\r\n")
                body += (f"a=ptime:{20 if legacy else 10}\r\na=sendrecv\r\n"
                    "m=video 35002 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\na=sendrecv\r\n")
                self.answer(message, address, body=body.encode())
            elif message.method != "ACK":
                self.answer(message, address)

    transport, peer = await runtime.hass.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    local_port = transport.get_extra_info("sockname")[1]
    remote_port = runtime.runtime.transport_config["sip_port"]
    register_uri = f"sip:127.0.0.1:{remote_port}"

    def register(sequence, authorization=None):
        headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch=z9hG4bK-unqueried-{sequence};rport"),
            ("From", f"<sip:{username}@127.0.0.1>;tag=register"), ("To", f"<sip:{username}@127.0.0.1>"),
            ("Call-ID", "unqueried-registration"), ("CSeq", f"{sequence} REGISTER"),
            ("Contact", f"<sip:{username}@127.0.0.1:{local_port}>"), ("Expires", "60")]
        if authorization:
            headers.append(("Authorization", authorization))
        transport.sendto(sip.build_request("REGISTER", register_uri, headers), ("127.0.0.1", remote_port))

    try:
        register(1)
        challenge = await asyncio.wait_for(replies.get(), 2)
        assert challenge.status_code == 401
        authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username=username, password="fixture-only", method="REGISTER", uri=register_uri)
        register(2, authorization)
        assert (await asyncio.wait_for(replies.get(), 2)).status_code == 200
        assert registrar.registered_contacts(username)[0].capabilities is None
        # Feed the actual directory export through the registered HA state;
        # this fixture does not load entity platforms. No media hints are added.
        entries = await async_refresh_directory(runtime.hass)
        runtime.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(entries)})
        await asyncio.wait_for(runtime.hass.services.async_call("voip_stack", "call", {
            "device_id": browser.device_id, "destination": endpoint.name, "send_video": True,
        }, blocking=True), 2)
        request = await asyncio.wait_for(invites.get(), 2)
        formats = sdp.offered_pcm_formats(request.body)
        assert any(item.encoding == "JPEG" for item in sdp.offered_video_formats(request.body))
        if not legacy:
            assert any(item.encoding == "L16" and item.sample_rate == 48000 and item.frame_ms == 10 for item in formats)
        client = runtime.calls.sip_client_for(request.header("Call-ID"))
        assert client is not None and client.dialog is not None
        assert client.dialog.send_format.sample_rate == (8000 if legacy else 48000)
        assert client.dialog.recv_format.sample_rate == (8000 if legacy else 16000)
        assert client.dialog.send_format.frame_ms == (20 if legacy else 10)
        assert client.dialog.recv_format.frame_ms == 10
        if legacy:
            assert client.dialog.recv_formats[0].max_frame_ms == 20
        assert client.dialog.video_format.encoding == "JPEG"
        if query == "late":
            assert held_options and registrar.registered_contacts(username)[0].capabilities is None
            peer.answer(*held_options[0], body=CAPS)
        probe = runtime.runtime.capability_probes[username]
        await asyncio.wait_for(asyncio.shield(probe), 4)
        assert client.dialog is not None  # Missing/failed/timed-out query cannot retire this negotiated call.
    finally:
        for session in tuple(runtime.calls.sessions.values()):
            await runtime.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        transport.close()
