"""The coordinated recipes require their actually qualified ESPHome baseline."""
from pathlib import Path

from esphome import yaml_util
from esphome.components.packages import resolve_packages
from esphome.core import CORE
import pytest

from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]
MINIMUM = "2026.12.0"
PLATFORMS = ("esp32s3_base.yaml", "esp32p4_base.yaml")
P4_ENTRYPOINTS = (
    "yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml",
    "yamls/voip-only/single-bus/waveshare-p4-touch-videophone-h264.yaml",
)


@pytest.mark.parametrize("name", PLATFORMS)
def test_coordinated_platform_declares_the_qualified_minimum(name):
    document = yaml_util.load_yaml(ROOT / "packages/platform" / name)
    assert str(document.get("esphome", {}).get("min_version")) == MINIMUM


@pytest.mark.parametrize("path", P4_ENTRYPOINTS)
def test_explicit_entrypoint_minimum_does_not_lower_the_platform(path):
    assert str(yaml_util.load_yaml(ROOT / path)["esphome"]["min_version"]) == MINIMUM


def _entrypoints():
    return [path for path in sorted((ROOT / "yamls").rglob("*.yaml"))
            if "firmware_features" in yaml_util.load_yaml(path).get("substitutions", {})
            and "device" in yaml_util.load_yaml(path).get("packages", {})]


@pytest.mark.parametrize("path", _entrypoints(), ids=lambda path: str(path.relative_to(ROOT)))
def test_native_package_composition_keeps_the_effective_minimum(tmp_path, path, monkeypatch):
    original = yaml_util.load_yaml(path)
    wrapper = device_config(tmp_path, original["substitutions"]["firmware_features"], str(path.relative_to(ROOT)))
    monkeypatch.setattr(CORE, "config_path", wrapper)
    document = yaml_util.load_yaml(wrapper)
    document.update({key: value for key, value in original.items()
                     if key not in ("substitutions", "packages")})
    resolved = resolve_packages(document)
    assert str(resolved["esphome"]["min_version"]) == MINIMUM
