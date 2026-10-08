"""Run production mixer cleanup with a start arriving during worker shutdown."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_pending_start_survives_stopped_task_cleanup(tmp_path):
    text = (ROOT / "esphome/components/mixer/speaker/mixer_speaker.cpp").read_text()
    start = text.index("void MixerSpeaker::loop()")
    method = text[start:text.index("esp_err_t MixerSpeaker::start(", start)]
    start_method = text[text.index("esp_err_t MixerSpeaker::start("):text.index("// NOLINTBEGIN", text.index("esp_err_t MixerSpeaker::start("))]
    source = r'''
#include <cassert>
#include <cstdint>
#include <functional>
#include <vector>
#include <optional>
using esp_err_t=int;constexpr int ESP_OK=0,ESP_ERR_INVALID_ARG=1;
namespace audio {struct AudioStreamInfo {AudioStreamInfo(unsigned=16,unsigned=1,unsigned=48000){}unsigned get_sample_rate(){return 48000;}};}
struct AppStub {void wake_loop_threadsafe(){}} App;
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
struct Output {bool stopped=true;unsigned stops=0;bool is_stopped(){return stopped;}void stop(){++stops;stopped=true;}void set_audio_stream_info(audio::AudioStreamInfo){}};
struct MixerSpeaker {
 uint32_t bits=MIXER_TASK_STATE_STOPPED;uint32_t* event_group_=&bits;
 Output output;Output *output_speaker_=&output;bool restarting_output_=false;
 std::optional<audio::AudioStreamInfo> audio_stream_info_;bool queue_mode_=false;unsigned output_bits_per_sample_=16,output_channels_=1;
 void enable_loop_soon_any_context(){}esp_err_t start(audio::AudioStreamInfo&);
 Task task_;bool task_stack_in_psram_=true,disabled=false,error=false;
 uint32_t all_stopped_since_ms_=0;Source source;std::vector<Source*> source_speakers_{&source};
 static void audio_mixer_task(void*){};
 bool status_has_error(){return error;}void status_clear_error(){error=false;}
 void status_set_error(const char*){error=true;}void status_momentary_error(const char*,int){}
 void disable_loop(){disabled=true;}void loop();
};
''' + method + start_method + r'''
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
 MixerSpeaker draining;draining.restarting_output_=true;draining.task_.reap_ready=false;
 draining.loop();assert(draining.output.stops==0);
 draining.task_.reap_ready=true;draining.loop();assert(draining.output.stops==1 && !draining.task_.created);
 MixerSpeaker forced;forced.task_.created=false;forced.bits=MIXER_TASK_COMMAND_START;
 forced.restarting_output_=true;forced.output.stopped=false;forced.loop();
 assert(!forced.task_.created && forced.restarting_output_);
 forced.output.stopped=true;forced.loop();
 assert(forced.task_.created && !forced.restarting_output_);
 audio::AudioStreamInfo info;
 MixerSpeaker cleanup;cleanup.restarting_output_=true;cleanup.bits=MIXER_TASK_COMMAND_STOP;
 assert(cleanup.start(info)==ESP_OK && (cleanup.bits&MIXER_TASK_COMMAND_STOP));
 MixerSpeaker idle_stop;idle_stop.bits=MIXER_TASK_COMMAND_STOP;
 assert(idle_stop.start(info)==ESP_OK && !(idle_stop.bits&MIXER_TASK_COMMAND_STOP));
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
