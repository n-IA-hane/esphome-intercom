"""Native HA admission proves notifications, wire refusal and secure-only cleanup."""
from __future__ import annotations

import asyncio
from hashlib import md5
import re
import socket

import pytest
from homeassistant.components import persistent_notification

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_listener import SipUdpEndpoint
from custom_components.voip_stack.sip_tcp_io import read_sip_stream_message
from .test_automation_originate_ha import PHONE_PASSWORD, lab as lab

pytestmark = pytest.mark.ha


def independent_digest(challenge, uri, password, count):
    params = {key.lower(): quoted or token for key, quoted, token in
        re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', challenge)}
    realm, nonce = params["realm"], params["nonce"]
    nc = f"{count:08x}"
    def digest(value):
        return md5(value.encode()).hexdigest()
    response = digest(f'{digest(f"bedroom0:{realm}:{password}")}:{nonce}:{nc}:native:auth:{digest(f"INVITE:{uri}")}')
    return (f'Digest username="bedroom0", realm="{realm}", nonce="{nonce}", uri="{uri}", '
        f'response="{response}", algorithm=MD5, qop=auth, nc={nc}, cnonce="native"')


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_invite_block_uses_public_notification_and_does_not_allocate_calls(lab, transport):
    registrar = lab.calls.component("registrar")
    notifications = []
    unsubscribe = persistent_notification.async_register_callback(lab.hass,
        lambda update, values: notifications.extend(values.values()))
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    writer = source = None
    if transport == "TCP":
        reader, writer = await asyncio.open_connection(*listener)
        port = writer.get_extra_info("sockname")[1]
        async def send(raw):
            writer.write(raw)
            await writer.drain()
        async def receive():
            return await read_sip_stream_message(reader)
    else:
        source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        source.bind(("127.0.0.1", 0))
        source.setblocking(False)
        port = source.getsockname()[1]
        async def send(raw):
            await lab.hass.loop.sock_sendto(source, raw, listener)
        async def receive():
            return await lab.hass.loop.sock_recv(source, 65535)
    uri = f"sip:bedroom1@127.0.0.1:{listener[1]}"
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Native admission\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        "m=audio 35000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=ptime:20\r\n").encode()

    async def attempt(sequence, authorization=""):
        headers = [("Via", f"SIP/2.0/{transport} 127.0.0.1:{port};branch=z9hG4bK-native-{sequence};rport"),
            ("From", "<sip:bedroom0@127.0.0.1>;tag=independent"), ("To", f"<{uri}>"),
            ("Call-ID", f"native-admission-{sequence}"), ("CSeq", "1 INVITE"),
            ("Contact", f"<sip:bedroom0@127.0.0.1:{port}>"), ("Content-Type", "application/sdp")]
        if authorization:
            headers.append(("Authorization", authorization))
        await send(sip.build_request("INVITE", uri, headers, offer))
        async with asyncio.timeout(3):
            while True:
                response = sip.parse_message(await receive())
                if response.status_code >= 200:
                    break
        await send(sip.build_request("ACK", uri,
            [(name, response.header(name)) for name in ("Via", "From", "To", "Call-ID")] + [("CSeq", "1 ACK")]))
        return response

    try:
        challenge_response = await attempt(0)
        assert challenge_response.status_code == 401
        assert not registrar.authentication_failures
        challenge = challenge_response.header("WWW-Authenticate")
        for sequence in range(1, 11):
            response = await attempt(sequence, independent_digest(challenge, uri, "incorrect", sequence))
            assert response.status_code == (401 if sequence < 10 else 403)
        count = len(registrar.nonces)
        response = await attempt(11, independent_digest(challenge, uri, PHONE_PASSWORD, 11))
        assert response.status_code == 403 and response.header("Retry-After")
        await lab.hass.async_block_till_done()
        assert len(registrar.nonces) == count
        assert len(notifications) == 1 and "127.0.0.1" in notifications[0]["message"]
        assert "300" in notifications[0]["message"]
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
        assert not any(phone.invites for phone in lab.phones)
    finally:
        unsubscribe()
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        if source is not None:
            source.close()


async def test_secure_media_only_offer_gets_clean_488_without_dialog_or_media(hass, socket_enabled):
    accepted = []
    async def answer(invite):
        accepted.append(invite)
        raise AssertionError("A secure-only offer must never reach call allocation")
    owner = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=44000,
        supported_formats=[AudioFormat(8000, frame_ms=20)], on_invite=answer)
    server, _ = await hass.loop.create_datagram_endpoint(lambda: owner, local_addr=("127.0.0.1", 0))
    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(("127.0.0.1", 0))
    source.setblocking(False)
    address = server.get_extra_info("sockname")
    uri = f"sip:ha@127.0.0.1:{address[1]}"
    headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{source.getsockname()[1]};branch=z9hG4bK-secure-only;rport"),
        ("From", "<sip:phone@127.0.0.1>;tag=phone"), ("To", f"<{uri}>"),
        ("Call-ID", "secure-media-only"), ("CSeq", "1 INVITE"), ("Content-Type", "application/sdp")]
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Secure-only peer\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        "m=audio 35000 RTP/SAVP 8\r\na=rtpmap:8 PCMA/8000\r\n"
        "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:dGVzdC1rZXktbm90LXJlYWwtc2VjcmV0\r\n").encode()
    try:
        await hass.loop.sock_sendto(source, sip.build_request("INVITE", uri, headers, offer), address)
        async with asyncio.timeout(3):
            while True:
                response = sip.parse_message(await hass.loop.sock_recv(source, 8192))
                if response.status_code >= 200:
                    break
        assert response.status_code == 488 and not response.body
        assert not accepted and not owner.active_dialogs and not owner.pending_invites
        await hass.loop.sock_sendto(source, sip.build_request("ACK", uri,
            [(name, response.header(name)) for name in ("Via", "From", "To", "Call-ID")] + [("CSeq", "1 ACK")]), address)
        await hass.async_block_till_done()
    finally:
        source.close()
        server.close()
        await owner.wait_closed()
    assert not owner._invite_tasks and not owner.active_dialogs and not owner.pending_invites
