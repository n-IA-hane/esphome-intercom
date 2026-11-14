"""Execute the actual shared Number widget lambdas against explicit owner traits."""

import asyncio
from pathlib import Path
import re
import shutil
import subprocess

import esphome
from esphome import codegen as cg, yaml_util
from esphome.components import number
from esphome.components.lvgl.types import lv_obj_base_t, lv_page_t
from esphome.core import CORE, ID, Lambda
import pytest


ROOT = Path(__file__).resolve().parents[1]


def generated_lambdas(release, refresh):
    """Use the same ID registry and lambda rewriting as the SDK, not id(x) macros."""
    CORE.reset()
    try:
        for name, type_ in [("gain", number.Number), ("slider", lv_obj_base_t),
                            ("label", lv_obj_base_t), ("settings_page_wrapper", lv_obj_base_t),
                            ("page", lv_page_t)]:
            cg.Pvariable(ID(name, is_declaration=True, type=type_), cg.RawExpression("nullptr"))

        async def generate():
            return (str(await cg.process_lambda(Lambda(release), [(cg.float_, "x")], capture="=")),
                    str(await cg.process_lambda(Lambda(refresh), [], capture="=")))

        result = asyncio.run(generate())
        assert not re.search(r"\bid\(", "\n".join(result))
        return result
    finally:
        CORE.reset()


