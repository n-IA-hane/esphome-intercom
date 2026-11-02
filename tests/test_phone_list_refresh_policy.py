"""Production page adapters redraw list data on visibility changes, not clock ticks."""
from pathlib import Path
import subprocess

import pytest
from esphome import yaml_util
from esphome.components.substitutions import do_substitution_pass

ROOT = Path(__file__).resolve().parents[1]


def execute(tmp_path, body, *, standalone=False):
    body = do_substitution_pass({"substitutions": {"firmware_features": ["voip"], "_p4_phone_has_video": False},
                                 "body": body}, {})["body"]
    code = r'''
#include <array>
#include <cassert>
#include <cstdint>
#include <string>
#define id(x) x
struct Obj {bool hidden=true;unsigned align=0;int x=0,y=0;std::string text;};
Obj objects[22];
Obj *call_idle_group=&objects[0],*call_keypad_group=&objects[1],*call_ringing_in_group=&objects[2],
 *call_ringing_out_group=&objects[3],*call_in_call_group=&objects[4],*call_ended_group=&objects[5],
 *call_ringing_in_peer_label=&objects[6],*call_ringing_out_peer_label=&objects[7],*call_in_call_peer_label=&objects[8],
 *phone_contacts_container=&objects[9],*videophone_peer_label=&objects[10],*videophone_call_button=&objects[11],
 *videophone_state_label=&objects[12],*videophone_call_label=&objects[13],*top_tileview=&objects[14],
 *videophone_title_label=&objects[15],*videophone_keypad_button=&objects[16];
constexpr unsigned LV_OBJ_FLAG_HIDDEN=1,LV_ANIM_OFF=0,LV_PART_MAIN=0,LV_ALIGN_TOP_MID=1,LV_ALIGN_BOTTOM_LEFT=2;
constexpr bool standalone_phone=STANDALONE;
bool lv_obj_has_flag(Obj*o,unsigned){return o->hidden;}
void lv_obj_remove_flag(Obj*o,unsigned){o->hidden=false;}
void lv_obj_clear_flag(Obj*o,unsigned){o->hidden=false;}
void lv_obj_align(Obj*o,unsigned align,int x,int y){o->align=align;o->x=x;o->y=y;}
void lv_obj_add_flag(Obj*o,unsigned){o->hidden=true;}
void lv_obj_set_y(Obj*,int){}
void lv_anim_del(Obj*,void*){}
void lv_obj_set_tile_id(Obj*,int,int,unsigned){}
void lv_label_set_text(Obj*o,const char*text){o->text=text;}
uint32_t lv_color_hex(uint32_t x){return x;}
void lv_obj_set_style_bg_color(Obj*,uint32_t,unsigned){}
void set_hidden(Obj*o,bool hidden){o->hidden=hidden;}
void set_label(Obj*o,const char*text){lv_label_set_text(o,text);}
std::array<int,20> main_layout{};
bool call_ended_active=false,voip_keypad_visible=false;
std::string peer_name="Peer",videophone_peer="Peer",videophone_terminal_notice;
struct Phone {std::string state="idle",selected="Selected peer",peer="Live peer";
 mutable unsigned active_peer_reads=0;
 std::string get_state_str()const{return state;}
 bool is_active()const{return state!="idle";}
 bool is_idle()const{return state=="idle";}
 bool is_ringing()const{return state=="ringing";}
 bool is_calling()const{return state=="calling";}
 bool is_in_call()const{return state=="in_call";}
 bool is_video_active()const{return false;}
 std::string get_current_destination()const{return selected;}
 std::string get_active_peer_name()const{++active_peer_reads;return peer;}
}phone;
struct List {unsigned calls=0;void refresh(){++calls;}}contacts_list_view;
void render(){BODY}
int main(){
 render();assert(contacts_list_view.calls==1);
 for(unsigned n=0;n<20;++n)render();assert(contacts_list_view.calls==1);
 phone.state="in_call";render();assert(contacts_list_view.calls==1);
 if(standalone_phone){
   assert(videophone_title_label->hidden&&videophone_keypad_button->align==LV_ALIGN_TOP_MID);
   assert(phone.active_peer_reads==1&&videophone_peer=="Live peer"&&videophone_peer_label->text=="Live peer");
   phone.selected="Another contact";render();assert(contacts_list_view.calls==1);
   assert(videophone_peer=="Live peer"&&videophone_peer_label->text=="Live peer");
   phone.peer="Updated live peer";render();assert(contacts_list_view.calls==1);
   assert(videophone_peer==phone.peer&&videophone_peer_label->text==phone.peer);
 }
 phone.state="idle";render();assert(contacts_list_view.calls==2);
 if(standalone_phone){
   assert(!videophone_title_label->hidden&&videophone_keypad_button->align==LV_ALIGN_BOTTOM_LEFT);
   assert(videophone_peer_label->text==phone.selected);
 }
}
'''.replace("BODY", str(body)).replace("STANDALONE", "true" if standalone else "false")
    path = tmp_path / "adapter.cpp"
    path.write_text(code)
    binary = tmp_path / "adapter"
    built = subprocess.run(["g++", "-std=c++17", "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                            str(path), "-o", str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([binary], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize("board", ["waveshare-p4-full-portrait", "waveshare-p4-full-landscape", "waveshare-p4-phone"])
def test_actual_p4_adapter_refreshes_once_when_contacts_become_visible(tmp_path, board):
    data = yaml_util.load_yaml(ROOT / "packages/features/presentation/devices" / (board + ".yaml"))
    if board.endswith("-phone"):
        script = next(item for item in data["defaults"]["_p4_phone_scripts"]
                      if str(getattr(item["id"], "value", item["id"])) == "update_status")
        body = script["then"][0]["lambda"]
    else:
        body = data["defaults"]["_p4_call_drawing"]
    execute(tmp_path, body, standalone=board.endswith("-phone"))


@pytest.mark.parametrize("relative", [
    "packages/features/voip/presentation/round.yaml",
    "packages/features/voip/presentation/waveshare-s3-box-v1-aec.yaml",
    "packages/features/presentation/devices/spotpear-ball-v2-phone.yaml",
])
def test_selected_peer_helpers_do_not_duplicate_the_data_callback_refresh(tmp_path, relative):
    data = yaml_util.load_yaml(ROOT / relative)
    script = next(item for item in data["script"] if str(item["id"]) == "update_call_idle_labels")
    code = []
    for action in script["then"]:
        if "lambda" in action:
            code.append(str(action["lambda"]))
        else:
            assert "phone_list_view.refresh" in action
            code.append("id(contacts_list_view).refresh();")
    source = r'''
#include <array>
#include <string>
#include <cstring>
#include <cassert>
#define id(x) x
std::string peer_name;
struct Phone{
 bool active=false;
 std::string selected="Selected peer",peer="Live peer";
 bool is_active()const{return active;}
 std::string get_current_destination()const{return selected;}
 std::string get_active_peer_name()const{return peer;}
}phone;
struct List{unsigned calls=0;void refresh(){++calls;}}contacts_list_view;
struct Label{std::string text="Previous peer";};
Label outgoing,incoming,connected;
Label *call_ringing_out_peer_label=&outgoing,*call_ringing_in_peer_label=&incoming,*call_in_call_peer_label=&connected;
const std::array<Label*,3> labels{&outgoing,&incoming,&connected};
unsigned label_writes=0;
const char *lv_label_get_text(Label*label){return label->text.c_str();}
void lv_label_set_text(Label*label,const char*text){label->text=text;++label_writes;}
void assert_labels(const std::string &text){for(auto*label:labels)assert(label->text==text);}
void update(){BODY}
int main(){
 for(unsigned i=0;i<20;++i)update();
 assert(peer_name=="Selected peer"&&contacts_list_view.calls==0&&label_writes==0);
 assert_labels("Previous peer");
 phone.active=true;
 for(unsigned i=0;i<20;++i)update();
 assert(peer_name=="Live peer"&&contacts_list_view.calls==0&&label_writes==3);
 assert_labels("Live peer");
 phone.selected="Another contact";update();
 assert(peer_name=="Live peer"&&contacts_list_view.calls==0&&label_writes==3);
 assert_labels("Live peer");
 phone.peer="Updated live peer";update();
 assert(peer_name==phone.peer&&contacts_list_view.calls==0&&label_writes==6);
 assert_labels("Updated live peer");
 phone.active=false;update();
 assert(peer_name==phone.selected&&contacts_list_view.calls==0&&label_writes==6);
 assert_labels("Updated live peer");
}
'''.replace("BODY", '\n'.join(code))
    path = tmp_path / "helper.cpp"
    path.write_text(source)
    binary = tmp_path / "helper"
    built = subprocess.run(["g++", "-std=c++17", "-fsanitize=address,undefined", "-no-pie", str(path), "-o", str(binary)],
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([binary], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
