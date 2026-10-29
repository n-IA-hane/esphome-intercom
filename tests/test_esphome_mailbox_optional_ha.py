"""HA mailbox setup stays usable when the optional ESPHome SDK is absent."""

from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.ha


def test_mailbox_bridge_without_esphome_in_fresh_ha_process(tmp_path):
    program = r'''
import asyncio
import importlib.abc
import json
import sys

sys.path.insert(0, sys.argv[1])
class MissingOptionalSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "aioesphomeapi" or fullname.startswith("aioesphomeapi."):
            raise ModuleNotFoundError("Optional ESPHome SDK is absent", name=fullname)
sys.meta_path.insert(0, MissingOptionalSdk())

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from custom_components.voip_stack.esphome_mailbox import EspHomeMailboxBridge
from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
from custom_components.voip_stack.runtime_data import VoipStackRuntime
from pytest_homeassistant_custom_component.common import MockConfigEntry

async def main():
    hass = HomeAssistant(sys.argv[2])
    hass.config_entries = ConfigEntries(hass, {})
    device_registry.async_setup(hass)
    await device_registry.async_load(hass, load_empty=True)
    await entity_registry.async_load(hass, load_empty=True)
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    endpoints = EndpointRegistry()
    entry.runtime_data = VoipStackRuntime(transport_config={}, assist_config={},
        trunk_config={}, endpoints=endpoints, phones=PhoneAdapterRegistry(hass, endpoints))
    bridge = EspHomeMailboxBridge(hass)
    bridge.start()
    await bridge.async_push()
    await bridge.async_close()
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_stop(force=True)
    assert "aioesphomeapi" not in sys.modules
    assert "homeassistant.components.esphome" not in sys.modules
    print(json.dumps({"import": "ok", "setup_push_stop": "ok", "esphome_loaded": False}))

asyncio.run(main())
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", program,
         str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"setup_push_stop": "ok"' in result.stdout
    assert '"esphome_loaded": false' in result.stdout