def test_number_widget_uses_owner_ticks_readiness_and_never_writes_on_refresh(tmp_path):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is required for the Number binding runtime oracle")
    binding = yaml_util.load_yaml(ROOT / "packages/features/presentation/settings/number_control.yaml")
    release = str(binding["widgets"][1]["slider"]["on_release"][0]["lambda"])
    refresh = str(binding["package"]["script"][0]["then"][0]["lambda"])
    for name, value in {"number_id": "gain", "slider_id": "slider", "label_id": "label",
                        "_settings_page_id": "page", "control_label": "Gain"}.items():
        release = release.replace("${" + name + "}", value)
        refresh = refresh.replace("${" + name + "}", value)
    assert "${" not in release + refresh
    generated_release, generated_refresh = generated_lambdas(release, refresh)
    assert "auto &owner = *gain;" in generated_release
    assert "auto &owner = *gain;" in generated_refresh
    assert "page->obj" in generated_refresh
    buggy_release, buggy_refresh = generated_lambdas(
        release.replace("auto &owner = *id(gain);", "auto &owner = id(gain);"),
        refresh.replace("auto &owner = *id(gain);", "auto &owner = id(gain);"))
    assert "auto &owner = gain;" in buggy_release + buggy_refresh
    prelude = r"""
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <string>
#include "esphome/components/number/number_traits.h"
#include "esphome/core/string_ref.h"
constexpr int LV_STATE_DISABLED=1, LV_STATE_PRESSED=2, LV_ANIM_OFF=0;
struct Obj { int flags=0, value=0, minimum=0, maximum=1; bool scrolling=false; std::string text; };
Obj slider_value, label_value, wrapper_value, page_value, other_page;
Obj *slider=&slider_value, *label=&label_value, *settings_page_wrapper=&wrapper_value, *active=&page_value;
struct Page { Obj *obj; } page_value_wrapper{&page_value};
Page *page=&page_value_wrapper;
int visual_writes=0;
Obj *lv_disp_get_scr_act(void*) { return active; }
bool lv_obj_is_scrolling(Obj *o) { return o->scrolling; }
bool lv_obj_has_state(Obj *o,int f) { return o->flags & f; }
void lv_obj_remove_state(Obj *o,int f) { o->flags &= ~f; ++visual_writes; }
void lv_obj_add_state(Obj *o,int f) { o->flags |= f; ++visual_writes; }
int lv_slider_get_min_value(Obj *o) { return o->minimum; }
int lv_slider_get_max_value(Obj *o) { return o->maximum; }
int lv_slider_get_value(Obj *o) { return o->value; }
void lv_slider_set_range(Obj *o,int lo,int hi) { o->minimum=lo; o->maximum=hi; ++visual_writes; }
void lv_slider_set_value(Obj *o,int x,int) { o->value=x; ++visual_writes; }
const char *lv_label_get_text(Obj *o) { return o->text.c_str(); }
void lv_label_set_text(Obj *o,const char *s) { o->text=s; ++visual_writes; }
struct Number { esphome::number::NumberTraits traits; float state=5.5f; bool ready=true; int writes=0;
 const char *unit="dB";
 bool has_state() const { return ready; }
 esphome::StringRef get_unit_of_measurement_ref() const { return esphome::StringRef(unit); }
 void configure(float lo,float hi,float step) {
  traits.set_min_value(lo); traits.set_max_value(hi); traits.set_step(step);
 }
 struct Call { Number *owner; float value=0;
  void set_value(float v) { value=v; } void perform() { owner->state=value; ++owner->writes; } };
 Call make_call() { return {this}; }
} gain_storage;
Number *const gain=&gain_storage;
int main() {
"""
    checks = r"""
  gain->configure(0,37.5f,.5f);
  refresh();
  assert(slider->minimum==0 && slider->maximum==75 && slider->value==11);
  assert(label->text=="Gain: 5.5 dB" && gain->writes==0);
  int previous=visual_writes; refresh(); assert(visual_writes==previous);
  release(74); assert(gain->writes==1 && gain->state==37);
  release(100); assert(gain->writes==2 && gain->state==37.5f);
  release(75); assert(gain->writes==2);
  gain->configure(0,36,6); gain->state=24; refresh();
  assert(slider->maximum==6 && slider->value==4 && label->text=="Gain: 24 dB");
  release(99); assert(gain->state==36 && gain->writes==3);
  gain->configure(-20,20,1); gain->state=-4; refresh();
  assert(slider->maximum==40 && slider->value==16);
  release(-10); assert(gain->state==-20 && gain->writes==4);
  wrapper_value.scrolling=true; gain->state=8; previous=visual_writes;
  refresh(); assert(visual_writes==previous && gain->writes==4);
  wrapper_value.scrolling=false; refresh(); assert(slider->value==28 && gain->writes==4);
  active=&other_page; gain->state=7; previous=visual_writes;
  refresh(); assert(visual_writes==previous); active=&page_value;
  slider->flags|=LV_STATE_PRESSED; previous=visual_writes; refresh();
  assert(visual_writes==previous); slider->flags&=~LV_STATE_PRESSED;
  gain->state=std::numeric_limits<float>::quiet_NaN(); refresh();
  assert(lv_obj_has_state(slider,LV_STATE_DISABLED) && label->text=="Gain: waiting");
  release(5); assert(gain->writes==4);
  gain->state=0; gain->ready=false; release(5); assert(gain->writes==4);
  gain->ready=true; refresh(); assert(!lv_obj_has_state(slider,LV_STATE_DISABLED));
  assert(gain->writes==4);
  // JPEG quality has a nonzero origin: tick0 is1, tick62 is63.
  gain->configure(1,63,1); gain->unit=""; gain->state=10; refresh();
  assert(slider->minimum==0 && slider->maximum==62 && slider->value==9);
  assert(label->text=="Gain: 10" && gain->writes==4);
  previous=visual_writes; refresh(); assert(visual_writes==previous && gain->writes==4);
  release(0); assert(gain->state==1 && gain->writes==5);
  release(62); assert(gain->state==63 && gain->writes==6);
  release(99); assert(gain->state==63 && gain->writes==6);
  gain->ready=false; refresh(); assert(lv_obj_has_state(slider,LV_STATE_DISABLED));
  release(0); assert(gain->state==63 && gain->writes==6);
  gain->ready=true; refresh(); assert(!lv_obj_has_state(slider,LV_STATE_DISABLED));
  assert(gain->writes==6);
}
"""
    include = tmp_path / "include/esphome/core"
    include.mkdir(parents=True)
    # Real StringRef with a host feature config, rather than its IDE-only flags.
    (include / "defines.h").write_text("#pragma once\n")
    esphome_root = Path(esphome.__file__).parent.parent

    def compile_lambdas(name, release_code, refresh_code):
        source = tmp_path / (name + ".cpp")
        source.write_text(prelude + "auto refresh=" + refresh_code + ";\n"
                          + "auto release=" + release_code + ";\n" + checks)
        binary = tmp_path / name
        result = subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
            "-I", str(tmp_path / "include"), "-I", str(esphome_root), str(source), "-o", str(binary)],
            capture_output=True, text=True)
        return result, binary

    bad, _ = compile_lambdas("old_object_assumption", buggy_release, buggy_refresh)
    assert bad.returncode != 0 and "pointer type" in bad.stderr, bad.stderr
    good, binary = compile_lambdas("number_binding", generated_release, generated_refresh)
    assert good.returncode == 0, good.stdout + good.stderr
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
