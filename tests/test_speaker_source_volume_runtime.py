"""The maintained speaker-source provider preserves mute/zero-volume behavior."""
import subprocess

from pathlib import Path

COMPONENTS = Path(__file__).resolve().parents[1] / "esphome/components"


def method(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def test_volume_preserves_mute_and_zero_silence(tmp_path):
    component = COMPONENTS / 'speaker_source'
    stub = tmp_path / 'esphome/components/speaker'
    stub.mkdir(parents=True)
    (stub / 'speaker.h').write_text('''#pragma once
namespace esphome::speaker {
constexpr float SILENT_VOLUME_THRESHOLD=0.001f;
struct Speaker {
 float volume=1;bool muted=false;
 void set_volume(float v){volume=v;}void set_mute_state(bool v){muted=v;}
protected:
 bool is_silent_()const{return muted || volume<SILENT_VOLUME_THRESHOLD;}
};
}
''')
    source = (component / 'speaker_source_media_player.cpp').read_text()
    cpp = tmp_path / 'volume.cpp'
    cpp.write_text(r'''
#include "esphome/components/speaker/speaker.h"
#include <cassert>
#include <vector>
namespace esphome::speaker_source {
template<class A,class B> float remap(float v,A lo,A hi,B out_lo,B out_hi){return out_lo+(v-lo)*(out_hi-out_lo)/(hi-lo);}
struct Trigger {template<class... T>void trigger(T...) {}};
struct Source {void notify_mute_changed(bool){}void notify_volume_changed(float){}};
struct Binding {Source* source;};
struct Pipeline {speaker::Speaker* speaker;std::vector<Binding*> sources;bool is_configured(){return true;}};
struct SpeakerSourceMediaPlayer {
 speaker::Speaker sink;std::vector<Pipeline> pipelines_{{&sink,{}}};
 float volume=0.5f,volume_min_=0.3f,volume_max_=1;bool is_muted_=false;
 Trigger mute_trigger_,unmute_trigger_,volume_trigger_;
 void save_volume_restore_state_(){}template<class F>void defer(F f){f();}
 void set_volume_(float,bool=true);void set_mute_state_(bool,bool=true);
};
''' + method(source, 'void SpeakerSourceMediaPlayer::set_mute_state_(') + '\n' + method(source, 'void SpeakerSourceMediaPlayer::set_volume_(') + r'''
}
int main(){
 using namespace esphome::speaker_source;
 SpeakerSourceMediaPlayer player;
 player.set_volume_(0);assert(player.sink.volume==0 && !player.is_muted_);
 player.set_mute_state_(true);player.set_volume_(0.5f);
 assert(player.is_muted_ && player.sink.muted);
 player.set_mute_state_(false);assert(!player.sink.muted);
 player.set_volume_(0.00001f);assert(player.sink.volume==0);
 player.set_volume_(0.5f);assert(!player.sink.muted && player.sink.volume>0.3f);
}
''')
    executable = tmp_path / 'volume'
    subprocess.run(['g++', '-std=c++20', '-I', str(tmp_path), '-I', str(component), str(cpp), '-o', str(executable)], check=True, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
