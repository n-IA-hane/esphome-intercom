"""P4's actual quick-control writer keeps local styles without repeat writes."""

import os
import subprocess

from esphome import yaml_util
import pytest

from tests.test_lvgl_round_text_runtime import ROOT, round_text_binary as round_text_binary


pytestmark = [pytest.mark.diagnostic_reference, pytest.mark.integration, pytest.mark.timeout(180)]


def _helper(source, name):
    start = source.index("auto " + name)
    return source[start:source.index("\n};", start) + 3]


def test_p4_controls_preserve_local_ownership_across_theme_updates(round_text_binary, tmp_path):
    config = yaml_util.load_yaml(ROOT / "packages/features/presentation/devices/waveshare-p4-full-landscape.yaml")
    actual = str(config["defaults"]["_p4_draw_actions"][0]["lambda"])
    code = r'''
#include "lvgl.h"
#include <array>
#include <cassert>
#include <cstring>
unsigned style_writes=0;
void record_background(lv_obj_t *o,lv_color_t v,lv_style_selector_t s){++style_writes;lv_obj_set_style_bg_color(o,v,s);}
void record_border(lv_obj_t *o,lv_color_t v,lv_style_selector_t s){++style_writes;lv_obj_set_style_border_color(o,v,s);}
void record_foreground(lv_obj_t *o,lv_color_t v,lv_style_selector_t s){++style_writes;lv_obj_set_style_text_color(o,v,s);}
void record_width(lv_obj_t *o,int32_t v,lv_style_selector_t s){++style_writes;lv_obj_set_style_border_width(o,v,s);}
#define lv_obj_set_style_bg_color record_background
#define lv_obj_set_style_border_color record_border
#define lv_obj_set_style_text_color record_foreground
#define lv_obj_set_style_border_width record_width
int main(){
 lv_init();auto *display=lv_display_create(160,96);lv_display_set_default(display);
 std::array<uint8_t,320> pixels{};lv_display_set_color_format(display,LV_COLOR_FORMAT_RGB565);
 lv_display_set_buffers(display,pixels.data(),nullptr,pixels.size(),LV_DISPLAY_RENDER_MODE_PARTIAL);
 lv_display_set_flush_cb(display,[](lv_display_t*d,const lv_area_t*,uint8_t*){lv_display_flush_ready(d);});
 auto *button=lv_obj_create(lv_screen_active());auto *label=lv_label_create(button);
 LABEL_WRITER
 QUICK_WRITER
 for(bool active:{false,true,false,true}){
  set_quick_button(button,label,active,0xB91C1C,0x44251D,"On","Off");
  assert(lv_color_to_int(lv_obj_get_style_bg_color(button,LV_PART_MAIN))==(active?0xB91C1C:0x44251D));
  assert(lv_obj_get_style_border_width(button,LV_PART_MAIN)==(active?2:0));
  assert(std::strcmp(lv_label_get_text(label),active?"On":"Off")==0);
  const auto before=style_writes;
  for(unsigned i=0;i<100;++i)set_quick_button(button,label,active,0xB91C1C,0x44251D,"On","Off");
  assert(style_writes==before);
 }
 lv_style_t theme;lv_style_init(&theme);lv_style_set_text_color(&theme,lv_color_hex(0xFFFFFF));
 lv_obj_remove_local_style_prop(label,LV_STYLE_TEXT_COLOR,LV_PART_MAIN);
 lv_obj_add_style(label,&theme,LV_PART_MAIN);
 set_quick_button(button,label,false,0xB91C1C,0x44251D,"On","Off");
 lv_style_set_text_color(&theme,lv_color_hex(0));lv_obj_report_style_change(&theme);
 assert(lv_color_to_int(lv_obj_get_style_text_color(label,LV_PART_MAIN))==0xFFFFFF);
 lv_display_delete(display);lv_style_reset(&theme);lv_deinit();
}
'''.replace("LABEL_WRITER", _helper(actual, "set_label")).replace(
        "QUICK_WRITER", _helper(actual, "set_quick_button"))
    source = tmp_path / "controls.cpp"
    source.write_text(code)
    executable = tmp_path / "controls"
    command = ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-UNDEBUG",
               "-DLV_KCONFIG_IGNORE", f'-DLV_CONF_PATH="{ROOT / "tests/fixtures/lvgl_round_text/lv_conf.h"}"',
               "-I" + os.environ["LVGL_HOST_SOURCE"], str(source),
               str(round_text_binary.parent / "lvgl/lib/liblvgl.a"), "-lm", "-o", str(executable)]
    if round_text_binary.parent.name.endswith("arabic"):
        command.append("-DROUND_TEXT_ARABIC=1")
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
