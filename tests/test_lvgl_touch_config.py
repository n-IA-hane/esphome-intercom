"""The coordinate service is optional and uses the standard ESPHome API action."""
from pathlib import Path
import os
import json
import shlex
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("enabled", [False, True])
def test_touch_package_codegen_is_optional_and_exposes_coordinates(tmp_path, enabled):
    config = tmp_path / "display.yaml"
    optional = f"packages:\n  touch: !include {ROOT / 'packages/diagnostics/touch.yaml'}\n" if enabled else ""
    config.write_text(f"""
substitutions:
  ext_components_source: {ROOT / 'esphome/components'}
esphome:
  name: touch-contract
esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf
logger:
wifi:
  ssid: fixture-network
  password: fixture-password
api:
{optional}
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
        - button:
            id: actual_button
            width: 100
            height: 50
""")
    result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    code = next((tmp_path / ".esphome/build").glob("*/src/main.cpp")).read_text()
    if enabled:
        assert "missing the synchronous=" not in result.stderr
        assert '"touch"' in code and '"x"' in code and '"y"' in code
        # The standard API maps YAML int variables to signed 32bit arguments.
        assert "lvgl_touch_diagnostic::TapAction<int32_t, int32_t>" in code
        assert '"swipe"' in code and '"duration_ms"' in code
        assert "lvgl_touch_diagnostic::SwipeAction<int32_t, int32_t, int32_t, int32_t, int32_t>" in code
        assert "diagnostic_touch->set_lvgl(" in code
        assert "lvgl_touch_diagnostic::LvglTouchDiagnostic" in code
    else:
        assert "lvgl_touch_diagnostic" not in code
        assert not list((tmp_path / ".esphome/build").glob("*/src/esphome/components/lvgl_touch_diagnostic"))


@pytest.mark.diagnostic_reference
def test_touch_action_instantiates_against_real_esphome_headers(tmp_path):
    # Reuse the already compiled SDK that supplies LVGL_HOST_SOURCE. This is
    # one syntax-only consumer, not an SDK build; all dependency/output flags
    # are removed so the frozen SDK remains read-only.
    lvgl = Path(os.environ["LVGL_HOST_SOURCE"]).resolve()
    database = lvgl.parents[1] / "build/compile_commands.json"
    assert database.is_file(), "Provide LVGL_HOST_SOURCE from a compiled native ESP-IDF SDK"
    entries = json.loads(database.read_text())
    entry = next(item for item in entries if item["file"].endswith("/src/main.cpp"))
    original = entry.get("arguments") or shlex.split(entry["command"])
    command = []
    i = 0
    while i < len(original):
        flag = original[i]
        if flag in {"-o", "-MT", "-MF", "-MQ"}:
            i += 2
            continue
        if flag not in {"-c", "-MD", "-MMD", "-MP", entry["file"]}:
            command.append(flag)
        i += 1
    source = tmp_path / "action.cpp"
    component = ROOT / "esphome/components/lvgl_touch_diagnostic/lvgl_touch_diagnostic.h"
    source.write_text(f'''#include "{component}"
esphome::lvgl_touch_diagnostic::TapAction<int32_t, int32_t> actual_action;
esphome::lvgl_touch_diagnostic::SwipeAction<int32_t, int32_t, int32_t, int32_t, int32_t> actual_swipe;
void consumer(esphome::lvgl_touch_diagnostic::LvglTouchDiagnostic *parent, int32_t x, int32_t y) {{
  actual_action.set_parent(parent);
  actual_action.set_x([](int32_t a, int32_t) -> int32_t {{ return a; }});
  actual_action.set_y([](int32_t, int32_t b) -> int32_t {{ return b; }});
  actual_action.play_complex(x, y);
  actual_swipe.set_parent(parent);
  actual_swipe.set_x1([](int32_t a, int32_t, int32_t, int32_t, int32_t) -> int32_t {{ return a; }});
  actual_swipe.set_y1([](int32_t, int32_t b, int32_t, int32_t, int32_t) -> int32_t {{ return b; }});
  actual_swipe.set_x2([](int32_t, int32_t, int32_t c, int32_t, int32_t) -> int32_t {{ return c; }});
  actual_swipe.set_y2([](int32_t, int32_t, int32_t, int32_t d, int32_t) -> int32_t {{ return d; }});
  actual_swipe.set_duration_ms([](int32_t, int32_t, int32_t, int32_t, int32_t t) -> int32_t {{ return t; }});
  actual_swipe.play_complex(x, y, x, y, 320);
}}
''')
    result = subprocess.run(command + ["-fsyntax-only", str(source)], cwd=entry["directory"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
