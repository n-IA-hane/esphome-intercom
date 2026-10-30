"""The actual keypad callbacks retain idle dialing and send private in-call digits."""
from pathlib import Path
import subprocess

import pytest
from esphome import yaml_util

from tests.native_profile import by_id, nodes

ROOT = Path(__file__).resolve().parents[1]
ROUND = 'packages/features/voip/presentation/round.yaml'
SPOT = 'packages/features/presentation/devices/spotpear-ball-v2-phone.yaml'
P4 = 'packages/features/presentation/devices/waveshare-p4-full-landscape.yaml'
P4_PHONE = 'packages/features/presentation/devices/waveshare-p4-phone.yaml'


def run_cpp(tmp_path, body):
    source = tmp_path / 'keypad.cpp'
    source.write_text(body)
    binary = tmp_path / 'keypad'
    build = subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           text=True, capture_output=True)
    assert build.returncode == 0, build.stderr
    result = subprocess.run([binary], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


BASE = r'''
#include <array>
#include <cassert>
#include <cstdint>
#include <string>
#define id(x) x
struct Phone {
 int state=0; char digit=0; unsigned sent=0; bool negotiated=true;
 bool is_idle()const{return state==0;}
 bool is_in_call()const{return state==2;}
 bool can_send_dtmf()const{return is_in_call()&&negotiated;}
 bool send_dtmf(char value){if(!can_send_dtmf())return false;digit=value;++sent;return true;}
} phone;
struct Script {unsigned count=0;void execute(){++count;}}voip_dial_refresh;
'''


@pytest.mark.parametrize('relative', [ROUND, SPOT])
def test_round_key_callback_does_not_append_or_echo_in_call_digits(tmp_path, relative):
    config = yaml_util.load_yaml(ROOT / relative)
    body = str(by_id(config['script'], 'voip_dial_append')['then'][0]['lambda'])
    condition = str(by_id(config['script'], 'call_to_dialer')['then'][0]['if']['condition']['lambda'])
    call = by_id(config['script'], 'voip_dial_call')['then'][0]['if']
    route = call['else'][0]['if']
    assert list(route['then'][0]) == ['voip_stack.call']
    target = str(route['then'][0]['voip_stack.call']['target'])
    run_cpp(tmp_path, BASE + r'''
std::array<char,33>voip_dial_buffer{};uint8_t voip_dial_length=0;
void press(int dial_char){BODY}
bool can_open(){CONDITION}
std::string idle_target(){TARGET}
int main(){
 assert(can_open());press('*');press('9');press('7');assert(idle_target()=="*97");
 for(unsigned i=0;i<40;++i){press('1');}
 assert(voip_dial_length==32&&voip_dial_buffer[32]=='\0');
 voip_dial_length=0;voip_dial_buffer[0]='\0';voip_dial_refresh.count=0;
 press('1');assert(voip_dial_length==1&&voip_dial_buffer[0]=='1');
 phone.state=2;assert(can_open());press('2');assert(phone.sent==1&&phone.digit=='2');
 assert(voip_dial_length==1&&voip_dial_buffer[0]=='1'&&voip_dial_refresh.count==1);
 phone.negotiated=false;assert(!can_open());press('3');assert(phone.sent==1&&voip_dial_length==1);
 phone.state=1;press('4');assert(!can_open()&&phone.sent==1&&voip_dial_length==1);
}
'''.replace('BODY', body).replace('CONDITION', condition).replace('TARGET', target))


@pytest.mark.parametrize('relative,buffer', [(P4, 'voip_dial_buffer'), (P4_PHONE, 'videophone_dial_buffer')])
def test_p4_matrix_sends_each_digit_without_mutating_the_dialed_destination(tmp_path, relative, buffer):
    config = yaml_util.load_yaml(ROOT / relative)
    matrix = next(node['buttonmatrix'] for node in nodes(config)
                  if 'buttonmatrix' in node and 'KEYS[]' in str(node['buttonmatrix']))
    body = str(matrix['on_click'][0]['lambda'])
    label = 'call_keypad_value_label' if buffer == 'voip_dial_buffer' else 'videophone_keypad_value'
    run_cpp(tmp_path, BASE + r'''
std::string BUFFER;int LABEL=0;unsigned label_writes=0;
void lv_label_set_text(int,const char*){++label_writes;}
void press(unsigned x){BODY}
int main(){
 press(0);assert(BUFFER=="1"&&label_writes==1);
 phone.state=2;press(11);assert(phone.sent==1&&phone.digit=='#'&&BUFFER=="1"&&label_writes==1);
 phone.negotiated=false;press(9);assert(phone.sent==1&&BUFFER=="1");
 phone.state=1;press(3);assert(BUFFER=="1");
 phone.state=0;press(12);assert(BUFFER=="1");
}
'''.replace('BUFFER', buffer).replace('LABEL', label).replace('BODY', body))


def test_round_call_owner_prioritizes_ringing_and_keeps_a_connected_keypad(tmp_path):
    defaults = yaml_util.load_yaml(ROOT / ROUND)['defaults']
    run_cpp(tmp_path, r'''
#include <cassert>
#define id(x) x
struct Phone{int state=0;bool is_active(){return state!=0;}bool is_ringing(){return state==1;}
 bool is_calling(){return state==2;}bool is_in_call(){return state==3;}}phone;
bool settings_active=false,voip_dialer_active=false;int current_mode=1;
struct Page{int *obj;};int a,b,c,d,e;Page p1{&a},p2{&b},p3{&c},p4{&d},p5{&e};
Page *call_ringing_in_page=&p1,*call_ringing_out_page=&p2,*call_in_call_page=&p3,*call_dialer_page=&p4,*call_idle_page=&p5;
int *route(){STATE int *target=nullptr;if(false){}ROUTE return target;}
int main(){
 phone.state=3;voip_dialer_active=true;assert(route()==&d&&voip_dialer_active);
 phone.state=1;assert(route()==&a&&!voip_dialer_active);
 phone.state=2;voip_dialer_active=true;assert(route()==&b&&!voip_dialer_active);
 phone.state=0;voip_dialer_active=true;assert(route()==&d);
 phone.state=3;voip_dialer_active=false;assert(route()==&c);
}
'''.replace('STATE', str(defaults['_round_phone_state'])).replace('ROUTE', str(defaults['_round_phone_active_route'])))


def test_empty_idle_backspace_refreshes_only_the_editable_buffer(tmp_path):
    config = yaml_util.load_yaml(ROOT / ROUND)
    backspace = str(by_id(config['script'], 'voip_dial_backspace')['then'][0]['lambda'])
    run_cpp(tmp_path, BASE + r'''
std::array<char,33>voip_dial_buffer{};uint8_t voip_dial_length=0;
void backspace(){BODY}
int main(){
 backspace();assert(voip_dial_refresh.count==1&&"An empty Delete must refresh the idle buffer, not retain a call URI");
 voip_dial_buffer[0]='4';voip_dial_buffer[1]='2';voip_dial_length=2;
 backspace();assert(voip_dial_length==1&&voip_dial_buffer[0]=='4'&&voip_dial_refresh.count==2);
 backspace();assert(voip_dial_length==0&&voip_dial_buffer[0]=='\0'&&voip_dial_refresh.count==3);
}
'''.replace('BODY', backspace))


@pytest.mark.parametrize('relative,owner,script', [
    (ROUND, 'script', 'update_call_idle_labels'),
    (SPOT, 'script', 'update_call_idle_labels'),
    ('packages/features/voip/presentation/waveshare-s3-box-v1-aec.yaml', 'script', 'update_call_idle_labels'),
    (P4, '_p4_phone_scripts', 'update_ic_idle_labels'),
    ('packages/features/presentation/devices/waveshare-p4-full-portrait.yaml', '_p4_phone_scripts', 'update_call_idle_labels'),
])
def test_active_peer_labels_ignore_the_selected_contact(tmp_path, relative, owner, script):
    config = yaml_util.load_yaml(ROOT / relative)
    scripts = config[owner] if owner == 'script' else config['defaults'][owner]
    body = str(by_id(scripts, script)['then'][0]['lambda'])
    run_cpp(tmp_path, r'''
#include <string>
#include <cstring>
#include <cassert>
#define id(x) x
struct Phone{bool active=true;std::string peer="Casa",selected="Other contact";
 bool is_active()const{return active;}std::string get_active_peer_name()const{return peer;}
 std::string get_current_destination()const{return selected;}}phone;
std::string peer_name="sip:pickup.Test@192.0.2.1",a=peer_name,b=peer_name,c=peer_name;
std::string *call_ringing_out_peer_label=&a,*call_ringing_in_peer_label=&b,*call_in_call_peer_label=&c;
const char *lv_label_get_text(std::string *label){return label->c_str();}
void lv_label_set_text(std::string *label,const char *text){*label=text;}
void refresh(){BODY}
int main(){refresh();assert(peer_name=="Casa"&&a=="Casa"&&b=="Casa"&&c=="Casa");
 phone.selected="Test@192.0.2.1";refresh();assert(c=="Casa");
 phone.active=false;refresh();assert(peer_name==phone.selected&&c=="Casa");}
'''.replace('BODY', body))


@pytest.mark.parametrize('relative', [
    'packages/features/presentation/layouts/round_240.yaml',
    'packages/features/presentation/layouts/round_360.yaml', SPOT,
])
def test_round_keypad_keeps_star_hash_and_backspace_without_an_extra_widget(tmp_path, relative):
    row = by_id(yaml_util.load_yaml(ROOT / relative), 'call_dial_row_del0hash')
    assert [button['text'] for button in row['rows'][0]['buttons']][:3] == ['*', '0', '#']
    branch = row['on_click'][0]['if']
    assert 'voip_dial_backspace' in str(branch['then'])
    mapping = str(branch['else'][0]['script.execute']['dial_char'])
    predicate = str(branch['condition']['lambda'])
    run_cpp(tmp_path, '#include <cassert>\nint key(unsigned x){' + mapping + '}\nbool back(unsigned x){' + predicate +
            "}\nint main(){assert(key(0)=='*'&&key(1)=='0'&&key(2)=='#'&&key(3)==0);assert(!back(0)&&back(3));}\n")


def test_shared_keypad_has_one_matrix_and_returns_before_the_owner_redraws(tmp_path):
    config = yaml_util.load_yaml(ROOT / 'packages/features/presentation/in_call_keypad.yaml')
    matrices = [node['buttonmatrix'] for node in nodes(config) if 'buttonmatrix' in node]
    assert len(matrices) == 1
    assert ''.join(button['text'] for row in matrices[0]['rows'] for button in row['buttons']) == '123456789*0#'
    close = by_id(config['script'], 'close_sip_in_call_keypad')['then']
    assert close == [{'lvgl.page.show': '${_sip_keypad_return_page}'}, {'script.execute': 'draw_display'}]
    body = str(matrices[0]['on_click'][0]['lambda'])
    run_cpp(tmp_path, BASE + '\nvoid press(unsigned x){' + body + r'''}
int main(){phone.state=2;const char *keys="123456789*0#";
 for(unsigned x=0;x<12;++x){press(x);assert(phone.digit==keys[x]&&phone.sent==x+1);}
 press(12);assert(phone.sent==12);phone.state=0;press(0);assert(phone.sent==12);}
''')


@pytest.mark.parametrize('relative', [
    'yamls/experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml',
    'yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml',
])
@pytest.mark.parametrize('voip', [False, True])
@pytest.mark.architecture
@pytest.mark.timeout(240)
def test_shared_keypad_is_selected_only_with_its_phone_owner(tmp_path, relative, voip):
    import sys
    from tests.test_package_composition import device_config
    original = yaml_util.load_yaml(ROOT / relative)
    features = [name for name in original['substitutions']['firmware_features'] if name not in ('voip', 'video')]
    if voip:
        features.append('voip')
    config = device_config(tmp_path, features, relative)
    data = yaml_util.load_yaml(config)
    data.update({key: value for key, value in original.items() if key not in ('packages', 'substitutions')})
    config.write_text(yaml_util.dump(data))
    checked = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                             capture_output=True, text=True, timeout=180)
    (tmp_path / 'validation.log').write_text(checked.stdout + checked.stderr)
    assert checked.returncode == 0, (checked.stdout + checked.stderr)[-6000:]
    assert ('sip_in_call_keypad_page' in checked.stdout) is voip
    assert ('id(phone).send_dtmf(KEYS[x])' in checked.stdout) is voip


