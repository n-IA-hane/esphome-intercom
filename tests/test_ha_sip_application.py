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
