"""Run the maintained Voice PE button filter through actual C++ press/release cycles."""
from pathlib import Path
import re
import subprocess


def test_call_buttons_consume_both_edges_and_keep_idle_guards(tmp_path):
    root = Path(__file__).resolve().parents[1]
    text = (root / 'yamls/experimental/home-assistant-voice-pe/home-assistant-voice-pe-voip.yaml').read_text()
    body = text.split('static bool voip_press_consumed = false;', 1)[1].split('return x;',1)[0] + 'return x;'
    body = re.sub(r'id\((\w+)\)', r'\1', body)
    source = r'''
#include <cassert>
#include <optional>
struct Phone {
 int state=0, answers=0, stops=0;
 bool is_active() { return state>=1 && state<=4; }
 bool is_ringing() { return state==1; }
 void answer_call(){ ++answers; state=3; }
 void stop(){ ++stops; state=0; }
} phone;
struct Switch { bool state=false; } center_button, disable_buttons;
bool init_in_progress=false, color_changed=false, group_volume_changed=false;
bool voice_pe_call_audio_claimed=false;
std::optional<bool> filter(bool x) {
 static bool voip_press_consumed = false;
''' + body + r'''
}
int main(){
 for(int state=0; state<=5; ++state){
  phone={state,0,0};
  auto down=filter(true);
  auto held=filter(true);
  auto up=filter(false);
  if(state>=1 && state<=4){
   assert(!down && !held && !up);
   assert(phone.answers==(state==1));
   assert(phone.stops==(state!=1));
  } else { assert(down.value() && held.value() && !up.value()); }
  assert(filter(false).has_value());
 }
 for(bool *guard : {&init_in_progress, &color_changed, &group_volume_changed, &disable_buttons.state}){
  *guard=true; phone={1,0,0};
  assert(filter(true).value()); assert(!filter(false).value());
  assert(phone.answers==0 && phone.stops==0); *guard=false;
 }
 voice_pe_call_audio_claimed=true; phone={5,0,0};
 assert(!filter(true)); assert(!filter(false)); assert(phone.stops==0);
 voice_pe_call_audio_claimed=false;
 phone={1,0,0}; assert(!filter(true));
 // Remote hangup before release must not leak a release to stock Assist.
 phone.state=0; assert(!filter(false)); assert(filter(true).value());
 assert(!filter(false).value());
}
'''
    cpp=tmp_path/'button.cpp'; cpp.write_text(source)
    binary=tmp_path/'button'
    subprocess.run(['g++','-std=c++17',str(cpp),'-o',str(binary)],check=True,capture_output=True)
    subprocess.run([str(binary)],check=True,capture_output=True)
