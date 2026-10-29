"""The visibility adapter binds real native pages without a polling task."""

from pathlib import Path
import subprocess
import sys

import pytest
from esphome import yaml_util

ROOT = Path(__file__).resolve().parents[1]

PROFILES = [
    "full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "voip-only/single-bus/spotpear-ball-v2-voip.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml",
    "voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml",
    "experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml",
    "experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-voip-only-afe.yaml",
]


def test_visibility_codegen_binds_all_boot_pages(tmp_path):
    config = tmp_path / "display.yaml"
    config.write_text(f"""
esphome:
  name: visibility-contract
esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf
logger:
external_components:
  - source: {ROOT / 'esphome/components'}
    components: [lvgl_visibility]
spi:
  clk_pin: GPIO4
  mosi_pin: GPIO2
display:
  - platform: mipi_spi
    id: screen
    model: ST7789V
    cs_pin: GPIO5
    dc_pin: GPIO6
    dimensions:
      width: 240
      height: 320
    auto_clear_enabled: false
lvgl:
  pages:
    - id: home
      widgets:
        - label:
            text: A long caller name
            long_mode: SCROLL
            width: 40
    - id: options
lvgl_visibility:
  id: visibility
""")
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    code = next((tmp_path / ".esphome/build").glob("*/src/main.cpp")).read_text()
    assert "policy.add_root(home->obj)" in code
    assert "policy.add_root(options->obj)" in code
    assert "policy.reserve_roots(5)" in code
    assert "visibility->set_initializer([](" in code
    assert "runtime_controller" not in code


@pytest.mark.timeout(190)
def test_public_round_video_page_has_one_router_and_no_layer_overlay(tmp_path):
    from tests.test_package_composition import FULL, device_config

    # The Box has no status-LED ring; its amplifier already owns GPIO38.
    config = device_config(tmp_path, [item for item in FULL if item != "status_led"] + ["display"],
                           "yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml")
    text = config.read_text().replace("packages:\n", "packages:\n  video_page: !include " +
        str(ROOT / "packages/voip/s3_video_call_page.yaml") + "\n", 1)
    config.write_text(text)
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    code = next((tmp_path / ".esphome/build").glob("*/src/main.cpp")).read_text()
    assert "target = call_video_page->obj" in code
    assert "s3_video->attach_video_container(call_video_surface)" in code
    assert "policy.add_root(call_video_page->obj)" in code
    assert "incoming_video_overlay" not in code
    assert "target != call_video_page->obj" in code
    assert "video_top_layer_was_hidden" not in code
    assert "g_mww_video_suspended" in code
    assert "get_video_start_trigger()" in code
    assert "get_video_end_trigger()" in code


@pytest.mark.timeout(190)
@pytest.mark.parametrize("profile", PROFILES)
def test_all_maintained_display_families_bind_visibility(profile, tmp_path):
    from tests.test_package_composition import device_config

    path = "yamls/" + profile
    original = yaml_util.load_yaml(ROOT / path)
    features = original["substitutions"]["firmware_features"]
    config = device_config(tmp_path, features, path)
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", "--no-defaults", str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not resolve substitution" not in result.stderr
    expanded = tmp_path / "expanded.yaml"
    expanded.write_text(result.stdout)
    value = yaml_util.load_yaml(expanded)
    assert "lvgl_visibility" in value
    if "voip-only" in profile:
        assert "runtime_controller" not in value
