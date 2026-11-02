"""Execute the shipped contact policy through the actual pooled LVGL view."""

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


def test_contact_projection_reorders_without_mutating_book_selection_or_watch_order(tmp_path):
    production = profile("full-experience/single-bus/spotpear-ball-v2-full-afe.yaml")
    shipped = by_id(production["phone_list_view"], "contacts_list_view")
    build = Path(os.environ.get("PHONE_LIST_HOST_BUILD", str(tmp_path / "host"))).resolve()
    build.mkdir(parents=True, exist_ok=True)
    voip = ROOT.parent / "esphome-voip-stack/esphome/components/voip_stack"
    inputs = build / "order_inputs.h"
    inputs.write_text(
        '#define USE_ESP32\n#define USE_ESPHOME_VOIP_BLF\n'
        f'#include "{voip / "phonebook.h"}"\n#include "{voip / "sip_blf.h"}"\n'
        '#undef USE_ESP32\n#undef USE_ESPHOME_VOIP_BLF\n'
        '#include "esphome/components/phone_list_view/phone_list_view.h"\n'
        + r'''
namespace ui_order_inputs {
using namespace esphome::voip_stack;
unsigned group_reads=0;
struct Phone {
 Phonebook book;
 std::array<BlfStatus,64> status{};
 unsigned reads=0;
 const ContactEntry *get_contact(unsigned i)const{return book.at(i);}
 unsigned get_contact_count()const{return book.size();}
 BlfStatus get_contact_blf(std::string_view id){++reads;return status[(id[3]-'0')*10+id[4]-'0'];}
 void initialize(){
  std::vector<ContactEntry> entries(64);
  const char *names[]={"Zulu external","Zeta HA","beta","Alpha HA","Zulu offline","Ring team",
    "Alpha offline","Saved alpha","Assist","Unknown device","Busy phone","Alpha device",
    "Ringing device","Beta","Alpha group","Static manual"};
  const ContactKind kinds[]={ContactKind::CONTACT,ContactKind::BROWSER,ContactKind::ESP,ContactKind::BROWSER,
    ContactKind::ESP,ContactKind::GROUP,ContactKind::SIP_ACCOUNT,ContactKind::CONTACT,ContactKind::AUTOMATION,
    ContactKind::ESP,ContactKind::SIP_ACCOUNT,ContactKind::SIP_ACCOUNT,ContactKind::ESP,ContactKind::ESP,
    ContactKind::GROUP,ContactKind::UNKNOWN};
  for(unsigned i=0;i<64;++i){
   char id[8],name[24];snprintf(id,sizeof(id),"id-%02u",i);snprintf(name,sizeof(name),"Saved %02u",i-16);
   entries[i].name=i<16?names[i]:name;entries[i].route=id;
   entries[i].kind=i<16?kinds[i]:ContactKind::CONTACT;
   entries[i].number=i<16?std::to_string(200+i):"+390001";
   entries[i].auto_blf=i<16&&i!=0&&i!=5&&i!=7&&i!=8&&i!=14&&i!=15;
   status[i].available=true;status[i].state=BlfState::IDLE;status[i].presence=PresenceState::OPEN;
  }
  status[4].presence=status[6].presence=PresenceState::CLOSED;
  status[9].available=false;status[9].state=BlfState::UNKNOWN;status[9].presence=PresenceState::UNKNOWN;
  status[10].state=BlfState::ACTIVE;status[10].presence=PresenceState::CLOSED;
  status[12].state=BlfState::RINGING;status[12].presence=PresenceState::UNKNOWN;status[12].ringing_dialogs=1;
  // HA logical phones remain callable even without an open browser/card.
  status[1].available=status[3].available=false;status[1].presence=status[3].presence=PresenceState::UNKNOWN;
  book.replace_all(std::move(entries));book.select("id-06");
 }
} phone;
struct Budget : esphome::phone_list_view::PhoneListView {
 static_assert(sizeof(Projection)==128);
};
}
''')
    config = list_config(240, build, tmp_path)
    probe = ROOT / "tests/fixtures/lvgl_host"
    config["esphome"]["includes"].extend([
        str(inputs), str(probe / "allocation_probe.h"), str(probe / "allocation_probe.cpp"),
    ])
    config["esphome"]["build_flags"] = ["-Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc,--wrap=free"]
    config["esphome"]["on_boot"].insert(0, {"priority": 1000, "then": [
        {"lambda": Lambda("ui_order_inputs::phone.initialize();")},
    ]})
    config["font"] = [by_id(production["font"], "phone_list_icons")]
    config["lvgl"]["pages"][0]["widgets"][1]["obj"].update(height=120)
    view = config["phone_list_view"][0]
    for key in ("row_count", "row_key", "row_group", "render_row"):
        view[key] = Lambda(str(shipped[key]).replace("id(phone)", "ui_order_inputs::phone"))
    view["row_group"] = Lambda("++ui_order_inputs::group_reads;\n" + str(view["row_group"]))
    view.update(row_height=40, icon_font="phone_list_icons")
    check = r'''
        using namespace ui_order_inputs;
        lv_screen_load(id(list_page)->obj);id(list_view).refresh();lv_refr_now(nullptr);
        assert(phone.book.current()->stable_id()=="id-06");
        for(unsigned i=0;i<64;++i){char expected[8];snprintf(expected,sizeof(expected),"id-%02u",i);
          assert(phone.book.at(i)->stable_id()==expected);}
        auto object_at=[&](unsigned index){
          for(unsigned i=1;i<=8;++i){auto*o=lv_obj_get_child(id(rows),i);
            if(!lv_obj_has_flag(o,LV_OBJ_FLAG_HIDDEN)&&lv_obj_get_style_y(o,LV_PART_MAIN)==int(index*40))return o;}
          return static_cast<lv_obj_t*>(nullptr);
        };
        const unsigned first[]={3,1,11,2,13,10,12,9,14,8,5};
        std::array<unsigned,64> expected{};unsigned used=0;
        for(auto i:first)expected[used++]=i;
        for(unsigned i=16;i<64;++i)expected[used++]=i;
        for(auto i:{7U,15U,0U,6U,4U})expected[used++]=i;
        assert(used==64);
        for(unsigned i=0;i<64;++i){
          lv_obj_scroll_to_y(id(rows),i*40,LV_ANIM_OFF);lv_refr_now(nullptr);
          auto*o=object_at(i);assert(o);
          if(phone.book.at(expected[i])->name!=lv_label_get_text(lv_obj_get_child(o,1)))
            fprintf(stderr,"ORDER_MISMATCH row=%u expected=%s actual=%s\n",i,
              phone.book.at(expected[i])->name.c_str(),lv_label_get_text(lv_obj_get_child(o,1)));
          assert(phone.book.at(expected[i])->name==lv_label_get_text(lv_obj_get_child(o,1)));
        }
        // Scroll events must not resnapshot BLF or allocate another order map.
        const auto before_scroll=group_reads;
        lv_obj_scroll_to_y(id(rows),0,LV_ANIM_OFF);lv_refr_now(nullptr);
        assert(group_reads==before_scroll);
        id(list_view).refresh();lv_refr_now(nullptr);
        allocation_probe::begin();
        for(unsigned i=0;i<100;++i)id(list_view).refresh();
        const auto allocations=allocation_probe::end();
        assert(allocations.calls==0&&allocations.frees==0);
        // Reappearing device returns to the device group, preserving the
        // top contact identity and the seven-pixel offset inside its row.
        lv_obj_scroll_to_y(id(rows),7*40+7,LV_ANIM_OFF);lv_refr_now(nullptr);
        phone.status[6].presence=PresenceState::OPEN;
        id(list_view).request_refresh();id(list_view).loop();lv_refr_now(nullptr);
        assert(lv_obj_get_scroll_y(id(rows))==8*40+7);
        auto*anchor=object_at(8);assert(anchor);
        assert(!strcmp(lv_label_get_text(lv_obj_get_child(anchor,1)),"Unknown device"));
        assert(phone.book.current()->stable_id()=="id-06");
        lv_obj_scroll_to_y(id(rows),3*40,LV_ANIM_OFF);lv_refr_now(nullptr);
        assert(!strcmp(lv_label_get_text(lv_obj_get_child(object_at(3),1)),"Alpha offline"));
        // A directory replacement removes a pressed row. Actual LVGL input
        // must not place a call to its replacement, or change selection.
        lv_obj_scroll_to_y(id(rows),8*40,LV_ANIM_OFF);lv_refr_now(nullptr);
        esphome::ui_fixture::PointerProbe input(lv_display_get_default());
        auto*pressed=object_at(9);assert(pressed);lv_area_t bounds;lv_obj_get_coords(pressed,&bounds);
        input.press((bounds.x1+bounds.x2)/2,(bounds.y1+bounds.y2)/2);
        std::vector<ContactEntry> replacement;
        for(unsigned i=0;i<64;++i)if(i!=14)replacement.push_back(*phone.book.at(i));
        phone.book.replace_all(std::move(replacement));
        id(list_view).refresh();input.release();
        assert(id(selected_count)==0&&phone.book.current()->stable_id()=="id-06");
        assert(lv_obj_get_scroll_y(id(rows))==8*40);
        assert(lv_obj_get_child_count(id(rows))==10);
        printf("CONTACT_ORDER groups=pass alphabetical=pass unknown=retained offline_restore=pass "
               "book_unchanged=pass selected_id=pass anchor=pass stale_touch=pass projection_bytes=128 warm_allocations=0\n");
        ::exit(0);
    '''
    config["esphome"]["on_boot"][1]["then"] = [{"lambda": Lambda(check)}]
    source = build / "host.yaml"
    source.write_text(yaml_util.dump(config))
    compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(source)],
                              capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-7000:]
    result = subprocess.run([build / "build/.pioenvs/phone-list-proof/program"],
                            capture_output=True, text=True, timeout=25,
                            env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "projection_bytes=128 warm_allocations=0" in result.stdout
