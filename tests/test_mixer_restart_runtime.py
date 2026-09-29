"""Run production mixer cleanup with a start arriving during worker shutdown."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_pending_start_survives_stopped_task_cleanup(tmp_path):
    text = (ROOT / "esphome/components/mixer/speaker/mixer_speaker.cpp").read_text()
    start = text.index("void MixerSpeaker::loop()")
    method = text[start:text.index("esp_err_t MixerSpeaker::start(", start)]
    source = r'''
#include <cassert>
#include <cstdint>
#include <functional>
#include <vector>
#define ESP_LOGD(...) ((void)0)
#define ESP_LOGV(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define LOG_STR(x) x
constexpr uint32_t MIXER_TASK_COMMAND_START=1, MIXER_TASK_COMMAND_STOP=2;
constexpr uint32_t MIXER_TASK_STATE_STARTING=1<<10, MIXER_TASK_STATE_RUNNING=1<<11;
constexpr uint32_t MIXER_TASK_STATE_STOPPING=1<<12, MIXER_TASK_STATE_STOPPED=1<<13;
constexpr uint32_t MIXER_TASK_ERR_ESP_NO_MEM=1<<19, MIXER_TASK_ALL_BITS=0x00FFFFFF;
constexpr unsigned TASK_STACK_SIZE=4096, MIXER_TASK_PRIORITY=10, MIXER_AUTO_STOP_DEBOUNCE_MS=200;
uint32_t millis(){return 1000;}
uint32_t xEventGroupGetBits(uint32_t* bits){return *bits;}
void xEventGroupClearBits(uint32_t* bits,uint32_t mask){*bits &= ~mask;}
void xEventGroupSetBits(uint32_t* bits,uint32_t mask){*bits |= mask;}
struct Task {
 bool created=true, reap_ready=true;unsigned starts=0;std::function<void()> before_reap;
 bool is_created(){return created;}
 bool deallocate(){if(!reap_ready)return false;if(before_reap)before_reap();created=false;return true;}
 template<typename... Args>bool create(Args...){created=true;++starts;return true;}
};
struct Source {bool is_stopped(){return false;}};
struct MixerSpeaker {
 uint32_t bits=MIXER_TASK_STATE_STOPPED;uint32_t* event_group_=&bits;
 Task task_;bool task_stack_in_psram_=true,disabled=false,error=false;
 uint32_t all_stopped_since_ms_=0;Source source;std::vector<Source*> source_speakers_{&source};
 static void audio_mixer_task(void*){};
 bool status_has_error(){return error;}void status_clear_error(){error=false;}
 void status_set_error(const char*){error=true;}void status_momentary_error(const char*,int){}
 void disable_loop(){disabled=true;}void loop();
};
''' + method + r'''
int main(){
 for(int arrival=0;arrival<2;++arrival){
  MixerSpeaker mixer;
  if(arrival==0)mixer.bits |= MIXER_TASK_COMMAND_START;
  else mixer.task_.before_reap=[&]{mixer.bits |= MIXER_TASK_COMMAND_START;};
  mixer.loop();
  assert(!mixer.task_.created);
  assert(mixer.bits & MIXER_TASK_COMMAND_START);
  assert(!mixer.disabled);
  mixer.loop();
  assert(mixer.task_.created && mixer.task_.starts==1);
  assert(!(mixer.bits & MIXER_TASK_COMMAND_START));
 }
 MixerSpeaker idle;idle.loop();assert(idle.disabled && !idle.task_.created && idle.task_.starts==0);
 MixerSpeaker waiting;waiting.bits |= MIXER_TASK_COMMAND_START;waiting.task_.reap_ready=false;
 waiting.loop();assert(waiting.task_.created && waiting.task_.starts==0 && (waiting.bits&MIXER_TASK_COMMAND_START));
 waiting.task_.reap_ready=true;waiting.loop();waiting.loop();assert(waiting.task_.starts==1);
}
'''
    cpp = tmp_path / "mixer_restart.cpp"
    binary = tmp_path / "mixer_restart"
    cpp.write_text(source)
    subprocess.run(["g++", "-std=c++17", str(cpp), "-o", str(binary)], check=True, capture_output=True, text=True)
    result = subprocess.run(["bash", "-c", 'ulimit -c 0; exec "$1"', "bash", str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
