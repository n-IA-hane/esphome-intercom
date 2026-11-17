"""Generic mic wiring selects the same channel for every actual consumer."""

import subprocess
import sys

from esphome import yaml_util
import pytest

from tests.test_generic_s3_camera_composition import configure
from tests.test_package_composition import device_config


PROFILES = [
    ("full-experience/single-bus/generic-s3-full-aec.yaml", "left", False),
    ("full-experience/single-bus/generic-s3-full-lite-aec.yaml", "left", False),
    ("full-experience/dual-bus/generic-s3-full-aec.yaml", "right", False),
    ("full-experience/esphome-native/generic-s3-full-esphome-native.yaml", "right", True),
    ("voip-only/single-bus/generic-s3-voip.yaml", "left", False),
    ("voip-only/dual-bus/generic-s3-voip.yaml", "right", False),
    ("voip-only/esphome-native/generic-s3-voip-esphome-native-full-duplex.yaml", "right", True),
    ("voip-only/esphome-native/generic-s3-voip-esphome-native-mic-only.yaml", "right", True),
]


@pytest.mark.parametrize("profile,default,native", PROFILES)
@pytest.mark.parametrize("override", [False, True])
def test_generic_capture_channel_preserves_defaults_and_follows_wiring(tmp_path, profile, default, native, override):
    selected = ("right" if default == "left" else "left") if override else default
    features = ["voice", "wake_word", "voip"] if profile.startswith("full-experience/esphome-native") else ["voip"]
    _, config, _ = configure(tmp_path, features,
                             {"audio_mic_channel": selected} if override else None,
                             profile="yamls/" + profile)
    if native:
        index = 0 if selected == "left" else 1
        assert config["voip_stack"]["microphone_source"]["channels"] == [index]
        if "voice" in features:
            sources = config["voice_assistant"]["microphone"]
            source = sources[0] if isinstance(sources, list) else sources
            assert source["channels"] == [index]
            assert config["micro_wake_word"]["microphone"]["channels"] == [index]
        # Keep raw stereo geometry and mono playback unchanged.
        assert config["microphone"][0]["channel"] == "stereo"
    else:
        assert config["esp_audio_stack"]["mic_channel"] == selected


@pytest.mark.parametrize("profile", [PROFILES[0][0], PROFILES[3][0]])
def test_invalid_channel_is_rejected_by_the_real_component_schema(tmp_path, profile):
    path = device_config(tmp_path, ["voip"], "yamls/" + profile)
    config = yaml_util.load_yaml(path)
    config["substitutions"]["audio_mic_channel"] = "centre"
    path.write_text(yaml_util.dump(config))
    result = subprocess.run([sys.executable, "-m", "esphome", "config", str(path)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode != 0
    assert "Could not resolve substitution" not in result.stderr
