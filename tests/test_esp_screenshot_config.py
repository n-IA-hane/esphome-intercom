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
        ({"lvgl_id": "ui", "rotation": 0}, True),
        ({"lvgl_id": "ui", "rotation": 90}, False),
        ({"lvgl_id": "ui", "rotation": 45}, False),
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
                "capture_id": {"id": "screenshot_id_entity", "name": "Screenshot ID"},
                "capture_status": {
                    "id": "screenshot_status_entity",
                    "name": "Screenshot status",
                },
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
        assert "name: Take screenshot" in result.stdout
        assert "entity_category: diagnostic" in result.stdout
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
            "set_capture_id_sensor(screenshot_id_entity)",
            "set_capture_status_sensor(screenshot_status_entity)",
        ]:
            assert call in main


@pytest.mark.parametrize("rotation", [0, 90, 180, 270, 45, 360])
def test_mipi_screenshot_rotation_schema_and_codegen(tmp_path, rotation):
    config = {
        "esphome": {"name": "screenshot-p4", "build_path": str(tmp_path / "build")},
        "esp32": {"board": "esp32-p4-evboard", "framework": {"type": "esp-idf"}},
        "network": {},
        "psram": {},
        "esp_ldo": [{"channel": 3, "voltage": "2.5V"}],
        "external_components": [
            {
                "source": {"type": "local", "path": str(ROOT / "esphome/components")},
                "components": ["esp_screenshot", "mipi_dsi"],
            }
        ],
        "display": [{"platform": "mipi_dsi", "id": "screen", "model": "WAVESHARE-P4-NANO-10.1"}],
        "esp_screenshot": {"id": "screen_capture", "mipi_dsi_id": "screen", "rotation": rotation},
    }
    path = tmp_path / "p4.yaml"
    path.write_text(yaml.safe_dump(config))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    valid = rotation in (0, 90, 180, 270)
    assert (result.returncode == 0) is valid, result.stdout + result.stderr
    if valid:
        generated = subprocess.run(
            [sys.executable, "-m", "esphome", "compile", str(path), "--only-generate"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert generated.returncode == 0, generated.stdout + generated.stderr
        main = (tmp_path / "build/src/main.cpp").read_text()
        assert f"screen_capture->set_rotation(display::DISPLAY_ROTATION_{rotation}_DEGREES)" in main
