"""The maintained YAML policy reaches ESPHome's real resolver and code generator."""
from pathlib import Path
import re
import subprocess
import sys

import esphome.config_validation as cv
from esphome import yaml_util
import pytest

from tests.native_profile import profile
from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(240)]
PUBLIC = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "yamls").rglob("*.yaml")
                if re.search(r"firmware_features:.*\bvoip\b", path.read_text()))
BINDINGS = sorted(path for path in (ROOT / "packages").rglob("*.yaml")
                  if "initial_contact: ${ voip_initial_contact" in path.read_text())


def test_every_maintained_phone_binding_declares_the_user_timeout():
    assert len(BINDINGS) == 24
    for path in BINDINGS:
        assert "calling_timeout: ${ voip_calling_timeout | default('120s') }" in path.read_text(), path


@pytest.mark.parametrize("relative", PUBLIC)
def test_public_phone_resolves_explicit_120_second_policy(relative):
    source = yaml_util.load_yaml(ROOT / relative)
    assert source["substitutions"]["voip_calling_timeout"] == "120s"
    resolved = profile(relative)
    assert cv.positive_time_period_milliseconds(resolved["voip_stack"]["calling_timeout"]).total_milliseconds == 120000


@pytest.mark.parametrize("relative", [
    "yamls/voip-only/single-bus/generic-s3-voip.yaml",
    "yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml",
])
@pytest.mark.parametrize("value,milliseconds", [("120s", 120000), ("45s", 45000), ("0s", 0)])
def test_user_override_reaches_the_actual_generated_setter(tmp_path, relative, value, milliseconds):
    config = device_config(tmp_path, ["voip"], relative, overrides={"voip_calling_timeout": value})
    document = yaml_util.load_yaml(config)
    document["esphome"] = {"build_path": str(tmp_path / "build")}
    config.write_text(yaml_util.dump(document))
    result = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = (tmp_path / "build/src/main.cpp").read_text()
    assert f"phone->set_calling_timeout({milliseconds});" in generated
    if "waveshare-p4" in relative:
        assert "phone->set_ringing_timeout(30000);" in generated
