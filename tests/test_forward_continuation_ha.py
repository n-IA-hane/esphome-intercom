"""Cancelling a transfer drains the actual active fallback route."""

import asyncio

import pytest

from custom_components.voip_stack.forward_plan import TransferResult, await_forward_result

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("handoff_complete", [False, True])
async def test_cancelled_transfer_drains_the_active_route_after_a_handoff(handoff_complete):
    entered, release, active, closed = (asyncio.Event() for _ in range(4))
    continuation = None

    async def route():
        nonlocal continuation
        entered.set()
        try:
            await release.wait()
            if handoff_complete:
                continuation = asyncio.create_task(fallback())
                return TransferResult("rerouted", "fallback", continuation=continuation)
            active.set()
            await asyncio.Event().wait()
        finally:
            if not handoff_complete:
                closed.set()

    async def fallback():
        active.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    initial = asyncio.create_task(route())
    waiter = asyncio.create_task(await_forward_result(initial))
    try:
        await entered.wait()
        release.set()
        if handoff_complete:
            await initial
        await active.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert closed.is_set()
        assert initial.done()
        assert continuation is None or continuation.done()
    finally:
        tasks = [waiter, initial, *([continuation] if continuation is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