@pytest.mark.architecture
@pytest.mark.timeout(240)
def test_native_lvgl_keypad_rows_have_room_for_the_actual_font(tmp_path):
    """The inherited card padding once reduced a 31 px row to a narrow stripe."""
    import sys
    import os
    from esphome.core import Lambda
    from esphome.core import CORE
    from tests.test_package_composition import device_config
    from tools.render_lvgl import make_host_config

    relative = 'yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml'
    original = yaml_util.load_yaml(ROOT / relative)
    source = device_config(tmp_path, original['substitutions']['firmware_features'], relative)
    checked = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(source)],
                             capture_output=True, text=True, timeout=120)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(checked.stdout)
    CORE.config_path = source
    config = yaml_util.load_yaml(expanded)
    build = Path(os.environ.get('KEYPAD_HOST_BUILD', str(tmp_path / 'host'))).resolve()
    host, _page_hash = make_host_config(config, 'settings_continuous_page', 240, 240, build, pointer=True)
    host['esphome']['on_boot'] = [{'priority': -300, 'then': [
        {'lambda': Lambda('id(phone).idle=false;id(phone).in_call=true;id(call_keypad_ui).connected();')},
        {'script.execute': 'compute_layout_metrics'},
        {'script.execute': 'ui_phone_geometry'},
        {'lvgl.page.show': 'call_dialer_page'},
        {'lambda': Lambda(r'''
          lv_refr_now(nullptr);
          for (auto *matrix : {id(call_dial_row_123)->obj, id(call_dial_row_456)->obj,
                               id(call_dial_row_789)->obj, id(call_dial_row_del0hash)->obj}) {
            const auto *font = lv_obj_get_style_text_font(matrix, LV_PART_ITEMS);
            const int content = lv_obj_get_content_height(matrix);
            const int line_height = lv_font_get_line_height(font);
            printf("KEYPAD_CONTENT content=%d font=%d\n", content, line_height);
            if (content < line_height) ::exit(94);
          }
          ::exit(0);
        ''')},
    ]}]
    path = tmp_path / 'host.yaml'
    path.write_text(yaml_util.dump(host))
    with (tmp_path / 'compile.log').open('w') as log:
        built = subprocess.run([sys.executable, '-m', 'esphome', 'compile', str(path)],
                               stdout=log, stderr=subprocess.STDOUT, timeout=210)
    assert built.returncode == 0, (tmp_path / 'compile.log').read_text()[-6000:]
    binary = build / 'build/.pioenvs/lvgl-render-240-240/program'
    executed = subprocess.run([binary], capture_output=True, text=True, timeout=20)
    (tmp_path / 'run.log').write_text(executed.stdout + executed.stderr)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert executed.stdout.count('KEYPAD_CONTENT content=') == 4


def test_shared_presenter_epoch_follows_connected_and_idle_events_only():
    binding = yaml_util.load_yaml(ROOT / 'packages/features/presentation/call_keypad_runtime.yaml')
    callbacks = binding['voip_stack']
    assert set(callbacks) == {'on_in_call', 'on_idle', 'on_peer_changed'}
    assert str(callbacks['on_in_call'][0]['lambda']) == 'id(call_keypad_ui).connected();'
    assert '!id(phone).is_idle()' in str(callbacks['on_idle'][0]['lambda'])
    assert 'connected()' not in str(callbacks['on_peer_changed'])
    assert 'show_idle' in str(callbacks['on_idle'])
    assert 'display_visibility).is_visible' in str(binding['lvgl_visibility']['on_change'])
