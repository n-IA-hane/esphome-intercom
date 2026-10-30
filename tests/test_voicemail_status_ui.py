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
GLYPH = "\U000F01EE"
SCENES = {
    "box": "yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml",
    "spotpear": "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "p4-landscape": "yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml",
    "p4-portrait": "yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
}
PRESENTATIONS = {
    "box": "round_runtime.yaml",
    "spotpear": "round_runtime.yaml",
    "p4-landscape": "devices/waveshare-p4-full-landscape.yaml",
    "p4-portrait": "devices/waveshare-p4-full-portrait.yaml",
}


def _badge_parameters(presentation):
    """Read the caller's include variables, before nested package expansion."""
    caller = yaml_util.load_yaml(PACKAGE / presentation)
    return caller["packages"]["phone_presentation"].vars


def _assert_badge_layout(action, parameters):
    layout = str(yaml_util.load_yaml(PACKAGE / "phone.yaml")["substitutions"]["_phone_badge_layout"])
    assert (layout in text(action)) is bool(parameters.get("_adaptive_phone_contacts", False))
    assert "${" not in text(action)


@pytest.mark.parametrize("relative,presentation,font,screen", [
    (SCENES["box"], PRESENTATIONS["box"], "font_clock", "idle_page"),
    (SCENES["spotpear"], PRESENTATIONS["spotpear"], "font_clock", "idle_page"),
    (SCENES["p4-landscape"], PRESENTATIONS["p4-landscape"], "font_body", "main_page"),
    (SCENES["p4-portrait"], PRESENTATIONS["p4-portrait"], "font_body", "main_page"),
    ("yamls/voip-only/single-bus/spotpear-ball-v2-voip.yaml", "devices/spotpear-ball-v2-phone.yaml", "font_clock", "call_idle_page"),
    ("yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml", "devices/waveshare-p4-phone.yaml", "videophone_small_font", "videophone_page"),
])
def test_mwi_only_preserves_the_callers_badge_parameters_across_nested_packages(tmp_path, relative, presentation, font, screen):
    """Exercise native package scoping without regenerating binary image assets."""
    from esphome.core import CORE
    from esphome.components.packages import do_packages_pass, merge_packages
    from esphome.components.substitutions import do_substitution_pass

    config = device_config(tmp_path, ["voip", "display", "mwi", "diagnostics"], relative)
    CORE.config_path = config
    document = merge_packages(do_substitution_pass(do_packages_pass(yaml_util.load_yaml(config)), {}))
    parameters = _badge_parameters(presentation)
    label = next(node for node in nodes(document.get("lvgl", {})) if str(node.get("id")) == "top_voicemail_label")
    assert str(label["text_font"]) == font
    assert str(label["y"]) == str(parameters["_voicemail_y"])
    action = next(node for node in document["script"] if str(node.get("id")) == "update_voicemail_status")
    _assert_badge_layout(action, parameters)
    assert f"id({screen})" in text(action)
    assert "voice_assistant_phase" not in text(action), "MWI does not depend on the voice feature"
    assert "call_mailbox()" in text(label["on_short_click"])
    assert "imap_voicemail" not in document and "ui_voicemail_active" not in text(document.get("globals", []))


