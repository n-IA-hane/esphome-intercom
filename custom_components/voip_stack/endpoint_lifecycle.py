"""SIP endpoint lifecycle helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from homeassistant.core import HomeAssistant

from .call_registry import CallRegistry
from .const import DOMAIN
from .media_ports import release_media_reservation
from .runtime_data import (
    conference_component,
    require_runtime_data,
    runtime_data,
    sip_endpoint_manager,
)
from .session_cleanup import async_cleanup_sip_runtime, async_wait_for_cleanup

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
    bucket = hass.data.setdefault(DOMAIN, {})
    task = bucket.get("sip_endpoint_stop_task")
    if not isinstance(task, asyncio.Task) or task.done():
        task = asyncio.create_task(
            _async_stop_sip_endpoint(hass),
            name="voip-sip-endpoint-runtime-stop",
        )
        bucket["sip_endpoint_stop_task"] = task
    try:
        await async_wait_for_cleanup(task)
    finally:
        if task.done() and bucket.get("sip_endpoint_stop_task") is task:
            bucket.pop("sip_endpoint_stop_task", None)


async def _async_stop_sip_endpoint(hass: HomeAssistant) -> None:
    registry = call_registry(hass)
    bucket = hass.data.get(DOMAIN, {})
    endpoint = sip_endpoint_manager(hass)
    runtime = runtime_data(hass)
    pbx_runtime = runtime.sip if runtime is not None else bucket.get("pbx_runtime")

    if endpoint is not None:
        snapshot = endpoint.snapshot()
        for call_id in snapshot.pending_call_ids:
            endpoint.send_final_response(call_id, 503, "Service Unavailable", decline_reason="shutdown")
        for call_id in snapshot.active_call_ids:
            endpoint.send_bye(call_id)

    await cancel_runtime_tasks(hass)
    bucket.pop("async_forward_call", None)
    bucket.pop("forward_tasks", None)
    bucket.pop("forward_claims", None)
    bucket.pop("call_deadlines", None)

    if pbx_runtime is None:
        watchers = {
            task
            for task in registry.client_watchers.values()
            if isinstance(task, asyncio.Task)
        }
        current = asyncio.current_task()
        for task in watchers:
            if task is not current:
                task.cancel()
        if watchers:
            await asyncio.gather(
                *(task for task in watchers if task is not current),
                return_exceptions=True,
            )

    manager = conference_component(hass)
    runtime_owns_manager = bool(
        pbx_runtime is not None
        and pbx_runtime.component("conference_manager") is manager
    )
    if manager is not None and not runtime_owns_manager:
        bucket.pop("conference_manager", None)
        try:
            await manager.close(reason="local_hangup")
        except Exception:
            _LOGGER.debug("Ignoring conference shutdown error", exc_info=True)

    async def _stop_relay(relay) -> None:
        try:
            await relay.stop()
        except Exception:
            _LOGGER.debug("Ignoring SIP RTP relay stop error", exc_info=True)

    async def _stop_client(client) -> None:
        await async_cleanup_sip_runtime(
            client=client,
            terminate_client=True,
        )

    if pbx_runtime is None:
        relays = {id(relay): relay for relay in registry.relays.values()}.values()
        clients = {
            id(client): client for client in registry.sip_clients.values()
        }.values()
        await asyncio.gather(
            *(_stop_relay(relay) for relay in relays),
            *(_stop_client(client) for client in clients),
        )
        # Compatibility cleanup for a registry created without the new owner.
        for media in [
            *registry.softphone_media.values(),
            *registry.preanswered.values(),
        ]:
            release_media_reservation(media)
    if pbx_runtime is not None:
        try:
            await pbx_runtime.shutdown()
        except Exception:
            _LOGGER.debug("Ignoring authoritative PBX runtime stop error", exc_info=True)
    elif endpoint is not None:
        try:
            await endpoint.stop()
        except Exception:
            _LOGGER.debug("Ignoring SIP endpoint stop error", exc_info=True)
    registry.bind_session_owner(None)
    if runtime is not None and runtime.sip is pbx_runtime:
        runtime.sip = None
    elif bucket.get("pbx_runtime") is pbx_runtime:
        bucket.pop("pbx_runtime", None)
    if pbx_runtime is not None:
        for key, component_name in (
            ("sip_trunk", "trunk"),
            ("sip_registrar", "registrar"),
            ("conference_manager", "conference_manager"),
        ):
            value = bucket.get(key)
            if value is not None and pbx_runtime.component(component_name) is value:
                bucket.pop(key, None)
    if bucket.get("sip_endpoint") is endpoint:
        bucket.pop("sip_endpoint", None)
    bucket.pop("sip_server", None)
    bucket.pop("sip_tcp_server", None)
    registry.clear_runtime()
