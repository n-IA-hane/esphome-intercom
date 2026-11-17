"""Validate the generic headless camera through ESPHome's real package/schema pipeline."""

import subprocess
import sys

from esphome import yaml_util
import pytest

from tests.test_package_composition import device_config


PROFILE = "yamls/full-experience/single-bus/generic-s3-full-aec-camera.yaml"
PHONE_PROFILE = "yamls/voip-only/single-bus/generic-s3-voip-camera.yaml"
FULL = ["voice", "wake_word", "voip", "music", "sendspin", "timers", "status_led", "diagnostics", "video"]
PHONE = ["voip", "status_led", "diagnostics", "video"]


def configure(directory, features, substitutions=None, profile=PROFILE):
    path = device_config(directory, features, profile)
    value = yaml_util.load_yaml(path)
    value.setdefault("esphome", {})["build_path"] = str(directory / "generated")
    if substitutions:
        value["substitutions"].update(substitutions)
    path.write_text(yaml_util.dump(value))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", str(path)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not resolve substitution" not in result.stderr
    expanded = directory / "expanded.yaml"
    expanded.write_text(result.stdout)
    return path, yaml_util.load_yaml(expanded), result.stderr


@pytest.mark.parametrize("features,enabled", [
    (FULL, True),
    (["voip", "video"], True),
    (["voip"], False),
    (["video"], False),
])
@pytest.mark.timeout(210)
def test_camera_and_video_controls_follow_real_capabilities(tmp_path, features, enabled):
    _, config, diagnostics = configure(tmp_path, features)
    assert ("esp32_camera" in config) is enabled
    assert ("i2c" in config) is enabled
    assert ("video" in config.get("voip_stack", {})) is enabled
    controls = [item for item in config.get("switch", []) if item.get("platform") == "voip_stack"]
    assert any("video_send" in item for item in controls) is enabled
    assert "lvgl" not in config
    assert "display" not in config
    assert "rx_bus" not in config["esp_audio_stack"]
    assert "tx_bus" not in config["esp_audio_stack"]
    assert config["esp_audio_stack"]["task_core"] == 0
    assert config["esp_audio_stack"]["mic_channel"] == "right"
    assert config["esp_audio_stack"]["aec_reference"] == "previous_frame"
    assert config["esp_audio_stack"]["sample_rate"] == 48000
    assert config["esp_audio_stack"]["output_sample_rate"] == 16000
    assert [config["esp_audio_stack"][key] for key in
            ("i2s_bclk_pin", "i2s_lrclk_pin", "i2s_din_pin", "i2s_dout_pin")] == [14, 21, 47, 1]
    assert config["esphome"]["min_version"] == "2026.12.0"
    assert config["esp32"]["flash_size"] == "8MB"
    assert config["esp32"]["flash_mode"] == "dio"
    assert config["psram"]["mode"] == "octal"
    assert all("resampler" not in item.get("components", [])
               for item in config.get("external_components", []))
    if enabled:
        assert "'video' is disabled" not in diagnostics
        assert config["esp32_camera"]["data_pins"] == [11, 9, 8, 10, 12, 18, 17, 16]
    elif "video" in features:
        assert "'video' is disabled: requires voip" in diagnostics


@pytest.mark.parametrize("profile", [PROFILE, PHONE_PROFILE])
@pytest.mark.timeout(210)
def test_board_memory_camera_and_amplifier_are_configurable(tmp_path, profile):
    _, config, _ = configure(tmp_path, ["voip", "video"], {
        "board_type": "esp32-s3-devkitc-1",
        "board_flash_size": "16MB",
        "board_flash_mode": "qio",
        "board_psram_mode": "quad",
        "board_psram_speed": "40MHz",
        "audio_task_core": 1,
        "audio_mic_channel": "left",
        "speaker_enable_pin": "",
        "camera_width": 320,
        "camera_height": 240,
        "camera_framerate": 5,
        "camera_vertical_flip": False,
        "camera_horizontal_mirror": False,
    }, profile=profile)
    assert config["esp32"]["board"] == "esp32-s3-devkitc-1"
    assert config["esp32"]["flash_size"] == "16MB"
    assert config["esp32"]["flash_mode"] == "qio"
    assert not config["esphome"].get("platformio_options")
    assert config["psram"]["mode"] == "quad"
    assert str(config["psram"]["speed"]).upper() == "40MHZ"
    assert config["esp_audio_stack"]["task_core"] == 1
    assert config["esp_audio_stack"]["mic_channel"] == "left"
    assert "output" not in config
    assert not any(str(item.get("id")) == "audio_stack_speaker_idle_powerdown"
                   for item in config.get("script", []))
    assert config["esp32_camera"]["resolution"] == "320X240"
    assert config["esp32_camera"]["vertical_flip"] is False
    assert config["esp32_camera"]["horizontal_mirror"] is False
    assert config["voip_stack"]["video"]["width"] == 320
    assert config["voip_stack"]["video"]["height"] == 240
    assert config["voip_stack"]["video"]["framerate"] == 5