def _expanded(tmp_path, scene, *, voip=True, extra_voip=None, extra_components=None):
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
    if extra_components is not None:
        with config.open("a") as stream:
            stream.write("\n" + yaml_util.dump(extra_components))
    result = subprocess.run(
        [sys.executable, "-m", "esphome", "config", "--no-defaults", str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (tmp_path / "validation.log").write_text(result.stderr)
    assert "Could not resolve substitution" not in result.stderr
    expanded = tmp_path / "expanded.yaml"
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    if isinstance(document["lvgl"], list):
        document["lvgl"] = document["lvgl"][0]
    return document


@pytest.mark.parametrize("scene", SCENES)
@pytest.mark.parametrize("voip", [False, True], ids=["no-voip", "automatic-registration"])
@pytest.mark.architecture
@pytest.mark.timeout(200)
def test_voicemail_composes_with_standard_registered_phone_services(tmp_path, scene, voip):
    document = _expanded(tmp_path, scene, voip=voip)
    ids = {str(node["id"]) for node in nodes(document) if "id" in node}
    assert ("ui_voicemail_waiting" in ids) is voip
    if voip:
        assert document["voip_stack"]["ha_integration"]["auto_register"] is True
        assert document["voip_stack"]["registration"]["message_waiting"] is True
        boot = text(document["esphome"]["on_boot"])
        assert boot.count("add_on_message_waiting_callback") == 1
        assert "mwi" in document["substitutions"]["firmware_features"]
        assert "has_waiting_messages()" in boot
    else:
        assert "voip_stack" not in document
        assert "add_on_message_waiting_callback" not in text(document)
        assert not {"ldap_directory", "imap_voicemail", "phone_list_view"} & document.keys()
        assert not {"phone_directory", "phone_voicemail", "contacts_list_view", "phone_voicemail_page", "top_voicemail_label"} & ids
        for feature in ("directory", "blf", "mwi"):
            assert f"feature '{feature}' is disabled: requires voip" in (tmp_path / "validation.log").read_text()
        assert not any({"ldap_directory", "imap_voicemail", "phone_client"} & set(item.get("components", []))
                       for item in document.get("external_components", []))
    font = next(item for item in document["font"] if str(item["id"]) == ("font_body" if scene.startswith("p4") else "font_clock"))
    glyphs = "".join(char for extra in font.get("extras", []) for char in extra["glyphs"])
    assert (GLYPH in glyphs) is voip
    assert "ui_voicemail" not in text(document.get("interval", []))
    assert ("top_voicemail_label" in ids) is voip
    if voip:
        label = next(node["label"] for node in nodes(document["lvgl"]["top_layer"])
                     if isinstance(node.get("label"), dict) and str(node["label"].get("id")) == "top_voicemail_label")
        assert label["hidden"] is True
        assert label["text"] == GLYPH
        assert label["align"] == "TOP_MID"
        parameters = _badge_parameters(PRESENTATIONS[scene])
        assert label["y"] == parameters["_voicemail_y"]
        action = next(item for item in document["script"] if str(item["id"]) == "update_voicemail_status")
        _assert_badge_layout(action, parameters)
        assert "id(phone).is_idle()" in text(action)
        assert "voice_assistant_phase" in text(action)
        assert "has_waiting_messages" not in text(action)
        assert "update_voicemail_status" in text(document["lvgl_visibility"]["on_change"])
        assert "update_voicemail_status" in text(next(item for item in document["script"] if str(item["id"]) == "draw_display"))
    if not scene.startswith("p4"):
        action = next(item for item in document["script"] if str(item["id"]) == "update_idle_labels")
        assert "ui_voicemail_waiting" not in text(action)


@pytest.mark.parametrize("feature", ["USE_ESPHOME_VOIP_MWI"])
@pytest.mark.parametrize("adaptive,row_height", [(True, 36), (True, 52), (False, 36)], ids=["round-240", "round-360", "fixed-p4"])
def test_native_mwi_projection_coalesces_events_and_video_visibility(tmp_path, feature, adaptive, row_height):
    """Execute the actual YAML lambda bodies with minimal LVGL/owner adapters."""
    from esphome.components.substitutions import do_substitution_pass

    projection = yaml_util.load_yaml(PACKAGE / "voicemail.yaml")
    boot = str(projection["esphome"]["on_boot"][0]["then"][0]["lambda"])
    boot = boot.replace("${voicemail_changed_code}", "++refreshes;")
    badge = yaml_util.load_yaml(PACKAGE / "voicemail_badge.yaml")
    layout = yaml_util.load_yaml(PACKAGE / "phone.yaml")["substitutions"]["_phone_badge_layout"]
    expanded = do_substitution_pass({
        "substitutions": {
            "_voicemail_visible_code": "(active == &home || active == &contacts) && assistant_idle",
            "_voicemail_layout_code": layout if adaptive else badge["defaults"]["_voicemail_layout_code"],
        },
        "script": badge["script"],
    }, {})
    update = str(expanded["script"][0]["then"][0]["lambda"])
    assert "${" not in update
    source = r'''
#include <cassert>
#include <functional>
#define USE_ESPHOME_VOIP_MWI
#define id(value) value
constexpr unsigned LV_OBJ_FLAG_HIDDEN=1;
constexpr bool ADAPTIVE_CONTACTS=ADAPTIVE_VALUE;
constexpr int ROW_HEIGHT=ROW_HEIGHT_VALUE;
struct Object { bool hidden=true; unsigned writes=0; int height=0; unsigned height_writes=0; } label, home, video, contacts, options, container;
struct ContactView { int row_height=ROW_HEIGHT; int get_row_height() { return row_height; } } contacts_list_view;
Object *phone_contacts_container=&container;
struct Page { Object *obj; } page{&home};
Page *main_page=&page;
Object *top_voicemail_label=&label, *active=&home;
bool ui_voicemail_waiting=false;
bool assistant_idle=true;
unsigned refreshes=0;
Object *lv_disp_get_scr_act(void *) { return active; }
bool lv_obj_has_flag(Object *object,unsigned) { return object->hidden; }
void lv_obj_clear_flag(Object *object,unsigned) { object->hidden=false; ++object->writes; }
void lv_obj_add_flag(Object *object,unsigned) { object->hidden=true; ++object->writes; }
int lv_obj_get_height(Object *object) { return object->height; }
void lv_obj_set_height(Object *object,int height) { object->height=height; ++object->height_writes; }
struct Phone {
 bool waiting=false;
 bool idle=true;
 bool is_idle() { return idle; }
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
void assert_rows(unsigned count) {
 assert(container.height==(ADAPTIVE_CONTACTS ? contacts_list_view.row_height*int(count) : 0));
}
int main() {
 install(); assert(refreshes==0 && !ui_voicemail_waiting);
 sync_badge(); assert(label.hidden && label.writes==0); assert_rows(4);
 unsigned height_writes=container.height_writes;
 sync_badge(); assert(container.height_writes==height_writes);
 phone.emit(true); assert(refreshes==1 && ui_voicemail_waiting);
 phone.emit(true); assert(refreshes==1);
 sync_badge(); assert(!label.hidden && label.writes==1); assert_rows(3);
 height_writes=container.height_writes;
 sync_badge(); assert(label.writes==1 && container.height_writes==height_writes);
 active=&video; sync_badge(); assert(label.hidden && label.writes==2); assert_rows(4);
 active=&home; sync_badge(); assert(!label.hidden && label.writes==3); assert_rows(3);
 active=&options; sync_badge(); assert(label.hidden); assert_rows(4);
 active=&contacts; sync_badge(); assert(!label.hidden); assert_rows(3);
 for (int call_state=1;call_state<=4;++call_state) {
   phone.idle=false; sync_badge(); assert(label.hidden); assert_rows(4);
   phone.idle=true; sync_badge(); assert(!label.hidden); assert_rows(3);
 }
 active=&home; assistant_idle=false; sync_badge(); assert(label.hidden); assert_rows(4);
 assistant_idle=true; sync_badge(); assert(!label.hidden); assert_rows(3);
 contacts_list_view.row_height+=4; sync_badge(); assert(!label.hidden); assert_rows(3);
 height_writes=container.height_writes;
 sync_badge(); assert(container.height_writes==height_writes);
 phone.emit(false); assert(refreshes==2 && !ui_voicemail_waiting);
 sync_badge(); assert(label.hidden); assert_rows(4);
 unsigned writes=label.writes; height_writes=container.height_writes;
 phone.emit(false); sync_badge(); assert(refreshes==2 && label.writes==writes && container.height_writes==height_writes);
 if (!ADAPTIVE_CONTACTS) assert(container.height_writes==0);
}
'''
    source = source.replace("#define USE_ESPHOME_VOIP_MWI", "#define " + feature)
    source = source.replace("ADAPTIVE_VALUE", str(adaptive).lower()).replace("ROW_HEIGHT_VALUE", str(row_height))
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
    }, extra_components={
        "ldap_directory": {"managed_by_ha": False},
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
    assert document["voip_stack"]["ha_integration"]["auto_register"] is True
    assert document["voip_stack"]["registration"]["message_waiting"] is True
    assert text(document["esphome"]["on_boot"]).count("add_on_message_waiting_callback") == 1
    assert "ui_voicemail_waiting" in text(document["globals"])
