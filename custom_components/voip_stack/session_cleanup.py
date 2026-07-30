"""Shared SIP runtime cleanup primitives."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import Any, TypeVar


_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


async def async_wait_for_cleanup(task: asyncio.Future[_T]) -> _T:
    """Wait for owned cleanup through any number of caller cancellations.

    ``asyncio.shield`` protects the child once, but a second ``cancel()`` can
    otherwise hit an unshielded recovery await and cancel the cleanup itself.
    Keep every wait shielded, remember caller cancellation, and propagate it
    only after the owned cleanup reaches a terminal state.
    """

    caller_cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                # Cancellation originated inside the owned operation rather
                # than from its waiter, so it remains the operation's result.
                raise
            caller_cancelled = True
    if caller_cancelled:
        raise asyncio.CancelledError
    return result


@dataclass(slots=True)
class SipRuntimeCleanupResult:
    """Result of an idempotent SIP runtime cleanup pass."""

    watcher_cancelled: bool = False
    client_closed: bool = False
    relay_stopped: bool = False


async def async_cleanup_sip_runtime(
    *,
    relay: Any = None,
    client: Any = None,
    watcher: asyncio.Task | None = None,
    terminate_client: bool = True,
    relay_first: bool = False,
) -> SipRuntimeCleanupResult:
    """Stop every SIP runtime resource before propagating caller cancellation."""

    # A dialog watcher may be the task that observed remote BYE and entered
    # bridge teardown.  Cleanup runs in a shielded child task, so comparing the
    # watcher only with the child would make it cancel and await its own caller.
    # Detach that self-owned watcher before crossing the task boundary.
    if watcher is asyncio.current_task():
        watcher = None

    task = asyncio.create_task(
        _async_cleanup_sip_runtime_impl(
            relay=relay,
            client=client,
            watcher=watcher,
            terminate_client=terminate_client,
            relay_first=relay_first,
        ),
        name="voip-sip-runtime-cleanup",
    )
    return await async_wait_for_cleanup(task)


async def _async_cleanup_sip_runtime_impl(
    *,
    relay: Any = None,
    client: Any = None,
    watcher: asyncio.Task | None = None,
    terminate_client: bool = True,
    relay_first: bool = False,
) -> SipRuntimeCleanupResult:
    """Perform one complete cleanup pass in a cancellation-isolated task."""

    result = SipRuntimeCleanupResult()

    async def _stop_relay() -> None:
        if relay is None or result.relay_stopped:
            return
        try:
            await relay.stop()
            result.relay_stopped = True
        except asyncio.CancelledError:
            _LOGGER.debug("SIP RTP relay cleanup was internally cancelled")
        except Exception:
            _LOGGER.debug("Ignoring SIP RTP relay cleanup error", exc_info=True)

    if relay_first:
        await _stop_relay()

    if watcher is not None:
        current_task = asyncio.current_task()
        if watcher is not current_task:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
            result.watcher_cancelled = True

    if client is not None:
        if terminate_client:
            try:
                await client.terminate()
            except asyncio.CancelledError:
                _LOGGER.debug("SIP client terminate cleanup was internally cancelled")
            except Exception:
                _LOGGER.debug("Ignoring SIP client terminate cleanup error", exc_info=True)
        try:
            await client.close()
            result.client_closed = True
        except asyncio.CancelledError:
            _LOGGER.debug("SIP client close cleanup was internally cancelled")
        except Exception:
            _LOGGER.debug("Ignoring SIP client close cleanup error", exc_info=True)

    if not relay_first:
        await _stop_relay()

    return result
