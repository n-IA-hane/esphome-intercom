"""MWI status presentation follows real package composition and native events."""

from pathlib import Path
import os
import subprocess
import sys

from esphome import yaml_util
import pytest

from tests.native_profile import nodes, text
from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/features/presentation"
GLYPH = "\U000F057D"
SCENES = {
    "box": "yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml",
    "spotpear": "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "p4-landscape": "yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml",
    "p4-portrait": "yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
}


def _expanded(tmp_path, scene, *, voip=True, extra_voip=None):
    original = yaml_util.load_yaml(ROOT / SCENES[scene])
    features = list(original["substitutions"]["firmware_features"])
    if not voip:
        features = [item for item in features if item not in {"voip", "video"}]
    if "display" not in features:
        features.append("display")
    config = device_config(tmp_path, features, SCENES[scene])
    if extra_voip is not None:
        with config.open("a") as stream:
            stream.write("\n" + yaml_util.dump({"voip_stack": extra_voip}))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", "--no-defaults", str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not resolve substitution" not in result.stderr
    expanded = tmp_path / "expanded.yaml"
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    if isinstance(document["lvgl"], list):
        document["lvgl"] = document["lvgl"][0]
    return document


@pytest.mark.parametrize("scene", SCENES)
@pytest.mark.parametrize("voip", [False, True], ids=["no-voip", "direct-no-registration"])
@pytest.mark.architecture
@pytest.mark.timeout(200)
def test_voicemail_composes_only_with_a_real_phone_and_requires_no_registration(tmp_path, scene, voip):
    document = _expanded(tmp_path, scene, voip=voip)
    ids = {str(node["id"]) for node in nodes(document) if "id" in node}
    assert ("ui_voicemail_waiting" in ids) is voip
    if voip:
        assert "registration" not in document["voip_stack"]
        assert document["voip_stack"]["ha_integration"]["message_waiting"] is True
        boot = text(document["esphome"]["on_boot"])
        assert boot.count("add_on_message_waiting_callback") == 1
        assert "USE_ESPHOME_VOIP_MWI" in boot
        assert "has_waiting_messages()" in boot
    else:
        assert "voip_stack" not in document
        assert "add_on_message_waiting_callback" not in text(document)
    font = next(item for item in document["font"] if str(item["id"]) == ("font_body" if scene.startswith("p4") else "font_clock"))
    glyphs = "".join(char for extra in font.get("extras", []) for char in extra["glyphs"])
    assert (GLYPH in glyphs) is voip
    assert "ui_voicemail" not in text(document.get("interval", []))
    if scene.startswith("p4"):
        assert ("top_voicemail_label" in ids) is voip
        if voip:
            label = next(node["label"] for node in nodes(document["lvgl"]["top_layer"]) if isinstance(node.get("label"), dict) and str(node["label"].get("id")) == "top_voicemail_label")
            assert label["hidden"] is True
            assert label["text"] == GLYPH
            action = next(item for item in document["script"] if str(item["id"]) == "update_voicemail_status")
            assert "id(main_page)->obj" in text(action)
            assert "has_waiting_messages" not in text(action)
            assert "update_voicemail_status" in text(document["lvgl_visibility"]["on_change"])
    else:
        assert "top_voicemail_label" not in ids
        action = next(item for item in document["script"] if str(item["id"]) == "update_idle_labels")
        assert ("ui_voicemail_waiting" in text(action)) is voip


