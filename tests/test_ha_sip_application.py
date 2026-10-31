"""Home Assistant boundary tests for application-level SIP methods."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import time

from homeassistant.core import HomeAssistant
import pytest

from custom_components.voip_stack.const import EVENT_SIP_MESSAGE, EVENT_SIP_PRESENCE
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.sip_registrar import (
    SipAccount,
    SipRegistrar,
    SipRegistration,
)


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registration_order", [("alice", "bob"), ("bob", "alice")])
async def test_two_authenticated_accounts_share_udp_flow_and_keep_subscription_identity(
    trunks, socket_enabled, registration_order,
):
    """Two account identities use one ordinary UDP client socket."""
    from .test_refer_correlation import Peer
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint

    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount(name, name.title(), "shared-flow-test-only")
                               for name in ("alice", "bob")])
    for name in ("alice", "bob"):
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="shared-" + name,
            name=name.title(), username=name, kind=EndpointKind.SIP_ACCOUNT, availability="available"))
    manager = lab.calls.component("udp_listener")
    transport, peer = await asyncio.get_running_loop().create_datagram_endpoint(
        Peer, local_addr=("127.0.0.1", 0))
    address = transport.get_extra_info("sockname")
    target = ("127.0.0.1", manager.port)
    notices = []
    extra_transports = []

    async def exchange(request, channel=transport, receiver=peer):
        channel.sendto(sip.build_request(request.method, request.uri, request.headers, request.body), target)
        async with asyncio.timeout(3):
            while True:
                message, sender = await receiver.receive()
                if not message.is_response:
                    if message.method == "NOTIFY":
                        notices.append(message)
                    receiver.answer(message, sender, 200)
                elif (message.header("Call-ID") == request.header("Call-ID")
                      and message.header("CSeq") == request.header("CSeq")):
                    return message

    async def register(name, *, expires=300, sequence=1):
        uri = f"sip:127.0.0.1:{manager.port}"
        headers = [("Via", f"SIP/2.0/UDP {address[0]}:{address[1]};branch=z9hG4bK-shared-{name}-{sequence}"),
            ("From", f"<sip:{name}@localhost>;tag=register-{name}"),
            ("To", f"<sip:{name}@localhost>"), ("Call-ID", "shared-register-" + name),
            ("CSeq", f"{sequence} REGISTER"), ("Contact", f"<sip:{name}@{address[0]}:{address[1]}>"),
            ("Expires", str(expires))]
        request = sip.parse_message(sip.build_request("REGISTER", uri, headers))
        challenge = await exchange(request)
        assert challenge.status_code == 401
        auth = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username=name, password="shared-flow-test-only", method="REGISTER", uri=uri)
        authenticated = replace(request, headers=(*request.headers, ("Authorization", auth)))
        response = await exchange(authenticated)
        assert response.status_code == 200

    try:
        for name in registration_order:
            await register(name)
        bindings = list(registrar.registrations.values())
        assert {item.username for item in bindings} == {"alice", "bob"}
        assert {(item.source_host, item.source_port, item.transport) for item in bindings} == {
            (address[0], address[1], "UDP")}
        assert registrar.account_for_source(address, "UDP") is None
        assert registrar.account_for_source(address, "TCP", username="alice") is None
        statuses = {}
        usages = {}
        for event in ("message-summary", "dialog"):
            for name in ("alice", "bob"):
                headers = [("Via", f"SIP/2.0/UDP {address[0]}:{address[1]};branch=z9hG4bK-sub-{event}-{name}"),
                    ("From", f"<sip:{name}@localhost>;tag=subscribe-{name}"),
                    ("To", f"<sip:{name}@localhost>"),
                    ("Contact", f"<sip:{name}@{address[0]}:{address[1]}>"),
                    ("Call-ID", f"shared-{event}-{name}"), ("CSeq", "1 SUBSCRIBE"),
                    ("Event", event), ("Expires", "300")]
                request = sip.parse_message(sip.build_request("SUBSCRIBE",
                    f"sip:{name}@127.0.0.1:{manager.port}", headers))
                response = await exchange(request)
                statuses[event, name] = response.status_code
                usages[event, name] = request, response
        assert statuses == {(event, name): 200 for event in ("message-summary", "dialog")
                            for name in ("alice", "bob")}
        application = lab.calls.component("sip_application")
        assert {item.subscriber for item in application.subscriptions.values()} == {"alice", "bob"}
        assert {(item.subscriber, item.mailbox_id) for item in application.subscriptions.values()
                if item.event == "message-summary"} == {("alice", "shared-alice"), ("bob", "shared-bob")}

        async def drain_until(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    message, sender = await peer.receive()
                    if not message.is_response:
                        if message.method == "NOTIFY":
                            notices.append(message)
                        peer.answer(message, sender, 200)

        def notice_key(message):
            import re
            return (message.header("Event"), re.search(r"<sip:([^@;>]+)", message.header("To"))[1])

        await drain_until(lambda: {notice_key(item) for item in notices} >= set(usages))
        assert {notice_key(item) for item in notices} == set(usages)
        wrong_transport, wrong_peer = await asyncio.get_running_loop().create_datagram_endpoint(
            Peer, local_addr=("127.0.0.1", 0))
        extra_transports.append(wrong_transport)
        original, _response = usages["message-summary", "alice"]
        wrong_flow = replace(original, headers=tuple((key, value + "-wrong-flow"
            if key in {"Via", "Call-ID"} else value) for key, value in original.headers))
        assert (await exchange(wrong_flow, wrong_transport, wrong_peer)).status_code == 403
        spoofed = replace(original, headers=tuple((key, value.replace("alice", "mallory"))
                                                  for key, value in original.headers))
        assert (await exchange(spoofed)).status_code == 403

        await register("bob", expires=0, sequence=2)
        assert registrar.registration_matches_source("alice", *address, "UDP")
        assert not registrar.registration_matches_source("bob", *address, "UDP")
        assert registrar.account_for_source(address, "UDP").username == "alice"
        assert {(item.event, item.subscriber) for item in application.subscriptions.values()} == set(usages)
        for event in ("message-summary", "dialog"):
            initial, response = usages[event, "bob"]
            closing = replace(initial, headers=tuple((key,
                value + ";tag=" + response.header("To").split(";tag=", 1)[1] if key == "To" else
                "2 SUBSCRIBE" if key == "CSeq" else "0" if key == "Expires" else
                value + "-close" if key == "Via" else value)
                for key, value in initial.headers))
            wrong_close = replace(closing, headers=tuple((key, value + "-wrong-flow" if key == "Via"
                                                         else "3 SUBSCRIBE" if key == "CSeq" else value)
                                                         for key, value in closing.headers))
            assert (await exchange(wrong_close, wrong_transport, wrong_peer)).status_code == 403
            assert (await exchange(closing)).status_code == 200, (
                event, closing.header("To"), [(item.event, item.subscriber, item.to_tag)
                for item in application.subscriptions.values()])
        await drain_until(lambda: {notice_key(item) for item in notices
            if item.header("Subscription-State").startswith("terminated")} >= {
                ("message-summary", "bob"), ("dialog", "bob")})
        async with asyncio.timeout(3):
            while application.notifiers:
                await asyncio.sleep(0)
        assert {item.subscriber for item in application.subscriptions.values()} == {"alice"}
        registrar.update_accounts([replace(registrar.accounts["alice"], enabled=False), registrar.accounts["bob"]])
        assert registrar.account_for_source(address, "UDP", username="alice") is None
        initial = usages["message-summary", "alice"][0]
        disabled = replace(initial, headers=tuple((key, value + "-disabled"
            if key in {"Via", "Call-ID"} else value) for key, value in initial.headers))
        assert (await exchange(disabled)).status_code == 403
    finally:
        await lab.calls.component("sip_application").stop()
        for item in extra_transports:
            item.close()
        transport.close()


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


@pytest.fixture
async def deregistered_subscription(lab, request, socket_enabled):
    from types import SimpleNamespace
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind

    address = ("127.0.0.2", 5060)
    wire_transport = None
    peer = None
    if getattr(request, "param", None) == "wire":
        from .test_refer_correlation import Peer
        wire_transport, peer = await asyncio.get_running_loop().create_datagram_endpoint(
            Peer, local_addr=("127.0.0.1", 0))
        address = wire_transport.get_extra_info("sockname")
        request.addfinalizer(wire_transport.close)
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("alice", "Alice", "test-only")],
                             local_ip="127.0.0.1", local_sip_port=5060)
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="unregister-alice", name="Alice",
        username="alice", kind=EndpointKind.SIP_ACCOUNT, availability="available"))

    async def registration(expires, sequence):
        headers = [("Via", f"SIP/2.0/UDP {address[0]}:{address[1]};branch=z9hG4bKreg{sequence}"),
            ("From", "<sip:alice@localhost>;tag=registration"), ("To", "<sip:alice@localhost>"),
            ("Call-ID", "registration-usage"), ("CSeq", f"{sequence} REGISTER"),
            ("Contact", f"<sip:alice@{address[0]}:{address[1]}>"), ("Expires", str(expires))]
        request = sip.parse_message(sip.build_request("REGISTER", "sip:127.0.0.1", headers))
        challenge = await registrar.handle_register(request, address, "UDP")
        assert challenge.status == 401
        authorization = build_digest_authorization(challenge_header=dict(challenge.headers)["WWW-Authenticate"],
            username="alice", password="test-only", method="REGISTER", uri=request.uri)
        authenticated = replace(request, headers=(*request.headers, ("Authorization", authorization)))
        result = await registrar.handle_register(authenticated, address, "UDP")
        assert result.status == 200

    await registration(300, 1)
    methods = SipApplicationMethods(lab.hass, registrar)
    notifications = asyncio.Queue()

    async def send(_request, _address, result):
        notifications.put_nowait(result.follow_up)
        return sip.SipMessage(status_code=200, reason="OK")

    initial = _subscribe()
    initial = replace(initial, headers=tuple((key, "message-summary" if key == "Event" else
                                            f"<sip:alice@{address[0]}:{address[1]}>" if key == "Contact" else value)
                                           for key, value in initial.headers))
    try:
        first = await methods.handle(initial, address, "UDP", send)
        assert first.status == 200
        first.follow_up_scheduler()
        usage = next(iter(methods.subscriptions.values()))
        worker = usage.notify_task
        await asyncio.wait_for(notifications.get(), 1)
        await asyncio.wait_for(worker, 1)
        await registration(0, 2)
        assert registrar.account_for_source(address, "UDP") is None
        closing = replace(initial, headers=tuple(
            (key, f"<sip:alice@localhost>;tag={first.to_tag}" if key == "To" else
             "2 SUBSCRIBE" if key == "CSeq" else "0" if key == "Expires" else value)
            for key, value in initial.headers))
        yield SimpleNamespace(methods=methods, registrar=registrar, request=closing, address=address,
                              send=send, notifications=notifications, usage=usage, peer=peer,
                              wire_transport=wire_transport)
    finally:
        await methods.stop()


async def test_existing_mwi_unsubscribe_survives_authenticated_unregister(deregistered_subscription):
    fixture = deregistered_subscription
    result = await fixture.methods.handle(fixture.request, fixture.address, "UDP", fixture.send)
    assert result.status == 200
    assert result.follow_up_dialog is fixture.usage.dialog
    assert not fixture.methods.subscriptions and not fixture.methods.subscription_timers
    result.follow_up_scheduler()
    worker = fixture.usage.notify_task
    terminal = await asyncio.wait_for(fixture.notifications.get(), 1)
    assert dict(terminal.headers)["Subscription-State"].startswith("terminated")
    await asyncio.wait_for(worker, 1)
    assert not fixture.methods.notifiers


@pytest.mark.parametrize("tamper", [
    "from_tag", "to_tag", "call_id", "from_user", "target", "stale_cseq", "cseq_method",
    "host", "port", "transport", "event", "event_id", "refresh", "new_usage", "disabled", "removed",
])
async def test_unregistered_subscription_close_requires_its_existing_authorized_usage(deregistered_subscription, tamper):
    fixture = deregistered_subscription
    changes = {
        "from_tag": ("From", "<sip:alice@localhost>;tag=other"),
        "to_tag": ("To", "<sip:alice@localhost>;tag=other"),
        "call_id": ("Call-ID", "unknown-subscription"),
        "from_user": ("From", "<sip:other@localhost>;tag=subscriber"),
        "stale_cseq": ("CSeq", "1 SUBSCRIBE"), "cseq_method": ("CSeq", "2 NOTIFY"),
        "event": ("Event", "presence"), "event_id": ("Event", "message-summary;id=other"),
        "refresh": ("Expires", "300"), "new_usage": ("To", "<sip:alice@localhost>"),
    }
    request, address, transport = fixture.request, fixture.address, "UDP"
    if tamper in changes:
        name, value = changes[tamper]
        request = replace(request, headers=tuple((key, value if key == name else item) for key, item in request.headers))
    elif tamper == "target":
        request = replace(request, uri="sip:other@localhost")
    elif tamper == "host":
        address = ("127.0.0.3", 5060)
    elif tamper == "port":
        address = ("127.0.0.2", 5062)
    elif tamper == "transport":
        transport = "TCP"
    elif tamper == "disabled":
        fixture.registrar.accounts["alice"] = replace(fixture.registrar.accounts["alice"], enabled=False)
    elif tamper == "removed":
        fixture.registrar.accounts.pop("alice")
    result = await fixture.methods.handle(request, address, transport, fixture.send)
    assert 400 <= result.status < 500
    assert fixture.methods.subscriptions[fixture.usage.key] is fixture.usage
    assert fixture.usage.dialog.remote_cseq == 1
    assert fixture.notifications.empty() and not fixture.methods.notifiers


@pytest.mark.parametrize("deregistered_subscription", ["wire"], indirect=True)
async def test_duplicate_unregistered_udp_close_uses_existing_transaction_cache(deregistered_subscription):
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.sip_listener import SipUdpEndpoint

    fixture = deregistered_subscription
    endpoint = SipUdpEndpoint(local_ip="127.0.0.1", local_sip_port=0, local_rtp_port=41000,
        supported_formats=[AudioFormat(16000, "s16le", 1, 20)], on_invite=lambda _: None,
        on_request=fixture.methods.handle)
    server, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: endpoint, local_addr=("127.0.0.1", 0))
    headers = (*fixture.request.headers,
        ("Via", f"SIP/2.0/UDP {fixture.address[0]}:{fixture.address[1]};branch=z9hG4bKclose;rport"))
    raw = sip.build_request("SUBSCRIBE", fixture.request.uri, headers)
    try:
        fixture.wire_transport.sendto(raw, server.get_extra_info("sockname"))
        received = [await fixture.peer.receive() for _ in range(2)]
        first = next(message for message, _ in received if message.status_code == 200)
        terminal, address = next((message, address) for message, address in received if message.method == "NOTIFY")
        assert terminal.header("Subscription-State").startswith("terminated")
        worker = fixture.usage.notify_task
        fixture.wire_transport.sendto(sip.build_response(200, "OK", [
            (name, terminal.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]), address)
        await asyncio.wait_for(worker, 1)
        assert not fixture.methods.subscriptions
        fixture.wire_transport.sendto(raw, server.get_extra_info("sockname"))
        duplicate, _ = await fixture.peer.receive()
        assert duplicate.status_code == 200
        assert duplicate.header("To") == first.header("To")
        assert duplicate.header("CSeq") == first.header("CSeq")
        assert not fixture.methods.subscriptions
    finally:
        server.close()
        await endpoint.wait_closed()


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
    next_tag = dict(refreshed.headers)["SIP-ETag"]
    assert next_tag != entity_tag
    stale = await methods.handle(_publish(body=b"", entity_tag=entity_tag), ("127.0.0.2", 5060), "UDP")
    assert stale.status == 412
    removed = await methods.handle(
        _publish(body=b"", expires=0, entity_tag=next_tag),
        ("127.0.0.2", 5060),
        "UDP",
    )
    await hass.async_block_till_done()

    assert created.status == refreshed.status == removed.status == 200
    assert dict(removed.headers)["SIP-ETag"] not in {entity_tag, next_tag}
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
        return sip.SipMessage(status_code=200, reason="OK")

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
    from xml.etree import ElementTree
    document = ElementTree.fromstring(notifications[0][2].follow_up.body)
    assert document.find(".//{urn:ietf:params:xml:ns:pidf}basic").text == "open"
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


async def test_publish_keeps_independent_publications_and_rotates_tags(hass):
    from xml.etree import ElementTree
    methods = SipApplicationMethods(hass, SipRegistrar(enabled=True, accounts=[], local_ip="127.0.0.1", local_sip_port=5060))
    try:
        first = methods._publish(_publish(body=_pidf()), "alice")
        second = methods._publish(_publish(body=_pidf().replace(b"open", b"closed")), "alice")
        a, b = dict(first.headers)["SIP-ETag"], dict(second.headers)["SIP-ETag"]
        assert first.status == second.status == 200 and a != b
        tuples = ElementTree.fromstring(methods._publication("alice").body).findall("{urn:ietf:params:xml:ns:pidf}tuple")
        assert len(tuples) == 2 and len({item.get("id") for item in tuples}) == 2
        changed = methods._publish(_publish(body=_pidf(), entity_tag=a), "alice")
        new = dict(changed.headers)["SIP-ETag"]
        assert new not in {a, b}
        assert methods._publish(_publish(body=b"", entity_tag=a), "alice").status == 412
        assert methods._publish(_publish(body=b"", entity_tag=new, expires=0), "alice").status == 200
        assert set(methods.publications) == {b}
        assert methods._publication("alice") is not None
    finally:
        await methods.stop()


async def test_subscription_refresh_retains_route_and_rejects_stale_cseq(hass):
    from dataclasses import replace
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("alice", "Alice", "test")], local_ip="127.0.0.1", local_sip_port=5060)
    methods = SipApplicationMethods(hass, registrar)
    async def send(*_args):
        return sip.SipMessage(status_code=200, reason="OK")
    initial = _subscribe()
    initial = replace(initial, headers=(*initial.headers, ("Record-Route", "<sip:proxy.example:5070;lr>")))
    try:
        first = methods._subscribe(initial, ("127.0.0.2", 5060), "alice", send)
        assert first.status == 200
        refresh = _subscribe()
        refresh = replace(refresh, headers=tuple(
            (key, f"<sip:alice@localhost>;tag={first.to_tag}" if key == "To" else
             "2 SUBSCRIBE" if key == "CSeq" else "<sip:alice@127.0.0.3:5090>" if key == "Contact" else value)
            for key, value in refresh.headers))
        second = methods._subscribe(refresh, ("127.0.0.2", 5060), "alice", send)
        assert second.status == 200 and second.follow_up_dialog is first.follow_up_dialog
        assert second.follow_up_dialog.route_set == ("<sip:proxy.example:5070;lr>",)
        assert second.follow_up_dialog.remote_target_uri == "sip:alice@127.0.0.3:5090"
        assert methods._subscribe(refresh, ("127.0.0.2", 5060), "alice", send).status == 500
    finally:
        await methods.stop()


async def test_slow_subscriber_coalesces_updates_and_preserves_terminal(hass):
    import asyncio
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("alice", "Alice", "test")], local_ip="127.0.0.1", local_sip_port=5060)
    methods = SipApplicationMethods(hass, registrar)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def send(_request, _address, result):
        sent.append(result.follow_up)
        entered.set()
        await release.wait()
        return sip.SipMessage(status_code=200, reason="OK")
    try:
        first = methods._subscribe(_subscribe(), ("127.0.0.2", 5060), "alice", send)
        first.follow_up_scheduler()
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(1000):
            methods._notify_subscribers("alice")
        subscription = next(iter(methods.subscriptions.values()))
        methods._schedule_notification(subscription, methods._notification_result(subscription, expires=0, terminated=True))
        methods._notify_subscribers("alice")
        assert len(sent) == 1
        assert methods.snapshot()["notifiers"] == 1
        assert methods.snapshot()["pending_notifications"] == 1
        assert methods.snapshot()["coalesced_notifications"] >= 999
        task = subscription.notify_task
        release.set()
        await asyncio.wait_for(task, 1)
        assert len(sent) == 2 and sent[1].cseq == sent[0].cseq + 1
        assert dict(sent[-1].headers)["Subscription-State"].startswith("terminated")
    finally:
        release.set()
        await methods.stop()


@pytest.mark.parametrize("status, retained", [(None, False), (481, False), (500, True), (200, True)])
async def test_notify_transaction_outcome_releases_only_failed_subscription(hass, status, retained):
    import asyncio
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("alice", "Alice", "test")],
                             local_ip="127.0.0.1", local_sip_port=5060)
    methods = SipApplicationMethods(hass, registrar)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def send(_request, _address, result):
        sent.append(result.follow_up)
        entered.set()
        await release.wait()
        return None if status is None else sip.SipMessage(status_code=status, reason="Test")

    try:
        initial = methods._subscribe(_subscribe(), ("127.0.0.2", 5060), "alice", send)
        initial.follow_up_scheduler()
        await asyncio.wait_for(entered.wait(), 1)
        subscription = next(iter(methods.subscriptions.values()))
        methods._notify_subscribers("alice")
        worker = subscription.notify_task
        release.set()
        await asyncio.wait_for(worker, 1)
        assert bool(methods.subscriptions) is retained
        assert bool(methods.subscription_timers) is retained
        assert not methods.notifiers
        assert subscription.pending_notification is None
        assert len(sent) == (2 if retained else 1)
    finally:
        release.set()
        await methods.stop()


async def test_subscription_limits_count_retiring_notifiers_until_they_finish(hass):
    import asyncio
    accounts = [SipAccount(f"watcher{i}", f"Watcher {i}", "test") for i in range(5)]
    registrar = SipRegistrar(enabled=True, accounts=accounts, local_ip="127.0.0.1", local_sip_port=5060)
    methods = SipApplicationMethods(hass, registrar)
    release, all_started = asyncio.Event(), asyncio.Event()
    senders = 0

    async def send(_request, _address, _result):
        nonlocal senders
        senders += 1
        if senders == 512:
            all_started.set()
        await release.wait()
        return sip.SipMessage(status_code=200, reason="OK")

    def request(index, owner, *, tag="", expires=300, sequence=1):
        return sip.SipMessage(method="SUBSCRIBE", uri="sip:watcher0@127.0.0.1", headers=(
            ("From", f"<sip:{owner}@127.0.0.1>;tag=watcher"),
            ("To", "<sip:watcher0@127.0.0.1>" + (f";tag={tag}" if tag else "")),
            ("Call-ID", f"limit-{index}"), ("CSeq", f"{sequence} SUBSCRIBE"),
            ("Contact", "<sip:watcher@127.0.0.2:5060>"), ("Event", "presence"), ("Expires", str(expires))))

    try:
        for index in range(512):
            owner = f"watcher{index // 132}"
            result = methods._subscribe(request(index, owner), ("127.0.0.2", 5060), owner, send)
            assert result.status == 200
            result.follow_up_scheduler()
            if index == 131:
                denied = methods._subscribe(request("account-overflow", owner), ("127.0.0.2", 5060), owner, send)
                assert denied.status == 503
                assert dict(denied.headers)["Retry-After"] == "1"
        await asyncio.wait_for(all_started.wait(), 2)
        assert len(methods.subscriptions) == len(methods.notifiers) == 512
        denied = methods._subscribe(request("global-overflow", "watcher4"), ("127.0.0.2", 5060), "watcher4", send)
        assert denied.status == 503
        assert dict(denied.headers)["Retry-After"] == "1"
        first = next(iter(methods.subscriptions.values()))
        removed = methods._subscribe(request(0, "watcher0", tag=first.to_tag, expires=0, sequence=2),
                                     ("127.0.0.2", 5060), "watcher0", send)
        assert removed.status == 200
        removed.follow_up_scheduler()
        assert len(methods.subscriptions) == 511 and len(methods.notifiers) == 512
        denied = methods._subscribe(request("retiring-overflow", "watcher4"), ("127.0.0.2", 5060), "watcher4", send)
        assert denied.status == 503
        workers = [item.notify_task for item in methods.notifiers.values()]
        release.set()
        await asyncio.wait_for(asyncio.gather(*workers), 2)
        assert not methods.notifiers
        accepted = methods._subscribe(request("new", "watcher4"), ("127.0.0.2", 5060), "watcher4", send)
        assert accepted.status == 200 and len(methods.subscriptions) == 512
        assert methods.snapshot()["peak_notifiers"] == 512
    finally:
        release.set()
        await methods.stop()
        assert not methods.tasks and not methods.subscription_timers and not methods.notifiers


from .test_trunk_calls_ha import trunks as trunks, lab as lab  # noqa: E402,F401


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("temporary_target", [False, True])
async def test_registered_instance_gruu_routes_ack_refer_and_bye_to_its_contact(trunks, monkeypatch, temporary_target):
    import asyncio
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.sip_gruu import GruuTokens
    from custom_components.voip_stack.core.sip_transfer import SipReferTarget
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_runtime import configure_local_contact

    lab = trunks
    phone = lab.phones[0]
    manager = lab.calls.component("udp_listener")
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount("bedroom0", "Bedroom", "test-only")])
    registrar._gruu_tokens = GruuTokens()
    instance = "urn:uuid:00000000-0000-0000-0000-000000000001"
    public, temporary = registrar._gruu_tokens.issue(sip.SipUri("bedroom0", "127.0.0.1", manager.port), instance, "epoch")
    advertised = temporary if temporary_target else public
    phone_port = phone.transport.get_extra_info("sockname")[1]
    registrar.registrations["bedroom0"] = SipRegistration(username="bedroom0",
        contact_uri=f"sip:bedroom0@127.0.0.1:{phone_port}", source_host="127.0.0.1", source_port=phone_port,
        transport="UDP", expires_at=time.time()+60, instance_id=instance, gruu_epoch="epoch",
        public_gruu=public, temporary_gruu=temporary)
    original_respond, original_receive = phone.respond, phone.datagram_received
    acked, referred, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def respond(request, address, status, body=b""):
        if request.method == "INVITE" and status == 200:
            headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
            headers += [("To", request.header("To") + ";tag=bedroom"), ("Contact", f"<{advertised}>"),
                        ("Content-Type", "application/sdp")]
            phone.transport.sendto(sip.build_response(status, "OK", headers, body), address)
        else:
            original_respond(request, address, status, body)

    def receive(raw, address):
        request = sip.parse_message(raw)
        if request.method == "ACK":
            acked.set()
        if request.method == "BYE":
            ended.set()
        if request.method == "REFER":
            assert request.uri == advertised
            referred.set()
            original_respond(request, address, 200)
            contact = sip.parse_sip_uri(request.header("Contact"))
            headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{phone_port};branch=z9hG4bK-gruu-notify"),
                ("From", request.header("To") + ";tag=bedroom"), ("To", request.header("From")),
                ("Call-ID", request.header("Call-ID")), ("CSeq", "1 NOTIFY"),
                ("Contact", f"<{advertised}>"),
                ("Event", f'refer;id={sip.parse_cseq(request.header("CSeq")).number}'),
                ("Subscription-State", "terminated;reason=noresource"), ("Content-Type", "message/sipfrag")]
            phone.transport.sendto(sip.build_request("NOTIFY", str(contact), headers, b"SIP/2.0 200 OK\r\n"),
                                   (contact.host, contact.port))
            return
        original_receive(raw, address)

    monkeypatch.setattr(phone, "respond", respond)
    monkeypatch.setattr(phone, "datagram_received", receive)
    client = SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=0, local_rtp_port=41000,
                           supported_formats=[AudioFormat(16000, "s16le", 1, 20)])
    configure_local_contact(lab.hass, client)
    try:
        outcome = await client.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=phone_port)
        if outcome == "ringing":
            outcome = await client.wait_for_final(timeout=2)
        assert outcome == "in_call"
        await asyncio.wait_for(acked.wait(), .5)
        result = await client.refer(SipReferTarget("sip:next@example.test"), timeout=2)
        assert referred.is_set() and result.accepted
        await client.terminate()
        assert ended.is_set()
        await client.close()
        from custom_components.voip_stack.sip_listener import _ActiveDialog
        endpoint = next(item for item in manager._dialog_endpoints() if item.signaling_transport == "UDP")
        incoming = sip.parse_message(sip.build_request("INVITE", "sip:HA@127.0.0.1", [
            ("From", "<sip:bedroom0@127.0.0.1>;tag=bedroom"), ("To", "<sip:HA@127.0.0.1>"),
            ("Contact", f"<{advertised}>"), ("Call-ID", "registered-gruu-inbound"), ("CSeq", "1 INVITE")]))
        endpoint.active_dialogs["registered-gruu-inbound"] = _ActiveDialog(
            request=incoming, addr=("127.0.0.1", phone_port), to_tag="ha", cseq=2, transport="UDP",
            remote_uri="sip:bedroom0@127.0.0.1", remote_target_uri=advertised)
        referred.clear()
        ended.clear()
        try:
            result = await endpoint.async_refer("registered-gruu-inbound", SipReferTarget("sip:next@example.test"), timeout=2)
            assert referred.is_set() and result.accepted
            assert await endpoint.async_send_bye("registered-gruu-inbound")
            assert ended.is_set()
        finally:
            endpoint.active_dialogs.pop("registered-gruu-inbound", None)
    finally:
        await client.close()


async def test_expired_subscription_emits_terminal_even_on_unrelated_update(hass):
    import asyncio
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("alice", "Alice", "test")], local_ip="127.0.0.1", local_sip_port=5060)
    methods = SipApplicationMethods(hass, registrar)
    sent = []
    async def send(_request, _addr, result):
        sent.append(result.follow_up)
        return sip.SipMessage(status_code=200, reason="OK")
    try:
        initial = methods._subscribe(_subscribe(), ("127.0.0.2", 5060), "alice", send)
        initial.follow_up_scheduler()
        sub = next(iter(methods.subscriptions.values()))
        await sub.notify_task
        sub.expires_at = time.time() - 1
        methods._notify_subscribers("unrelated")
        await asyncio.wait_for(sub.notify_task, 1)
        assert len(sent) == 2
        assert dict(sent[-1].headers)["Subscription-State"] == "terminated;reason=timeout"
        assert not methods.subscriptions and not methods.subscription_timers
    finally:
        await methods.stop()
