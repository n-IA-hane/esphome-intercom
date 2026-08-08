"""SIP endpoint lifecycle helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from homeassistant.core import HomeAssistant

from .pbx_runtime import SipEndpointRuntime
from .endpoint_session import EndpointCallSession, TerminationInitiator, TerminationIntent
from .fsm import TerminalReason
from .runtime_data import (
    require_runtime_data,
)
from .session_cleanup import async_wait_for_cleanup

_LOGGER = logging.getLogger(__name__)


def project_session_termination(
    hass: HomeAssistant,
    session: EndpointCallSession,
    intent: TerminationIntent,
) -> None:
    """Project one terminal session without taking lifecycle ownership."""

    from .websocket_api import (
        _set_ha_softphone_call_state,
        _set_sip_bridge_call_state,
    )

    metadata = session.metadata
    endpoint_id = str(metadata.get("endpoint_id") or "")
    remote = intent.initiator is TerminationInitiator.REMOTE_PEER
    event = "CANCEL" if intent.reason == TerminalReason.CANCELLED.value else "BYE"
    common = {
        "call_id": session.call_id,
        "reason": intent.reason,
        "origin": "remote" if remote else "self",
        "last_sip_event": event,
    }
    for key in ("connected_party", "target_device_id", "sip_uri"):
        if value := str(metadata.get(key) or ""):
            common[key] = value
    if intent.response_status:
        common["sip_status_code"] = intent.response_status
    if endpoint_id:
        _set_ha_softphone_call_state(
            hass,
            intent.public_state,
            endpoint_id=endpoint_id,
            session_device_id=str(metadata.get("session_device_id") or ""),
            caller=session.caller,
            callee=session.callee,
            peer_name=session.caller if remote else session.callee,
            direction=str(metadata.get("direction") or ("incoming" if remote else "")),
            **common,
        )
    if metadata.get("bridge_dest_call_id") or (
        not endpoint_id and session.route_kind
    ):
        _set_sip_bridge_call_state(
            hass,
            intent.public_state,
            dest_call_id=str(metadata.get("bridge_dest_call_id") or ""),
            caller=session.caller,
            callee=session.callee,
            peer_name=session.callee,
            target=session.callee,
            terminal_reason=intent.reason,
            route_kind=session.route_kind,
            **common,
        )


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


def call_registry(hass: HomeAssistant) -> SipEndpointRuntime:
    runtime = require_runtime_data(hass)
    if runtime.sip is None:
        runtime.sip = SipEndpointRuntime()
    if not runtime.sip.has_termination_signaler:
        from .sip_runtime import async_signal_termination

        async def _signal_termination(call_id, intent) -> None:
            await async_signal_termination(hass, call_id, intent)

        runtime.sip.bind_termination_signaler(_signal_termination)
    if not runtime.sip.has_termination_observer:
        runtime.sip.bind_termination_observer(
            lambda session, intent: project_session_termination(
                hass, session, intent
            )
        )
    runtime.sip.bind_endpoint_registry(runtime.endpoints)
    return runtime.sip


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
    runtime = require_runtime_data(hass)
    pbx_runtime = runtime.sip

    await cancel_runtime_tasks(hass)
    if pbx_runtime is not None:
        pbx_runtime.forward_call = None

    if pbx_runtime is not None:
        try:
            await pbx_runtime.shutdown()
        except Exception:
            _LOGGER.debug("Ignoring authoritative PBX runtime stop error", exc_info=True)
    registry.clear_runtime()
    if runtime.sip is pbx_runtime:
        runtime.sip = None
