"""Execute shipped button routing with the actual Runtime Controller reducer."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

from tests.native_profile import by_id, nodes, profile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / "esphome-runtime-controller"


def _condition(value):
    if "lambda" in value:
        return "([&](){" + value["lambda"] + "})()"
    if "and" in value:
        return " && ".join("(" + _condition(item) + ")" for item in value["and"])
    if "not" in value:
        return "!(" + _condition(value["not"]) + ")"
    if "runtime_controller.is_active" in value:
        return 'runtime.is_activity_active("' + value["runtime_controller.is_active"]["activity"] + '")'
    if "voip_stack.is_idle" in value:
        return "phone.is_idle()"
    if "voip_stack.is_active" in value:
        return "!phone.is_idle()"
    raise AssertionError(value)


def _actions(values):
    result = ""
    for value in values:
        if "if" in value:
            item = value["if"]
            result += "if(" + _condition(item["condition"]) + "){" + _actions(item["then"]) + "}else{" + _actions(item.get("else", [])) + "}"
        elif "runtime_controller.event" in value:
            result += 'runtime.event("' + value["runtime_controller.event"]["event"] + '");'
        elif "voip_stack.call_toggle" in value:
            result += "++phone.toggles;"
        elif value.get("script.execute") == "voip_dial_call":
            result += "++phone.toggles;"
        else:
            raise AssertionError(value)
    return result


@pytest.mark.parametrize("device,button", [
    ("waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml", "main_button"),
    ("waveshare-p4-touch-full-afe-portrait.yaml", "boot_button"),
    ("waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml", "boot_button"),
    ("waveshare-p4-touch-full-afe-portrait.yaml", "va_touch_area"),
    ("waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml", "va_touch_area"),
])
def test_manual_button_cancels_assist_before_telephone(device, button, tmp_path, monkeypatch):
    configuration = profile("full-experience/single-bus/" + device)
    voice_only = button == "va_touch_area"
    if voice_only:
        actions = by_id(configuration["lvgl"], button)["on_click"]
        for group in ("va_listening_group", "va_thinking_group", "va_replying_group"):
            clicks = [node["on_click"] for node in nodes(by_id(configuration["lvgl"], group)) if "on_click" in node]
            assert clicks == [actions]
    else:
        actions = by_id(configuration["binary_sensor"], button)["on_multi_click"][0]["then"]
    monkeypatch.syspath_prepend(str(RUNTIME / "tests"))
    spec = importlib.util.spec_from_file_location("button_runtime_profile", RUNTIME / "tests/test_runtime_profile_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = module._profile_cpp().replace("r.setup();", '''
        r.add_activity("voip_in_call",700,false);
        r.add_activity_policy("voip_in_call","display_status","voip_in_call");
        r.setup();''') + r'''
#include <cassert>
#include <cstring>
#define id(x) (x)
struct Phone {bool idle=true;unsigned toggles=0;bool is_idle()const{return idle;}};
struct Voice {bool running=false;bool is_running()const{return running;}};
int current_mode=1;bool voip_dialer_active=false;
void button(RuntimeController &runtime,Phone &phone,Voice &va){BUTTON}
int main(){
 for(bool call:{false,true})for(const char*phase:{"va_start_requested","va_starting","va_listening","va_thinking","va_responding","va_barging","va_stopping"})for(bool announcement:{false,true}){
  if(announcement&&std::strcmp(phase,"va_responding")!=0)continue;
  RuntimeController runtime;load_profile(runtime);Phone phone;Voice va;
  runtime.event("boot_ready");runtime.event("wifi_connected");runtime.event("ha_connected");runtime.event("va_client_connected");
  runtime.event("media_playing");current_mode=call?1:0;phone.idle=!call;runtime.set_activity("voip_in_call",call);
  runtime.set_activity(phase,true);
  if(announcement)runtime.event("announcement_started");
  unsigned stops=0,starts=0;
  esphome::Trigger<> stop,start;stop.fn=[&](){++stops;};start.fn=[&](){++starts;};
  runtime.add_action_trigger("voice_stop_pipeline",&stop);runtime.add_action_trigger("voice_stop_all",&stop);
  runtime.add_action_trigger("voice_start",&start);runtime.add_action_trigger("voice_cancel_pipeline",&start);
  button(runtime,phone,va);runtime.loop();
  assert(phone.toggles==0&&"Assist cancellation must preserve the telephone owner");
  assert(runtime.is_activity_active("va_stopping")&&!runtime.is_activity_active("va_start_requested"));
  assert(starts==0&&stops==(std::strcmp(phase,"va_stopping")!=0));
  button(runtime,phone,va);runtime.loop();assert(starts==0&&phone.toggles==0);
  runtime.event("va_end");runtime.event("va_stop_complete");
  assert(!runtime.is_activity_active("va_stopping")&&runtime.is_activity_active("media"));
  assert(runtime.is_activity_active("voip_in_call")==call);
  assert(std::strcmp(runtime.get_policy("display_status"),call?"voip_in_call":"media")==0);
  button(runtime,phone,va);runtime.loop();
  assert(phone.toggles==((call&&!VOICE_ONLY)?1:0));assert(starts==((call&&!VOICE_ONLY)?0:1));
 }
 // A genuine wake word keeps its restart/barge policy during the same call.
 RuntimeController runtime;load_profile(runtime);
 unsigned restarts=0;esphome::Trigger<> restart;restart.fn=[&](){++restarts;};
 runtime.add_action_trigger("voice_cancel_pipeline",&restart);
 runtime.set_activity("voip_in_call",true);runtime.event("va_listening");runtime.event("wake_word");
 runtime.loop();assert(runtime.is_activity_active("va_barging")&&restarts==1);
 assert(runtime.is_activity_active("voip_in_call")&&!runtime.is_activity_active("va_stopping"));
}
'''.replace("BUTTON", _actions(actions)).replace("VOICE_ONLY", str(voice_only).lower())
    cpp = tmp_path / "button.cpp"
    cpp.write_text(source)
    binary = tmp_path / "button"
    component = RUNTIME / "esphome/components/runtime_controller"
    run = subprocess.run(["g++", "-std=c++17", "-I", str(RUNTIME / "tests/fixtures/core/stubs"), "-I", str(component),
                          str(cpp), str(component / "runtime_controller.cpp"), str(component / "runtime_controller_state.cpp"),
                          "-o", str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
