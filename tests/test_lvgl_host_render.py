"""Regression gates for source-faithful LVGL host rendering and owner sync."""
from pathlib import Path
import json
import os
import subprocess
import sys

from esphome import yaml_util
from esphome.components.lvgl.lv_validation import size_validator
from esphome.core import Lambda
from PIL import Image
import pytest

from tests.test_lvgl_settings_composition import expanded_scene
from tools.render_lvgl import find_id, yaml_input


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(600)]


def test_reloading_validated_sizes_does_not_turn_percent_into_pixels():
    source = {"width": 1.0, "height": "LV_SIZE_CONTENT", "x": 0, "bg_opa": 0.0}
    public = yaml_input(source)
    assert public == {"width": "100%", "height": "SIZE_CONTENT", "x": 0, "bg_opa": 0.0}
    assert size_validator(public["width"]) == source["width"]
    assert size_validator(public["height"]) == source["height"]
    assert yaml_input({"width": 1})["width"] == 1


def test_owner_lookup_uses_declaration_instead_of_an_earlier_script_reference():
    reference = {"id": "caller", "then": [{"script.execute": {"id": "draw_display"}}]}
    declaration = {"id": "draw_display", "then": [{"lambda": Lambda("const bool show_status = true;")}]}
    assert find_id({"script": [reference, declaration]}, "script", "draw_display") is declaration
    assert find_id({"script": [reference]}, "script", "draw_display") is None


@pytest.mark.parametrize("scene,size", [("spot240", 240), ("box360-aec", 360)])
def test_production_page_runs_real_lvgl_owner_sync_and_snapshot(tmp_path, scene, size):
    document = expanded_scene(scene, "full")
    source = tmp_path / "expanded.yaml"
    source.write_text(yaml_util.dump(document))
    (tmp_path / "secrets.yaml").write_text("wifi_ssid: fixture-network\nwifi_password: fixture-password\n")
    output = tmp_path / "render"
    command = [sys.executable, str(ROOT / "tools/render_lvgl.py"), "--input", str(source),
               "--page", "settings_continuous_page", "--width", str(size), "--height", str(size),
               "--output", str(output), "--build-parent",
               str(Path(os.environ["LVGL_HOST_BUILD_ROOT"]) / str(size))
               if "LVGL_HOST_BUILD_ROOT" in os.environ else str(tmp_path / "host-cache"), "--round", "--pointer"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=480)
    diagnostics = "\n".join(path.read_text(errors="replace")[-5000:]
                            for path in (output / "compile.log", output / "run.log") if path.exists())
    assert result.returncode == 0, result.stdout + result.stderr + diagnostics
    report = json.loads((output / "RESULTS.json").read_text())
    assert len(report["snapshots"]) == 3
    log = (output / "run.log").read_text()
    assert "UI_PROBE owner_sync=pass user_write=pass drag=pass reject=pass" in log
    assert "UI_PROBE overlay_restore=pass" in log
    assert "UI_POINTER source_dialer_gestures=pass suppressed_commands=pass positive_digit_tap=pass" in log
    assert "UI_BOOT registry=1 first_valid=1" in log
    assert "UI_POINTER ordered_navigation=pass bounded_ends=pass real_registry=pass" in log
    assert "UI_POINTER options_body_scroll=pass navigation_suppressed=pass" in log
    assert "UI_POINTER in_call_gesture=pass hangup_suppressed=pass positive_hangup_tap=pass" in log
    for name in ("master", "playback", *(["mic"] if scene == "spot240" else [])):
        assert f"UI_POINTER slider={name} knob=pass extension=pass track_rejected=pass" in log
    assert "UI_POINTER passive_row_child_switch=pass writes=1" in log
    if scene == "spot240":
        assert "UI_POINTER microphone_owner=microphone_pga minimum=0 maximum=36" in log
        for value in (24, 36):
            assert f"UI_POINTER mic_value={value} knob=pass extended_topmost=pass no_switch_overlap=pass" in log
    else:
        assert "UI_POINTER microphone_control=absent" in log
        assert "UI_POINTER slider=mic " not in log
    assert report["production_overlay_policy_sha256"]
    assert report["production_page_selector_sha256"]
    assert report["production_settings_registry_boot_sha256"]
    for position in ("top", "mid", "end"):
        with Image.open(output / (position + ".png")) as image:
            assert image.size == (size, size)
            assert len(image.getcolors(size * size) or []) > 8
