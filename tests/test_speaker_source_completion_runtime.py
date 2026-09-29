"""Completion callbacks during a blocking speaker write must remain accounted."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_completion_during_partial_write(tmp_path):
    source = (ROOT / "esphome/components/speaker_source/speaker_source_media_player.cpp").read_text()

    def method(signature):
        start = source.index(signature)
        opening = source.index("{", start)
        depth, end = 1, opening + 1
        while depth:
            depth += (source[end] == "{") - (source[end] == "}")
            end += 1
        return source[start:end]

    stub = r'''
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstddef>
#include <cassert>
#include <functional>
#include <cstdio>
void vTaskDelay(unsigned) {}
unsigned pdMS_TO_TICKS(unsigned x) {return x;}
namespace audio {struct AudioStreamInfo {
  uint32_t bytes_to_frames(size_t n) const {return n/2;}
  bool operator!=(const AudioStreamInfo&)const{return false;}
};}
namespace media_source {struct MediaSource {
  uint32_t completed=0;
  void notify_audio_played(uint32_t n,int64_t){completed+=n;}
};}
struct Sink {
  size_t accept=0; std::function<void()> during_write;
  audio::AudioStreamInfo get_audio_stream_info(){return {};}
  void set_audio_stream_info(const audio::AudioStreamInfo&){}
  size_t play(const uint8_t*,size_t,unsigned){during_write();return accept;}
};
struct PipelineContext {
  std::atomic<media_source::MediaSource*> active_source{nullptr};
  std::atomic<uint32_t> pending_frames{0}; Sink *speaker;
};
struct SpeakerSourceMediaPlayer {
 PipelineContext pipelines_[1];
 void handle_speaker_playback_callback_(uint32_t,int64_t,uint8_t);
 size_t handle_media_output_(uint8_t,media_source::MediaSource*,const uint8_t*,size_t,uint32_t,const audio::AudioStreamInfo&);
};
'''
    checks = r'''
int main() {
 for(unsigned accepted:{0u,60u,100u}) {
  SpeakerSourceMediaPlayer p;media_source::MediaSource src;Sink sink;
  p.pipelines_[0].active_source=&src;p.pipelines_[0].speaker=&sink;
  unsigned early=accepted/2;sink.accept=accepted*2;
  sink.during_write=[&]{p.handle_speaker_playback_callback_(early,100,0);};
  uint8_t data[200]{};
  assert(p.handle_media_output_(0,&src,data,200,20,{})==accepted*2);
  p.handle_speaker_playback_callback_(accepted-early,200,0);
  std::printf("accepted=%u completed=%u pending=%u\n",accepted,src.completed,p.pipelines_[0].pending_frames.load());
  assert(src.completed==accepted);
  assert(p.pipelines_[0].pending_frames==0);
 }
}
'''

    code = stub + method("void SpeakerSourceMediaPlayer::handle_speaker_playback_callback_")
    code += "\n" + method("size_t SpeakerSourceMediaPlayer::handle_media_output_") + checks
    cpp = tmp_path / "completion.cpp"
    cpp.write_text(code)
    binary = tmp_path / "completion"
    subprocess.run(
        ["g++", "-std=c++20", str(cpp), "-o", str(binary)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["bash", "-c", 'ulimit -c 0; exec "$1"', "bash", str(binary)],
        check=True, capture_output=True, text=True,
    )


def test_queue_snapshot_reads_state_without_mutating_or_logging_urls(tmp_path):
    """The diagnostic reports the blocked owner without changing queue progress."""
    source = (ROOT / "esphome/components/speaker_source/speaker_source_media_player.cpp").read_text()
    begin = source.index("void SpeakerSourceMediaPlayer::dump_diagnostics()")
    method = source[begin:source.index("void SpeakerSourceMediaPlayer::setup()", begin)]
    code = r'''
#include <array>
#include <atomic>
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>
#define ESP_LOGI(tag, fmt, ...) std::printf(fmt "\n", __VA_ARGS__)
#define YESNO(value) ((value) ? "YES" : "NO")
constexpr int pdTRUE=1;
namespace media_source {
enum class MediaSourceState {IDLE,PLAYING,PAUSED,ERROR};
struct MediaSource {MediaSourceState value=MediaSourceState::IDLE; auto get_state(){return value;}};
}
struct Speaker {bool is_stopped(){return false;}bool is_running(){return true;}bool get_pause_state(){return false;}bool has_buffered_data(){return false;}};
struct MediaPlayerControlCommand {int type=3;unsigned pipeline=1;};
struct Queue {int depth=4;MediaPlayerControlCommand head;};
int xQueuePeek(Queue* q,MediaPlayerControlCommand* h,int){*h=q->head;return q->depth?1:0;}
int uxQueueMessagesWaiting(Queue* q){return q->depth;}
struct Pipeline {
 Speaker* speaker=nullptr;std::atomic<media_source::MediaSource*> active_source{nullptr};
 media_source::MediaSource *pending_source=nullptr,*stopping_source=nullptr;
 std::vector<std::string> playlist;unsigned playlist_index=0;std::atomic<unsigned> pending_frames{0};
 bool is_configured(){return speaker!=nullptr;}
};
struct SpeakerSourceMediaPlayer {
 Queue* media_control_command_queue_=nullptr;std::array<Pipeline,2> pipelines_;
 media_source::MediaSource source;
 unsigned get_playlist_position_(unsigned i){return pipelines_[i].playlist_index;}
 media_source::MediaSource* find_source_for_uri_(const std::string&,unsigned){return &source;}
 void dump_diagnostics();
};
''' + method + r'''
int main(){
 SpeakerSourceMediaPlayer player;player.dump_diagnostics();
 Queue queue;Speaker speaker;player.media_control_command_queue_=&queue;
 auto &p=player.pipelines_[1];p.speaker=&speaker;p.active_source=&player.source;
 p.playlist={"https://private.example/secret-token"};p.pending_frames=13;
 player.dump_diagnostics();
 assert(queue.depth==4 && queue.head.type==3 && queue.head.pipeline==1);
 assert(p.playlist_index==0 && p.playlist.size()==1 && p.pending_frames==13);
 assert(p.active_source==&player.source && player.source.value==media_source::MediaSourceState::IDLE);
}
'''
    cpp = tmp_path / "snapshot.cpp"
    binary = tmp_path / "snapshot"
    cpp.write_text(code)
    subprocess.run(["g++", "-std=c++17", str(cpp), "-o", str(binary)], check=True, capture_output=True, text=True)
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
    assert "depth=4 head=3 pipeline=1" in result.stdout
    assert "active=idle target=idle" in result.stdout
    assert "pending_frames=13" in result.stdout
    assert "stopped=NO running=YES" in result.stdout
    assert "secret-token" not in result.stdout
