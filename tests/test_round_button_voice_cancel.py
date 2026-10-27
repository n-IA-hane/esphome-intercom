"""Execute the maintained physical-button branches and their actual C++ guards."""
from pathlib import Path
import json
import subprocess

import pytest
import yaml

from tests.native_profile import PackageLoader

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/features/controls/spotpear_full.yaml"


def condition_cpp(condition):
    if "lambda" in condition:
        return "([&]() {\n" + str(condition["lambda"]) + "\n})()"
    if "and" in condition:
        return "(" + " && ".join(condition_cpp(item) for item in condition["and"]) + ")"
    if "runtime_controller.is_active" in condition:
        return "runtime.is_activity_active(" + json.dumps(condition["runtime_controller.is_active"]["activity"]) + ")"
    for name, method in (("voip_stack.is_idle", "is_idle"), ("voip_stack.is_active", "is_active")):
        if name in condition:
            return "phone." + method + "()"
    raise AssertionError(f"Unexecuted production condition: {condition}")


def actions_cpp(actions):
    output = []
    for action in actions:
        if "if" in action:
            branch = action["if"]
            output.append("if (" + condition_cpp(branch["condition"]) + ") {\n" + actions_cpp(branch["then"]) + "}\n")
            if "else" in branch:
                output.append("else {\n" + actions_cpp(branch["else"]) + "}\n")
        elif "runtime_controller.event" in action:
            output.append("runtime.event(" + json.dumps(action["runtime_controller.event"]["event"]) + ");\n")
        elif "script.execute" in action:
            output.append("run_script(" + json.dumps(action["script.execute"]) + ");\n")
        elif "voip_stack.call_toggle" in action:
            output.append("phone.call_toggle();\n")
        else:
            raise AssertionError(f"Unexecuted production action: {action}")
    return "".join(output)


@pytest.mark.parametrize("with_timer", [False, True])
def test_active_assist_cancellation_precedes_phone_button_actions(tmp_path, with_timer):
    package = yaml.load(PACKAGE.read_text(), Loader=PackageLoader)
    branch = package["defaults"]["_spotpear_first_with_timer" if with_timer else "_spotpear_first_without_timer"]
    source = r"""
#include <cassert>
#include <string>
#include <vector>
#define id(x) (x)
struct Voice { bool running=false; bool is_running() const { return running; } } va;
struct Runtime {
  std::string active;
  std::vector<std::string> events;
  bool is_activity_active(const char *name) const { return active==name; }
  void event(const char *name) { events.emplace_back(name); }
} runtime;
struct Phone {
  int state=0, starts=0, answers=0, stops=0;
  bool is_idle() const { return state==0; }
  bool is_active() const { return state!=0; }
  void call_toggle() {
    if(state==1){++answers;state=2;}
    else if(state!=0){++stops;state=0;}
    else {++starts;state=3;}
  }
} phone;
int current_mode=1;
bool voip_dialer_active=false;
std::vector<std::string> scripts;
void run_script(const char *name){ scripts.emplace_back(name); }
void click() {
""" + actions_cpp(branch) + r"""
}
void reset(int state=2,int page=1){phone={state,0,0,0};runtime={};va={};current_mode=page;voip_dialer_active=false;scripts.clear();}
void expect_voice_only(){
  assert(runtime.events==std::vector<std::string>{"manual_voice_toggle"});
  assert(phone.state==2&&phone.stops==0&&phone.starts==0&&phone.answers==0&&scripts.empty());
}
int main(){
  // Independent expectation: Assist cancel must not mutate an established call.
  for(const char *activity:{"va_start_requested","va_starting","va_listening","va_thinking","va_responding","va_barging","va_stopping"}){
    reset();runtime.active=activity;click();expect_voice_only();
  }
  // Covers actual VA state before the observer event has reached the reducer.
  reset();va.running=true;click();expect_voice_only();
  // Without Assist, all existing phone and page semantics are retained.
  reset();click();assert(phone.state==0&&phone.stops==1&&runtime.events.empty());
  reset(1);click();assert(phone.state==2&&phone.answers==1&&runtime.events.empty());
  reset(0);click();assert(phone.state==3&&phone.starts==1&&runtime.events.empty());
  reset(0);voip_dialer_active=true;click();assert(scripts==std::vector<std::string>{"voip_dial_call"}&&phone.starts==0);
  reset(0,0);click();assert(runtime.events==std::vector<std::string>{"manual_voice_toggle"}&&phone.starts==0);
  // Existing timer-first policy is preserved, even while VA and a call coexist.
  reset();runtime.active="timer_ringing";va.running=true;click();
"""
    if with_timer:
        source += 'assert(runtime.events==std::vector<std::string>{"timer_stopped"}&&phone.state==2&&phone.stops==0);\n'
    else:
        source += 'expect_voice_only();\n'
    source += "}\n"
    cpp = tmp_path / "button.cpp"
    cpp.write_text(source)
    binary = tmp_path / "button"
    result = subprocess.run(
        ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(cpp), "-o", str(binary)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
