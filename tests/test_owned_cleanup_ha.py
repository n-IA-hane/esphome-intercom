"""Task admission and indirect terminal dependencies use real PBX owners."""

import asyncio

import pytest

from custom_components.voip_stack.endpoint_lifecycle import create_runtime_task
from custom_components.voip_stack.session_cleanup import async_wait_for_cleanup
from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


async def test_runtime_and_session_own_synchronous_failure_before_it_executes(lab):
    session = lab.calls.upsert("fast", state="in_call", owner="assist")
    observed = []
    async def fail():
        current = asyncio.current_task()
        observed.append((current in lab.runtime.tasks, current in session.tasks,
                         session.named_tasks.get("forward") is current))
        raise RuntimeError("synchronous preparation failure")
    task = create_runtime_task(lab.hass, fail())
    lab.calls.own_task(session.call_id, task, name="forward")
    assert not observed
    with pytest.raises(RuntimeError, match="synchronous preparation failure"):
        await task
    assert observed == [(True, True, True)]
    await lab.calls.terminate_call_wait(session.call_id, reason="local_hangup")


async def test_helper_child_can_request_termination_but_external_waits_full_cleanup(lab):
    session = lab.calls.upsert("indirect", state="in_call", owner="assist")
    entered, release, helper_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def close(_reason):
        entered.set()
        await release.wait()
    session.add_resource("gate", release, close)
    async def helper():
        await lab.calls.terminate_call_wait(session.call_id, reason="local_hangup")
        helper_done.set()
    async def work():
        await async_wait_for_cleanup(asyncio.create_task(helper()))
    owned = session.create_task(work(), name="parent-operation")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert helper_done.is_set()
        external = asyncio.create_task(lab.calls.terminate_call_wait(session.call_id, reason="local_hangup"))
        await asyncio.sleep(0)
        assert not external.done() and not session.terminated.is_set()
        release.set()
        await asyncio.wait_for(external, 1)
        assert session.terminated.is_set()
        assert lab.calls.get_session(session.call_id) is None
    finally:
        release.set()
        await asyncio.gather(owned, return_exceptions=True)
