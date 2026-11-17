"""Compile the shared Switch binding with real ESPHome ID and Switch types."""

import asyncio
from pathlib import Path
import shutil
import subprocess

import esphome
from esphome import codegen as cg, yaml_util
from esphome.components import switch
from esphome.components.lvgl.types import lv_obj_base_t, lv_page_t
from esphome.core import CORE, ID, Lambda
import pytest


ROOT = Path(__file__).resolve().parents[1]


def binding_lambdas():
    binding = yaml_util.load_yaml(ROOT / "packages/features/presentation/settings/switch_control.yaml")
    change = str(binding["widgets"][0]["obj"]["widgets"][1]["switch"]["on_change"][0]["lambda"])
    action = binding["package"]["script"][0]["then"][0]["if"]
    condition = str(action["condition"]["lambda"])
    checked = str(action["then"][0]["lvgl.widget.update"]["state"]["checked"])
    callback = str(binding["callback"])
    fragments = [change, condition, checked, callback]
    for name, value in {"switch_id": "owner", "widget_id": "widget", "sync_id": "test_sync_script",
                        "_settings_page_id": "page"}.items():
        fragments = [part.replace("${" + name + "}", value) for part in fragments]
    assert not any("${" in part for part in fragments)
    CORE.reset()
    try:
        for name, type_ in [("owner", switch.Switch), ("widget", lv_obj_base_t),
                            ("settings_page_wrapper", lv_obj_base_t), ("page", lv_page_t),
                            ("test_sync_script", cg.esphome_ns.namespace("script").class_("Script").template())]:
            cg.Pvariable(ID(name, is_declaration=True, type=type_), cg.RawExpression("nullptr"))

        async def generate():
            return [str(await cg.process_lambda(Lambda(part), [(cg.bool_, "x")] if index == 0 else [],
                         capture="=", return_type=cg.bool_ if index in (1, 2) else None))
                    for index, part in enumerate(fragments)]

        return asyncio.run(generate())
    finally:
        CORE.reset()


def test_switch_binding_preserves_owner_callbacks_deferred_refresh_and_idempotence(tmp_path):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is required for the Switch binding runtime oracle")
    change, condition, checked, callback = binding_lambdas()
    prelude = r'''
#include <cassert>
#include <functional>
#include <unistd.h>  // Keep the POSIX sync() declaration visible on every host.
#ifdef REAL_SWITCH_API
#include "esphome/components/switch/switch.h"
using Switch=esphome::switch_::Switch;
#else
// Runtime adapter; the same generated lambdas also compile with real Switch below.
struct Switch {
 bool state=false; int writes=0; std::function<void(bool)> callback;
 void publish_state(bool value) { state=value; if(callback)callback(value); }
 void turn_on() { ++writes; publish_state(true); }
 void turn_off() { ++writes; publish_state(false); }
 template<class F>void add_on_state_callback(F f) { callback=f; }
};
#endif
struct Obj { bool scrolling=false, checked=false; } widget_value, wrapper_value, page_value, other_page;
Obj *const widget=&widget_value, *const settings_page_wrapper=&wrapper_value;
Obj *active=&page_value;
struct Page { Obj *obj; } page_value_wrapper{&page_value};
Page *const page=&page_value_wrapper;
struct Script { std::function<void()> action; void execute(){action();} } test_sync_script_value;
Script *const test_sync_script=&test_sync_script_value;
constexpr int LV_STATE_CHECKED=1;
Obj *lv_disp_get_scr_act(void*) { return active; }
bool lv_obj_is_scrolling(Obj *o) { return o->scrolling; }
bool lv_obj_has_state(Obj *o,int) { return o->checked; }
#ifdef REAL_SWITCH_API
extern Switch *const owner;
#else
Switch owner_value;
Switch *const owner=&owner_value;
#endif
int main() {
'''
    code = (prelude + "auto change=" + change + ";\nauto condition=" + condition
            + ";\nauto checked=" + checked + ";\nauto callback=" + callback + ";\n" + r'''
 int visual_writes=0;
 test_sync_script->action=[&](){if(condition()){widget->checked=checked();++visual_writes;}};
 callback();
#ifndef REAL_SWITCH_API
 owner->publish_state(true); assert(widget->checked && visual_writes==1 && owner->writes==0);
 owner->publish_state(true); assert(visual_writes==1 && owner->writes==0);
 change(true); test_sync_script->execute(); assert(owner->writes==0 && visual_writes==1);
 change(false); test_sync_script->execute(); assert(!widget->checked && owner->writes==1 && visual_writes==2);
 // During scrolling callbacks are deferred, not lost or written back to the owner.
 settings_page_wrapper->scrolling=true; owner->publish_state(true);
 assert(!widget->checked && visual_writes==2 && owner->writes==1);
 settings_page_wrapper->scrolling=false; test_sync_script->execute();
 assert(widget->checked && visual_writes==3 && owner->writes==1);
 active=&other_page; owner->publish_state(false);
 assert(widget->checked && visual_writes==3 && owner->writes==1);
 active=page->obj; test_sync_script->execute();
 assert(!widget->checked && visual_writes==4 && owner->writes==1);
 // Programmatic refresh never calls change/turn_on/turn_off.
 owner->publish_state(false); test_sync_script->execute(); assert(visual_writes==4 && owner->writes==1);
#else
 (void)visual_writes; (void)change;
#endif
}
''')
    source = tmp_path / "switch_binding.cpp"
    source.write_text(code)
    include = tmp_path / "include/esphome/core"
    include.mkdir(parents=True)
    (include / "defines.h").write_text("#pragma once\n#define USE_HOST\n#define USE_SWITCH\n")
    flags = [compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I", str(tmp_path / "include"),
             "-I", str(Path(esphome.__file__).parent.parent)]
    # This catches pointer/member/API mismatches against each installed Core.
    actual = subprocess.run([*flags, "-DREAL_SWITCH_API", "-fsyntax-only", str(source)],
                            capture_output=True, text=True)
    assert actual.returncode == 0, actual.stdout + actual.stderr
    binary = tmp_path / "switch_binding"
    built = subprocess.run([*flags, str(source), "-o", str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
