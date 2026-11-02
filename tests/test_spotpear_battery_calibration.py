"""Full-charge anchoring changes the display curve, not the ADC measurement."""

import math
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util
from esphome.components.sensor import map_linear, validate_datapoint
import pytest

from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]
PROFILES = [
    "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "yamls/voip-only/single-bus/spotpear-ball-v2-voip.yaml",
]


@pytest.mark.parametrize("profile", PROFILES, ids=["full", "voip-only"])
@pytest.mark.parametrize("anchor", [None, 4.114], ids=["default", "unit-calibrated"])
def test_battery_anchor_composes_with_a_monotonic_curve_and_unchanged_adc(tmp_path, profile, anchor):
    original = yaml_util.load_yaml(ROOT / profile)
    config = device_config(tmp_path, original["substitutions"]["firmware_features"], profile)
    if anchor is not None:
        config.write_text(config.read_text().replace(
            "substitutions:\n", f"substitutions:\n  spotpear_battery_full_voltage: '{anchor}'\n", 1))
    result = subprocess.run([sys.executable, "-m", "esphome", "config", "--no-defaults", str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / "expanded.yaml"
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    voltage = next(sensor for sensor in document["sensor"] if str(sensor["id"]) == "battery_voltage")
    assert str(voltage["pin"]) == "GPIO1"
    assert voltage["attenuation"] == "12db"
    assert voltage["filters"][0]["multiply"] == 2.0
    median = voltage["filters"][1]["median"]
    assert (median["window_size"], median["send_every"], median["send_first_at"]) == (7, 7, 1)
    percentage = next(sensor for sensor in document["sensor"] if str(sensor["id"]) == "battery_percentage")
    calibration = percentage["filters"][0]["calibrate_linear"]
    assert calibration["method"] == "exact"
    points = [validate_datapoint(point) for point in calibration["datapoints"]]
    x = [point["from"] for point in points]
    y = [point["to"] for point in points]
    assert x[-1] == pytest.approx(4.20 if anchor is None else anchor)
    assert y[-1] == 100.0 and x[-2] == 4.0 and y[-2] == 90.0
    assert all(left < right for left, right in zip(x, x[1:]))
    assert all(left < right for left, right in zip(y, y[1:]))
    # Use the official ESPHome piecewise mapping, including its last segment.
    k, b, _ = next(segment for segment in map_linear(x, y)
                   if math.isnan(segment[2]) or 4.114 < segment[2])
    assert k * 4.114 + b == pytest.approx(95.7 if anchor is None else 100.0)
