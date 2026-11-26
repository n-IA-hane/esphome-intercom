"""Dialog leases refresh snapshots within 60 seconds without widening other packages."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from .test_automation_originate_ha import lab as lab
from .test_dialog_info_ha import blf as blf, subscribe

pytestmark = pytest.mark.ha


async def drained(methods):
    async with asyncio.timeout(2):
        while methods.notifiers or any(task.done() for task in methods.tasks):
            await asyncio.sleep(0)


@pytest.mark.parametrize("requested,granted", [(3600, 60), (300, 60), (60, 60), (17, 17)])
async def test_dialog_server_grants_bounded_lease_and_honors_shorter_request(blf, requested, granted):
    methods, notifications, send = blf
    loop = asyncio.get_running_loop()
    before = loop.time()
    response = await methods.handle(subscribe(expires=requested), ("127.0.0.3", 5060), "UDP", send)
    assert response.status == 200 and dict(response.headers)["Expires"] == str(granted)
    owner = next(iter(methods.subscriptions.values()))
    assert before + granted <= owner.expires_at <= loop.time() + granted
    response.follow_up_scheduler()
    initial = await asyncio.wait_for(notifications.get(), 1)
    assert dict(initial.headers)["Subscription-State"] == f"active;expires={granted}"
    assert ElementTree.fromstring(initial.body).attrib["state"] == "full"
    await drained(methods)
    assert len(methods.subscription_timers) == 1 and not methods.tasks


@pytest.mark.parametrize("event,content_type", [("presence", "application/pidf+xml"),
                                                ("message-summary", "application/simple-message-summary")])
async def test_dialog_cap_does_not_shorten_presence_or_mailbox_leases(blf, event, content_type):
    methods, notifications, send = blf
    methods.mailbox_archive = SimpleNamespace(counts=lambda _: (0, 0))
    request = subscribe(target="watcher", event=event, accept=content_type, expires=900)
    response = methods._subscribe(request, ("127.0.0.3", 5060), "watcher", send,
                                  transport="UDP", mailbox_id="watcher" if event == "message-summary" else "")
    assert response.status == 200 and dict(response.headers)["Expires"] == "900"
    response.follow_up_scheduler()
    notification = await asyncio.wait_for(notifications.get(), 1)
    assert dict(notification.headers)["Subscription-State"] == "active;expires=900"
    await drained(methods)


@pytest.mark.parametrize("watches", [12, 128])
async def test_many_watches_renew_full_snapshots_without_register_bursts_or_owner_growth(blf, monkeypatch, watches):
    methods, notifications, send = blf
    loop = asyncio.get_running_loop()
    real_time = loop.time
    elapsed = [0.0]
    # Subscription leases and asyncio timers share the loop's monotonic clock.
    monkeypatch.setattr(loop, "time", lambda: real_time() + elapsed[0])
    address = ("127.0.0.3", 5060)

    async def register(sequence):
        request = sip.parse_message(sip.build_request("REGISTER", "sip:127.0.0.1", [
            ("Via", f"SIP/2.0/UDP 127.0.0.3:5060;branch=z9hG4bK-register-{sequence}"),
            ("From", "<sip:watcher@127.0.0.1>;tag=registration"), ("To", "<sip:watcher@127.0.0.1>"),
            ("Contact", "<sip:watcher@127.0.0.3:5060>"), ("Call-ID", "lease-registration"),
            ("CSeq", f"{sequence} REGISTER"), ("Expires", "300")]))
        challenge = await methods.registrar.handle_register(request, address, "UDP")
        assert challenge.status == 401
        authorization = build_digest_authorization(challenge_header=dict(challenge.headers)["WWW-Authenticate"],
            username="watcher", password="test-only", method="REGISTER", uri=request.uri,
            nonce_count=sequence, cnonce="lease-fixture")
        result = await methods.registrar.handle_register(
            replace(request, headers=(*request.headers, ("Authorization", authorization))), address, "UDP")
        assert result.status == 200
        return methods.registrar.registered_contacts("watcher")[0]

    binding = await register(1)
    tags = {}
    for index in range(watches):
        identity = f"watch-{index}"
        result = await methods.handle(subscribe(call_id=identity, expires=3600), address, "UDP", send)
        assert result.status == 200 and dict(result.headers)["Expires"] == "60"
        tags[identity] = result.to_tag
        result.follow_up_scheduler()
    for _ in range(watches):
        initial = await asyncio.wait_for(notifications.get(), 1)
        assert ElementTree.fromstring(initial.body).attrib["version"] == "0"
    await drained(methods)
    for cycle in (1, 2):
        elapsed[0] += 48
        for identity, tag in tags.items():
            result = await methods.handle(subscribe(call_id=identity, tag=tag, cseq=cycle+1, expires=3600),
                                          address, "UDP", send)
            assert result.status == 200 and dict(result.headers)["Expires"] == "60"
            result.follow_up_scheduler()
        for _ in range(watches):
            full = await asyncio.wait_for(notifications.get(), 1)
            root = ElementTree.fromstring(full.body)
            assert root.attrib["state"] == "full" and root.attrib["version"] == str(cycle)
        await drained(methods)
        snapshot = methods.snapshot()
        assert snapshot["subscriptions"] == watches
        assert snapshot["notifiers"] == snapshot["pending_notifications"] == snapshot["task_failures"] == 0
        assert len(methods.subscription_timers) == watches and not methods.tasks
        timers = dict(methods.subscription_timers)
        refreshed = await register(cycle+1)
        assert refreshed.flow_key == binding.flow_key and refreshed.capability_generation == binding.capability_generation
        assert notifications.empty() and methods.subscription_timers == timers
    for identity, tag in tags.items():
        result = await methods.handle(subscribe(call_id=identity, tag=tag, cseq=4, expires=0), address, "UDP", send)
        assert result.status == 200
        result.follow_up_scheduler()
    for _ in range(watches):
        terminal = await asyncio.wait_for(notifications.get(), 1)
        assert dict(terminal.headers)["Subscription-State"].startswith("terminated")
    await drained(methods)
    assert not methods.subscriptions and not methods.subscription_timers and not methods.notifiers
    assert not methods.tasks


async def test_configured_dialog_lease_is_snapshotted_and_uses_no_sleeping_task(blf, lab):
    methods, notifications, send = blf
    lab.runtime.transport_config['max_dialog_subscription_expires'] = 600
    response = await methods.handle(subscribe(expires=3600), ('127.0.0.3', 5060), 'UDP', send)
    assert response.status == 200 and dict(response.headers)['Expires'] == '600'
    response.follow_up_scheduler()
    initial = await asyncio.wait_for(notifications.get(), 1)
    assert dict(initial.headers)['Subscription-State'] == 'active;expires=600'
    await drained(methods)
    subscription = next(iter(methods.subscriptions.values()))
    timer = methods.subscription_timers[subscription.key]
    assert isinstance(timer, asyncio.TimerHandle)
    assert not methods.tasks, 'An idle subscription must not retain a sleeping asyncio task'
    deadline = subscription.expires_at
    lab.runtime.transport_config['max_dialog_subscription_expires'] = 3600
    assert subscription.expires_at == deadline and methods.subscription_timers[subscription.key] is timer
    response = await methods.handle(subscribe(tag=subscription.to_tag, cseq=2, expires=3600),
                                    ('127.0.0.3', 5060), 'UDP', send)
    assert dict(response.headers)['Expires'] == '3600'
    assert timer.cancelled()
    response.follow_up_scheduler()
    await notifications.get()
    await drained(methods)
    handles = tuple(methods.subscription_timers.values())
    await methods.stop()
    assert all(handle.cancelled() for handle in handles)
    assert not methods.subscription_timers and not methods.tasks


async def test_refreshed_timer_ignores_old_deadline_then_expires_once(blf, lab, monkeypatch):
    methods, notifications, send = blf
    lab.runtime.transport_config['max_dialog_subscription_expires'] = 600
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = [0.0]
    monkeypatch.setattr(loop, 'time', lambda: clock() + offset[0])
    first = await methods.handle(subscribe(expires=1), ('127.0.0.3', 5060), 'UDP', send)
    first.follow_up_scheduler()
    await notifications.get()
    await drained(methods)
    owner = next(iter(methods.subscriptions.values()))
    timer = methods.subscription_timers[owner.key]
    old_callback, old_args = timer._callback, timer._args
    refreshed = await methods.handle(subscribe(tag=first.to_tag, cseq=2, expires=600),
                                      ('127.0.0.3', 5060), 'UDP', send)
    refreshed.follow_up_scheduler()
    await notifications.get()
    await drained(methods)
    offset[0] = 2
    old_callback(*old_args)
    assert methods.subscriptions[owner.key] is owner
    assert not notifications.qsize()
    offset[0] = 601
    # The actual event-loop handle fires; no sleeping expiry task is needed.
    terminal = await asyncio.wait_for(notifications.get(), 1)
    assert dict(terminal.headers)['Subscription-State'] == 'terminated;reason=timeout'
    await drained(methods)
    assert not methods.subscriptions and not methods.subscription_timers and not methods.tasks
    assert notifications.empty()


async def test_failed_notify_retires_the_lease_handle(blf):
    methods, notifications, send = blf
    async def gone(request, address, result):
        await send(request, address, result)
        return sip.SipMessage(status_code=481, reason='Call/Transaction Does Not Exist')
    response = await methods.handle(subscribe(expires=3600), ('127.0.0.3', 5060), 'UDP', gone)
    timer = next(iter(methods.subscription_timers.values()))
    response.follow_up_scheduler()
    await notifications.get()
    await drained(methods)
    assert timer.cancelled()
    assert not methods.subscriptions and not methods.subscription_timers and not methods.tasks
