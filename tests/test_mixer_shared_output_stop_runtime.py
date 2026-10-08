"""A forced shared-output stop must not strand another mixer source's credits."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _method(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def test_paused_output_stop_does_not_strand_other_source(tmp_path):
    source = (ROOT / 'esphome/components/mixer/speaker/mixer_speaker.cpp').read_text()
    code = r'''
#include "playback_timeline.h"
#include <cassert>
#include <cstdint>
#include <memory>
#include <optional>
#include <cstdio>
#include <vector>
using esphome::mixer_speaker::PlaybackTimeline;
namespace speaker {enum State {STATE_STOPPED,STATE_STARTING,STATE_RUNNING,STATE_STOPPING};}
using esp_err_t=int;
constexpr int ESP_OK=0,ESP_ERR_NO_MEM=1,ESP_ERR_NOT_SUPPORTED=2,ESP_ERR_INVALID_ARG=3,ESP_ERR_INVALID_STATE=4;
constexpr unsigned SOURCE_SPEAKER_COMMAND_STOP=1,SOURCE_SPEAKER_COMMAND_FINISH=2,SOURCE_SPEAKER_COMMAND_START=4;
constexpr unsigned STOPPING_TIMEOUT_MS=5000;
unsigned now=1000;
unsigned millis(){return now;}
unsigned xEventGroupGetBits(unsigned *p){return *p;}
void xEventGroupClearBits(unsigned *p,unsigned n){*p&=~n;}
void xEventGroupSetBits(unsigned *p,unsigned n){*p|=n;}
constexpr unsigned MIXER_TASK_COMMAND_STOP=2,MIXER_TASK_COMMAND_START=1;
struct Mutex{};struct LockGuard {explicit LockGuard(Mutex&) {}};
#define LOG_STR(x) x
struct SourceSpeaker;
struct Output {
 bool paused=false,stopped=false;unsigned queued=200,discarded=0,stops=0;
 bool get_pause_state(){return paused;}
 bool is_stopped(){return stopped;}
 void stop(){++stops;stopped=true;discarded+=queued;queued=0;}
};
struct Parent {
 Output output;Output *output_speaker_=&output;bool restarting_output_=false;
 struct Task {bool is_created(){return true;}} task_;
 unsigned bits=0,*event_group_=&bits;
 std::atomic<unsigned> frames_in_pipeline_{200};Mutex playback_mutex_;
 std::vector<SourceSpeaker*> source_speakers_;
 void enable_loop_soon_any_context(){}
 void stop_output_();void complete_worker_stop();
 Output *get_output_speaker(){return &output;}
 void reset_source_playback_(SourceSpeaker *source);
};
using MixerSpeaker=Parent;
struct AudioSource {bool buffered=false;bool has_buffered_data(){return buffered;}};
struct SourceSpeaker {
 Parent *parent_;PlaybackTimeline playback_;
 speaker::State state_=speaker::STATE_RUNNING;
 unsigned bits=0,*event_group_=&bits,last_seen_data_ms_=0,stopping_start_ms_=0;
 bool stop_gracefully_=false,disabled=false;
 std::optional<unsigned> timeout_ms_{10000};
 std::unique_ptr<AudioSource> audio_source_=std::make_unique<AudioSource>();
 esp_err_t start_(){return ESP_OK;}
 void status_clear_error(){}void status_set_error(const char*){}void disable_loop(){disabled=true;}
 void loop();void enter_stopping_state_();
};
void Parent::reset_source_playback_(SourceSpeaker *s){s->playback_.clear();}
''' + _method(source, 'void MixerSpeaker::stop_output_()') + '\n' + _method(source, 'void SourceSpeaker::loop()') + '\n' + _method(source, 'void SourceSpeaker::enter_stopping_state_()') + r'''
int main(){
 Parent parent;SourceSpeaker media{&parent},announcement{&parent};
 parent.source_speakers_={&media,&announcement};
 // Both sources contributed to the same 200 hardware-bound frames.
 media.playback_.append(0,200);announcement.playback_.append(0,200);
 // Explicit output pause is a supported speaker operation, not a lost IRQ.
 // STOP on one source invokes the production force-stop path while paused.
 parent.output.paused=true;media.bits=SOURCE_SPEAKER_COMMAND_STOP;
 media.loop();assert(parent.output.stops==0);
 assert((parent.bits & (MIXER_TASK_COMMAND_STOP|MIXER_TASK_COMMAND_START))==3);
 parent.complete_worker_stop();parent.output.stop();media.loop();
 assert(parent.output.stops==1 && parent.output.discarded==200);
 assert(media.state_==speaker::STATE_STOPPED);
 // The completed announcement requests FINISH after its source has gone idle.
 announcement.bits=SOURCE_SPEAKER_COMMAND_FINISH;
 for(unsigned i=0;i<3;++i){now+=20000;announcement.loop();}
 std::printf("discarded=%u announcement_state=%d pending=%u buffered=%d\n",parent.output.discarded,
             announcement.state_,announcement.playback_.pending_frames(),announcement.audio_source_ ? announcement.audio_source_->has_buffered_data() : false);
 // The hardware has discarded these frames. No valid playback completion can
 // ever arrive for them; the other source must reconcile and reach STOPPED.
 assert(announcement.state_==speaker::STATE_STOPPED);
 assert(announcement.playback_.pending_frames()==0 && parent.frames_in_pipeline_==0);
 // Buffered input on another source survives output cleanup and remains active.
 Parent other;SourceSpeaker stopping{&other},buffered{&other};
 other.source_speakers_={&stopping,&buffered};
 stopping.playback_.append(0,200);buffered.playback_.append(0,200);
 buffered.audio_source_->buffered=true;
 other.output.paused=true;stopping.bits=SOURCE_SPEAKER_COMMAND_STOP;stopping.loop();
 other.complete_worker_stop();other.output.stop();buffered.bits=SOURCE_SPEAKER_COMMAND_FINISH;buffered.loop();
 assert(buffered.state_==speaker::STATE_RUNNING && buffered.audio_source_->has_buffered_data());
 assert(buffered.playback_.pending_frames()==0);

}
'''
    cleanup_start = source.index('  // Reset pipeline frame count since the task is stopping')
    cleanup_end = source.index('  xEventGroupSetBits(this_mixer->event_group_, MIXER_TASK_STATE_STOPPED)', cleanup_start)
    cleanup = source[cleanup_start:cleanup_end]
    code += '\nvoid Parent::complete_worker_stop(){auto *this_mixer=this;\n' + cleanup + '\n}\n'
    cpp = tmp_path / 'shared_stop.cpp'
    binary = tmp_path / 'shared_stop'
    cpp.write_text(code)
    subprocess.run(['g++', '-std=c++17', '-I', str(ROOT / 'esphome/components/mixer/speaker'), str(cpp), '-o', str(binary)], check=True, capture_output=True, text=True)
    result = subprocess.run(['bash', '-c', 'ulimit -c 0; exec "$1"', 'bash', str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
