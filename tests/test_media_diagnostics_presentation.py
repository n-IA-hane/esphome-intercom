"""Validate the optional diagnostic labels through native package composition."""
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
def test_phone_media_diagnostics_labels_resolve(profile_path, video, tmp_path):
    features = ["voip", "display"] + (["video"] if video else [])
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run([sys.executable, "-m", "esphome", "config", "--no-defaults", str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not resolve substitution" not in result.stderr, result.stderr
    document = yaml.load(result.stdout, Loader=PackageLoader)
    sensor = by_id(document, "phone_media_diagnostics")
    assert sensor["type"] == "media_diagnostics"
    if "voip-only" in profile_path:
        by_id(document, "videophone_media_diagnostics_label")
        if video:
            by_id(document, "videophone_video_media_diagnostics_label")
        else:
            assert "videophone_video_media_diagnostics_label" not in result.stdout
    elif "waveshare-p4" in profile_path:
        # The FULL page switches to a separate video page. Its existing top
        # layer keeps this single small label visible across both presentations.
        layers = [node["top_layer"] for node in nodes(document["lvgl"]) if "top_layer" in node]
        by_id(layers, "call_media_diagnostics_label")
    else:
        by_id(document, "call_media_diagnostics_label")
        if video:
            by_id(document, "call_video_media_diagnostics_label")
    assert "set_media_route" not in result.stdout
