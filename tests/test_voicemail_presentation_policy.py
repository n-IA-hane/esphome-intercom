"""Run the production mailbox callbacks and stop policy outside any GUI page."""
from pathlib import Path
import subprocess

from esphome import yaml_util

ROOT = Path(__file__).resolve().parents[1]


def test_voicemail_preemption_tracks_source_ownership_and_pending_start(tmp_path):
    document = yaml_util.load_yaml(ROOT / 'packages/features/voip/presentation/phone_lists.yaml')
    boot = document['esphome']['on_boot'][0]['then'][0]['lambda']
    stop = next(script for script in document['script'] if str(script['id']) == 'stop_voicemail_playback')['then']
    branch = stop[0]['if']
    predicate = branch['condition']['lambda']
    clear = next(action['lambda'] for action in branch['then'] if 'lambda' in action)
    source_stop = next(action['lambda'] for action in stop if 'lambda' in action)
    code = r'''
#include <cassert>
#include <functional>
#define id(x) x
namespace esphome::media_source { enum class MediaSourceState {IDLE,PLAYING,PAUSED,ERROR}; }
using esphome::media_source::MediaSourceState;
bool ui_voicemail_active=false,ui_voicemail_play_requested=false,ui_voicemail_play_started=false;
struct Phone {
 bool idle=true;std::function<void(int)> callback;
 bool is_idle()const{return idle;}
 template<class F> void add_on_state_callback(F callback){this->callback=callback;}
}phone;
struct Source {
 MediaSourceState state=MediaSourceState::IDLE;unsigned stops=0;std::function<void()> callback;
 MediaSourceState get_state()const{return state;}
 template<class F>void add_on_update_callback(F callback){this->callback=callback;}
 void stop(){++stops;}
}phone_voicemail;
struct View {void refresh(){}}voicemail_list_view;
struct Controls {void execute(){}}update_voicemail_controls;
unsigned player_stops=0;
struct Stop {void execute(){
 auto owned=[](){PREDICATE};
 if(owned()){++player_stops;CLEAR}
 SOURCE_STOP
 update_voicemail_controls.execute();
}}stop_voicemail_playback;
void install(){BOOT}
void reset(){ui_voicemail_active=false;ui_voicemail_play_requested=false;ui_voicemail_play_started=false;
 phone.idle=true;phone_voicemail.state=MediaSourceState::IDLE;phone_voicemail.stops=0;player_stops=0;}
int main(){install();
 reset();ui_voicemail_play_requested=true;phone_voicemail.callback();
 assert(ui_voicemail_play_requested&&!ui_voicemail_play_started); // catalogue update while native start is pending
 reset();phone_voicemail.state=MediaSourceState::PLAYING;phone_voicemail.callback();
 assert(ui_voicemail_play_requested&&ui_voicemail_play_started&&!ui_voicemail_active); // API playback on another page
 phone.idle=false;phone.callback(0);
 assert(player_stops==1&&phone_voicemail.stops==1&&!ui_voicemail_play_requested&&!ui_voicemail_active);
 reset();phone_voicemail.state=MediaSourceState::PAUSED;ui_voicemail_play_requested=true;phone.idle=false;phone.callback(0);
 assert(player_stops==1&&phone_voicemail.stops==1);
 reset();phone.idle=false;phone.callback(0);assert(player_stops==0&&phone_voicemail.stops==0); // unrelated announcement
 reset();ui_voicemail_active=true;phone.idle=false;phone.callback(0);assert(!ui_voicemail_active&&player_stops==0);
 reset();ui_voicemail_play_requested=true;phone.idle=false;phone.callback(0);
 assert(player_stops==1&&phone_voicemail.stops==1&&!ui_voicemail_play_requested); // queued UI start
 reset();ui_voicemail_play_requested=true;phone_voicemail.state=MediaSourceState::ERROR;phone_voicemail.callback();
 assert(!ui_voicemail_play_requested&&!ui_voicemail_play_started);
 reset();ui_voicemail_play_requested=ui_voicemail_play_started=true;phone_voicemail.callback();
 assert(!ui_voicemail_play_requested&&!ui_voicemail_play_started); // real playback completion
}
'''.replace('PREDICATE', predicate).replace('CLEAR', clear).replace('SOURCE_STOP', source_stop).replace('BOOT', boot)
    source = tmp_path / 'policy.cpp'
    source.write_text(code)
    binary = tmp_path / 'policy'
    built = subprocess.run(['g++', '-std=c++17', '-O1', '-g', '-fsanitize=address,undefined', '-fno-pie', '-no-pie', str(source), '-o', str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([binary], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
