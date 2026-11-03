"""The coordinate service is optional and uses the standard ESPHome API action."""
from pathlib import Path
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
        assert '"touch"' in code and '"x"' in code and '"y"' in code
        # The standard API maps YAML int variables to signed 32bit arguments.
        assert "lvgl_touch_diagnostic::TapAction<int32_t, int32_t>" in code
        assert "diagnostic_touch->set_lvgl(" in code
        assert "lvgl_touch_diagnostic::LvglTouchDiagnostic" in code
    else:
        assert "lvgl_touch_diagnostic" not in code
        assert not list((tmp_path / ".esphome/build").glob("*/src/esphome/components/lvgl_touch_diagnostic"))
