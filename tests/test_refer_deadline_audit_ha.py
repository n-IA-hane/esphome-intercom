"""The real REFER notifier keeps a finite lease and one subscription owner."""

from .support.sip_policy import configured_registrar_policy

import asyncio
import math
import socket

import pytest

from custom_components.voip_stack.core import sip, sip_transfer
from custom_components.voip_stack.core.sip_dialog import DialogKey, SubscriptionDialog
from custom_components.voip_stack.core.sip_transaction import SIP_TIMER_F
from custom_components.voip_stack.sip_application import SipApplicationMethods, SipEventSubscription
from custom_components.voip_stack.sip_registrar import SipRegistrar

pytestmark = [pytest.mark.ha, pytest.mark.usefixtures("socket_enabled")]


class NotifyWire:
    """A peer acknowledges actual serialized UDP NOTIFY requests."""

    def __init__(self):
        self.local, self.peer = (socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2))
        for sock in (self.local, self.peer):
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
        self.dialog = SubscriptionDialog(key=DialogKey("refer-deadline", "local", "remote"),
            local_uri="sip:ha@127.0.0.1", remote_uri="sip:phone@127.0.0.1",
            remote_target_uri=f"sip:phone@127.0.0.1:{self.peer.getsockname()[1]}", remote_cseq=0)
        self.requests = []
        self.first = asyncio.Event()

    async def send(self, headers, body):
        loop = asyncio.get_running_loop()
        cseq = self.dialog.next_cseq()
        common = [("Via", f"SIP/2.0/UDP 127.0.0.1:{self.local.getsockname()[1]};branch=z9hG4bK-{cseq}"),
            ("From", "<sip:ha@127.0.0.1>;tag=local"), ("To", "<sip:phone@127.0.0.1>;tag=remote"),
            ("Call-ID", self.dialog.key.call_id), ("CSeq", f"{cseq} NOTIFY"),
            ("Content-Type", "message/sipfrag")]
        raw = sip.build_request("NOTIFY", self.dialog.remote_target_uri, (*common, *headers), body)
        await loop.sock_sendto(self.local, raw, self.peer.getsockname())
        received, address = await asyncio.wait_for(loop.sock_recvfrom(self.peer, 8192), 2)
        request = sip.parse_message(received)
        self.requests.append(request)
        response_headers = [(name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]
        await loop.sock_sendto(self.peer, sip.build_response(200, "OK", response_headers), address)
        response = sip.parse_message(await asyncio.wait_for(loop.sock_recv(self.local, 8192), 2))
        self.first.set()
        return response

    def refresh(self, expires, *, cseq=1):
        return sip.SipMessage(method="SUBSCRIBE", uri="sip:ha@127.0.0.1", headers=(
            ("Call-ID", self.dialog.key.call_id), ("From", "<sip:phone@127.0.0.1>;tag=remote"),
            ("To", "<sip:ha@127.0.0.1>;tag=local"), ("CSeq", f"{cseq} SUBSCRIBE"),
            ("Contact", f"<{self.dialog.remote_target_uri}>"), ("Event", "refer;id=7"),
            ("Expires", str(expires))))

    def close(self):
        self.local.close()
        self.peer.close()


async def test_ninety_second_operation_gets_lease_before_first_notify_and_one_final():
    wire = NotifyWire()
    gate = asyncio.get_running_loop().create_future()
    notifier = sip_transfer.ReferNotifier(wire.dialog, 7, wire.send)
    async def action():
        return await gate
    task = asyncio.create_task(sip_transfer.run_refer_action(action, notifier, timeout=90))
    try:
        await asyncio.wait_for(wire.first.wait(), 2)
        initial = wire.requests[0]
        seconds = int(initial.header("Subscription-State").partition("expires=")[2])
        assert 90 < seconds <= math.ceil(90 + SIP_TIMER_F)
        gate.set_result(200)
        assert await task == 200
        finals = [request for request in wire.requests if request.header("Subscription-State").startswith("terminated")]
        assert len(finals) == 1 and sip_transfer.parse_sipfrag_status(finals[0].body) == 200
        assert len({request.header("CSeq") for request in wire.requests}) == len(wire.requests)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await notifier.close()
        wire.close()


async def test_refer_refresh_updates_registry_owner_in_monotonic_domain(hass, monkeypatch):
    from custom_components.voip_stack import sip_application

    wire = NotifyWire()
    registrar = SipRegistrar(policy=configured_registrar_policy(), enabled=True, accounts=[], local_ip="127.0.0.1", local_sip_port=5060)
    application = SipApplicationMethods(hass, registrar)
    notifier = sip_transfer.ReferNotifier(wire.dialog, 7, wire.send)
    async def follow_up(_request, _address, result):
        return await wire.send(result.follow_up.headers, result.follow_up.body)
    row = SipEventSubscription(key="refer-owner", dialog=wire.dialog, event="refer",
        target="sip:target@127.0.0.1", subscriber="phone", to_tag="local",
        request=wire.refresh(45), addr=wire.peer.getsockname(), _expires_at=None,
        sender=follow_up, notify_lock=asyncio.Lock(), refer_notifier=notifier)
    application.subscriptions[row.key] = row
    try:
        notifier.bind_operation(asyncio.get_running_loop().time() + 90)
        assert row.expires_at == notifier.expires_at
        status, _reason, _headers = notifier.refresh(wire.refresh(45))
        assert status == 200 and row.expires_at == notifier.expires_at
        assert 44 < row.expires_at - asyncio.get_running_loop().time() <= 45
        # Wall-clock movement cannot expire the REFER usage or unrelated event.
        with monkeypatch.context() as context:
            context.setattr(sip_application.time, "time", lambda: 10**12)
            application._notify_subscribers("irrelevant", event="presence")
            assert application.subscriptions[row.key] is row
            assert row.expires_at == notifier.expires_at
        await notifier.report(200, True)
        assert sum(request.header("Subscription-State").startswith("terminated") for request in wire.requests) == 1
    finally:
        await notifier.close()
        await application.stop()
        wire.close()


@pytest.mark.parametrize("expires", [0, 1], ids=["unsubscribe", "real-expiry"])
async def test_expired_refer_observer_does_not_cancel_operation_or_leave_owner(expires):
    wire = NotifyWire()
    loop = asyncio.get_running_loop()
    gate = loop.create_future()
    ended = asyncio.Event()
    owners = {}
    def observation_ended():
        owners.pop("refer", None)
        ended.set()
    notifier = sip_transfer.ReferNotifier(wire.dialog, 7, wire.send, on_end=observation_ended)
    owners["refer"] = notifier
    async def action():
        return await gate
    task = asyncio.create_task(sip_transfer.run_refer_action(action, notifier))
    try:
        await asyncio.wait_for(wire.first.wait(), 2)
        assert notifier.refresh(wire.refresh(expires))[0] == 200
        await asyncio.wait_for(ended.wait(), 2)
        assert not owners and not task.done()
        gate.set_result(200)
        assert await task == 200
        finals = [r for r in wire.requests if r.header("Subscription-State").startswith("terminated")]
        assert len(finals) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await notifier.close()
        assert notifier.task is None or notifier.task.done()
        wire.close()
