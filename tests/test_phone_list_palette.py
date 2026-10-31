"""Read the actual LVGL styles and pixels produced by the shared contact view."""

import os
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util
from esphome.core import Lambda
import pytest

from tests.test_phone_list_view import list_config

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(480)]


def palette_config(build, output, body, icon_font):
    """Replace SIP input only; keep the production row lambda and LVGL renderer."""
    config = list_config(240, build, output)
    header = build / "palette_inputs.h"
    voip = ROOT.parent / "esphome-voip-stack/esphome/components/voip_stack"
    header.write_text(
        '#define USE_ESP32\n#define USE_ESPHOME_VOIP_BLF\n'
        f'#include "{voip / "phonebook.h"}"\n#include "{voip / "sip_blf.h"}"\n'
        '#undef USE_ESP32\n#undef USE_ESPHOME_VOIP_BLF\n'
    )
    probe = ROOT / "tests/fixtures/lvgl_host"
    config["esphome"]["includes"].extend([
        str(header), str(probe / "allocation_probe.h"), str(probe / "allocation_probe.cpp"),
    ])
    config["esphome"]["build_flags"] = ["-Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc,--wrap=free"]
    config["font"] = [icon_font]
    config["lvgl"]["pages"][0]["widgets"][1]["obj"].update(x=24, y=52, width=192, height=120)
    view = config["phone_list_view"][0]
    view.update(row_height=40, row_count=Lambda("return 7;"), icon_font="phone_list_icons")
    inputs = r'''
        using namespace esphome::voip_stack;
        struct Contact {
          std::string_view name; ContactKind kind;
          bool auto_blf{true};
          std::string number;
          std::string_view stable_id()const{return name;}
        };
        struct Phone {
          Contact contact; BlfStatus status;
          const Contact *get_contact(unsigned)const{return &contact;}
          BlfStatus get_contact_blf(std::string_view)const{return status;}
        };
        static constexpr const char *NAMES[]={"Unknown phone","Ringing phone","Idle phone",
          "Active phone","Held phone","Calling phone","Unavailable phone"};
        static constexpr BlfState STATES[]={BlfState::UNKNOWN,BlfState::RINGING,BlfState::IDLE,
          BlfState::ACTIVE,BlfState::HELD,BlfState::CALLING,BlfState::IDLE};
        static constexpr ContactKind KINDS[]={ContactKind::BROWSER,ContactKind::SIP_ACCOUNT,
          ContactKind::ESP,ContactKind::CONTACT,ContactKind::GROUP,ContactKind::AUTOMATION,ContactKind::UNKNOWN};
        Phone phone{{NAMES[index],KINDS[index]},{}};
        phone.status.state=STATES[index];phone.status.available=index!=0;
        phone.status.presence=index==6?PresenceState::CLOSED:PresenceState::OPEN;
        phone.status.ringing_dialogs=index==1?1:0;
        if(index==0 && id(epoch)==1){
          phone.contact.name="Updated caller";phone.status.state=BlfState::ACTIVE;phone.status.available=true;
        }
        row.highlighted=index==0 && id(epoch)==2;
        row.enabled=!(index==0 && id(epoch)==3);
    '''
    view["render_row"] = Lambda(inputs + str(body).replace("id(phone)", "phone"))
    checks = r'''
        auto rgb=[](lv_color_t c){return (unsigned(c.red)<<16)|(unsigned(c.green)<<8)|c.blue;};
        auto neutral=[](lv_color_t c){return c.red==c.green && c.green==c.blue;};
        printf("ROW_DATA_BYTES=%u\n",unsigned(sizeof(esphome::phone_list_view::RowData)));
        allocation_probe::begin();
        for(unsigned i=0;i<100;i++)id(list_view).refresh();
        auto allocations=allocation_probe::end();
        printf("PALETTE_WARM calls=%u frees=%u bytes=%u\n",unsigned(allocations.calls),unsigned(allocations.frees),unsigned(allocations.bytes));
        static constexpr const char *GLYPHS[]={"\U000F07D0","\U000F01C3","\U000F061A",
          "\U000F011C","\U000F0849","\U000F06A9","\U000F0625"};
        for(unsigned i=0;i<7;i++){
          auto *row=lv_obj_get_child(id(rows),i+1);
          const auto name=lv_obj_get_style_text_color(lv_obj_get_child(row,1),LV_PART_MAIN);
          if(!neutral(lv_obj_get_style_bg_color(row,LV_PART_MAIN)))::exit(81);
          for(unsigned child:{0U,2U,3U})
            if(rgb(lv_obj_get_style_text_color(lv_obj_get_child(row,child),LV_PART_MAIN))!=rgb(name))::exit(82);
          if((i==0 || i==6) && !neutral(name))::exit(83);
          if((i==1 || i==5) && !(name.blue>name.green && name.green>name.red))::exit(84);
          if(i==2 && rgb(name)!=0x4ADE80)::exit(85);
          if(i==3 && rgb(name)!=0xF87171)::exit(86);
          if(i==4 && rgb(name)!=0xFBBF24)::exit(87);
          if(strcmp(lv_label_get_text(lv_obj_get_child(row,0)),GLYPHS[i]))::exit(90);
        }
        auto *outgoing=lv_obj_get_child(id(rows),6);
        if(strcmp(lv_label_get_text(lv_obj_get_child(outgoing,2)),"Calling"))::exit(88);
        auto *ringing=lv_obj_get_child(lv_obj_get_child(id(rows),2),2);
        if(strcmp(lv_label_get_text(ringing),"Ringing: pickup"))::exit(98);
        lv_point_t measured;
        lv_text_get_size(&measured,lv_label_get_text(ringing),lv_obj_get_style_text_font(ringing,LV_PART_MAIN),
          lv_obj_get_style_text_letter_space(ringing,LV_PART_MAIN),lv_obj_get_style_text_line_space(ringing,LV_PART_MAIN),
          LV_COORD_MAX,LV_TEXT_FLAG_NONE);
        if(measured.x>lv_obj_get_content_width(ringing))::exit(99);
        printf("RINGING_LABEL_FIT pixels=%d available=%d\n",int(measured.x),int(lv_obj_get_content_width(ringing)));
        if(allocations.calls || allocations.frees)::exit(89);
        id(epoch)=1;id(list_view).refresh();lv_refr_now(nullptr);
        auto *first=lv_obj_get_child(id(rows),1);
        if(strcmp(lv_label_get_text(lv_obj_get_child(first,1)),"Updated caller") ||
           strcmp(lv_label_get_text(lv_obj_get_child(first,2)),"In call") ||
           rgb(lv_obj_get_style_text_color(lv_obj_get_child(first,1),LV_PART_MAIN))!=0xF87171)::exit(91);
        id(epoch)=2;id(list_view).refresh();
        if(rgb(lv_obj_get_style_bg_color(first,LV_PART_MAIN))!=0x303030)::exit(92);
        id(epoch)=3;id(list_view).refresh();
        if(lv_obj_get_style_opa(first,LV_PART_MAIN)!=LV_OPA_50)::exit(93);
        id(epoch)=0;lv_obj_set_width(id(rows),168);lv_obj_update_layout(id(rows));id(list_view).refresh();lv_refr_now(nullptr);
        if(lv_obj_get_style_width(lv_obj_get_child(first,1),LV_PART_MAIN)!=92 ||
           lv_obj_get_style_width(lv_obj_get_child(first,2),LV_PART_MAIN)!=92)::exit(94);
        lv_obj_scroll_to_y(id(rows),120,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);
        auto *active=lv_obj_get_child(id(rows),2);
        if(strcmp(lv_label_get_text(lv_obj_get_child(active,1)),"Active phone"))::exit(95);
        lv_area_t bounds,viewport;lv_obj_get_coords(active,&bounds);lv_obj_get_coords(id(rows),&viewport);
        if(bounds.y1<viewport.y1 || bounds.y2>viewport.y2)::exit(96);
        lv_obj_scroll_to_y(id(rows),0,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);
        esphome::ui_fixture::PointerProbe input(lv_display_get_default());
        lv_obj_get_coords(first,&bounds);
        input.press((bounds.x1+bounds.x2)/2,(bounds.y1+bounds.y2)/2);input.release();
        if(id(selected_count)!=1 || id(selected_id)!="Unknown phone")::exit(97);
        printf("PALETTE neutral_unknown=pass neutral_background=pass state_text=pass "
               "known_colors=pass calling=pass warm_allocations=%u row_bytes=%u positive_updates=pass\n",
               unsigned(allocations.calls),unsigned(sizeof(esphome::phone_list_view::RowData)));
        ::exit(0);
    '''
    config["esphome"]["on_boot"][0]["then"] = [
        {"lambda": Lambda("lv_screen_load(id(list_page)->obj);id(list_view).refresh();lv_refr_now(nullptr);")},
        {"snapshot.take": {"id": "proof_display", "filename": "palette-top"}},
        {"lambda": Lambda("lv_obj_scroll_to_y(id(rows),120,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);")},
        {"snapshot.take": {"id": "proof_display", "filename": "palette-bottom"}},
        {"lambda": Lambda("lv_obj_scroll_to_y(id(rows),160,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);")},
        {"snapshot.take": {"id": "proof_display", "filename": "palette-unavailable"}},
        {"lambda": Lambda("lv_obj_scroll_to_y(id(rows),0,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);")},
        {"lambda": Lambda(checks)},
    ]
    return config


def test_actual_lvgl_contact_palette_separates_neutral_and_call_states(tmp_path):
    from tests.native_profile import by_id, profile

    production = profile("full-experience/single-bus/spotpear-ball-v2-full-afe.yaml")
    build = Path(os.environ.get("PHONE_LIST_HOST_BUILD", str(tmp_path / "host"))).resolve()
    build.mkdir(parents=True, exist_ok=True)
    config = palette_config(build, tmp_path,
        by_id(production["phone_list_view"], "contacts_list_view")["render_row"],
        by_id(production["font"], "phone_list_icons"))
    source = build / "host.yaml"
    source.write_text(yaml_util.dump(config))
    compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(source)],
        capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-6000:]
    result = subprocess.run([build / "build/.pioenvs/phone-list-proof/program"],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "calling=pass warm_allocations=0" in result.stdout
