"""Execute the package predicates against independent timer/ringtone scenarios."""

from pathlib import Path
import subprocess

from tests.test_runtime_ui_connectivity import _load, _scripts, PackageLoader
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_timer_cancellation_and_announcement_priority(tmp_path):
    timer_events = _load("packages/voice_assistant/timer_events.yaml")[
        "voice_assistant"
    ]
    remaining = timer_events["on_timer_cancelled"][0][
        "runtime_controller.set_activity"
    ]["active"]
    finished = timer_events["on_timer_finished"][0]["runtime_controller.set_activity"][
        "active"
    ]
    assert remaining == finished
    timer = _scripts(_load("packages/runtime/timer_alarm_orchestration.yaml"))[
        "timer_alarm_loop"
    ]
    play_guard = timer["then"][0]["while"]["then"][0]["if"]["condition"]["lambda"]
    controller = (
        ROOT.parent
        / "esphome-runtime-controller/packages/runtime_controller/timers.yaml"
    )
    policy = yaml.load(controller.read_text(), Loader=PackageLoader)[
        "runtime_controller"
    ]["policies"]["timer_alarm"]
    stop_guard = policy["values"]["stop"]["then"][-1]["if"]["condition"]["lambda"]
    source = r"""
#include <cassert>
#include <string>
#include <vector>
#define id(x) (x)
struct Timer { std::string id; } timer;
struct Voice { std::vector<Timer> timers; const auto &get_timers() const { return timers; } } va;
struct Runtime {
 bool ringing=false, draining=false, voice=false;
 const char *get_policy(const char *) const { return ringing ? "play" : "stop"; }
 bool is_activity_active(const char *name) const {
   return std::string(name)=="ringtone_draining" ? draining : voice;
 }
} runtime;
"""
    source += "bool remaining() {\n" + remaining + "\n}\n"
    source += "bool can_play() {\n" + play_guard + "\n}\n"
    source += "bool can_stop() {\n" + stop_guard + "\n}\n"
    source += r"""
int main() {
 timer.id="a"; va.timers={{"a"}}; assert(!remaining());
 va.timers={{"a"},{"b"}}; assert(remaining());
 for (int mask=0;mask<8;mask++) {
   runtime.ringing=mask&1;runtime.draining=mask&2;runtime.voice=mask&4;
   assert(can_play()==(!(mask&1) && !(mask&2)));
   assert(can_stop()==(mask==0));
 }
}
"""
    cpp = tmp_path / "ownership.cpp"
    cpp.write_text(source)
    binary = tmp_path / "ownership"
    subprocess.run(
        ["g++", "-std=c++17", "-Wall", "-Werror", str(cpp), "-o", str(binary)],
        check=True,
        capture_output=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True)


def test_ringtone_stop_releases_player_playlist():
    stop = _scripts(_load("packages/runtime/voip_ringtone_orchestration.yaml"))[
        "voip_stop_ringtone"
    ]
    owned_stop = stop["then"][0]["if"]
    assert "ringtone_draining" in owned_stop["condition"]["lambda"]
    assert owned_stop["then"][0] == {"script.stop": "voip_ringing_loop"}
    assert owned_stop["then"][1] == {
        "media_player.stop": {"id": "speaker_media_player", "announcement": True}
    }
