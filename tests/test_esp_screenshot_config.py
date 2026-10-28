"""Validate the public screenshot schema through the official ESPHome loader."""

from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"lvgl_id": "ui"}, True),
        ({}, False),
        ({"lvgl_id": "ui", "mipi_dsi_id": "screen"}, False),
        ({"lvgl_id": "ui", "max_jpeg_size": 1024}, False),
        ({"lvgl_id": "ui", "quality": 101}, False),
        ({"lvgl_id": "ui", "task_core": 2}, False),
        ({"lvgl_id": "ui", "task_priority": 25}, False),
        ({"lvgl_id": "ui", "task_stack_size": 2048}, False),
        ({"lvgl_id": "ui", "task_stack_size": 6145}, False),
        (
            {
                "lvgl_id": "ui",
                "task_core": 1,
                "task_priority": 3,
                "task_stack_size": 8192,
                "task_stack_in_psram": False,
                "buffer_in_psram": False,
            },
            True,
        ),
    ],
)
def test_screenshot_schema_and_native_api_package(tmp_path, settings, expected):
    config = {
        "esphome": {
            "name": "screenshot-contract",
            "build_path": str(tmp_path / "build"),
        },
        "esp32": {"board": "esp32-s3-devkitc-1", "framework": {"type": "esp-idf"}},
        "psram": {"mode": "octal", "speed": "80MHz"},
        "wifi": {"ssid": "test-network", "password": "test-password"},
        "spi": {"clk_pin": "GPIO14", "mosi_pin": "GPIO13"},
        "display": [
            {
                "platform": "ili9xxx",
                "id": "screen",
                "model": "GC9A01A",
                "cs_pin": "GPIO15",
                "dc_pin": "GPIO16",
                "invert_colors": False,
            }
        ],
        "lvgl": {"id": "ui", "displays": ["screen"]},
        "external_components": [
            {
                "source": {"type": "local", "path": str(ROOT / "esphome/components")},
                "components": ["esp_screenshot"],
            }
        ],
        "esp_screenshot": {"id": "screen_capture", **settings},
    }
    if (
        settings.get("task_stack_in_psram") is False
        and settings.get("buffer_in_psram") is False
    ):
        config.pop("psram")
    path = tmp_path / "screenshot.yaml"
    path.write_text(
        yaml.safe_dump(config)
        + f"\npackages:\n  screenshot_api: !include {ROOT / 'packages/diagnostics/screenshot.yaml'}\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (result.returncode == 0) is expected, result.stdout + result.stderr
    if expected:
        assert "take_screenshot" in result.stdout
        if "task_core" not in settings:
            assert "task_core: any" in result.stdout

    if expected and settings.get("task_core") == 1:
        generated = subprocess.run(
            [sys.executable, "-m", "esphome", "compile", str(path), "--only-generate"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert generated.returncode == 0, generated.stdout + generated.stderr
        main = (tmp_path / "build/src/main.cpp").read_text()
        for call in [
            "set_task_core(1)",
            "set_task_priority(3)",
            "set_task_stack_size(8192)",
            "set_task_stack_in_psram(false)",
            "set_buffer_in_psram(false)",
        ]:
            assert call in main
