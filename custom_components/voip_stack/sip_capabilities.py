"""Event-driven capability discovery on registered peers' existing SIP flows."""

from __future__ import annotations

import asyncio

from .core import sdp
from .endpoint_lifecycle import create_runtime_task
from .phone_endpoint import EndpointKind
from .runtime_data import require_runtime_data, sip_endpoint_manager, sip_registrar


def registration_key(binding):
    return (binding.call_id, binding.contact_uri, binding.source_host, binding.source_port, binding.transport)


def schedule_capability_probe(hass, endpoint_id, username):
    runtime = require_runtime_data(hass)
    task = runtime.capability_probes.get(endpoint_id)
    if task is None or task.done():
        runtime.capability_probes[endpoint_id] = create_runtime_task(hass, async_probe(hass, endpoint_id, username))


async def async_probe(hass, endpoint_id, username):
    runtime = require_runtime_data(hass)
    registrar = sip_registrar(hass)
    manager = sip_endpoint_manager(hass)
    if registrar is None or manager is None:
        return
    bindings = registrar.registered_contacts(username)
    if not bindings:
        return
    binding = bindings[0]
    key = registration_key(binding)
    if runtime.peer_capabilities.get(endpoint_id, {}).get("registration") == key:
        return
    try:
        response = await manager.async_options(binding.contact_uri,
            (binding.source_host, binding.source_port), binding.transport)
        current = registrar.registered_contacts(username)
        if not current or registration_key(current[0]) != key or runtime.endpoints.get(endpoint_id) is None:
            return
        if response is None or response.status_code != 200 or not response.body:
            return
        capabilities = sdp.options_capabilities(response.body)
    except (OSError, TimeoutError, UnicodeError, ValueError, sdp.SdpError):
        # OPTIONS is a pre-call optimization; actual calls still negotiate.
        return
    runtime.peer_capabilities[endpoint_id] = {**capabilities, "registration": key}
    endpoint = runtime.endpoints.get(endpoint_id)
    if endpoint is not None and endpoint.kind is EndpointKind.ESPHOME:
        flags = set(endpoint.capabilities)
        if capabilities["sip_video_codec"]:
            flags.add("video")
        else:
            flags.discard("video")
        runtime.endpoints.update(endpoint_id, capabilities=frozenset(flags))
    from .config_entry_runtime import async_refresh_and_push_phonebook
    await async_refresh_and_push_phonebook(hass)


async def async_forget_peer(hass, endpoint_id):
    runtime = require_runtime_data(hass)
    task = runtime.capability_probes.pop(endpoint_id, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    runtime.peer_capabilities.pop(endpoint_id, None)
