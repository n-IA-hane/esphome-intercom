"""Concurrent camera choices complete on the same real UDP SIP dialog."""

import asyncio

import pytest

from tests.test_softphone_video_intent import _Registry, intent
from tests.test_source_video_updates_ha import source_uac


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("choices", [(True, False), (True, False, True)])
async def test_overlapping_camera_choices_commit_and_leave_dialog_reusable(
    intent, source_uac, monkeypatch, choices,
):
    from custom_components.voip_stack.endpoint_session import EndpointCallSession

    module, store = intent
    client, peer = source_uac
    call_id = client.dialog_ids.call_id
    store.update(call_id=call_id, send_video=True)
    registry = _Registry(client)
    registry.session = EndpointCallSession(call_id, 7)
    monkeypatch.setattr(module, "call_registry", lambda _hass: registry)
    first_answer = asyncio.Event()
    release_answer = asyncio.Event()
    all_acks = asyncio.Event()
    original_request = client._run_built_dialog_request
    original_receive = peer.datagram_received
    paused = False

    def receive(raw, address):
        original_receive(raw, address)
        if sum(message.method == "ACK" for message in peer.requests) == len(choices) + 2:
            all_acks.set()

    async def run_request(*args, **kwargs):
        nonlocal paused
        message = await original_request(*args, **kwargs)
        if not paused and message is not None and message.status_code == 200:
            paused = True
            first_answer.set()
            await release_answer.wait()
        return message

    monkeypatch.setattr(client, "_run_built_dialog_request", run_request)
    monkeypatch.setattr(peer, "datagram_received", receive)
    first = asyncio.create_task(module.async_apply_send_video_intent(object(), "phone", True))
    tasks = [first]
    try:
        await asyncio.wait_for(first_answer.wait(), 2)
        for enabled in choices[1:]:
            store["send_video"] = enabled
            tasks.append(asyncio.create_task(module.async_apply_send_video_intent(object(), "phone", enabled)))
            # Let the next choice suspend while ON still owns offer/answer.
            await asyncio.sleep(0)
        release_answer.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert results == [True] * len(choices)
        assert client._prepared_reinvite is None
        assert client.dialog.local_video_direction == ("sendrecv" if choices[-1] else "recvonly")
        store["send_video"] = not choices[-1]
        assert await module.async_apply_send_video_intent(object(), "phone", not choices[-1])
        assert client._prepared_reinvite is None
        await asyncio.wait_for(all_acks.wait(), 1)
        invites = [message for message in peer.requests if message.method == "INVITE"]
        acks = [message for message in peer.requests if message.method == "ACK"]
        assert len(invites) == len(acks) == len(choices) + 2
        assert len({message.header("Call-ID") for message in invites}) == 1
    finally:
        release_answer.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("change", ["hangup", "replacement", "generation", "cancel"])
async def test_queued_camera_choice_cannot_mutate_a_stale_or_cancelled_owner(
    intent, source_uac, monkeypatch, change,
):
    from custom_components.voip_stack.endpoint_session import EndpointCallSession

    module, store = intent
    client, peer = source_uac
    call_id = client.dialog_ids.call_id
    store.update(call_id=call_id)
    registry = _Registry(client)
    registry.session = EndpointCallSession(call_id, 7)
    monkeypatch.setattr(module, "call_registry", lambda _hass: registry)
    lock = registry.session.video_intent_lock
    await lock.acquire()
    task = asyncio.create_task(module.async_apply_send_video_intent(object(), "phone", False))
    try:
        await asyncio.sleep(0)
        if change == "hangup":
            store["state"] = "idle"
        elif change == "replacement":
            registry.session = EndpointCallSession(call_id, 8)
        elif change == "generation":
            registry.is_generation_current = lambda *_args: False
        else:
            task.cancel()
        lock.release()
        if change == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert not await task
        assert client._prepared_reinvite is None
        assert sum(message.method == "INVITE" for message in peer.requests) == 1
        assert not lock.locked()
    finally:
        if lock.locked():
            lock.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
