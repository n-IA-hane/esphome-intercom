"""SIP endpoint lifecycle helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from homeassistant.core import HomeAssistant

from .call_registry import CallRegistry
from .runtime_data import (
    require_runtime_data,
    sip_endpoint_manager,
)
from .session_cleanup import async_wait_for_cleanup

_LOGGER = logging.getLogger(__name__)


def create_runtime_task(hass: HomeAssistant, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Create a detached integration task that is cancelled on endpoint reload."""

    tasks = require_runtime_data(hass).tasks
    task = hass.async_create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def cancel_runtime_tasks(hass: HomeAssistant) -> None:
    runtime = require_runtime_data(hass)
    tasks = set(runtime.tasks)
    runtime.tasks.clear()
    current = asyncio.current_task()
    for task in tasks:
        if task is not current:
            task.cancel()
    if tasks:
        await asyncio.gather(*(task for task in tasks if task is not current), return_exceptions=True)


def call_registry(hass: HomeAssistant) -> CallRegistry:
    runtime = require_runtime_data(hass)
    if runtime.calls is None:
        runtime.calls = CallRegistry()
    runtime.calls.bind_endpoint_registry(runtime.endpoints)
    return runtime.calls


async def async_stop_sip_endpoint(hass: HomeAssistant) -> None:
    runtime = require_runtime_data(hass)
    task = runtime.shutdown_task
    if not isinstance(task, asyncio.Task) or task.done():
        task = asyncio.create_task(
            _async_stop_sip_endpoint(hass),
            name="voip-sip-endpoint-runtime-stop",
        )
        runtime.shutdown_task = task
    try:
        await async_wait_for_cleanup(task)
    finally:
        if task.done() and runtime.shutdown_task is task:
            runtime.shutdown_task = None


async def _async_stop_sip_endpoint(hass: HomeAssistant) -> None:
    registry = call_registry(hass)
    endpoint = sip_endpoint_manager(hass)
    runtime = require_runtime_data(hass)
    pbx_runtime = runtime.sip

    if endpoint is not None:
        snapshot = endpoint.snapshot()
        for call_id in snapshot.pending_call_ids:
            endpoint.send_final_response(call_id, 503, "Service Unavailable", decline_reason="shutdown")
        for call_id in snapshot.active_call_ids:
            endpoint.send_bye(call_id)

    await cancel_runtime_tasks(hass)
    if pbx_runtime is not None:
        pbx_runtime.forward_tasks.clear()
        pbx_runtime.forward_claims.clear()
        pbx_runtime.deadlines.clear()
        pbx_runtime.trunk_info_queues.clear()
        pbx_runtime.trunk_closed_calls.clear()
        pbx_runtime.forward_call = None

    if pbx_runtime is not None:
        try:
            await pbx_runtime.shutdown()
        except Exception:
            _LOGGER.debug("Ignoring authoritative PBX runtime stop error", exc_info=True)
    registry.bind_session_owner(None)
    if runtime.sip is pbx_runtime:
        runtime.sip = None
    registry.clear_runtime()
