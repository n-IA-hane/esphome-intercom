"""Run the production UI/connectivity lambdas against lifecycle regressions.

Only ESPHome/LVGL adapters are replaced. Decision bodies are loaded from the
shipped packages, so these checks cannot pass against a separate target model.
"""

from pathlib import Path
import re
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILES = (
    "full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml",
    "experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml",
)


class PackageLoader(yaml.SafeLoader):
    pass


def _tagged(loader, _tag, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


PackageLoader.add_multi_constructor("!", _tagged)


def _load(relative):
    return yaml.load((ROOT / relative).read_text(), Loader=PackageLoader)


def _scripts(package):
    return {item["id"]: item for item in package["script"]}


@pytest.fixture(scope="module")
def regression_binary(tmp_path_factory):
    projection = _scripts(_load("packages/runtime/lvgl_ui_projection.yaml"))
    connectivity = _load("packages/runtime/ha_connectivity.yaml")
    presence = _scripts(connectivity)
    voip_callbacks = _load("packages/voip/full_runtime_callbacks.yaml")["voip_stack"]
    substitutions = _load("packages/runtime/presentation_globals.yaml")["substitutions"]
    substitutions |= _load("yamls/" + PROFILES[0])["substitutions"]

    def expand(source):
        return re.sub(r"\$\{([^}]+)\}", lambda match: substitutions[match[1]], source)

    phase = expand(projection["runtime_project_ui_phase"]["then"][0]["lambda"])
    begin = projection["runtime_ui_call_begin"]["then"]
    assert begin[0] == {"script.stop": "ui_call_ended"}
    end = projection["runtime_ui_call_end"]["then"][0]["lambda"]
    update_ha = presence["runtime_update_ha_presence"]["then"][0]["lambda"]
    reconcile = presence["runtime_reconcile_ha_presence"]["then"]
    assert reconcile[0] == {"delay": "250ms"}
    disconnected = reconcile[1]["if"]
    assert (
        disconnected["then"][0]["runtime_controller.event"]["event"]
        == "ha_disconnected"
    )
    ha_empty = disconnected["condition"]["lambda"]
    for name, value in (
        ("on_client_connected", True),
        ("on_client_disconnected", False),
    ):
        action = connectivity["api"][name][0]["script.execute"]
        assert action["id"] == "runtime_update_ha_presence"
        assert action["connected"] is value
    terminal_guards = {}
    for callback in ("on_hangup", "on_call_failed"):
        guarded = voip_callbacks[callback][0]["if"]
        assert guarded["then"][0]["script.execute"]["id"] == "ui_call_ended"
        terminal_guards[callback] = guarded["condition"]["lambda"]

    constants = "\n".join(
        f"constexpr int {key} = {value};"
        for key, value in substitutions.items()
        if key.startswith("ui_state_") or key.startswith("voice_assist_")
    )
    source = r"""
#include <cassert>
#include <string>
#include <vector>
#define id(name) name
int ui_state=0, voice_assistant_phase=0, current_mode=0, previous_mode=0;
int runtime_visual_source=0, g_ha_api_clients=0;
bool runtime_response_text_ready=false, runtime_ui_call_session_active=false;
bool call_ended_active=false;
struct Switch { bool state=false; } mute;
struct Phone { bool active=false; bool is_active() const { return active; } } phone;
struct Script {
  int starts=0, stops=0; bool running=false;
  void execute() { ++starts; running=true; }
  void stop() { ++stops; running=false; }
  bool is_running() const { return running; }
};
Script apply_media_artwork, ui_call_ended, runtime_reconcile_ha_presence;
struct Runtime {
  std::vector<std::string> events;
  void event(const char *name) { events.emplace_back(name); }
} runtime;
int background=0, neutral=1; int *mood_bg=&background, *mood_neutral=&neutral;
void lv_img_set_src(int *target, int *source) { *target=*source; }
void lv_obj_invalidate(int *) {}
"""
    source += constants
    source += "\nvoid project() {\n" + phase + "\n}\n"
    source += (
        "void call_begin() { ui_call_ended.stop();\n" + begin[1]["lambda"] + "\n}\n"
    )
    source += "void call_end() {\n" + end + "\n}\n"
    source += (
        "void update_ha(std::string client_info, bool connected) {\n"
        + update_ha
        + "\n}\n"
    )
    source += "bool ha_empty() {\n" + ha_empty + "\n}\n"
    for callback, guard in terminal_guards.items():
        source += f"bool accept_{callback}() {{\n{guard}\n}}\n"
    source += r"""
void expire_ha_grace() {
  if (!runtime_reconcile_ha_presence.running) return;
  runtime_reconcile_ha_presence.running=false;
  if (ha_empty()) runtime.event("ha_disconnected");
}
int main(int argc, char **argv) {
  assert(argc==2);
  const std::string test=argv[1];
  if (test=="projection") {
    // Completion and reconnect project their current snapshot, even when the
    // previous local phase was replying. No media-state reconstruction here.
    ui_state=ui_state_va_replying; project();
    assert(voice_assistant_phase==voice_assist_replying_phase_id);
    ui_state=ui_state_idle; runtime_response_text_ready=true; project();
    assert(voice_assistant_phase==voice_assist_idle_phase_id);
    assert(!runtime_response_text_ready && background==neutral);
    ui_state=ui_state_no_va; project();
    assert(voice_assistant_phase==voice_assist_not_ready_phase_id);
    ui_state=ui_state_va_listening; current_mode=1; project();
    assert(voice_assistant_phase==voice_assist_listening_phase_id && current_mode==0);
    ui_state=ui_state_va_thinking; project();
    assert(voice_assistant_phase==voice_assist_thinking_phase_id);
    // A cleanup/projection request cannot turn a new listening/thinking turn idle.
    project(); assert(voice_assistant_phase==voice_assist_thinking_phase_id);
    ui_state=ui_state_spk_muted; project();
    assert(voice_assistant_phase==voice_assist_idle_phase_id);
    ui_state=ui_state_media; project();
    assert(apply_media_artwork.starts==1);
    mute.state=true; project();
    assert(voice_assistant_phase==voice_assist_muted_phase_id);
  } else if (test=="call") {
    current_mode=0; previous_mode=1; call_ended_active=true; ui_call_ended.running=true;
    call_begin(); assert(previous_mode==0 && current_mode==1);
    assert(!call_ended_active && !ui_call_ended.running);
    // Calling -> remote ringing -> in call must preserve the original mode.
    call_begin(); call_begin(); assert(previous_mode==0);
    call_end(); assert(current_mode==0 && !runtime_ui_call_session_active);
    call_ended_active=true; ui_call_ended.running=true;
    // New call invalidates the old delayed overlay; a user-selected call mode
    // also survives the next call's completion.
    current_mode=1; call_begin(); assert(previous_mode==1 && !call_ended_active);
    assert(!ui_call_ended.running);
    call_end(); call_end(); assert(current_mode==1);
  } else if (test=="ha") {
    update_ha("ESPHome Logs 2026.9",true);
    update_ha("ESPHome Dashboard",true);
    update_ha("ESPHome Web",true);
    update_ha("",true);
    assert(g_ha_api_clients==0 && runtime.events.empty());
    update_ha("Home Assistant 2026.9",true);
    update_ha("Home Assistant secondary",true);
    assert(g_ha_api_clients==2 && runtime.events.back()=="ha_connected");
    update_ha("Home Assistant 2026.9",false); expire_ha_grace();
    assert(g_ha_api_clients==1 && runtime.events.back()=="ha_connected");
    // The log client is still connected; the final HA disconnect must win.
    update_ha("Home Assistant secondary",false); expire_ha_grace();
    assert(g_ha_api_clients==0 && runtime.events.back()=="ha_disconnected");
    const auto before=runtime.events.size();
    update_ha("ESPHome Logs 2026.9",false); expire_ha_grace();
    assert(runtime.events.size()==before);
    update_ha("Home Assistant 2026.9",true);
    update_ha("Home Assistant 2026.9",false);
    update_ha("Home Assistant 2026.9",true); expire_ha_grace();
    assert(g_ha_api_clients==1 && runtime.events.back()=="ha_connected");
  } else if (test=="late_call_end") {
    current_mode=0; call_begin(); phone.active=true;
    // An old deferred hangup/failure must not restore mode or create an Ended
    // overlay after the next call has become active.
    if (accept_on_hangup()) call_end();
    if (accept_on_call_failed()) call_end();
    assert(current_mode==1 && runtime_ui_call_session_active);
    phone.active=false;
    assert(accept_on_hangup() && accept_on_call_failed());
    call_end(); assert(current_mode==0 && !runtime_ui_call_session_active);
  } else { return 2; }
}
"""
    work = tmp_path_factory.mktemp("runtime_ui_connectivity")
    cpp = work / "regressions.cpp"
    cpp.write_text(source)
    binary = work / "regressions"
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(cpp),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize("scenario", ["projection", "call", "ha", "late_call_end"])
def test_runtime_lifecycle_regressions(regression_binary, scenario):
    subprocess.run(
        [str(regression_binary), scenario], check=True, capture_output=True, text=True
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_lvgl_profiles_delegate_semantics_to_common_adapter(profile):
    config = _load("yamls/" + profile)
    scripts = _scripts(config)
    assert "lvgl_ui_projection.yaml" in config["packages"]["runtime_ui_projection"]
    assert scripts["render_ui_state"]["then"][0] == {
        "script.execute": "runtime_project_ui_phase"
    }
    assert scripts["ui_call_started"]["then"][0] == {
        "script.execute": "runtime_ui_call_begin"
    }
    assert scripts["ui_call_ended"]["then"][0] == {
        "script.execute": "runtime_ui_call_end"
    }

    # Hooks may update response content, but no board automation may assign a
    # semantic VA phase or resurrect MWW from a screen redraw.
    actions = yaml.safe_dump(config["script"])
    assert not re.search(r"id\(voice_assistant_phase\)\s*=(?!=)", actions)
    assert not re.search(r"globals\.set:\n\s+id: voice_assistant_phase", actions)
    assert "start_mww_when_ready" not in yaml.safe_dump(scripts["draw_display"])
    assert "api::global_api_server->is_connected()" not in yaml.safe_dump(
        scripts["draw_display"]
    )
    assert "set_idle_or_mute_phase" not in scripts
