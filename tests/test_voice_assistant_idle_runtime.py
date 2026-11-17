"""Execute the exact VA transition methods in a focused host class, not full VA."""
from pathlib import Path
import os
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "noncontinuous_real_idle_transition_once",
    "repeated_idle_and_loop_no_extra_callback",
    "continuous_existing_idle_loop_unchanged",
    "idle_callback_new_run_not_overwritten",
    "initial_idle_does_not_create_completion",
    "nonterminal_states_do_not_emit_idle",
    "official_end_is_deferred_after_idle",
    "official_run_end_wait_is_preserved",
    "official_end_checks_client_when_dispatched",
)


def block(text, marker, include_signature=True):
    start = text.index(marker)
    opening = text.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start if include_signature else opening:end]


def transition_program():
    component = ROOT / "esphome/components/voice_assistant"
    cpp = (component / "voice_assistant.cpp").read_text()
    header = (component / "voice_assistant.h").read_text()
    methods = [block(cpp, "void VoiceAssistant::set_state_(State state) {"),
               block(cpp, "void VoiceAssistant::set_state_(State state, State desired_state) {"),
               block(cpp, "void VoiceAssistant::end_session_() {"),
               block(cpp, "void VoiceAssistant::request_start(bool continuous, bool silence_detection) {")]
    idle = block(cpp, "case State::IDLE: {", False)
    enum = block(header, "enum class State") + ";"
    source='''#include <cstdint>
    #include <functional>
    #include <iostream>
    #include <string>
    #include <vector>
    #define ESP_LOGD(...) discard_log(__VA_ARGS__)
    #define ESP_LOGE(...) discard_log(__VA_ARGS__)
    #define LOG_STR_ARG(x) (x)
    constexpr const char *TAG="voice_assistant";
    constexpr uint32_t RUN_END_TIMEOUT_ID=5, RUN_END_TIMEOUT_MS=2000;
    template<typename... Args> void discard_log(Args&&...) {}
    '''+enum+'''
    const char *voice_assistant_state_to_string(State) { return "state"; }
    struct Trigger { unsigned count=0; std::function<void()> callback; void trigger() { ++count; if (callback) callback(); } };
    class VoiceAssistant {
     public:
      State state_=State::IDLE, desired_state_=State::IDLE;
      bool continuous_=false, silence_detection_=false;
      bool end_pending_=false, run_active_=false;
      void *api_client_=this;
      unsigned releases=0;
      Trigger idle_trigger_, end_trigger_;
      std::vector<std::function<void()>> deferred;
      std::function<void()> timeout;
      uint32_t timeout_id=0, timeout_ms=0;
      void set_state_(State state);
      void set_state_(State state, State desired_state);
      void end_session_();
      void request_start(bool continuous, bool silence_detection);
      template<class F>void defer(F callback) { deferred.push_back(callback); }
      template<class F>void set_timeout(uint32_t id,uint32_t ms,F callback) {
        timeout_id=id; timeout_ms=ms; timeout=callback;
      }
      void flush_deferred() { auto callbacks=std::move(deferred); for(auto &callback:callbacks)callback(); }
      void deallocate_buffers_() { ++releases; }
      void idle_loop() { switch (state_) { case State::IDLE: '''+idle+''' default: break; } }
    };
    '''+ '\n'.join(methods)+'''
    int main() {
     unsigned failed=0;
     auto check=[&](const char *name,bool ok) { std::cout<<(ok?"PASS ":"FAIL ")<<name<<"\\n"; failed+=!ok; };
     {
      VoiceAssistant va; va.state_=va.desired_state_=State::RESPONSE_FINISHED;
      va.set_state_(State::IDLE,State::IDLE);
      check("noncontinuous_real_idle_transition_once",va.idle_trigger_.count==1 && va.state_==State::IDLE);
     }
     {
      VoiceAssistant va; va.state_=va.desired_state_=State::RESPONSE_FINISHED;
      va.set_state_(State::IDLE,State::IDLE); unsigned first=va.idle_trigger_.count;
      va.set_state_(State::IDLE,State::IDLE); va.idle_loop(); va.idle_loop();
      check("repeated_idle_and_loop_no_extra_callback",first==1 && va.idle_trigger_.count==1);
     }
     {
      VoiceAssistant va; va.continuous_=true; va.state_=va.desired_state_=State::RESPONSE_FINISHED;
      va.set_state_(State::IDLE,State::IDLE); bool before=va.idle_trigger_.count==0;
      va.idle_loop();
      check("continuous_existing_idle_loop_unchanged",before && va.idle_trigger_.count==1 && va.state_==State::START_MICROPHONE && va.desired_state_==State::START_PIPELINE);
     }
     {
      VoiceAssistant va; va.state_=va.desired_state_=State::RESPONSE_FINISHED;
      va.idle_trigger_.callback=[&]() { va.request_start(false,true); };
      va.set_state_(State::IDLE,State::IDLE);
      check("idle_callback_new_run_not_overwritten",va.idle_trigger_.count==1 && va.state_==State::START_MICROPHONE && va.desired_state_==State::START_PIPELINE);
     }
     {
      VoiceAssistant va; va.set_state_(State::IDLE,State::IDLE);va.idle_loop();
      check("initial_idle_does_not_create_completion",va.idle_trigger_.count==0);
     }
     {
      VoiceAssistant va;va.set_state_(State::START_MICROPHONE,State::START_PIPELINE);va.set_state_(State::STREAMING_RESPONSE,State::STREAMING_RESPONSE);
      check("nonterminal_states_do_not_emit_idle",va.idle_trigger_.count==0);
     }
     {
      VoiceAssistant va; va.state_=va.desired_state_=State::RESPONSE_FINISHED; va.end_pending_=true;
      va.set_state_(State::IDLE,State::IDLE);
      bool deferred=va.idle_trigger_.count==1 && va.end_trigger_.count==0 && !va.end_pending_;
      va.flush_deferred();
      va.set_state_(State::IDLE,State::IDLE); va.flush_deferred();
      check("official_end_is_deferred_after_idle",deferred && va.end_trigger_.count==1 && va.idle_trigger_.count==1);
     }
     {
      VoiceAssistant va; va.state_=va.desired_state_=State::RESPONSE_FINISHED;
      va.end_pending_=true; va.run_active_=true;
      va.set_state_(State::IDLE,State::IDLE); va.flush_deferred();
      bool waiting=va.idle_trigger_.count==1 && va.end_trigger_.count==0 && va.end_pending_ &&
                   va.timeout_id==5 && va.timeout_ms==2000 && bool(va.timeout);
      if(va.timeout)va.timeout();
      va.flush_deferred();
      check("official_run_end_wait_is_preserved",waiting && va.end_trigger_.count==1 && !va.end_pending_);
     }
     {
      VoiceAssistant va; va.state_=va.desired_state_=State::RESPONSE_FINISHED; va.end_pending_=true;
      va.idle_trigger_.callback=[&]() { va.api_client_=nullptr; };
      va.set_state_(State::IDLE,State::IDLE); va.flush_deferred();
      check("official_end_checks_client_when_dispatched",va.idle_trigger_.count==1 && va.end_trigger_.count==0);
     }
     return failed?1:0;
    }
    '''
    return source


@pytest.fixture(scope="module")
def transition_results(tmp_path_factory):
    directory = tmp_path_factory.mktemp("voice-assistant-idle")
    source = directory / "transitions.cpp"
    binary = directory / "transitions"
    source.write_text(transition_program())
    built = subprocess.run(
        ["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(source), "-o", str(binary)],
        capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True,
                            env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0"}, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == len(CASES), result.stdout
    return lines


@pytest.mark.parametrize("case", CASES)
def test_voice_assistant_idle_transition(transition_results, case):
    assert "PASS " + case in transition_results
