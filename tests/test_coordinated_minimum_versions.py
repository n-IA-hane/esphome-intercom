"""Validate each public firmware recipe with the coordinated ESPHome builder."""
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util
import pytest

from tests.native_profile import profile
from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]
MINIMUM = "2026.11.0"
PLATFORMS = ("esp32s3_base.yaml", "esp32p4_base.yaml")
BOX3_DIRECTORY = Path("yamls/experimental/esp32-s3-box-3")
BOX3_ADDITIONS = BOX3_DIRECTORY / "esp32-s3-box-3-additions.yaml"


@pytest.mark.parametrize("name", PLATFORMS)
def test_coordinated_platform_declares_the_qualified_minimum(name):
    document = yaml_util.load_yaml(ROOT / "packages/platform" / name)
    assert str(document["esphome"]["min_version"]) == MINIMUM


def _entrypoints():
    public = subprocess.check_output(
        ["git", "ls-files", "--", "yamls/*.yaml"], cwd=ROOT, text=True
    ).splitlines()
    # Each new public YAML must pass as an entrypoint or have its overlay tested.
    return [name for name in public if Path(name) != BOX3_ADDITIONS]


@pytest.mark.parametrize("path", _entrypoints())
def test_public_firmware_recipe_validates_with_the_coordinated_minimum(path):
    assert str(profile(path)["esphome"]["min_version"]) == MINIMUM


def test_box3_additions_validate_with_the_parent_i2c_buses(tmp_path):
    parent = BOX3_DIRECTORY / "esp32-s3-box-3-full-aec.yaml"
    # The legacy overlay needs the board buses and supplies display/dock entities.
    # Omit the parent's newer integrated display and dock to give those IDs one owner.
    wrapper = device_config(tmp_path, ["voice", "wake_word", "voip"], str(parent))
    config = yaml_util.load_yaml(wrapper)
    bus = ROOT / "packages/board/box3/sensor_bus_aec.yaml"
    config["packages"]["sensor_bus"] = yaml_util.IncludeFile(wrapper, str(bus), yaml_util.load_yaml)
    additions = yaml_util.load_yaml(ROOT / BOX3_ADDITIONS)
    # Mirror the two public package references into the local candidate checkout.
    for name, ref in additions["packages"].items():
        if isinstance(ref, yaml_util.IncludeFile):
            path = (ref.parent_file.parent / ref.file).resolve()
        else:
            path = ROOT / "packages" / str(ref).split("/packages/", 1)[1].rsplit("@", 1)[0]
        additions["packages"][name] = yaml_util.IncludeFile(wrapper, str(path), yaml_util.load_yaml)
    config["packages"]["additions"] = additions
    wrapper.write_text(yaml_util.dump(config))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", "--no-defaults", str(wrapper)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not resolve substitution" not in result.stderr
    expanded = tmp_path / "expanded.yaml"
    expanded.write_text(result.stdout)
    resolved = yaml_util.load_yaml(expanded)
    assert str(resolved["esphome"]["min_version"]) == MINIMUM
    assert any(str(display["id"]) == "s3_box_lcd" for display in resolved["display"])
    assert any(str(sensor.get("id")) == "radar_sensor" for sensor in resolved["binary_sensor"])
