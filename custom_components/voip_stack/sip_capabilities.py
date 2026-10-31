"""Event-driven capability discovery on registered peers' existing SIP flows."""

from __future__ import annotations

import asyncio

from .core import sdp
from .endpoint_lifecycle import create_runtime_task
from .runtime_data import require_runtime_data, sip_endpoint_manager, sip_registrar


def registration_key(binding):
    return binding.flow_key


def schedule_capability_probe(hass, username):
    runtime = require_runtime_data(hass)
    registrar = sip_registrar(hass)
    bindings = registrar.registered_contacts(username) if registrar is not None else ()
    key = username.casefold()
    task = runtime.capability_probes.get(key)
    if not bindings or bindings[0].capabilities is not None:
        return
    if task is not None and not task.done():
        task.cancel()
    runtime.capability_probes[key] = create_runtime_task(hass, async_probe(hass, username))


async def async_probe(hass, username):
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
    if binding.capabilities is not None:
        return
    try:
        response = await manager.async_options(binding.contact_uri,
            (binding.source_host, binding.source_port), binding.transport)
        current = registrar.registered_contacts(username)
        if (sip_registrar(hass) is not registrar or not current
                or registration_key(current[0]) != key):
            return
        if response is None or response.status_code != 200 or not response.body:
            return
        capabilities = sdp.options_capabilities(response.body)
    except (OSError, TimeoutError, UnicodeError, ValueError, sdp.SdpError):
        # OPTIONS is a pre-call optimization; actual calls still negotiate.
        return
    current[0].capabilities = capabilities
    endpoint = runtime.endpoints.by_username(username)
    if endpoint is not None:
        flags = set(endpoint.capabilities)
        if capabilities["sip_video_codec"]:
            flags.add("video")
        else:
            flags.discard("video")
        runtime.endpoints.update(endpoint.endpoint_id, capabilities=frozenset(flags))
    from .config_entry_runtime import async_refresh_and_push_phonebook
    await async_refresh_and_push_phonebook(hass)


async def async_forget_peer(hass, endpoint_id):
    runtime = require_runtime_data(hass)
    endpoint = runtime.endpoints.get(endpoint_id)
    username = endpoint.username if endpoint is not None else endpoint_id
    task = runtime.capability_probes.pop(username.casefold(), None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    registrar = sip_registrar(hass)
    for binding in registrar.registered_contacts(username) if registrar is not None else ():
        binding.capabilities = None
