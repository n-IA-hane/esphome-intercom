"""Native phone packages leave diagnostic placement to the user."""
import subprocess
import sys

import pytest
import yaml

from tests.native_profile import PackageLoader, by_id, nodes
from tests.test_package_composition import device_config


@pytest.mark.timeout(190)
@pytest.mark.parametrize("profile_path,video", [
    ("yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml", True),
    ("yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml", True),
    ("yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml", False),
    ("yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml", False),
    ("yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml", True),
])
def test_phone_packages_do_not_install_diagnostic_overlays(profile_path, video, tmp_path):
    features = ["voip", "display"] + (["video"] if video else [])
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run([sys.executable, "-m", "esphome", "config", "--no-defaults", str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not resolve substitution" not in result.stderr, result.stderr
    document = yaml.load(result.stdout, Loader=PackageLoader)
    ids = {str(node.get("id", "")) for node in nodes(document)}
    assert not any("media_diagnostics" in identity for identity in ids)
    assert "set_pbx_media_diagnostics" not in result.stdout
    assert "set_media_route" not in result.stdout
    by_id(document, "phone")


@pytest.mark.timeout(190)
def test_headless_phone_preserves_explicit_initial_sip_target(tmp_path):
    target = "sip:desk-2@192.0.2.10:5060"
    config = device_config(tmp_path, ["voip"], "yamls/voip-only/single-bus/generic-s3-voip.yaml",
                           overrides={"voip_initial_contact": target})
    result = subprocess.run([sys.executable, "-m", "esphome", "config", "--no-defaults", str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    document = yaml.load(result.stdout, Loader=PackageLoader)
    assert document["voip_stack"]["initial_contact"] == target
    assert "use_ha_as_first_contact" not in result.stdout
    assert "lvgl" not in document
