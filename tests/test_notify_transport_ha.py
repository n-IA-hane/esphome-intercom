"""Large UDP subscription notifications retain their owner over SIP/TCP."""

import asyncio
from dataclasses import replace
import socket
import time
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_dialog import SubscriptionDialog
from custom_components.voip_stack.dialog_info import DialogState, document
from custom_components.voip_stack.sip_application import SipApplicationMethods, SipEventSubscription
from custom_components.voip_stack.sip_endpoint import SipEndpointManager
from custom_components.voip_stack.sip_listener import SipFollowUpRequest, SipRequestResult
from custom_components.voip_stack.sip_tcp_io import read_sip_stream_message


pytestmark = pytest.mark.ha


@pytest.fixture
async def notification_wire(hass, socket_enabled):
    messages = asyncio.Queue()
    writers, readers = [], []

    class Subscriber(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            # Independent IPv4 MTU oracle: the ESP cannot reassemble these.
            kind = "fragmented_udp" if len(raw) > 1472 else "UDP"
            messages.put_nowait((kind, raw, lambda reply: self.transport.sendto(reply, address)))

    udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        Subscriber, local_addr=("127.0.0.1", 0))
    port = udp.get_extra_info("sockname")[1]

    async def connected(reader, writer):
        writers.append(writer)
        readers.append(asyncio.current_task())
        try:
            while (raw := await read_sip_stream_message(reader)) is not None:
                messages.put_nowait(("TCP", raw, writer.write))
        finally:
            writer.close()
            await writer.wait_closed()

    tcp = await asyncio.start_server(connected, "127.0.0.1", port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
    manager = SipEndpointManager(host="127.0.0.1", port=local_port, local_ip="127.0.0.1",
        local_rtp_port=41000, supported_formats=[AudioFormat(16000, "s16le", 1, 20)],
        on_invite=lambda _: None)
    assert await manager.start()
    methods = SipApplicationMethods(hass, None)

    def usage(identity="watch-audio"):
        request = sip.parse_message(sip.build_request("SUBSCRIBE", "sip:audio@127.0.0.1", [
            ("From", "<sip:watcher@127.0.0.1>;tag=watcher"),
            ("To", "<sip:audio@127.0.0.1>"),
            ("Contact", f"<sip:watcher@127.0.0.1:{port};transport=udp>"),
            ("Call-ID", identity), ("CSeq", "1 SUBSCRIBE"), ("Event", "dialog")]))
        state = SubscriptionDialog.from_request(request, "notifier")
        subscription = SipEventSubscription(key=identity, dialog=state, target="audio", subscriber="watcher",
            to_tag="notifier", request=request, addr=("127.0.0.1", port), expires_at=time.time()+60,
            sender=manager.udp_server.endpoint._send_application_follow_up,
            notify_lock=asyncio.Lock(), event="dialog")
        methods.subscriptions[identity] = subscription
        return subscription

    def update(body):
        return SipRequestResult(to_tag="notifier", follow_up=SipFollowUpRequest(method="NOTIFY",
            headers=(("Event", "dialog"), ("Subscription-State", "active;expires=60")),
            content_type="application/dialog-info+xml", body=body))

    def schedule(subscription, body, *, credentials=None):
        result = replace(update(body), follow_up_dialog=subscription.dialog, follow_up_lock=subscription.notify_lock,
                         follow_up_credentials=credentials)
        methods._schedule_notification(subscription, result)

    async def receive():
        kind, raw, reply = await asyncio.wait_for(messages.get(), 1)
        return kind, raw, sip.parse_message(raw), reply

    def acknowledge(request, reply):
        reply(sip.build_response(200, "OK", [
            (name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]))

    yield manager, methods, usage, schedule, receive, acknowledge, messages, writers, tcp
    await methods.stop()
    await manager.stop()
    udp.close()
    tcp.close()
    await tcp.wait_closed()
    for writer in writers:
        writer.close()
    await asyncio.gather(*readers, return_exceptions=True)


def full_state(*, busy, padding=457):
    states = (DialogState("session:12:wire-call", "confirmed", "recipient", call_id="wire-call",
        local_tag="phone-tag", remote_tag="ha-tag", peer_name="Caller " + "x" * padding,
        local_uri="sip:audio@127.0.0.1", local_target="sip:audio@127.0.0.1;transport=tcp",
        remote_uri="sip:caller@127.0.0.1"),) if busy else ()
    return document("sip:audio@127.0.0.1", 0, states)


async def test_large_notify_uses_tcp_and_releases_coalesced_idle_after_200(notification_wire):
    manager, methods, usage, schedule, receive, acknowledge, messages, writers, _ = notification_wire
    subscription = usage()
    body = full_state(busy=True)
    schedule(subscription, body)
    kind, raw, confirmed, reply = await receive()
    assert kind == "TCP", f"oversized SIP request used {kind}: {len(raw)} bytes"
    assert len(raw) == 1571 and confirmed.body == body
    assert confirmed.header("Via").startswith("SIP/2.0/TCP ")
    assert ";transport=udp" in confirmed.header("Contact")
    assert confirmed.header("Call-ID") == subscription.dialog.key.call_id
    assert sip.extract_tag(confirmed.header("From")) == subscription.dialog.key.local_tag
    assert sip.extract_tag(confirmed.header("To")) == subscription.dialog.key.remote_tag
    assert confirmed.header("CSeq") == "1 NOTIFY"
    sender = subscription.notify_task
    schedule(subscription, full_state(busy=False))
    assert subscription.notify_task is sender and subscription.pending_notification is not None
    assert messages.empty()
    acknowledge(confirmed, reply)
    kind, _, idle, reply = await receive()
    assert kind == "UDP" and idle.header("Via").startswith("SIP/2.0/UDP ")
    assert idle.header("Call-ID") == confirmed.header("Call-ID")
    assert idle.header("CSeq") == "2 NOTIFY"
    assert not list(ElementTree.fromstring(idle.body))
    acknowledge(idle, reply)
    await asyncio.wait_for(sender, 1)
    assert subscription.key in methods.subscriptions
    assert subscription.dialog_version == 2 and subscription.pending_notification is None
    assert not methods.notifiers and len(writers) == 1
    assert not manager.tcp_server.endpoint._client_transaction_responses


async def test_concurrent_large_notifies_share_tcp_flow_and_correlate_responses(notification_wire):
    manager, methods, usage, schedule, receive, acknowledge, _, writers, _ = notification_wire
    subscriptions = [usage(f"watch-{index}") for index in range(8)]
    for subscription in subscriptions:
        schedule(subscription, full_state(busy=True))
    received = [await receive() for _ in subscriptions]
    assert all(kind == "TCP" for kind, _, _, _ in received)
    assert len(writers) == 1
    held = received.pop()
    for _, _, request, reply in received:
        acknowledge(request, reply)
    pending = methods.subscriptions[held[2].header("Call-ID")]
    await asyncio.wait_for(asyncio.gather(*(item.notify_task for item in subscriptions if item is not pending)), 1)
    assert pending.notify_task is not None and not pending.notify_task.done()
    acknowledge(held[2], held[3])
    await asyncio.wait_for(pending.notify_task, 1)
    assert not methods.notifiers
    assert len(manager.tcp_server._tcp_writers) == 1


async def test_small_notify_stays_udp(notification_wire):
    _, methods, usage, schedule, receive, acknowledge, _, writers, _ = notification_wire
    subscription = usage()
    schedule(subscription, full_state(busy=False))
    kind, raw, request, reply = await receive()
    assert kind == "UDP" and len(raw) < 1300 and not writers
    acknowledge(request, reply)
    await asyncio.wait_for(subscription.notify_task, 1)
    assert subscription.key in methods.subscriptions


async def test_digest_retry_that_exceeds_udp_limit_uses_tcp(notification_wire):
    from custom_components.voip_stack.core.sip_auth import SipDigestCredentials, verify_digest_authorization

    _, methods, usage, schedule, receive, acknowledge, _, writers, _ = notification_wire
    subscription = usage()
    body = full_state(busy=True, padding=100)
    schedule(subscription, body, credentials=SipDigestCredentials("watcher", "test-only"))
    kind, raw, initial, reply = await receive()
    assert kind == "UDP" and len(raw) < 1300
    reply(sip.build_response(401, "Unauthorized", [
        *((name, initial.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")),
        ("WWW-Authenticate", 'Digest realm="watcher", nonce="notify-challenge", qop="auth"')]))
    kind, raw, retry, reply = await receive()
    assert kind == "TCP" and len(raw) > 1300 and retry.body == body
    assert retry.header("CSeq") == "2 NOTIFY" and retry.header("Call-ID") == initial.header("Call-ID")
    assert retry.header("Contact") == initial.header("Contact")
    verify_digest_authorization(authorization_header=retry.header("Authorization"), username="watcher",
        password="test-only", method="NOTIFY", uri=retry.uri, realm="watcher", nonce="notify-challenge")
    acknowledge(retry, reply)
    await asyncio.wait_for(subscription.notify_task, 1)
    assert subscription.key in methods.subscriptions and len(writers) == 1


async def test_retired_tcp_listener_does_not_resurrect_flow_or_keep_subscription(notification_wire):
    manager, methods, usage, schedule, _, _, messages, writers, _ = notification_wire
    subscription = usage()
    current_tcp = manager.udp_server.endpoint.notify_tcp_endpoint
    assert current_tcp() is manager.tcp_server.endpoint
    await manager.stop()
    assert current_tcp() is None
    schedule(subscription, full_state(busy=True))
    await asyncio.wait_for(subscription.notify_task, 1)
    assert not messages.qsize() and not writers
    assert subscription.key not in methods.subscriptions and subscription.notify_terminated
    assert subscription.pending_notification is None and not methods.notifiers


async def test_tcp_refusal_retries_same_notify_over_udp(notification_wire):
    _, methods, usage, schedule, receive, acknowledge, _, writers, peer_tcp = notification_wire
    peer_tcp.close()
    await peer_tcp.wait_closed()
    subscription = usage()
    body = full_state(busy=True)
    schedule(subscription, body)
    kind, raw, request, reply = await receive()
    assert kind == "fragmented_udp" and len(raw) == 1571 and request.body == body
    assert request.header("Via").startswith("SIP/2.0/UDP ") and request.header("CSeq") == "1 NOTIFY"
    acknowledge(request, reply)
    await asyncio.wait_for(subscription.notify_task, 1)
    assert subscription.key in methods.subscriptions and not writers


async def test_tcp_connection_timeout_never_falls_back_to_udp(notification_wire, monkeypatch):
    manager, methods, usage, schedule, _, _, messages, writers, _ = notification_wire

    async def unreachable(*_args, **_kwargs):
        raise TimeoutError("unreachable test subscriber")

    monkeypatch.setattr(asyncio, "open_connection", unreachable)
    subscription = usage()
    schedule(subscription, full_state(busy=True))
    with pytest.raises(ConnectionError, match="next hop is unreachable"):
        await subscription.notify_task
    assert subscription.key not in methods.subscriptions and subscription.notify_terminated
    assert subscription.pending_notification is None and not methods.notifiers
    assert messages.empty() and not writers and not manager.tcp_server._tcp_writers


async def test_stop_during_tcp_connect_closes_unpublished_flow(notification_wire, monkeypatch):
    manager, methods, usage, schedule, _, _, messages, writers, _ = notification_wire
    tcp_server = manager.tcp_server
    entered, release = asyncio.Event(), asyncio.Event()
    connect = asyncio.open_connection

    async def delayed_connect(*args, **kwargs):
        entered.set()
        await release.wait()
        return await connect(*args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", delayed_connect)
    subscription = usage()
    schedule(subscription, full_state(busy=True))
    worker = subscription.notify_task
    await asyncio.wait_for(entered.wait(), 1)
    await manager.stop()
    release.set()
    with pytest.raises(ConnectionError, match="next hop is unreachable"):
        await asyncio.wait_for(worker, 1)
    assert subscription.key not in methods.subscriptions
    assert not tcp_server._tcp_writers and not tcp_server._writers and not tcp_server._client_tasks
    assert not tcp_server._connections_by_host and not methods.notifiers and messages.empty()
    assert all(writer.is_closing() or writer.transport.is_closing() for writer in writers)