@pytest.mark.parametrize("feature", ["USE_ESPHOME_VOIP_MWI", "USE_ESPHOME_VOIP_HA_MWI"])
def test_native_mwi_projection_coalesces_events_and_video_visibility(tmp_path, feature):
    """Execute the actual YAML lambda bodies with minimal LVGL/owner adapters."""
    projection = yaml_util.load_yaml(PACKAGE / "voicemail.yaml")
    boot = str(projection["esphome"]["on_boot"][0]["then"][0]["lambda"])
    boot = boot.replace("${voicemail_changed_code}", "++refreshes;")
    badge = yaml_util.load_yaml(PACKAGE / "p4_voicemail.yaml")
    update = str(badge["script"][0]["then"][0]["lambda"])
    update = update.replace("${_p4_voicemail_visible_code}", badge["defaults"]["_p4_voicemail_visible_code"])
    source = r'''
#include <cassert>
#include <functional>
#define USE_ESPHOME_VOIP_MWI
#define id(value) value
constexpr unsigned LV_OBJ_FLAG_HIDDEN=1;
struct Object { bool hidden=true; unsigned writes=0; } label, home, video;
struct Page { Object *obj; } page{&home};
Page *main_page=&page;
Object *top_voicemail_label=&label, *active=&home;
bool ui_voicemail_waiting=false;
unsigned refreshes=0;
Object *lv_disp_get_scr_act(void *) { return active; }
bool lv_obj_has_flag(Object *object,unsigned) { return object->hidden; }
void lv_obj_clear_flag(Object *object,unsigned) { object->hidden=false; ++object->writes; }
void lv_obj_add_flag(Object *object,unsigned) { object->hidden=true; ++object->writes; }
struct Phone {
 bool waiting=false;
 std::function<void()> callback;
 bool has_waiting_messages() { return waiting; }
 void add_on_message_waiting_callback(std::function<void()> fn) { callback=fn; }
 void emit(bool value) { waiting=value; callback(); }
} phone;
void install() {
''' + boot + r'''
}
void sync_badge() {
''' + update + r'''
}
int main() {
 install(); assert(refreshes==0 && !ui_voicemail_waiting);
 phone.emit(true); assert(refreshes==1 && ui_voicemail_waiting);
 phone.emit(true); assert(refreshes==1);
 sync_badge(); assert(!label.hidden && label.writes==1);
 sync_badge(); assert(label.writes==1);
 active=&video; sync_badge(); assert(label.hidden && label.writes==2);
 active=&home; sync_badge(); assert(!label.hidden && label.writes==3);
 phone.emit(false); assert(refreshes==2 && !ui_voicemail_waiting);
 sync_badge(); assert(label.hidden && label.writes==4);
 phone.emit(false); sync_badge(); assert(refreshes==2 && label.writes==4);
}
'''
    source = source.replace("#define USE_ESPHOME_VOIP_MWI", "#define " + feature)
    cpp = tmp_path / "voicemail.cpp"
    cpp.write_text(source)
    binary = tmp_path / "voicemail"
    compiled = subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scene", ["spotpear", "p4-landscape"])
@pytest.mark.architecture
@pytest.mark.timeout(200)
def test_pbx_only_phone_preserves_disabled_ha_adapter_and_native_mwi(tmp_path, scene):
    document = _expanded(tmp_path, scene, extra_voip={
        "ha_integration": False,
        "registration": {"message_waiting": True},
    })
    assert document["voip_stack"]["ha_integration"] is False
    assert document["voip_stack"]["registration"]["message_waiting"] is True
    assert document["voip_stack"]["registration"].get("accounts", []) == []
    assert "add_on_message_waiting_callback" in text(document["esphome"]["on_boot"])
    assert "ui_voicemail_waiting" in text(document)


@pytest.mark.parametrize("relative", [
    "yamls/voip-only/single-bus/spotpear-ball-v2-voip.yaml",
    "yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml",
])
@pytest.mark.architecture
@pytest.mark.timeout(200)
def test_standalone_phone_keeps_mwi_without_runtime_controller(tmp_path, relative):
    original = yaml_util.load_yaml(ROOT / relative)
    config = device_config(tmp_path, list(original["substitutions"]["firmware_features"]), relative)
    checked = subprocess.run([sys.executable, "-m", "esphome", "config", "--no-defaults", str(config)],
                             capture_output=True, text=True, timeout=180)
    assert checked.returncode == 0, checked.stdout[-4000:] + checked.stderr[-4000:]
    expanded = tmp_path / "expanded.yaml"
    expanded.write_text(checked.stdout)
    document = yaml_util.load_yaml(expanded)
    assert "runtime_controller" not in document
    assert "registration" not in document["voip_stack"]
    assert document["voip_stack"]["ha_integration"]["message_waiting"] is True
    assert text(document["esphome"]["on_boot"]).count("add_on_message_waiting_callback") == 1
    assert "ui_voicemail_waiting" in text(document["globals"])
