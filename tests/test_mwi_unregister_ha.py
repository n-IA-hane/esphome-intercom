"""An unregistered Contact can retire only its already authorized MWI dialog."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.sip_listener import SipUdpEndpoint, SipTcpServer, _read_sip_stream_message
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar

from .test_voicemail_ha import (  # noqa: F401
    configured_tts_provider, custom_integrations, lab as lab, mailbox_lab as mailbox_lab,
)

pytestmark = pytest.mark.ha


class MwiPeer(asyncio.DatagramProtocol):
    """Independent UDP client; a NOTIFY is acknowledged only by the test."""

    def __init__(self):
        self.messages = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def datagram_received(self, raw, addr):
        self.messages.put_nowait((sip.parse_message(raw), addr))

    async def receive(self):
        async with asyncio.timeout(2):
            return await self.messages.get()

    async def exchange(self, request, target):
        self.transport.sendto(request, target)
        response, _ = await self.receive()
        assert response.status_code is not None
        return response

    def acknowledge(self, request, target):
        self.transport.sendto(sip.build_response(200, "OK", [
            (name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")
        ]), target)


class TcpMwiPeer(MwiPeer):
    async def connect(self, target):
        self.reader, self.writer = await asyncio.open_connection(*target)
        self.port = self.writer.get_extra_info("sockname")[1]
        self.reader_task = asyncio.create_task(self.read())
        self.transport = self
        return self

    async def read(self):
        while raw := await _read_sip_stream_message(self.reader):
            self.messages.put_nowait((sip.parse_message(raw), self.writer.get_extra_info("peername")))

    def sendto(self, raw, _target):
        self.writer.write(raw)

    def close(self):
        self.writer.close()

    async def wait_closed(self):
        await self.writer.wait_closed()
        await self.reader_task


class MwiSession:
    def __init__(self, lab, transport="UDP"):
        self.lab = lab
        self.transport = transport
        self.registrar = SipRegistrar(enabled=True, accounts=[SipAccount("kitchen", "Kitchen", "test-only")],
                                      local_ip="127.0.0.1", local_sip_port=5060)
        self.methods = SipApplicationMethods(lab.hass, self.registrar)
        self.endpoint = SipUdpEndpoint(local_ip="127.0.0.1", local_sip_port=0, local_rtp_port=41000,
            supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=lambda _: None,
            on_register=self.registrar.handle_register, on_request=self.methods.handle)
        self.sequence = 0

    async def start(self):
        self.lab.runtime.endpoints.update("kitchen", kind=EndpointKind.SIP_ACCOUNT, username="kitchen")
        loop = asyncio.get_running_loop()
        if self.transport == "TCP":
            self.server = SipTcpServer(host="127.0.0.1", port=0, local_ip="127.0.0.1", local_rtp_port=41000,
                supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=lambda _: None,
                on_register=self.registrar.handle_register, on_request=self.methods.handle)
            assert await self.server.start()
            self.endpoint = self.server.endpoint
            self.target = self.server.server.sockets[0].getsockname()
            self.peer = await TcpMwiPeer().connect(self.target)
            self.peer_transport = self.peer
        else:
            self.local, _ = await loop.create_datagram_endpoint(lambda: self.endpoint, local_addr=("127.0.0.1", 0))
            self.target = self.local.get_extra_info("sockname")
            self.peer_transport, self.peer = await loop.create_datagram_endpoint(MwiPeer, local_addr=("127.0.0.1", 0))
        self.addr = ("127.0.0.1", self.peer.port)

    def request(self, method, *, expires=300, cseq=None, tag="", call_id=None, event="message-summary",
                from_tag="subscriber", target="kitchen", contact=None, authorization=""):
        self.sequence += 1
        cseq = cseq if cseq is not None else self.sequence
        headers = [
            ("Via", f"SIP/2.0/{self.transport} 127.0.0.1:{self.peer.port};branch=z9hG4bKmwi{self.sequence};rport"),
            ("From", f"<sip:kitchen@127.0.0.1>;tag={from_tag}"),
            ("To", f"<sip:{target}@127.0.0.1>" + (f";tag={tag}" if tag else "")),
            ("Call-ID", call_id or ("mwi-owner" if method == "SUBSCRIBE" else "registration-owner")),
            ("CSeq", f"{cseq} {method}"),
            ("Contact", contact or f"<sip:kitchen@127.0.0.1:{self.peer.port};transport={self.transport.lower()}>"),
            ("Expires", str(expires)),
        ]
        if method == "SUBSCRIBE":
            headers.append(("Event", event))
        if authorization:
            headers.append(("Authorization", authorization))
        return sip.build_request(method, f"sip:{target}@127.0.0.1", headers)

    async def register(self, *, expires=300):
        first = await self.peer.exchange(self.request("REGISTER", expires=expires), self.target)
        assert first.status_code == 401
        authorization = build_digest_authorization(challenge_header=first.header("WWW-Authenticate"),
            username="kitchen", password="test-only", method="REGISTER", uri="sip:kitchen@127.0.0.1")
        accepted = await self.peer.exchange(self.request("REGISTER", expires=expires, authorization=authorization), self.target)
        assert accepted.status_code == 200

    async def subscribe(self):
        accepted = await self.peer.exchange(self.request("SUBSCRIBE", cseq=1, event="message-summary;id=owned"), self.target)
        assert accepted.status_code == 200
        self.tag = sip.extract_tag(accepted.header("To"))
        self.subscription = next(iter(self.methods.subscriptions.values()))
        notification, source = await self.peer.receive()
        assert notification.method == "NOTIFY"
        assert notification.header("Event") == "message-summary;id=owned"
        assert notification.header("Subscription-State").startswith("active")
        assert b"Messages-Waiting: no\r\n" in notification.body
        task = self.subscription.notify_task
        self.peer.acknowledge(notification, source)
        await asyncio.wait_for(task, 2)

    async def close(self):
        await self.methods.stop()
        self.peer_transport.close()
        if self.transport == "TCP":
            await self.peer.wait_closed()
            await self.server.stop()
        else:
            self.local.close()
            await self.endpoint.wait_closed()


def shared_flow_request(session, username, method, *, target=None, event="presence", call_id=None,
                        cseq=1, authorization=""):
    target = target or username
    headers = [
        ("Via", f"SIP/2.0/{session.transport} 127.0.0.1:{session.peer.port};branch=z9hG4bK-{method}-{username}-{cseq}"),
        ("From", f"<sip:{username}@127.0.0.1>;tag={username}"),
        ("To", f"<sip:{target}@127.0.0.1>"),
        ("Contact", f"<sip:{username}@127.0.0.1:{session.peer.port};transport={session.transport.lower()}>"),
        ("Call-ID", call_id or f"shared-{method}-{username}"),
        ("CSeq", f"{cseq} {method}"), ("Expires", "300"),
    ]
    if method in {"SUBSCRIBE", "PUBLISH"}:
        headers.append(("Event", event))
    body = b""
    if method == "MESSAGE":
        headers.append(("Content-Type", "text/plain"))
        body = b"shared flow identity"
    elif method == "PUBLISH":
        headers.append(("Content-Type", "application/pidf+xml"))
        body = (f'<presence xmlns="urn:ietf:params:xml:ns:pidf" entity="sip:{username}@127.0.0.1">'
                '<tuple id="phone"><status><basic>open</basic></status></tuple></presence>').encode()
    if authorization:
        headers.append(("Authorization", authorization))
    return sip.build_request(method, f"sip:{target}@127.0.0.1", headers, body)


async def register_shared_flow_user(session, username):
    first = await session.peer.exchange(shared_flow_request(session, username, "REGISTER"), session.target)
    assert first.status_code == 401
    authorization = build_digest_authorization(challenge_header=first.header("WWW-Authenticate"),
        username=username, password="test-only", method="REGISTER", uri=f"sip:{username}@127.0.0.1")
    accepted = await session.peer.exchange(shared_flow_request(session, username, "REGISTER", cseq=2,
        authorization=authorization), session.target)
    assert accepted.status_code == 200


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("order", [("kitchen", "bedroom"), ("bedroom", "kitchen")])
@pytest.mark.parametrize("method", ["MESSAGE", "PUBLISH", "SUBSCRIBE"])
async def test_authenticated_accounts_keep_identity_on_one_signaling_flow(mailbox_lab, transport, order, method):
    from custom_components.voip_stack.const import EVENT_SIP_MESSAGE, EVENT_SIP_PRESENCE

    session = MwiSession(mailbox_lab, transport)
    session.registrar.accounts["bedroom"] = SipAccount("bedroom", "Bedroom", "test-only")
    messages, presence = [], []
    unsub_message = mailbox_lab.hass.bus.async_listen(EVENT_SIP_MESSAGE, messages.append)
    unsub_presence = mailbox_lab.hass.bus.async_listen(EVENT_SIP_PRESENCE, presence.append)
    await session.start()
    try:
        for username in order:
            await register_shared_flow_user(session, username)
        bindings = [session.registrar.registered_contacts(username) for username in order]
        assert all(len(items) == 1 for items in bindings)
        assert {(items[0].source_host, items[0].source_port, items[0].transport) for items in bindings} == {
            (session.addr[0], session.addr[1], transport)}
        for username in order:
            result = await session.peer.exchange(shared_flow_request(session, username, method), session.target)
            assert result.status_code == 200
            if method == "SUBSCRIBE":
                notification, source = await session.peer.receive()
                assert notification.method == "NOTIFY"
                assert sip.parse_sip_uri(notification.uri).user == username
                subscription = next(item for item in session.methods.subscriptions.values()
                    if item.subscriber == username)
                session.peer.acknowledge(notification, source)
                await asyncio.wait_for(subscription.notify_task, 2)
        await mailbox_lab.hass.async_block_till_done()
        if method == "MESSAGE":
            assert [event.data["sender"] for event in messages] == list(order)
        elif method == "PUBLISH":
            assert [event.data["account"] for event in presence] == list(order)
        else:
            assert {item.subscriber for item in session.methods.subscriptions.values()} == set(order)
    finally:
        unsub_message()
        unsub_presence()
        await session.close()
        assert not session.methods.subscriptions and not session.methods.tasks and not session.methods.notifiers


@pytest.mark.parametrize("mwi_position", [0, 64, 128])
async def test_sixty_four_blf_presence_watches_leave_room_for_mwi(mailbox_lab, mwi_position):
    session = MwiSession(mailbox_lab)
    await session.start()
    try:
        await session.register()
        operations = []
        for index in range(64):
            username = f"watch{index}"
            session.registrar.accounts[username] = SipAccount(username, f"Watch {index}", "test-only")
            mailbox_lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=f"sip:{username}",
                name=f"Watch {index}", kind=EndpointKind.SIP_ACCOUNT,
                username=username, extension=str(8100 + index)))
            operations.extend((event, username) for event in ("dialog", "presence"))
        operations.insert(mwi_position, ("message-summary", "kitchen"))
        for index, (event, target) in enumerate(operations):
            result = await session.peer.exchange(shared_flow_request(session, "kitchen", "SUBSCRIBE",
                target=target, event=event, call_id=f"budget-{index}"), session.target)
            assert result.status_code == 200, (index, event, target)
            notification, source = await session.peer.receive()
            assert notification.method == "NOTIFY" and notification.header("Event") == event
            subscription = next(item for item in session.methods.subscriptions.values()
                if item.request.header("Call-ID") == f"budget-{index}")
            session.peer.acknowledge(notification, source)
            await asyncio.wait_for(subscription.notify_task, 2)
        assert len(session.methods.subscriptions) == 129
        assert sum(item.event == "dialog" for item in session.methods.subscriptions.values()) == 64
        assert sum(item.event == "presence" for item in session.methods.subscriptions.values()) == 64
        assert sum(item.event == "message-summary" for item in session.methods.subscriptions.values()) == 1
    finally:
        await session.close()
        assert not session.methods.subscriptions and not session.methods.tasks and not session.methods.notifiers


@pytest.fixture
async def mwi_session(mailbox_lab, request):
    session = MwiSession(mailbox_lab, getattr(request, "param", "UDP"))
    await session.start()
    try:
        await session.register()
        await session.subscribe()
        await session.register(expires=0)
        assert not session.registrar.registrations
        assert session.registrar.account_for_source(session.addr, session.transport) is None
        assert len(session.methods.subscriptions) == len(session.methods.subscription_timers) == 1
        yield session
    finally:
        await session.close()


@pytest.mark.parametrize("mwi_session", ["UDP", "TCP"], indirect=True)
async def test_unregister_then_owned_unsubscribe_completes_final_notify_before_releasing_owner(mwi_session):
    session = mwi_session
    result = await session.peer.exchange(session.request("SUBSCRIBE", expires=0, cseq=2, tag=session.tag,
        event="message-summary;id=owned"), session.target)
    assert result.status_code == 200, "REGISTER0 must not deny the existing dialog's final SUBSCRIBE0"
    notification, source = await session.peer.receive()
    assert notification.method == "NOTIFY" and notification.header("Call-ID") == "mwi-owner"
    assert notification.header("Event") == "message-summary;id=owned"
    assert notification.header("Subscription-State").startswith("terminated")
    assert sip.extract_tag(notification.header("From")) == session.tag
    assert sip.extract_tag(notification.header("To")) == "subscriber"
    assert sip.parse_cseq(notification.header("CSeq")).number > 1
    assert not session.methods.subscriptions and not session.methods.subscription_timers
    assert session.subscription.notify_task is not None
    assert session.methods.snapshot()["notifiers"] == 1
    assert session.endpoint._client_transaction_responses, "the final NOTIFY transaction still owns its lifetime"
    task = session.subscription.notify_task
    session.peer.acknowledge(notification, source)
    await asyncio.wait_for(task, 2)
    assert not session.endpoint._client_transaction_responses
    assert not session.methods.tasks and not session.methods.notifiers
    assert not session.methods.signaling_leases and not session.methods.event_clients


@pytest.mark.parametrize("change", [
    {"tag": "forged"}, {"from_tag": "forged"}, {"call_id": "new-subscription"},
    {"tag": ""}, {"event": "message-summary;id=forged"}, {"event": "presence;id=owned"},
    {"expires": 300}, {"cseq": 0}, {"target": "other"},
])
async def test_unregistered_dialog_rejects_forgery_new_subscribe_and_positive_refresh(mwi_session, change):
    session = mwi_session
    options = dict(expires=0, cseq=2, tag=session.tag, event="message-summary;id=owned")
    options.update(change)
    before = session.subscription.expires_at
    result = await session.peer.exchange(session.request("SUBSCRIBE", **options), session.target)
    assert result.status_code >= 400 and not result.body
    assert next(iter(session.methods.subscriptions.values())) is session.subscription
    assert session.subscription.expires_at == before
    assert session.subscription.dialog.remote_cseq == 1
    assert session.subscription.notify_task is None
    assert session.peer.messages.empty()


async def test_unregistered_subscription_requires_original_source_tuple(mwi_session):
    session = mwi_session
    other_transport, other = await asyncio.get_running_loop().create_datagram_endpoint(
        MwiPeer, local_addr=("127.0.0.1", 0))
    try:
        result = await other.exchange(session.request("SUBSCRIBE", expires=0, cseq=2, tag=session.tag,
            event="message-summary;id=owned"), session.target)
        assert result.status_code == 403 and not result.body
        assert next(iter(session.methods.subscriptions.values())) is session.subscription
        assert session.subscription.dialog.remote_cseq == 1
        assert session.peer.messages.empty()
    finally:
        other_transport.close()


@pytest.mark.parametrize("change", ["transport", "sender", "method"])
async def test_unregistered_subscription_requires_original_listener_flow_and_subscribe_method(mwi_session, change):
    session = mwi_session
    request = sip.parse_message(session.request("SUBSCRIBE", expires=0, cseq=2, tag=session.tag,
        event="message-summary;id=owned"))

    async def foreign_sender(*_):
        pytest.fail("an unrelated listener must never inherit a private notification")

    if change == "method":
        request = replace(request, headers=tuple((name, "2 INVITE" if name == "CSeq" else value)
                                                for name, value in request.headers))
    result = await session.methods.handle(request, session.addr,
        "TCP" if change == "transport" else "UDP",
        foreign_sender if change == "sender" else session.endpoint._send_application_follow_up)
    assert result.status == 403 and result.follow_up is None
    assert next(iter(session.methods.subscriptions.values())) is session.subscription
    assert session.subscription.dialog.remote_cseq == 1


@pytest.mark.parametrize("revoke", ["disable", "remove", "password", "disable_then_enable"])
async def test_revoked_principal_cannot_read_or_resume_previous_mwi_dialog(mwi_session, revoke):
    from custom_components.voip_stack.mailbox import EVENT_VOICEMAIL_CHANGED, get_archive

    session = mwi_session
    original = session.registrar.accounts["kitchen"]
    if revoke == "remove":
        session.registrar.update_accounts([])
    elif revoke == "password":
        session.registrar.update_accounts([replace(original, password="replacement-test-only")])
    else:
        session.registrar.update_accounts([replace(original, enabled=False)])
        if revoke == "disable_then_enable":
            session.registrar.update_accounts([original])
    assert not session.methods.subscriptions and not session.methods.subscription_timers
    archive = await get_archive(session.lab.hass)
    writer = await archive.begin("kitchen", caller="private-after-revocation")
    assert writer.offer(bytes(640), AudioFormat(16000, frame_ms=20))
    await writer.finish()
    session.lab.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": "kitchen"})
    await session.lab.hass.async_block_till_done()
    assert session.peer.messages.empty()
    for tag, expires in ((session.tag, 0), (session.tag, 300), ("", 300)):
        result = await session.peer.exchange(session.request("SUBSCRIBE", expires=expires, tag=tag,
            event="message-summary;id=owned"), session.target)
        assert result.status_code == 403 and not result.body
    assert not session.methods.tasks and not session.methods.notifiers
    assert not session.endpoint._client_transaction_responses


async def test_revocation_cancels_existing_pending_notify_without_sending_later_mailbox_state(mwi_session):
    from custom_components.voip_stack.mailbox import EVENT_VOICEMAIL_CHANGED

    session = mwi_session
    session.lab.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": "kitchen"})
    notification, _ = await session.peer.receive()
    assert notification.method == "NOTIFY"
    task = session.subscription.notify_task
    assert task is not None and session.endpoint._client_transaction_responses
    session.registrar.update_accounts([])
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    session.lab.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": "kitchen"})
    await session.lab.hass.async_block_till_done()
    assert session.peer.messages.empty()
    assert not session.methods.subscriptions and not session.methods.subscription_timers
    assert not session.methods.notifiers and not session.methods.tasks
    assert not session.endpoint._client_transaction_responses

