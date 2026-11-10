"""Execute composed TTS publication, text callback and renderer on real LVGL."""

from pathlib import Path
import subprocess

import pytest

from tests.native_profile import action_id, by_id, nodes, profile
from tests.test_lvgl_round_text_runtime import ROOT, round_text_binary as round_text_binary

pytestmark = [pytest.mark.diagnostic_reference, pytest.mark.integration, pytest.mark.timeout(180)]
PROFILES = [
    "full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml",
    "experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml",
]


def _text_actions(actions):
    result = []
    for action in actions:
        if "lambda" in action:
            result.append(action["lambda"])
        elif "globals.set" in action:
            setting = action["globals.set"]
            result.append(setting["id"] + "=" + str(setting["value"]) + ";")
        elif "if" in action:
            branch = action["if"]
            result.append("if ([&]() {" + branch["condition"]["lambda"] + "}()) {" +
                          _text_actions(branch["then"]) + "}")
        elif action_id(action, "script.execute") == "update_replying_text":
            result.append("show_page();")
    return "\n".join(result)


@pytest.mark.parametrize("relative", PROFILES)
def test_actual_tts_route_preserves_previous_page_on_allocation_failure(round_text_binary, tmp_path, relative):
    config = profile(relative)
    scripts = {item["id"]: item for item in config["script"]}
    sensor = by_id(config["text_sensor"], "text_response")
    callback = next(item["lambda"] for item in nodes(sensor["on_value"]) if "lambda" in item)
    render = next(item["lambda"] for item in scripts["update_replying_text"]["then"] if "lambda" in item)
    tts = _text_actions(scripts["ui_tts_started"]["then"])
    pagination = _text_actions(scripts["reply_pagination_timer"]["then"])
    constants = "\n".join(f"constexpr int {key}={value};" for key, value in config["substitutions"].items()
                            if key.startswith(("ui_state_", "voice_assist_")))
    source = tmp_path / "caller.cpp"
    source.write_text(f'''
#define main round_text_fixture_main
#include "{ROOT / 'tests/fixtures/lvgl_round_text/runtime.cpp'}"
#undef main
#include <functional>
#define id(name) name
#undef lv_img_set_src
namespace esphome::image {{ struct Image {{}}; }}
static void lv_img_set_src(void *, esphome::image::Image *) {{}}
struct CallerAdapter {{
 RoundText &core; const lv_font_t *font;
 void bind_reply(lv_obj_t *const *rows, lv_obj_t *counter) {{ core.bind_reply(rows,counter); }}
 void set_reply_font(const lv_font_t *value) {{ font=value; }}
 template<typename Width> bool set_reply(const std::string &value, const Width *widths, size_t count=10) {{
   return core.set_reply(value.data(),value.size(),font,widths,count);
 }}
 size_t total_pages() const {{ return core.total_pages(); }}
 bool show_reply(size_t page) {{ return core.show_reply(page); }}
}};
struct Sensor {{ std::function<void(const std::string &)> callback;
 void publish_state(const std::string &value) {{ callback(value); }} }};
struct Visibility {{ bool is_visible(lv_obj_t *) const {{ return true; }} }};
{constants}
int main() {{
 Scene scene; scene.chords.fill(40);
 CallerAdapter round_text{{scene.text,scene.font}};
 auto &chord_widths_cache=scene.chords;
 {''.join(f'auto *reply_line_{i}=scene.reply[{i}];' for i in range(10))}
 auto *reply_page_counter=scene.counter;
 int reply_page_index=0,reply_total_pages=1,current_mode=0;
 int voice_assistant_phase=voice_assist_replying_phase_id,ui_state=ui_state_va_replying;
 int runtime_visual_source=0; bool runtime_response_text_ready=false;
 Visibility display_visibility;
 esphome::image::Image neutral,happy,angry,background;
 auto *mood_neutral=&neutral,*mood_happy=&happy,*mood_angry=&angry,*mood_bg=&background;
 Sensor text_response;
 text_response.callback=[&](const std::string &x) {{{callback}}};
 auto show_page=[&]() {{{render}}};
 auto tts=[&](const std::string &text) {{{tts}}};
 auto pager=[&]() {{{pagination}}};
 std::string previous;
 for (unsigned i=0;i<30;++i) previous += "word"+std::to_string(i)+" ";
 text_response.publish_state(previous);
 assert(reply_total_pages>1); reply_page_index=1; show_page();
 const auto visible=scene.visible();
 const size_t bytes=scene.allocations.bytes();
 for (size_t failure : {{0,1}}) {{
   scene.allocations.fail_at=scene.allocations.calls+failure;
   tts(std::string(10000,'W'));
   assert(reply_page_index==1);
   assert(scene.visible()==visible);
   assert(scene.allocations.bytes()==bytes);
 }}
 scene.allocations.fail_at=SIZE_MAX;
 std::string next;
 for (unsigned i=0;i<10;++i) next += "small ";
 for (unsigned i=0;i<10;++i) next += std::string(600,'W')+" ";
 text_response.publish_state(next); show_page();
 assert(reply_page_index==0 && reply_total_pages==2);
 const auto page_zero=scene.visible();
 const size_t page_bytes=scene.allocations.bytes();
 for (size_t failure=0;failure<10;++failure) {{
   scene.allocations.fail_at=scene.allocations.calls+failure;
   pager();
   assert(reply_page_index==0 && scene.visible()==page_zero);
   assert(scene.allocations.bytes()==page_bytes);
 }}
 scene.allocations.fail_at=SIZE_MAX; pager(); assert(reply_page_index==1);
 (void)runtime_visual_source; (void)runtime_response_text_ready;
}}
''')
    library = round_text_binary.parent / "lvgl/lib/liblvgl.a"
    # Use the exact matching LVGL headers and config already qualified by this fixture.
    import os
    lvgl_source = Path(os.environ["LVGL_HOST_SOURCE"])
    executable = tmp_path / "caller"
    command = ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-UNDEBUG",
               "-DUSE_LVGL_ROUND_TEXT_DEBUG=1", "-DLV_KCONFIG_IGNORE", "-DLV_LVGL_H_INCLUDE_SIMPLE",
               f'-DLV_CONF_PATH="{ROOT / "tests/fixtures/lvgl_round_text/lv_conf.h"}"',
               f'-I{lvgl_source}', f'-I{ROOT / "tests/fixtures/lvgl_round_text/stubs"}',
               f'-I{ROOT / "esphome/components/lvgl_round_text"}', str(source), str(library), "-lm", "-o", str(executable)]
    if round_text_binary.parent.name.endswith("arabic"):
        command.append("-DROUND_TEXT_ARABIC=1")
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
