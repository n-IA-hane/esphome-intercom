"""Exercise the documented screenshot helper against official Home Assistant."""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_template_image_updates_from_capture_ids_and_caches_demand_fetches(tmp_path):
    interpreter = Path(
        os.environ.get(
            "HA_PYTHON", str(ROOT.parent / "ha-voip-lab/.venv/bin/python")
        )
    )
    if not interpreter.is_file():
        pytest.skip("Set HA_PYTHON to an interpreter with Home Assistant installed")

    documented = (ROOT / "docs/ESP_SCREENSHOT.md").read_text().split(
        "For YAML-managed helpers", 1
    )[1].split("```yaml\n", 1)[1].split("```", 1)[0]
    config_path = tmp_path / "helper.json"
    config_path.write_text(json.dumps(yaml.safe_load(documented)["template"][0]["image"][0]))
    program = tmp_path / "ha_image_contract.py"
    program.write_text(
        r"""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

import httpx

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.components.template.image import IMAGE_YAML_SCHEMA, StateImageEntity

async def main():
    hass = HomeAssistant(sys.argv[1])
    hass.config_entries = ConfigEntries(hass, {})
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    fetched, updates = [], []

    def respond(request):
        fetched.append(str(request.url))
        capture = request.url.params["capture"]
        return httpx.Response(200, headers={"content-type": "image/jpeg"},
                              content=f"JPEG:{capture}".encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        config = IMAGE_YAML_SCHEMA(json.loads(Path(sys.argv[2]).read_text()))
        with patch("homeassistant.components.image.get_async_client", return_value=client):
            image = StateImageEntity(hass, config, config.get("unique_id"))
        image.entity_id = "image.screen_screenshot"

        def changed(state, attributes, listeners, error):
            assert error is None, error
            updates.append(state)

        remove = image.async_start_preview(changed)
        try:
            assert not image.available and not image.should_poll and not fetched
            for empty in ("", "unknown", "unavailable"):
                hass.states.async_set("sensor.screen_screenshot_id", empty)
                await hass.async_block_till_done()
                assert not image.available and not fetched

            hass.states.async_set("sensor.screen_screenshot_id", "first")
            await hass.async_block_till_done()
            assert image.available and image.image_url.endswith("?capture=first")
            first_state = image.state
            assert first_state is not None and updates[-1] == first_state
            assert not fetched  # A state event does not itself download pixels.
            assert await image.async_image() == b"JPEG:first"
            assert await image.async_image() == b"JPEG:first"
            assert len(fetched) == 1

            # Unrelated events and a repeated ID leave the existing cache valid.
            hass.states.async_set("sensor.unrelated", "changed")
            hass.states.async_set("sensor.screen_screenshot_id", "first")
            await hass.async_block_till_done()
            assert image.state == first_state
            assert await image.async_image() == b"JPEG:first" and len(fetched) == 1

            hass.states.async_set("sensor.screen_screenshot_id", "second")
            await hass.async_block_till_done()
            assert image.state != first_state and updates[-1] == image.state
            assert image.image_url.endswith("?capture=second") and len(fetched) == 1
            assert await image.async_image() == b"JPEG:second"
            assert await image.async_image() == b"JPEG:second" and len(fetched) == 2
        finally:
            remove()
    await hass.async_block_till_done()
    await hass.async_stop(force=True)

asyncio.run(main())
"""
    )
    result = subprocess.run(
        [str(interpreter), str(program), str(tmp_path), str(config_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
