"""Independent discovery and optional provisioning on the existing PBX runtime."""

from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError

from .physical_phone import discovery_enabled, provisioning_enabled
from .runtime_data import runtime_data


_LOGGER = logging.getLogger(__name__)


async def async_reconcile_physical_phone_runtime(hass, entry):
    runtime = entry.runtime_data
    if runtime_data(hass) is not runtime:
        return
    discovery = runtime.phone_discovery
    if not discovery_enabled(entry):
        if discovery is not None:
            runtime.phone_discovery = None
            await discovery.async_close()
    else:
        if discovery is None:
            from .physical_phone_discovery import PhoneDiscovery

            runtime.phone_discovery = discovery = PhoneDiscovery(hass, entry)
        try:
            await discovery.async_start()
        except BaseException as error:
            await discovery.async_close()
            if runtime.phone_discovery is discovery:
                runtime.phone_discovery = None
            if not isinstance(error, (OSError, HomeAssistantError)):
                raise
            _LOGGER.warning("Phone discovery unavailable (%s)", type(error).__name__)
    if runtime_data(hass) is not runtime or entry.runtime_data is not runtime:
        return
    # Discovery-only never imports the catalog, delivery worker or HTTP views.
    if provisioning_enabled(entry) or runtime.physical_phones is not None:
        from .physical_phone_provisioning import async_reconcile_physical_phones

        await async_reconcile_physical_phones(hass, entry)
