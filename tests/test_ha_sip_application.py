"""Home Assistant boundary tests for application-level SIP methods."""

from __future__ import annotations

import time

from homeassistant.core import HomeAssistant
import pytest

from custom_components.voip_stack.const import EVENT_SIP_MESSAGE, EVENT_SIP_PRESENCE
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


def _publish(
    *,
    body: bytes,
    expires: int = 300,
    entity_tag: str = "",
    target: str = "alice",
):
    headers = [
        ("From", "<sip:alice@localhost>;tag=sender"),
        ("To", f"<sip:{target}@localhost>"),
        ("Call-ID", "publish-call"),
        ("CSeq", "1 PUBLISH"),
        ("Event", "presence"),
        ("Expires", str(expires)),
    ]
    if body:
        headers.append(("Content-Type", "application/pidf+xml"))
    if entity_tag:
        headers.append(("SIP-If-Match", entity_tag))
    return sip.parse_message(
        sip.build_request("PUBLISH", f"sip:{target}@localhost", headers, body)
    )


def _pidf(account: str = "alice") -> bytes:
    return (
        '<presence xmlns="urn:ietf:params:xml:ns:pidf" '
        f'entity="sip:{account}@localhost"><tuple id="phone">'
        "<status><basic>open</basic></status></tuple></presence>"
    ).encode()


def _subscribe(*, target: str = "alice", expires: int = 300):
    return sip.parse_message(
        sip.build_request(
            "SUBSCRIBE",
            f"sip:{target}@localhost",
            [
                ("From", "<sip:alice@localhost>;tag=subscriber"),
                ("To", f"<sip:{target}@localhost>"),
                ("Contact", "<sip:alice@127.0.0.2:5060>"),
                ("Call-ID", "subscribe-call"),
                ("CSeq", "1 SUBSCRIBE"),
                ("Event", "presence"),
                ("Expires", str(expires)),
            ],
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


@pytest.mark.asyncio
async def test_publish_presence_create_refresh_and_remove(
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
    hass.bus.async_listen(EVENT_SIP_PRESENCE, events.append)
    methods = SipApplicationMethods(hass, registrar)

    created = await methods.handle(
        _publish(body=_pidf()), ("127.0.0.2", 5060), "UDP"
    )
    entity_tag = dict(created.headers)["SIP-ETag"]
    refreshed = await methods.handle(
        _publish(body=b"", entity_tag=entity_tag),
        ("127.0.0.2", 5060),
        "UDP",
    )
    removed = await methods.handle(
        _publish(body=b"", expires=0, entity_tag=entity_tag),
        ("127.0.0.2", 5060),
        "UDP",
    )
    await hass.async_block_till_done()

    assert created.status == refreshed.status == removed.status == 200
    assert dict(refreshed.headers)["SIP-ETag"] == entity_tag
    assert methods.publications == {}
    assert methods.publication_timers == {}
    assert methods.tasks == set()
    assert [event.data["published"] for event in events] == [True, True, False]


@pytest.mark.asyncio
async def test_publish_rejects_cross_account_and_stale_entity_tag(
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
    methods = SipApplicationMethods(hass, registrar)

    cross_account = await methods.handle(
        _publish(body=_pidf("bob"), target="bob"),
        ("127.0.0.2", 5060),
        "UDP",
    )
    stale = await methods.handle(
        _publish(body=b"", entity_tag="unknown"),
        ("127.0.0.2", 5060),
        "UDP",
    )

    assert cross_account.status == 403
    assert stale.status == 412


@pytest.mark.asyncio
async def test_presence_subscription_notifies_initial_and_published_state(
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
    notifications = []

    async def send_follow_up(request, addr, result):
        notifications.append((request, addr, result))
        return None

    methods = SipApplicationMethods(hass, registrar)
    subscribed = await methods.handle(
        _subscribe(),
        ("127.0.0.2", 5060),
        "UDP",
        send_follow_up,
    )
    assert subscribed.status == 200
    assert subscribed.to_tag
    assert subscribed.follow_up is not None
    assert dict(subscribed.follow_up.headers)["Subscription-State"].startswith(
        "active;expires="
    )
    assert b"<basic>closed</basic>" in subscribed.follow_up.body

    published = await methods.handle(
        _publish(body=_pidf()),
        ("127.0.0.2", 5060),
        "UDP",
        send_follow_up,
    )
    await hass.async_block_till_done()

    assert published.status == 200
    assert len(notifications) == 1
    assert notifications[0][2].follow_up is not None
    assert b"<basic>open</basic>" in notifications[0][2].follow_up.body
    await methods.stop()
    assert methods.subscriptions == {}


@pytest.mark.asyncio
async def test_presence_subscription_requires_authenticated_from_identity(
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
    spoofed = sip.parse_message(
        sip.build_request(
            "SUBSCRIBE",
            "sip:alice@localhost",
            [
                ("From", "<sip:bob@localhost>;tag=spoofed"),
                ("To", "<sip:alice@localhost>"),
                ("Contact", "<sip:bob@127.0.0.2:5060>"),
                ("Call-ID", "spoofed-subscribe"),
                ("CSeq", "1 SUBSCRIBE"),
                ("Event", "presence"),
            ],
        )
    )

    result = await SipApplicationMethods(hass, registrar).handle(
        spoofed,
        ("127.0.0.2", 5060),
        "UDP",
        lambda *_args: None,
    )

    assert result.status == 403
