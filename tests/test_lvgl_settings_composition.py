"""Exercise settings composition through the real ESPHome package resolver."""
from functools import lru_cache
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile

from esphome import yaml_util
import pytest

from tests.native_profile import action_id, nodes, text
from tests.test_package_composition import device_config


ROOT = Path(__file__).resolve().parents[1]
BOX = "yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml"
SCENES = {
    "spot240": ("yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml", {}),
    "box360-aec": (BOX, {"audio_processor": "aec", "mic_mix": "average"}),
    "box360-afe-average": (BOX, {"audio_processor": "afe", "mic_mix": "average"}),
    "box360-afe-independent": (BOX, {"audio_processor": "afe", "mic_mix": "passthrough"}),
    "p4-landscape": ("yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml", {}),
    "p4-portrait": ("yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml", {}),
}
VARIANTS = ("full", "no-voip", "no-wake", "display-only", "voice-display")
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(240)]


def scene_features(scene, variant):
    original = yaml_util.load_yaml(ROOT / SCENES[scene][0])
    full = list(original["substitutions"]["firmware_features"])
    if "display" not in full:
        full.append("display")
    if variant == "display-only":
        return ["display"]
    if variant == "voice-display":
        return ["voice", "display"]
    if variant == "voip-display":
        return ["voip", "display"]
    excluded = {"voip", "video"} if variant == "no-voip" else {"wake_word"} if variant == "no-wake" else set()
    return [value for value in full if value not in excluded]