@pytest.mark.parametrize("profile", [PROFILE, PHONE_PROFILE])
@pytest.mark.timeout(210)
def test_headless_camera_generates_native_camera_adapter(tmp_path, profile):
    path, _, _ = configure(tmp_path, PHONE if profile == PHONE_PROFILE else ["voip", "video"], profile=profile)
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(path)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    generated = next(tmp_path.rglob("main.cpp")).read_text()
    assert "esp32_camera::ESP32Camera" in generated
    assert "set_video_camera(esp32_cam," in generated
    assert "set_audio_task_stack_in_psram(true)" in generated
    assert "ignored when building with the native" not in result.stdout + result.stderr
    assert "voip_stack::VoipStackVideoSendSwitch" in generated
    assert "p4_video_renderer" not in generated
    assert "esp_video_camera::" not in generated
    if profile == PHONE_PROFILE:
        assert "runtime_controller::" not in generated
        assert "voice_assistant::" not in generated
        assert "micro_wake_word::" not in generated
        assert "sendspin::" not in generated
        assert "media_player::" not in generated
        headers = next(tmp_path.rglob("defines.h")).read_text()
        for excluded in ("RUNTIME_CONTROLLER", "VOICE_ASSISTANT", "MICRO_WAKE_WORD", "SENDSPIN", "MEDIA_PLAYER"):
            assert f"#define USE_{excluded}" not in headers


@pytest.mark.parametrize("transport", ["udp", "tcp"])
@pytest.mark.timeout(210)
def test_phone_camera_keeps_standalone_native_callbacks_and_opus(tmp_path, transport):
    _, config, _ = configure(tmp_path, PHONE, {"voip_transport": transport}, profile=PHONE_PROFILE)
    for excluded in ("runtime_controller", "voice_assistant", "micro_wake_word", "sendspin", "media_player", "lvgl", "display"):
        assert excluded not in config
    external = {name for item in config.get("external_components", []) for name in item.get("components", [])}
    assert not external.intersection({"runtime_controller", "voice_assistant", "micro_wake_word", "sendspin", "media_player"})
    assert all(item["platform"] in {"esp_audio_stack", "resampler"} for item in config["speaker"])
    phone = config["voip_stack"]
    assert phone["transport"] == transport
    assert phone["audio"]["tx"]["codec"] == phone["audio"]["rx"]["codec"] == "opus"
    assert phone["audio"]["tx"]["sample_rate"] == 16000
    assert phone["audio"]["rx"]["sample_rate"] == 48000
    assert phone["on_calling"] and phone["on_ringing"] and phone["on_in_call"] and phone["on_hangup"]
    assert phone["video"]["codec"] == "jpeg"
    assert [phone["video"][key] for key in ("width", "height", "framerate")] == [640, 480, 10]
    assert config["esp_audio_stack"]["bits_per_sample"] == 32
    assert config["esp_audio_stack"]["slot_bit_width"] == 32
    assert config["esp_audio_stack"]["mic_channel"] == "right"
    assert config["esp_audio_stack"]["rx_slot_mode"] == "stereo"
    assert config["esp_audio_stack"]["aec_reference"] == "previous_frame"
    assert config["esp32"]["board"] == "freenove_esp32_s3_wroom"
    assert config["psram"]["mode"] == "octal"


@pytest.mark.timeout(210)
def test_phone_camera_can_remove_video_without_adding_full_runtime(tmp_path):
    _, config, _ = configure(tmp_path, ["voip"], profile=PHONE_PROFILE)
    assert "esp32_camera" not in config
    assert "i2c" not in config
    assert "video" not in config["voip_stack"]
    assert "runtime_controller" not in config
    assert "media_player" not in config
    assert not any("video_send" in item for item in config.get("switch", []) if item.get("platform") == "voip_stack")
