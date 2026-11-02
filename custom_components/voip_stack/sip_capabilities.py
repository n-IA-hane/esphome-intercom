"""Advisory audio discovery owned by authenticated registrar bindings."""

from __future__ import annotations

import asyncio

from .core import sdp
from .endpoint_lifecycle import create_runtime_task
from .runtime_data import require_runtime_data, sip_endpoint_manager, sip_registrar


def schedule_capability_probe(hass, username):
    """Schedule each uncached Contact once, without delaying REGISTER or INVITE."""
    runtime = require_runtime_data(hass)
    registrar = sip_registrar(hass)
    bindings = registrar.registered_contacts(username) if registrar is not None else ()
    user = username.casefold()
    live = {binding.capability_generation for binding in bindings}
    for key, task in tuple(runtime.capability_probes.items()):
        if key[0] == user and key[1] not in live:
            task.cancel()
            runtime.capability_probes.pop(key, None)
    for binding in bindings:
        key = (user, binding.capability_generation)
        if binding.capabilities is not None or key in runtime.capability_probes:
            continue
        task = create_runtime_task(hass, async_probe(hass, username, binding.capability_generation))
        runtime.capability_probes[key] = task

        def completed(done, key=key):
            if runtime.capability_probes.get(key) is done:
                runtime.capability_probes.pop(key, None)

        task.add_done_callback(completed)


async def async_probe(hass, username, generation=None):
    """Publish only while registrar, listener and binding lifetime still match."""
    registrar = sip_registrar(hass)
    manager = sip_endpoint_manager(hass)
    if registrar is None or manager is None:
        return
    bindings = registrar.registered_contacts(username)
    binding = next((item for item in bindings if generation is None
        or item.capability_generation == generation), None)
    if binding is None or binding.capabilities is not None:
        return
    generation = binding.capability_generation
    flow_key = binding.flow_key
    try:
        response = await manager.async_options(binding.contact_uri,
            (binding.source_host, binding.source_port), binding.transport)
        if (response is None or response.status_code != 200 or not response.body
                or response.header("Content-Type").split(";", 1)[0].strip().lower() != "application/sdp"):
            return
        capabilities = sdp.options_capabilities(response.body)
    except (OSError, TimeoutError, UnicodeError, ValueError, sdp.SdpError):
        return
    if sip_registrar(hass) is not registrar or sip_endpoint_manager(hass) is not manager:
        return
    current = next((item for item in registrar.registered_contacts(username)
        if item.capability_generation == generation and item.flow_key == flow_key), None)
    if current is None:
        return
    current.capabilities = capabilities
    from .config_entry_runtime import async_refresh_and_push_phonebook

    await async_refresh_and_push_phonebook(hass)
