"""The real presenter uses a paused LVGL timer and retains static warm storage."""
from pathlib import Path
import os
import subprocess
import sys

from esphome import yaml_util
from esphome.core import Lambda
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(240)]


def test_real_lvgl_call_clock_visibility_and_warm_allocations(tmp_path):
    fixture = ROOT / 'tests/fixtures/lvgl_host'
    build = Path(os.environ.get('CALL_KEYPAD_HOST_BUILD', str(tmp_path / 'host'))).resolve()
    config = {
        'esphome': {'name': 'call-keypad-proof', 'build_path': str(build / 'build'), 'compile_process_limit': 2,
            'includes': [str(fixture / 'ui_fixture/lvgl_pointer_probe.h'), str(fixture / 'allocation_probe.h'), str(fixture / 'allocation_probe.cpp')],
            'build_flags': ['-Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc,--wrap=free']},
        'host': {}, 'logger': {'level': 'WARN'},
        'external_components': [{'source': str(ROOT / 'esphome/components'), 'components': ['call_keypad', 'lvgl_visibility']}],
        'display': [{'platform': 'snapshot', 'id': 'screen', 'dimensions': {'width': 240, 'height': 240}}],
        'globals': [{'id': 'connected', 'type': 'bool', 'initial_value': 'false'}],
        'lvgl': {'displays': ['screen'], 'pages': [
            {'id': 'keypad', 'widgets': [
                {'label': {'id': 'peer', 'x': 20, 'y': 30, 'width': 120, 'text': '', 'long_mode': 'SCROLL'}},
                {'label': {'id': 'duration', 'x': 140, 'y': 30, 'width': 80, 'text': '00:00', 'long_mode': 'CLIP', 'hidden': True}},
            ]}, {'id': 'other'}]},
        'call_keypad': {'id': 'presenter', 'name_label_id': 'peer', 'duration_label_id': 'duration'},
        'lvgl_visibility': {'id': 'visibility', 'on_change': [{'lambda': Lambda('id(presenter).sync(id(connected) && id(visibility).is_visible(id(peer)), "A very long contact name that needs native LVGL scrolling");')}]},
    }
    code = r'''
      using esphome::call_keypad::CallKeypad;
      auto *owner = id(presenter);
      auto find_timer = [&]() {
        for(auto *timer=lv_timer_get_next(nullptr);timer;timer=lv_timer_get_next(timer))
          if(lv_timer_get_user_data(timer)==owner)return timer;
        return static_cast<lv_timer_t*>(nullptr);
      };
      auto *clock=find_timer();if(clock==nullptr || !lv_timer_get_paused(clock)) ::exit(71);
      esphome::ui_fixture::PointerProbe input(lv_display_get_default());
      input.pump(1100); // Ringing never starts the connected clock.
      id(connected)=true;id(presenter).connected();
      id(presenter).sync(true,"A very long contact name that needs native LVGL scrolling");
      if(strcmp(lv_label_get_text(id(duration)),"00:00") || lv_timer_get_paused(clock)) ::exit(72);
      lv_refr_now(nullptr);input.pump(300);
      auto *animation=lv_anim_get(id(peer),nullptr);if(animation==nullptr)::exit(73);
      auto previous_time=animation->act_time;
      const char *duration_buffer=lv_label_get_text(id(duration));
      allocation_probe::watch_realloc(duration_buffer);
      allocation_probe::begin();
      for(unsigned i=0;i<1000;i++)id(presenter).sync(true,"A very long contact name that needs native LVGL scrolling");
      auto warm=allocation_probe::end();
      if(warm.calls || warm.frees)::exit(74);
      lv_refr_now(nullptr);animation=lv_anim_get(id(peer),nullptr);
      if(animation==nullptr || animation->act_time<previous_time)::exit(75);
      esphome::delay(1100);
      allocation_probe::begin();
      id(presenter).sync(true,"A very long contact name that needs native LVGL scrolling");
      auto changed=allocation_probe::end();
      printf("CLOCK_CHANGED calls=%u frees=%u bytes=%u reallocations=%u text=%s millis=%u\n",unsigned(changed.calls),unsigned(changed.frees),unsigned(changed.bytes),unsigned(changed.reallocations),lv_label_get_text(id(duration)),unsigned(esphome::millis()));
      if(changed.watched_reallocations || lv_label_get_text(id(duration))!=duration_buffer || !strcmp(lv_label_get_text(id(duration)),"00:00"))::exit(76);
      char value[16];
      for(auto test:{0U,59U,60U,3599U,3600U,86400U}) {
        CallKeypad::format_duration(value,sizeof(value),test);
        const char *expected=test==0?"00:00":test==59?"00:59":test==60?"01:00":test==3599?"59:59":test==3600?"1:00:00":"24:00:00";
        if(strcmp(value,expected))::exit(77);
      }
      lv_screen_load(id(other)->obj);lv_refr_now(nullptr);
      if(!lv_timer_get_paused(clock) || lv_label_get_long_mode(id(peer))!=LV_LABEL_LONG_MODE_CLIP || lv_anim_get(id(peer),nullptr)!=nullptr)::exit(78);
      id(connected)=false;id(presenter).ended();
      if(!lv_obj_has_flag(id(duration),LV_OBJ_FLAG_HIDDEN) || find_timer()!=clock)::exit(79);
      id(presenter).show_idle("Number / extension");
      if(strcmp(lv_label_get_text(id(peer)),"Number / extension"))::exit(80);
      allocation_probe::begin();
      for(unsigned i=0;i<1000;i++)id(presenter).show_idle("Number / extension");
      auto idle_warm=allocation_probe::end();
      if(idle_warm.calls || idle_warm.frees || !lv_timer_get_paused(clock))::exit(81);
      printf("CALL_KEYPAD timer=single-paused hidden_scroll=stopped warm_allocations=%u changed_tick_allocations=%u text_reallocations=%u hours=pass connected_epoch=pass\n",unsigned(warm.calls),unsigned(changed.calls),unsigned(changed.watched_reallocations));
      ::exit(0);
    '''
    config['esphome']['on_boot'] = [{'priority': -300, 'then': [{'lambda': Lambda(code)}]}]
    build.mkdir(parents=True, exist_ok=True)
    path = build / 'host.yaml'
    path.write_text(yaml_util.dump(config))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', str(path)], capture_output=True, text=True, timeout=210)
    (tmp_path / 'compile.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]
    result = subprocess.run([build / 'build/.pioenvs/call-keypad-proof/program'], capture_output=True, text=True, timeout=15)
    (tmp_path / 'run.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'warm_allocations=0' in result.stdout
    assert 'text_reallocations=0' in result.stdout
