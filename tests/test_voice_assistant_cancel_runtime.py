"""Run production cancellation methods and relevant loop branches on the host."""

from pathlib import Path
import os
import subprocess

import pytest

from tests.test_voice_assistant_idle_runtime import block


ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "queued_url_finishes_without_playback_timeout",
    "stop_microphone_keeps_response_owner_until_both_channels_stop",
    "stopping_microphone_keeps_response_owner",
    "playing_waits_for_player_completion",
    "repeated_stop_does_not_duplicate_completion",
    "streaming_tts_still_signals_home_assistant",
    "missing_player_does_not_finish_unowned_response",
    "idle_callback_restart_keeps_new_desired_state",
)


def cancellation_program():
    component = ROOT / "esphome/components/voice_assistant"
    cpp = (component / "voice_assistant.cpp").read_text()
    header = (component / "voice_assistant.h").read_text()
    methods = [
        block(cpp, signature)
        for signature in (
            "void VoiceAssistant::request_stop() {",
            "void VoiceAssistant::cancel_response_() {",
            "void VoiceAssistant::signal_stop_() {",
            "void VoiceAssistant::set_state_(State state) {",
            "void VoiceAssistant::set_state_(State state, State desired_state) {",
            "void VoiceAssistant::set_state_after_mic_stop_(State state) {",
            "void VoiceAssistant::end_session_() {",
            "void VoiceAssistant::request_start(bool continuous, bool silence_detection) {",
        )
    ]
    branches = "\n".join(
        f"case State::{state}: " + block(cpp, f"case State::{state}: {{", False)
        for state in ("STOP_MICROPHONE", "STOPPING_MICROPHONE", "STREAMING_RESPONSE", "RESPONSE_FINISHED")
    )
    enums = "\n".join(block(header, f"enum class {name}") + ";" for name in ("State", "MediaPlayerResponseState"))
    return r'''
#include <cstdint>
#include <cstring>
#include <functional>
#include <iostream>
#include <vector>
#define USE_MEDIA_PLAYER
#define ESP_LOGD(...) discard_log(__VA_ARGS__)
#define ESP_LOGE(...) discard_log(__VA_ARGS__)
#define API_LOG_MSG_DROPPED(...) discard_log(__VA_ARGS__)
#define LOG_STR_ARG(x) (x)
template<typename... Args> void discard_log(Args&&...) {}
constexpr const char *TAG = "voice_assistant";
constexpr uint32_t RUN_END_TIMEOUT_ID=5, RUN_END_TIMEOUT_MS=2000;
constexpr uint32_t PLAYBACK_TIMEOUT_ID=1, PLAYBACK_STALL_TIMEOUT_MS=5000;
''' + enums + r'''
const char *voice_assistant_state_to_string(State) { return "state"; }
namespace api {
struct VoiceAssistantRequest { bool start = true; };
struct VoiceAssistantAnnounceFinished { bool success = false; };
}
namespace media_player { constexpr int MEDIA_PLAYER_COMMAND_STOP=1; }
struct Client {
  unsigned stops=0, finishes=0;
  bool send_message(const api::VoiceAssistantRequest &msg) { stops += !msg.start; return true; }
  bool send_message(const api::VoiceAssistantAnnounceFinished &msg) { finishes += msg.success; return true; }
};
struct Player {
  unsigned stops=0;
  struct Call {
    Player *owner; int command=0; bool announcement=false;
    Call &set_command(int value) { command=value; return *this; }
    Call &set_announcement(bool value) { announcement=value; return *this; }
    void perform() { if (announcement && command==media_player::MEDIA_PLAYER_COMMAND_STOP) ++owner->stops; }
  };
  Call make_call() { return {this}; }
};
struct Mic {
  bool running=true, stopped=false;
  bool is_running() { return running; }
  bool is_stopped() { return stopped; }
  void stop() { running=false; }
};
struct Trigger {
  unsigned count=0; std::function<void()> callback;
  void trigger() { ++count; if(callback) callback(); }
};
struct VoiceAssistant {
  State state_=State::STREAMING_RESPONSE, desired_state_=State::STREAMING_RESPONSE;
  MediaPlayerResponseState media_player_response_state_=MediaPlayerResponseState::URL_SENT;
  bool continuous_=false, continue_conversation_=false, silence_detection_=false;
  bool end_pending_=false, run_active_=false, started_streaming_tts_=false;
  uint8_t dest_addr_[16]{};
  Player player; Player *media_player_=&player;
  Client client; Client *api_client_=&client;
  Mic mic, mic2; Mic *mic_source_=&mic, *mic_source2_=nullptr;
  Trigger idle_trigger_, end_trigger_;
  bool playback_timeout_active=true;
  unsigned watchdogs=0;
  std::vector<std::function<void()>> deferred;
  void request_stop(); void cancel_response_(); void signal_stop_();
  void set_state_(State state); void set_state_(State state, State desired_state);
  void set_state_after_mic_stop_(State state); void end_session_();
  void request_start(bool continuous, bool silence_detection);
  template<class F> void defer(F callback) { deferred.push_back(callback); }
  template<class F> void set_timeout(uint32_t, uint32_t, F) {}
  void cancel_timeout(uint32_t id) { if(id==PLAYBACK_TIMEOUT_ID) playback_timeout_active=false; }
  void start_playback_timeout_(uint32_t) { ++watchdogs; }
  void loop() { switch(state_) {
''' + branches + r'''
    default: break;
  } }
};
''' + "\n".join(methods) + r'''
int main() {
  unsigned failed=0;
  auto check=[&](const char *name, bool ok) { std::cout<<(ok?"PASS ":"FAIL ")<<name<<"\n"; failed+=!ok; };
  {
    VoiceAssistant va; va.request_stop(); va.loop(); va.loop();
    check("queued_url_finishes_without_playback_timeout",va.state_==State::IDLE &&
          va.media_player_response_state_==MediaPlayerResponseState::IDLE &&
          !va.playback_timeout_active && va.client.finishes==1 && va.player.stops==1 && va.idle_trigger_.count==1);
  }
  {
    VoiceAssistant va; va.state_=State::STOP_MICROPHONE; va.mic_source2_=&va.mic2;
    va.request_stop(); va.loop();
    bool waiting=va.state_==State::STOPPING_MICROPHONE && va.desired_state_==State::STREAMING_RESPONSE &&
                 va.client.finishes==0 && va.idle_trigger_.count==0;
    va.mic.stopped=true; va.loop(); waiting &= va.state_==State::STOPPING_MICROPHONE;
    va.mic2.stopped=true; va.loop(); va.loop(); va.loop();
    check("stop_microphone_keeps_response_owner_until_both_channels_stop",waiting &&
          va.state_==State::IDLE && va.client.finishes==1 && !va.playback_timeout_active && va.idle_trigger_.count==1);
  }
  {
    VoiceAssistant va; va.state_=State::STOPPING_MICROPHONE; va.request_stop(); va.loop();
    bool waiting=va.state_==State::STOPPING_MICROPHONE && va.desired_state_==State::STREAMING_RESPONSE;
    va.mic.stopped=true; va.loop(); va.loop(); va.loop();
    check("stopping_microphone_keeps_response_owner",waiting && va.state_==State::IDLE &&
          va.client.finishes==1 && !va.playback_timeout_active);
  }
  {
    VoiceAssistant va; va.media_player_response_state_=MediaPlayerResponseState::PLAYING;
    va.request_stop(); va.loop();
    bool waiting=va.state_==State::STREAMING_RESPONSE && va.client.finishes==0 && va.watchdogs==1;
    // The player boundary reports completion after its asynchronous STOP.
    va.media_player_response_state_=MediaPlayerResponseState::FINISHED; va.loop(); va.loop();
    check("playing_waits_for_player_completion",waiting && va.state_==State::IDLE && va.client.finishes==1);
  }
  {
    VoiceAssistant va; va.request_stop(); va.request_stop(); va.loop(); va.loop(); va.request_stop(); va.loop();
    check("repeated_stop_does_not_duplicate_completion",va.client.finishes==1 && va.idle_trigger_.count==1);
  }
  {
    VoiceAssistant va; va.started_streaming_tts_=true; va.request_stop();
    bool stopped=va.client.stops==1; va.loop(); va.loop();
    check("streaming_tts_still_signals_home_assistant",stopped && va.client.finishes==1 && va.state_==State::IDLE);
  }
  {
    VoiceAssistant va; va.media_player_=nullptr; va.cancel_response_();
    check("missing_player_does_not_finish_unowned_response",va.media_player_response_state_==MediaPlayerResponseState::URL_SENT &&
          va.player.stops==0 && va.client.finishes==0 && va.client.stops==0);
  }
  {
    VoiceAssistant va; va.idle_trigger_.callback=[&]() { va.request_start(false,true); };
    va.request_stop(); va.loop(); va.loop();
    check("idle_callback_restart_keeps_new_desired_state",va.state_==State::START_MICROPHONE &&
          va.desired_state_==State::START_PIPELINE && va.client.finishes==1 && va.idle_trigger_.count==1);
  }
  return failed?1:0;
}
'''


@pytest.fixture(scope="module")
def cancellation_results(tmp_path_factory):
    directory = tmp_path_factory.mktemp("voice-assistant-cancel")
    source = directory / "cancellation.cpp"
    binary = directory / "cancellation"
    source.write_text(cancellation_program())
    compiled = subprocess.run(
        ["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(source), "-o", str(binary)],
        capture_output=True, text=True, timeout=60,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run(
        [str(binary)], capture_output=True, text=True,
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0"}, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == len(CASES), result.stdout
    return lines


@pytest.mark.parametrize("case", CASES)
def test_voice_assistant_cancel_response(cancellation_results, case):
    assert "PASS " + case in cancellation_results
