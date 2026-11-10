"""Public round profiles use the same bounded native text owner."""

import subprocess
import sys

from esphome import yaml_util
import pytest

from tests.test_package_composition import ROOT, device_config

PROFILES = [
    "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml",
    "yamls/experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml",
]
CASES = [pytest.param(path, True, True, id=f"voice-{index}") for index, path in enumerate(PROFILES)]
CASES += [pytest.param(path, False, False, id=f"no-voice-with-media-{index}") for index, path in enumerate(PROFILES)]
CASES += [pytest.param("yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
                       True, False, id="nonround-voice")]


@pytest.mark.timeout(190)
@pytest.mark.parametrize("profile,voice,expected", CASES)
def test_public_round_profiles_generate_one_native_text_owner(tmp_path, profile, voice, expected):
    original = yaml_util.load_yaml(ROOT / profile)
    features = original["substitutions"]["firmware_features"]
    if not voice:
        features = [name for name in features if name != "voice"]
        assert "music" in features and "sendspin" in features
    config = device_config(tmp_path, features, profile)
    result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    code = next((tmp_path / ".esphome/build").glob("*/src/main.cpp")).read_text()
    defines = next((tmp_path / ".esphome/build").glob("*/src/esphome/core/defines.h")).read_text()
    headers = next((tmp_path / ".esphome/build").glob("*/src/esphome.h")).read_text()
    assert "USE_LVGL_ROUND_TEXT_DEBUG" not in defines
    if not expected:
        assert "lvgl_round_text::" not in code and "round_text->" not in code
        assert "lvgl_round_text/" not in headers
        generated = next((tmp_path / ".esphome/build").glob("*/src/esphome/components"))
        assert not (generated / "lvgl_round_text").exists()
        return
    assert code.count("new(round_text) lvgl_round_text::LvglRoundText()") == 1
    assert "round_text->bind_thinking(lines)" in code
    assert "round_text->bind_reply(lines, reply_page_counter)" in code
    assert "round_text->set_thinking(x, chord_widths_cache->value().data()" in code
    assert "round_text->set_reply(x, chord_widths_cache->value().data()" in code
    assert "round_text->show_reply" in code and "round_text->clear()" in code
    assert "wrapped_lines_reply" not in code and "visible_lines" not in code
    assert "->set_buffers_in_psram(true)" in code
    # The established script owns the same pagination interval.
    assert "14000" in code


def test_round_text_schema_exposes_only_bounded_owner_options():
    import importlib.util
    path = ROOT / "esphome/components/lvgl_round_text/__init__.py"
    spec = importlib.util.spec_from_file_location("round_text_external", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.CONFIG_SCHEMA({"id": "text_owner"})
    assert config["buffers_in_psram"] is True and config["debug"] is False
    assert module.CONFIG_SCHEMA({"id": "text_owner", "buffers_in_psram": False, "debug": True})["debug"]