@lru_cache(maxsize=None)
def expanded_scene(scene, variant):
    with tempfile.TemporaryDirectory(prefix="lvgl-settings-contract-") as directory:
        config = device_config(Path(directory), scene_features(scene, variant), SCENES[scene][0])
        value = yaml_util.load_yaml(config)
        value["substitutions"].update(SCENES[scene][1])
        config.write_text(yaml_util.dump(value))
        result = subprocess.run(
            [sys.executable, "-m", "esphome", "config", str(config)],
            capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Could not resolve substitution" not in result.stderr
        expanded = Path(directory) / "expanded.yaml"
        expanded.write_text(result.stdout)
        document = yaml_util.load_yaml(expanded)
        if isinstance(document["lvgl"], list):
            assert len(document["lvgl"]) == 1
            document["lvgl"] = document["lvgl"][0]
        return document


def widget_nodes(value):
    for node in nodes(value):
        for kind in ("obj", "button", "buttonmatrix", "label", "slider", "switch", "dropdown"):
            if isinstance(node.get(kind), dict):
                yield kind, node[kind]


def page_and_controls(config):
    pages = [page for page in config["lvgl"]["pages"] if str(page.get("id", "")).startswith("settings_")]
    assert len(pages) == 1
    page = pages[0]
    return page, {str(node["id"]): node for _, node in widget_nodes(page) if "id" in node}


@pytest.mark.parametrize("scene", ["spot240", "box360-aec"])
@pytest.mark.parametrize("variant", ["full", "no-voip"])
def test_round_bootstrap_preserves_settings_registry_and_owner_callbacks(scene, variant):
    config = expanded_scene(scene, variant)
    boot = config["esphome"]["on_boot"]
    startup = [entry for entry in boot if entry["priority"] == 600]
    assert len(startup) == 1, "Round startup was discarded while merging on_boot packages"
    actions = text(startup[0]["then"])
    assert "id(g_settings_pages).clear();" in actions
    assert "id(g_settings_pages).push_back(id(settings_continuous_page)->obj);" in actions
    assert "backlight_timer" in actions
    assert "init_in_progress" in actions
    lifecycle = [entry for entry in boot if entry["priority"] == 700]
    assert len(lifecycle) == 1
    assert [action_id(action, "runtime_controller.event") for action in lifecycle[0]["then"]
            if "runtime_controller.event" in action] == ["runtime", "runtime"]
    assert "boot_start" in text(lifecycle[0]) and "boot_ready" in text(lifecycle[0])
    assert any("delay" in action for action in lifecycle[0]["then"])
    assert any(entry["priority"] == -100 for entry in boot)
    registrations = [entry for entry in boot if entry["priority"] == -200]
    assert len(registrations) == 1
    for marker in ("master_volume", "mic_gain", "LV_EVENT_SCREEN_LOADED", "settings_sync_widgets"):
        assert marker in text(registrations[0])


@pytest.mark.parametrize("scene", SCENES)
@pytest.mark.parametrize("variant", VARIANTS)
def test_settings_match_compiled_capabilities(scene, variant):
    config = expanded_scene(scene, variant)
    page, controls = page_and_controls(config)
    has_voip = "voip_stack" in config
    has_player = bool(config.get("media_player"))
    has_wake = "micro_wake_word" in config
    afe = config.get("esp_afe")
    mono_afe = afe is not None and int(afe.get("mic_num", 1)) == 1
    ids = {str(node["id"]) for node in nodes(config) if "id" in node}
    owners = {
        "ui_auto_answer_sw": (has_voip, "auto_answer_switch"),
        "ui_dnd_sw": (has_voip, "dnd_switch"),
        "ui_conference_ring_sw": (has_voip, "phone"),
        "ui_playback_vol_slider": (has_player, "speaker_media_player"),
        "ui_wake_word_sw": (has_wake, "wake_word_switch"),
        "ui_ns_sw": (mono_afe, "ns_switch"),
        "ui_agc_sw": (mono_afe, "agc_switch"),
        "ui_vad_sw": (afe is not None, "vad_switch"),
        "ui_video_send_sw": ("voip_video_send" in ids, "voip_video_send"),
    }
    for widget, (enabled, owner) in owners.items():
        assert (widget in controls) is enabled, (scene, variant, widget)
        if enabled:
            assert owner in ids
            assert owner in text(controls[widget])
    has_sendspin = "sendspin" in config
    for widget in ("ui_sendspin_pairing_label", "ui_sendspin_confirm_btn", "ui_sendspin_cancel_btn"):
        assert (widget in controls) is has_sendspin
    labels = [node["text"] for kind, node in widget_nodes(page) if kind == "label" and isinstance(node.get("text"), str)]
    assert ("VoIP" in labels) is has_voip
    if has_voip:
        assert labels.index("VoIP") > labels.index("Processing")
        assert not any(label in ("Audio", "Processing", "Voice", "Playback", "Sendspin")
                       for label in labels[labels.index("VoIP") + 1:])
    wrapper = controls["settings_page_wrapper"]
    header = controls["settings_page_header"]
    assert page["scrollable"] is False
    assert wrapper["scrollable"] is True and str(wrapper["scroll_dir"]).removeprefix("LV_DIR_") == "VER"
    assert "settings_back_button" not in text(page)
    assert str(header["layout"]["flex_align_main"]).removeprefix("LV_FLEX_ALIGN_") == "CENTER"
    assert len(header["widgets"]) == 1
    assert header["widgets"][0]["label"]["text"] == "Options"
    assert "on_scroll" not in page
    assert wrapper["gesture_bubble"] is True
    if not scene.startswith("p4"):
        assert wrapper["scroll_elastic"] is False
    assert "settings_close" not in text(wrapper)

    settings_scripts = [script for script in config.get("script", [])
                        if str(script.get("id", "")).startswith("settings_sync")]
    for script in settings_scripts:
        assert not any(action in text(script) for action in
                       ("number.set", "switch.turn_on", "switch.turn_off", "media_player.volume_set", "select.set_index"))
    assert "settings_sync" not in text(config.get("interval", []))
    for kind, control in widget_nodes(page):
        if kind in ("switch", "dropdown"):
            assert "on_change" in control and "on_value" not in control
        if kind == "slider":
            assert "on_release" in control and "on_value" not in control

    # Removed owners must not survive inside a lambda or a settings callback.
    relevant = text(page) + text(settings_scripts)
    boot = config.get("esphome", {}).get("on_boot", [])
    relevant += "\n".join(str(node.get("lambda", "")) for node in nodes(boot)
                           if "settings_sync" in str(node.get("lambda", "")))
    for widget, (enabled, owner) in owners.items():
        if not enabled:
            assert not re.search(r"\bid\(" + re.escape(owner) + r"\)", relevant), (scene, variant, owner)
    for name in ("speaker", "playback", "mic"):
        sync = [script for script in settings_scripts if script.get("id") == "settings_sync_" + name]
        if sync:
            assert "LV_STATE_PRESSED" in text(sync)
    if scene.startswith("p4") and variant in ("full", "voice-display"):
        speakers = {str(node["id"]): node for node in nodes(config["speaker"]) if "id" in node}
        if variant == "full":
            for source, sink in (("media_speaker_input", "media_mixer_input"),
                                 ("tts_speaker_input", "tts_mixer_input"),
                                 ("voip_speaker_input", "voip_mixer_input")):
                assert str(speakers[source]["output_speaker"]) == sink
        else:
            assert "audio_mixer" not in speakers
            assert str(speakers["tts_speaker_input"]["output_speaker"]) == "hw_speaker"


@pytest.mark.parametrize("scene", ["spot240", "box360-afe-independent", "p4-landscape"])
def test_options_header_has_one_centered_title_and_swipe_exit(scene):
    config = expanded_scene(scene, "full")
    page, controls = page_and_controls(config)
    header = controls["settings_page_header"]
    assert len(header["widgets"]) == 1
    assert header["widgets"][0]["label"]["text"] == "Options"
    assert str(header["layout"]["flex_align_main"]).removeprefix("LV_FLEX_ALIGN_") == "CENTER"
    assert str(header["layout"]["flex_align_cross"]).removeprefix("LV_FLEX_ALIGN_") == "CENTER"
    assert not any(kind == "button" for kind, _ in widget_nodes(header))
    exit_actions = list(nodes(page["on_swipe_down"]))
    if scene.startswith("p4"):
        assert any(action_id(action, "lvgl.page.show") == "main_page"
                   for action in exit_actions if "lvgl.page.show" in action)
    else:
        assert any(action_id(action, "script.execute") == "round_navigate"
                   and action["script.execute"]["delta"] == -1
                   for action in exit_actions if "script.execute" in action)


@pytest.mark.parametrize("scene", ["spot240", "box360-aec", "box360-afe-independent"])
@pytest.mark.parametrize("variant", ["full", "no-voip"])
def test_round_options_navigation_and_child_gestures(scene, variant):
    config = expanded_scene(scene, variant)
    pages = config["lvgl"]["pages"]
    normal = [page for page in pages if page.get("id") in
              ("idle_page", "main_page", "call_idle_page", "call_dialer_page", "settings_continuous_page", "call_in_call_page")]
    assert normal
    for page in normal:
        # The user's ordered, bounded navigation replaces the former wrap and
        # separate DOWN-to-Options shortcut. Both directions use one owner.
        for trigger, delta in (("on_swipe_up", 1), ("on_swipe_down", -1)):
            assert any(action_id(action, "script.execute") == "round_navigate"
                       and action["script.execute"]["delta"] == delta
                       for action in nodes(page.get(trigger, [])) if "script.execute" in action), page["id"]
    if "voip_stack" in config:
        dialer = next(page for page in normal if page["id"] == "call_dialer_page")
        assert dialer["scrollable"] is False
        for kind, widget in widget_nodes(dialer):
            if kind in ("button", "buttonmatrix", "label"):
                assert widget.get("gesture_bubble") is True, (kind, widget.get("id"))
                assert widget.get("scrollable") is False, (kind, widget.get("id"))
    else:
        assert not any(str(page.get("id", "")).startswith("call_") for page in pages)


@pytest.mark.parametrize("scene", ["spot240", "box360-aec"])
@pytest.mark.parametrize("variant", ["full", "no-voip", "voip-display", "display-only"])
def test_round_initial_navigation_uses_only_compiled_views(scene, variant):
    config = expanded_scene(scene, variant)
    boot = next(entry for entry in config["esphome"]["on_boot"] if entry["priority"] == 600)
    registry = text(boot["then"])
    has_voice = "voice_assistant" in config
    has_phone = "voip_stack" in config
    assert f"id(current_mode) = {int(not has_voice and has_phone)};" in registry
    expected_settings = "true" if not has_voice and not has_phone else "false"
    assert f"id(settings_active) = {expected_settings};" in registry


@pytest.mark.parametrize("scene", ["spot240", "box360-aec"])
def test_round_bootstrap_is_present_in_generated_cpp(tmp_path, scene):
    config = device_config(tmp_path, scene_features(scene, "full"), SCENES[scene][0])
    document = yaml_util.load_yaml(config)
    document["substitutions"].update(SCENES[scene][1])
    document["esphome"] = {"build_path": str(tmp_path / "build")}
    unique = tmp_path / ("navigation-" + scene + ".yaml")
    unique.write_text(yaml_util.dump(document))
    environment = {**os.environ, "ESPHOME_DATA_DIR": str(tmp_path / "data")}
    result = subprocess.run([sys.executable, "-m", "esphome", "compile", str(unique), "--only-generate"],
                            env=environment, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = (tmp_path / "build/src/main.cpp").read_text()
    for priority in (700, 600, -100, -200):
        assert re.search(r"StartupTrigger\(" + str(priority) + r"(?:\.0f)?\)", generated)
    assert "g_settings_pages->value().push_back(settings_continuous_page->obj);" in generated
    for event in ("boot_start", "boot_ready"):
        assert f'set_event(ESPHOME_F("{event}"))' in generated
    assert "LV_EVENT_SCREEN_LOADED" in generated


def test_round_boot_events_execute_against_the_real_runtime_profile(tmp_path):
    config = expanded_scene("spot240", "full")
    boot = next(entry for entry in config["esphome"]["on_boot"] if entry["priority"] == 700)
    events = [action["runtime_controller.event"]["event"] for action in boot["then"]
              if "runtime_controller.event" in action]
    runtime = Path(os.environ.get("RUNTIME_CONTROLLER_REPO", ROOT.parent / "esphome-runtime-controller"))
    profile = subprocess.run([sys.executable, "-c",
        "import sys;sys.path.insert(0,sys.argv[1]);from test_runtime_profile_runtime import _profile_cpp;print(_profile_cpp())",
        str(runtime / "tests")], capture_output=True, text=True)
    assert profile.returncode == 0, profile.stderr
    source = tmp_path / "boot-profile.cpp"
    source.write_text(profile.stdout + '#include <cassert>\nint main(){RuntimeController r;load_profile(r);\n'
                      + f'r.event("{events[0]}");\n'
                      + 'assert(r.is_activity_active("boot"));assert(r.is_activity_active("no_wifi"));\n'
                      + 'assert(r.is_activity_active("no_ha"));\n'
                      + f'r.event("{events[1]}");\n'
                      + 'assert(!r.is_activity_active("boot"));\n'
                      + 'r.event("wifi_connected");r.event("ha_connected");r.event("va_client_connected");\n'
                      + 'assert(!r.is_activity_active("no_wifi"));assert(!r.is_activity_active("no_ha"));}\n')
    component = runtime / "esphome/components/runtime_controller"
    binary = tmp_path / "boot-profile"
    compiled = subprocess.run(["g++", "-std=c++17", "-I", str(runtime / "tests/fixtures/core/stubs"),
        "-I", str(component), str(source), str(component / "runtime_controller.cpp"),
        str(component / "runtime_controller_state.cpp"), "-o", str(binary)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    executed = subprocess.run([str(binary)], capture_output=True, text=True)
    assert executed.returncode == 0, executed.stderr


@pytest.mark.parametrize("scene", ["spot240", "box360-aec", "p4-landscape", "p4-portrait"])
def test_options_title_is_part_of_the_full_height_scroll_content(scene):
    config = expanded_scene(scene, "full")
    page, controls = page_and_controls(config)
    wrapper = controls["settings_page_wrapper"]
    header = controls["settings_page_header"]
    assert page["widgets"] == [{"obj": wrapper}]
    assert wrapper["widgets"][0] == {"obj": header}
    assert wrapper["y"] == 0 and wrapper["height"] == 1.0
    assert str(wrapper["scrollbar_mode"]).removeprefix("LV_SCROLLBAR_MODE_") == "AUTO"
    assert header["height"] == "LV_SIZE_CONTENT"
    assert wrapper["pad_top"] <= (240 if scene == "spot240" else 360 if scene.startswith("box") else 800) // 12
    if not scene.startswith("p4"):
        assert header["scroll_chain_ver"] is False
        assert header["gesture_bubble"] is False
        assert "round_navigate" in text(header["on_swipe_down"])
