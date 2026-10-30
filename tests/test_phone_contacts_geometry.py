"""Exercise the actual pooled LVGL rows and production MWI layout callback."""

import copy
import os
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util
from esphome.core import Lambda
import pytest

from tests.native_profile import by_id, profile
from tests.test_phone_list_view import list_config

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(480)]


@pytest.mark.parametrize("size,relative", [
    (240, "full-experience/single-bus/spotpear-ball-v2-full-afe.yaml"),
    (360, "full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml"),
])
def test_four_contact_rows_and_mwi_reuse_five_slots_without_mask_overlap(tmp_path, size, relative):
    production = profile(relative)
    view = by_id(production["phone_list_view"], "contacts_list_view")
    parent = by_id(production["lvgl"], "phone_contacts_container")
    build = (Path(os.environ["PHONE_CONTACTS_HOST_BUILD"]) / str(size)
             if "PHONE_CONTACTS_HOST_BUILD" in os.environ else tmp_path / "host").resolve()
    build.mkdir(parents=True, exist_ok=True)
    config = list_config(size, build, tmp_path)
    source_parent = config["lvgl"]["pages"][0]["widgets"][1]["obj"]
    source_parent.clear()
    source_parent.update(copy.deepcopy(parent))
    source_parent["id"] = "rows"
    config["phone_list_view"][0].update(row_height=view["row_height"], visible_rows=view["visible_rows"])
    clock = copy.deepcopy(by_id(production["lvgl"], "top_clock_label"))
    status = copy.deepcopy(by_id(production["lvgl"], "top_battery_label"))
    badge = copy.deepcopy(by_id(production["lvgl"], "top_voicemail_label"))
    clock["text"], status["text"] = "12:00", "100%"
    badge.pop("on_short_click", None)
    config["lvgl"]["top_layer"] = {"widgets": [{"label": value} for value in (clock, status, badge)]}
    fonts = {str(value["text_font"]) for value in (clock, status, badge)}
    config["font"] = [copy.deepcopy(by_id(production["font"], value)) for value in sorted(fonts)]
    phase = int(production["substitutions"]["voice_assist_idle_phase_id"])
    config["globals"].extend([
        {"id": "ui_voicemail_waiting", "type": "bool", "initial_value": "false"},
        {"id": "voice_assistant_phase", "type": "int", "initial_value": str(phase)},
    ])
    callback = str(by_id(production["script"], "update_voicemail_status")["then"][0]["lambda"])
    # Idle phone/voice data are fixture inputs; the production callback owns all geometry.
    callback = (callback.replace("id(phone).is_idle()", "true")
                .replace("id(call_idle_page)", "id(list_page)").replace("id(idle_page)", "id(blank_page)")
                .replace("id(phone_contacts_container)", "id(rows)").replace("id(contacts_list_view)", "id(list_view)"))
    config["script"] = [{"id": "update_badge", "then": [{"lambda": Lambda(callback)}]}]
    check = r'''
        const int row=id(list_view).get_row_height();
        if(lv_obj_get_child_count(id(rows))!=7 || lv_obj_get_height(id(rows))!=4*row)::exit(101);
        lv_area_t viewport;lv_obj_get_coords(id(rows),&viewport);
        for(unsigned i=1;i<=4;i++){
          auto *item=lv_obj_get_child(id(rows),i);lv_area_t box;lv_obj_get_coords(item,&box);
          if(lv_obj_has_flag(item,LV_OBJ_FLAG_HIDDEN) || box.y1<viewport.y1 || box.y2>viewport.y2)::exit(102);
          for(int x:{box.x1,box.x2})for(int y:{box.y1,box.y2}){
            const double dx=x-($SIZE-1)/2.0,dy=y-($SIZE-1)/2.0;
            if(dx*dx+dy*dy>($SIZE/2.0)*($SIZE/2.0))::exit(103);
          }
          lv_area_t name,detail;lv_obj_get_coords(lv_obj_get_child(item,1),&name);lv_obj_get_coords(lv_obj_get_child(item,2),&detail);
          if(lv_obj_has_flag(lv_obj_get_child(item,2),LV_OBJ_FLAG_HIDDEN) || name.y2>=detail.y1)::exit(104);
        }
        lv_obj_scroll_to_y(id(rows),71,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);
        const std::string wanted="Phone "+std::to_string((71+lv_obj_get_height(id(rows))-2)/row+1);
        bool covered=false;
        for(unsigned i=1;i<=5;i++){
          auto *item=lv_obj_get_child(id(rows),i);lv_area_t box;lv_obj_get_coords(item,&box);
          if(wanted==lv_label_get_text(lv_obj_get_child(item,1)) && box.y1<=viewport.y2-2 && box.y2>=viewport.y2-2)covered=true;
        }
        if(!covered)::exit(105);
        printf("CONTACT_FIVE_POOL offset71_bottom=pass rows4=pass two_lines=pass circle=pass\n");
    '''.replace("$SIZE", str(size))
    badge_check = r'''
        const int row=id(list_view).get_row_height();
        if(lv_obj_get_height(id(rows))!=3*row || lv_obj_has_flag(id(top_voicemail_label),LV_OBJ_FLAG_HIDDEN))::exit(106);
        lv_area_t viewport,badge,status;lv_obj_get_coords(id(rows),&viewport);
        lv_obj_get_coords(id(top_voicemail_label),&badge);lv_obj_get_coords(id(top_battery_label),&status);
        if(viewport.y2+8>$BADGE || badge.y2>=status.y1)::exit(107);
        if(lv_obj_get_child_count(id(rows))!=7)::exit(108);
        printf("CONTACT_MWI rows3=pass badge_y=%d viewport_end=%d status_y=%d object_pool=5\n",badge.y1,viewport.y2,status.y1);
    '''.replace("$BADGE", "badge.y1")
    config["esphome"]["on_boot"][0]["then"] = [
        {"lvgl.page.show": "list_page"}, {"script.execute": "update_badge"},
        {"lambda": Lambda("id(list_view).refresh();lv_refr_now(nullptr);")},
        {"snapshot.take": {"id": "proof_display", "filename": "contacts-four"}},
        {"lambda": Lambda(check)},
        {"snapshot.take": {"id": "proof_display", "filename": "contacts-offset71"}},
        {"lambda": Lambda("lv_obj_scroll_to_y(id(rows),0,LV_ANIM_OFF);id(ui_voicemail_waiting)=true;")},
        {"script.execute": "update_badge"}, {"lambda": Lambda("id(list_view).refresh();lv_refr_now(nullptr);")},
        {"snapshot.take": {"id": "proof_display", "filename": "contacts-badge"}},
        {"lambda": Lambda(badge_check)},
        {"lambda": Lambda("id(ui_voicemail_waiting)=false;")}, {"script.execute": "update_badge"},
        {"lambda": Lambda("lv_refr_now(nullptr);if(lv_obj_get_height(id(rows))!=4*id(list_view).get_row_height())::exit(109);::exit(0);")},
    ]
    source = build / "host.yaml"
    source.write_text(yaml_util.dump(config))
    compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(source)],
        capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-6500:]
    result = subprocess.run([build / "build/.pioenvs/phone-list-proof/program"],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "offset71_bottom=pass rows4=pass" in result.stdout
    assert "CONTACT_MWI rows3=pass" in result.stdout
