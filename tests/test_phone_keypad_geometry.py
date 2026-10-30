"""Measure real LVGL hit regions from each round phone's production geometry."""

import copy
import os
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util
from esphome.core import CORE, Lambda
import pytest

from tests.test_package_composition import device_config
from tests.native_profile import by_id
from tools.render_lvgl import make_host_config

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(480)]


@pytest.mark.parametrize("size,relative", [
    (240, "full-experience/single-bus/spotpear-ball-v2-full-afe.yaml"),
    (360, "full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml"),
])
def test_round_keypad_uses_the_freed_call_space_without_clipping_or_losing_hits(tmp_path, size, relative):
    original = yaml_util.load_yaml(ROOT / "yamls" / relative)
    device = device_config(tmp_path, original["substitutions"]["firmware_features"], "yamls/" + relative)
    checked = subprocess.run([sys.executable, "-m", "esphome", "config", "--no-defaults", str(device)],
        capture_output=True, text=True, timeout=180)
    (tmp_path / "validation.log").write_text(checked.stdout + checked.stderr)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    expanded = tmp_path / "expanded.yaml"
    expanded.write_text(checked.stdout)
    CORE.config_path = device
    source = yaml_util.load_yaml(expanded)
    build = (Path(os.environ["PHONE_KEYPAD_GEOMETRY_BUILD"]) / str(size)
             if "PHONE_KEYPAD_GEOMETRY_BUILD" in os.environ else tmp_path / "host").resolve()
    config, _hash = make_host_config(source, "settings_continuous_page", size, size, build, pointer=True)
    if not any(script["id"] == "update_call_idle_labels" for script in config["script"]):
        config["script"].append(copy.deepcopy(by_id(source["script"], "update_call_idle_labels")))
    idle = next(action["lambda"] for action in source["voip_stack"]["on_idle"]
                if "show_idle" in str(action.get("lambda", "")))
    check = r'''
        lv_refr_now(nullptr);
        auto in_circle=[](const lv_area_t &box){
          for(int x:{box.x1,box.x2})for(int y:{box.y1,box.y2}){
            const double dx=x-($SIZE-1)/2.0,dy=y-($SIZE-1)/2.0;
            if(dx*dx+dy*dy>($SIZE/2.0)*($SIZE/2.0))return false;
          }
          return true;
        };
        lv_obj_t *matrices[]={id(call_dial_row_123)->obj,id(call_dial_row_456)->obj,
          id(call_dial_row_789)->obj,id(call_dial_row_del0hash)->obj};
        lv_area_t previous{},first{},last{};
        for(unsigned i=0;i<4;i++){
          lv_area_t box;lv_obj_get_coords(matrices[i],&box);
          if(!in_circle(box) || (i && box.y1<=previous.y2))::exit(111);
          const auto *font=lv_obj_get_style_text_font(matrices[i],LV_PART_ITEMS);
          if(lv_obj_get_content_height(matrices[i])<lv_font_get_line_height(font) || lv_obj_get_height(matrices[i])<$SIZE*32/240)::exit(112);
          if(i==0)first=box;
          previous=last=box;
        }
        lv_area_t action;lv_obj_get_coords(id(call_dial_call_btn),&action);
        if(!in_circle(action) || action.y1<=last.y2)::exit(113);
        const double key_area=(first.x2-first.x1+1)/3.0*(first.y2-first.y1+1);
        const int action_area=(action.x2-action.x1+1)*(action.y2-action.y1+1);
        if(action_area>key_area*1.35)::exit(114);
        if(id(phone).in_call){
          lv_area_t peer,duration;lv_obj_get_coords(id(call_dial_value_label),&peer);
          lv_obj_get_coords(id(call_dial_duration_label),&duration);
          if(peer.y2>=duration.y1 || duration.y2>=first.y1)::exit(115);
          if(lv_obj_has_flag(id(call_dial_duration_label),LV_OBJ_FLAG_HIDDEN))::exit(116);
        }
        printf("KEYPAD_GEOMETRY size=$SIZE mode=%s key_h=%d call_w=%d call_h=%d call_y=%d circle=pass labels=pass\n",
          id(phone).in_call?"connected":"idle",int(first.y2-first.y1+1),int(action.x2-action.x1+1),int(action.y2-action.y1+1),action.y1);
    '''.replace("$SIZE", str(size))
    hit = r'''
        esphome::ui_fixture::PointerProbe input(lv_display_get_default());
        lv_area_t box;lv_obj_get_coords(id(call_dial_row_123)->obj,&box);
        const auto digits=id(ui_digit_events);
        input.press(box.x1+(box.x2-box.x1+1)/6,(box.y1+box.y2)/2);input.release();
        if(id(ui_digit_events)!=digits+1)::exit(117);
        lv_obj_get_coords(id(call_dial_row_del0hash)->obj,&box);
        input.press(box.x1+7*(box.x2-box.x1+1)/8,(box.y1+box.y2)/2);input.release();
        if(id(ui_digit_events)!=digits+2)::exit(118);
        const auto calls=esphome::ui_fixture::voip_action_writes;
        lv_obj_get_coords(id(call_dial_call_btn),&box);
        input.press((box.x1+box.x2)/2,(box.y1+box.y2)/2);input.release();
        if(esphome::ui_fixture::voip_action_writes!=calls+1)::exit(119);
        printf("KEYPAD_REAL_HITS digit=pass delete=pass hangup=pass\n");
    '''
    config["esphome"]["on_boot"] = [{"priority": -300, "then": [
        {"lambda": Lambda("id(phone).idle=true;id(phone).in_call=false;")},
        {"script.execute": "compute_layout_metrics"}, {"script.execute": "ui_phone_geometry"},
        {"lvgl.page.show": "call_dialer_page"},
        {"lambda": Lambda(check)}, {"snapshot.take": {"id": "render_display", "filename": "keypad-idle"}},
        {"lambda": Lambda("id(phone).idle=false;id(phone).in_call=true;id(call_keypad_ui).connected();")},
        {"script.execute": "ui_phone_geometry"},
        {"lambda": Lambda('id(peer_name)="Reception";id(voip_dial_refresh).execute();lv_refr_now(nullptr);')},
        {"lambda": Lambda(check)}, {"snapshot.take": {"id": "render_display", "filename": "keypad-connected"}},
        {"lambda": Lambda(hit)},
        {"lambda": Lambda('id(phone).peer_name="Casa";id(phone).selected_name="OTHER";')},
        {"script.execute": "update_call_idle_labels"},
        {"lambda": Lambda('for(auto*label:{id(call_ringing_out_peer_label),id(call_ringing_in_peer_label),id(call_in_call_peer_label)})'
                          'if(strcmp(lv_label_get_text(label),"Casa"))::exit(120);'
                          'id(phone).idle=true;id(phone).in_call=false;id(voip_dial_length)=1;id(voip_dial_buffer)[0]=\'9\';')},
        {"lambda": copy.deepcopy(idle)},
        {"script.execute": "voip_dial_backspace"},
        {"lambda": Lambda('if(id(voip_dial_length)!=0 || id(voip_dial_buffer)[0] || '
                          'strcmp(lv_label_get_text(id(call_dial_value_label)),"Number / extension"))::exit(121);'
                          'printf("KEYPAD_OWNER_LABELS active_peer=Casa idle_buffer=empty delete_empty=pass\\n");::exit(0);')},
    ]}]
    path = tmp_path / "host.yaml"
    path.write_text(yaml_util.dump(config))
    compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(path)],
        capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-6500:]
    result = subprocess.run([build / f"build/.pioenvs/lvgl-render-{size}-{size}/program"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("circle=pass labels=pass") == 2
    assert "KEYPAD_REAL_HITS digit=pass delete=pass hangup=pass" in result.stdout
    assert "KEYPAD_OWNER_LABELS active_peer=Casa idle_buffer=empty delete_empty=pass" in result.stdout
