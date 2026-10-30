"""P4 presentations share one contact list and an exclusive voicemail page."""
import pytest

from tests.native_profile import profile, by_id, text

PROFILES = [
    'full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml',
    'full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml',
    'voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml',
]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(240)]


@pytest.mark.parametrize('path', PROFILES)
def test_p4_uses_shared_typed_lists_and_preserves_voicemail_until_a_call(path):
    config = profile(path)
    contact = by_id(config['phone_list_view'], 'contacts_list_view')
    voicemail = by_id(config['phone_list_view'], 'voicemail_list_view')
    assert contact['parent_id'] == 'phone_contacts_container'
    assert contact['row_height'] == voicemail['row_height'] == 72
    assert 'get_contact(index)' in text(contact['render_row'])
    assert 'get_contact_blf(contact->stable_id())' in text(contact['render_row'])
    assert 'pickup_contact(row_id)' in text(contact['on_select'])
    assert 'call_contact(row_id)' in text(contact['on_select'])
    assert 'request_play_uid' in text(voicemail['on_select'])
    assert by_id(config['lvgl'], 'phone_voicemail_page')
    assert by_id(config['lvgl'], 'phone_contacts_container')
    presentation = text(config['lvgl'])
    for old in ('voip_stack.next_contact', 'voip_stack.prev_contact', 'call_idle_swipe_area'):
        assert old not in presentation
    if 'voip-only/' in path:
        show = text(by_id(config['script'], 'show_phone_page'))
        assert 'ui_voicemail_active' in show and 'is_idle()' in show
        status = text(by_id(config['script'], 'update_status'))
        assert 'id(contacts_list_view).refresh()' in status
        assert 'id(phone_contacts_container), LV_OBJ_FLAG_HIDDEN' in status
    else:
        draw = text(by_id(config['script'], 'draw_display'))
        assert 'id(ui_voicemail_active) && id(phone).is_idle()' in draw
        assert 'id(contacts_list_view).refresh()' in draw


@pytest.mark.parametrize('board', ['waveshare-p4-full-landscape', 'waveshare-p4-full-portrait', 'waveshare-p4-phone'])
def test_p4_presentation_adapter_selects_the_actual_voicemail_screen(tmp_path, board):
    from pathlib import Path
    import subprocess
    from esphome import yaml_util
    root = Path(__file__).resolve().parents[1]
    document = yaml_util.load_yaml(root / 'packages/features/presentation/devices' / f'{board}.yaml')
    if board.endswith('-phone'):
        script = next(s for s in document['defaults']['_p4_phone_scripts'] if str(s['id']) == 'show_phone_page')
        body = script['then'][0]['lambda']
    else:
        body = document['defaults']['_p4_voicemail_guard'] + '\nif (active_scr != main_page->obj) lv_disp_load_scr(main_page->obj);'
    code = r'''
#include <cassert>
#include <string>
#define id(x) x
struct Phone {bool idle=true;bool is_idle()const{return idle;}}phone;
bool ui_voicemail_active=true;std::string runtime_rendered_page;
int a,b;struct Page {void *obj;};Page main_screen{&a},vm{&b};
Page *main_page=&main_screen,*videophone_page=&main_screen,*phone_voicemail_page=&vm;
void *active_scr=main_screen.obj;unsigned loaded=0;
void lv_disp_load_scr(void *p){active_scr=p;++loaded;}
void lv_screen_load(void *p){active_scr=p;++loaded;}
void *lv_screen_active(){return active_scr;}
void route(){BODY}
int main(){route();assert(active_scr==vm.obj&&loaded==1);route();assert(loaded==1);
 phone.idle=false;route();assert(active_scr==main_screen.obj&&loaded==2);
 phone.idle=true;route();assert(active_scr==vm.obj&&loaded==3);
 ui_voicemail_active=false;route();assert(active_scr==main_screen.obj&&loaded==4);}
'''.replace('BODY', str(body))
    source = tmp_path / 'route.cpp'
    source.write_text(code)
    binary = tmp_path / 'route'
    built = subprocess.run(['g++', '-std=c++17', '-fsanitize=address,undefined', '-no-pie', str(source), '-o', str(binary)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([binary], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
