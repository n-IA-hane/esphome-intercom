"""Home Assistant boundary tests for application-level SIP methods."""

from __future__ import annotations

import time

from homeassistant.core import HomeAssistant
import pytest

from custom_components.voip_stack.const import EVENT_SIP_MESSAGE
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.sip_registrar import (
    SipAccount,
    SipRegistrar,
    SipRegistration,
)


pytestmark = pytest.mark.ha


def _request(*, content_type: str = "text/plain", body: bytes = b"hello"):
    return sip.parse_message(
        sip.build_request(
            "MESSAGE",
            "sip:Casa@localhost",
            [
                ("From", "<sip:spoofed@localhost>;tag=sender"),
                ("To", "<sip:Casa@localhost>"),
                ("Call-ID", "message-call"),
                ("CSeq", "1 MESSAGE"),
                ("Content-Type", content_type),
            ],
            body,
        )
    )


@pytest.mark.asyncio
async def test_message_uses_authenticated_registration_identity(
    hass: HomeAssistant,
) -> None:
    registrar = SipRegistrar(
        enabled=True,
        accounts=[SipAccount("alice", "Alice", "secret")],
        local_ip="127.0.0.1",
        local_sip_port=5060,
    )
    registrar.registrations["alice"] = SipRegistration(
        username="alice",
        contact_uri="sip:alice@127.0.0.2:5060",
        source_host="127.0.0.2",
        source_port=5060,
        transport="UDP",
        expires_at=time.time() + 300,
    )
    events = []
    hass.bus.async_listen(EVENT_SIP_MESSAGE, events.append)

    result = await SipApplicationMethods(hass, registrar).handle(
        _request(), ("127.0.0.2", 5060), "UDP"
    )
    await hass.async_block_till_done()

    assert result.status == 200
    assert events[0].data == {
        "sender": "alice",
        "recipient": "Casa",
        "content_type": "text/plain",
        "message": "hello",
    }


@pytest.mark.asyncio
async def test_message_rejects_unknown_flow_and_unsupported_body(
    hass: HomeAssistant,
) -> None:
    registrar = SipRegistrar(
        enabled=True,
        accounts=[],
        local_ip="127.0.0.1",
        local_sip_port=5060,
    )
    methods = SipApplicationMethods(hass, registrar)

    assert (
        await methods.handle(_request(), ("127.0.0.3", 5060), "UDP")
    ).status == 403

    registrar.accounts["alice"] = SipAccount("alice", "Alice", "secret")
    registrar.registrations["alice"] = SipRegistration(
        username="alice",
        contact_uri="sip:alice@127.0.0.2:5060",
        source_host="127.0.0.2",
        source_port=5060,
        transport="UDP",
        expires_at=time.time() + 300,
    )
    assert (
        await methods.handle(
            _request(content_type="application/octet-stream"),
            ("127.0.0.2", 5060),
            "UDP",
        )
    ).status == 415
