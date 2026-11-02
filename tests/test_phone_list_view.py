"""Actual LVGL pointer, bounded rows and stale-touch protection on host."""

from pathlib import Path
import os
import subprocess
import sys

from esphome import yaml_util
from esphome.core import Lambda
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(480)]


def list_config(size, build_parent, output):
    return {
        "esphome": {"name": "phone-list-proof", "build_path": str(build_parent / "build"), "compile_process_limit": 2,
            "includes": [str(ROOT / "tests/fixtures/lvgl_host/ui_fixture/lvgl_pointer_probe.h")],
            "on_boot": [{"priority": -250, "then": [
                {"lambda": Lambda(r'''
                    ::setvbuf(stdout, nullptr, _IONBF, 0);
                    lv_screen_load(id(list_page)->obj);
                    id(list_view).refresh(); lv_refr_now(nullptr);
                    if(lv_obj_get_child_count(id(rows)) != 10) ::exit(31);
                    auto *long_row=lv_obj_get_child(id(rows),2);
                    auto *caller=lv_obj_get_child(long_row,1), *metadata=lv_obj_get_child(long_row,2);
                    lv_area_t caller_box, metadata_box;
                    lv_obj_get_coords(caller,&caller_box);lv_obj_get_coords(metadata,&metadata_box);
                    if(lv_obj_get_height(caller)>lv_font_get_line_height(LV_FONT_DEFAULT) || caller_box.y2>=metadata_box.y1) ::exit(39);
                ''')},
                {"snapshot.take": {"id": "proof_display", "filename": "top"}},
                {"lambda": Lambda(r'''
                    esphome::ui_fixture::PointerProbe input(lv_display_get_default());
                    lv_area_t bounds; lv_obj_get_coords(lv_obj_get_child(id(rows),1),&bounds);
                    const int x=(bounds.x1+bounds.x2)/2,y=(bounds.y1+bounds.y2)/2;
                    input.press(x,y); id(epoch)++; id(list_view).refresh(); input.release();
                    if(id(selected_count)!=0) ::exit(32);
                    input.press(x,y);input.release();
                    if(id(selected_count)!=1 || id(selected_id)!="1000") ::exit(33);
                    lv_obj_scroll_to_y(id(rows), 0, LV_ANIM_OFF);id(list_view).refresh();input.pump();
                    lv_area_t body;lv_obj_get_coords(id(rows),&body);
                    input.press((body.x1+body.x2)/2,body.y2-20);
                    input.move((body.x1+body.x2)/2,body.y1+20,180);input.release(160);
                    if(lv_obj_get_scroll_y(id(rows))<=0 || id(selected_count)!=1) ::exit(34);
                    lv_obj_scroll_to_y(id(rows),600,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);
                ''')},
                {"snapshot.take": {"id": "proof_display", "filename": "mid"}},
                {"lambda": Lambda(r'''
                    lv_obj_scroll_to_y(id(rows),LV_COORD_MAX,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);
                    if(lv_obj_get_child_count(id(rows))!=10 || lv_obj_get_scroll_y(id(rows))<2000) ::exit(35);
                ''')},
                {"snapshot.take": {"id": "proof_display", "filename": "end"}},
                {"lambda": Lambda(r'''
                    lv_screen_load(id(blank_page)->obj);lv_refr_now(nullptr);
                    for(uint32_t i=1;i<=8;i++)
                      if(lv_anim_get(lv_obj_get_child(lv_obj_get_child(id(rows),i),3),nullptr)!=nullptr) ::exit(36);
                    printf("UI_LIST pool=8 logical=64 stale_touch=pass tap=pass drag=pass hidden_animation=pass\n");
                    ::exit(0);
                ''')},
            ]}]},
        "host": {}, "logger": {"level": "INFO"},
        "external_components": [{"source": str(ROOT / "esphome/components"), "components": ["phone_list_view"]}],
        "globals": [
            {"id": "epoch", "type": "uint32_t", "initial_value": "0"},
            {"id": "selected_count", "type": "uint32_t", "initial_value": "0"},
            {"id": "selected_id", "type": "std::string", "initial_value": '""'},
        ],
        "display": [{"platform": "snapshot", "id": "proof_display", "dimensions": {"width": size, "height": size}, "auto_clear_enabled": False}],
        "lvgl": {"displays": ["proof_display"], "pages": [
            {"id": "list_page", "bg_color": 0x10151F, "scrollable": False, "widgets": [
                {"label": {"text": "Contacts", "align": "TOP_MID", "y": 10, "text_color": 0xFFFFFF}},
                {"obj": {"id": "rows", "x": 8, "y": 36, "width": size-16, "height": size-72,
                    "pad_all": 0, "border_width": 0, "bg_opa": "TRANSP", "scroll_dir": "VER", "scrollbar_mode": "AUTO"}},
            ]},
            {"id": "blank_page", "bg_color": 0x10151F},
        ]},
        "phone_list_view": [{"id": "list_view", "parent_id": "rows", "visible_rows": 8,
            "row_height": 48, "row_count": Lambda("return 64;"),
            "render_row": Lambda(r'''
                row.set_id(std::to_string(id(epoch)*1000+index));
                row.set_name(index==0?"Reception":index==1?"A very long synthetic caller name that exceeds the available row width":"Phone "+std::to_string(index+1));
                row.set_detail(index%3==0?"Ringing":index%3==1?"Available":"In call");
                row.set_leading(index%2==0?"P":"A");row.set_trailing(index%3==0?"R":index%3==1?"+":"C");
                row.color=index%3==0?0x38BDF8:index%3==1?0x4ADE80:0xF87171;
                row.blink=index%3==0;
            '''),
            "on_select": {"then": [{"lambda": Lambda("id(selected_count)++;id(selected_id)=row_id;")}]},
        }],
    }


@pytest.mark.parametrize("size", [240, 360])
def test_lvgl_list_has_bounded_objects_and_safe_pointer_selection(tmp_path, size):
    build = Path(os.environ.get("PHONE_LIST_HOST_BUILD", str(tmp_path / "host"))).resolve()
    build.mkdir(parents=True, exist_ok=True)
    source = build / "host.yaml"
    source.write_text(yaml_util.dump(list_config(size, build, tmp_path)))
    result = subprocess.run([sys.executable, "-m", "esphome", "compile", str(source)],
        capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-7000:]
    executable = build / "build/.pioenvs/phone-list-proof/program"
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20,
        env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UI_LIST pool=8 logical=64 stale_touch=pass tap=pass drag=pass hidden_animation=pass" in result.stdout


@pytest.mark.parametrize("view_id,count", [("contacts_list_view", 64)])
def test_portrait_p4_pool_keeps_last_entry_visible(tmp_path, view_id, count):
    """Use the shipped pool/row geometry, and inspect the last actual LVGL row."""
    from tests.native_profile import by_id, profile

    production = profile("full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml")
    view = by_id(production["phone_list_view"], view_id)
    container = by_id(production["lvgl"], str(view["parent_id"]))
    height = container["height"]
    height = int(float(str(height)[:-1]) * 1280 / 100) if str(height).endswith("%") else int(height)
    pool = int(view["visible_rows"])
    build = Path(os.environ.get("PHONE_LIST_HOST_BUILD", str(tmp_path / "host"))).resolve()
    build.mkdir(parents=True, exist_ok=True)
    config = list_config(800, build, tmp_path)
    parent = config["lvgl"]["pages"][0]["widgets"][1]["obj"]
    parent.update(height=height, width=720)
    config["phone_list_view"][0].update(visible_rows=pool, row_height=int(view["row_height"]), row_count=Lambda(f"return {count};"))
    check = r'''
        lv_screen_load(id(list_page)->obj); id(list_view).refresh(); lv_refr_now(nullptr);
        lv_obj_scroll_to_y(id(rows),4*$ROW+$ROW-1,LV_ANIM_OFF); id(list_view).refresh(); lv_refr_now(nullptr);
        const unsigned needed=(lv_obj_get_scroll_y(id(rows))+$HEIGHT-1)/$ROW;
        const std::string last_visible="Phone "+std::to_string(needed+1);
        bool middle=false;
        for(unsigned i=1;i<=$POOL;i++) {
          auto *row=lv_obj_get_child(id(rows),i);
          if(!lv_obj_has_flag(row,LV_OBJ_FLAG_HIDDEN) && last_visible==lv_label_get_text(lv_obj_get_child(row,1))) middle=true;
        }
        if(!middle)::exit(41);
        lv_obj_scroll_to_y(id(rows),LV_COORD_MAX,LV_ANIM_OFF); id(list_view).refresh(); lv_refr_now(nullptr);
        bool found=false; lv_area_t viewport; lv_obj_get_coords(id(rows),&viewport);
        for(unsigned i=1;i<=$POOL;i++) {
          auto *row=lv_obj_get_child(id(rows),i);
          if(lv_obj_has_flag(row,LV_OBJ_FLAG_HIDDEN))continue;
          if(strcmp(lv_label_get_text(lv_obj_get_child(row,1)),"Phone $COUNT")==0) {
            lv_area_t rectangle;lv_obj_get_coords(row,&rectangle);
            found=rectangle.y1>=viewport.y1 && rectangle.y2<=viewport.y2;
          }
        }
        if(!found) ::exit(40);
        printf("P4_LAST_ENTRY_VISIBLE pool=$POOL count=$COUNT\n");
    '''.replace("$POOL", str(pool)).replace("$COUNT", str(count)).replace("$ROW", str(view["row_height"])).replace("$HEIGHT", str(height))
    config["esphome"]["on_boot"][0]["then"] = [
        {"lambda": Lambda(check)},
        {"snapshot.take": {"id": "proof_display", "filename": "last-entry"}},
        {"lambda": Lambda("::exit(0);")},
    ]
    source = build / "host.yaml"
    source.write_text(yaml_util.dump(config))
    compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(source)], capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-4000:]
    executable = build / "build/.pioenvs/phone-list-proof/program"
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20,
        env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "P4_LAST_ENTRY_VISIBLE" in result.stdout


def test_boundary_swipe_exits_list_and_compact_rows_fit(tmp_path):
    """Real pointer drags must distinguish list scrolling from page navigation."""
    from tests.native_profile import by_id, profile

    production = profile("full-experience/single-bus/spotpear-ball-v2-full-afe.yaml")
    dimensions = production["display"][0]["dimensions"]
    size = dimensions["width"]
    container = by_id(production["lvgl"], "phone_contacts_container")
    view = by_id(production["phone_list_view"], "contacts_list_view")

    def pixels(value, extent):
        return int(float(value[:-1]) * extent / 100) if isinstance(value, str) and value.endswith("%") else int(value)

    width = pixels(container["width"], size)
    height = pixels(container["height"], dimensions["height"])
    top = pixels(container["y"], dimensions["height"])
    build = Path(os.environ.get("PHONE_LIST_HOST_BUILD", str(tmp_path / "host"))).resolve()
    build.mkdir(parents=True, exist_ok=True)
    config = list_config(size, build, tmp_path)
    config["globals"].extend([
        {"id": "nav_up", "type": "unsigned", "initial_value": "0"},
        {"id": "nav_down", "type": "unsigned", "initial_value": "0"},
    ])
    page = config["lvgl"]["pages"][0]
    page["on_swipe_up"] = [{"lambda": Lambda("id(nav_up)++; lv_indev_wait_release(lv_indev_active());")}]
    page["on_swipe_down"] = [{"lambda": Lambda("id(nav_down)++; lv_indev_wait_release(lv_indev_active());")}]
    page["widgets"][1]["obj"].update(x=(size-width)//2, y=top, width=width, height=height)
    config["phone_list_view"][0]["row_height"] = view["row_height"]
    check = r'''
        lv_screen_load(id(list_page)->obj);id(list_view).refresh();lv_refr_now(nullptr);
        esphome::ui_fixture::PointerProbe input(lv_display_get_default());
        input.press($X,$TOP+16);input.move($X,$TOP+$HEIGHT-16,150);input.release(160);
        if(id(nav_down)!=1 || id(selected_count)!=0)::exit(43);
        lv_obj_scroll_to_y(id(rows),400,LV_ANIM_OFF);id(list_view).refresh();input.pump();
        const auto before=lv_obj_get_scroll_y(id(rows));
        input.press($X,$TOP+$HEIGHT-16);input.move($X,$TOP+16,150);input.release(160);
        if(lv_obj_get_scroll_y(id(rows))<=before || id(nav_up)!=0 || id(selected_count)!=0)::exit(44);
        lv_obj_scroll_to_y(id(rows),LV_COORD_MAX,LV_ANIM_OFF);id(list_view).refresh();input.pump();
        input.press($X,$TOP+$HEIGHT-16);input.move($X,$TOP+16,150);input.release(160);
        if(id(nav_up)!=1 || id(selected_count)!=0)::exit(45);
        id(list_view).set_count_callback([](){return uint32_t(3);});
        lv_obj_scroll_to_y(id(rows),0,LV_ANIM_OFF);id(list_view).refresh();lv_refr_now(nullptr);
        lv_area_t viewport;lv_obj_get_coords(id(rows),&viewport);
        for(unsigned i=1;i<=3;++i){
          auto *row=lv_obj_get_child(id(rows),i);lv_area_t bounds;lv_obj_get_coords(row,&bounds);
          if(lv_obj_has_flag(row,LV_OBJ_FLAG_HIDDEN) || bounds.y1<viewport.y1 || bounds.y2>viewport.y2)::exit(46);
          lv_area_t name,detail;lv_obj_get_coords(lv_obj_get_child(row,1),&name);lv_obj_get_coords(lv_obj_get_child(row,2),&detail);
          if(name.y2>=detail.y1)::exit(47);
        }
        printf("BOUNDARY_NAV top=pass middle=scroll bottom=pass rows=3 no_phantom_call=pass\n");
    '''.replace("$X", str(size//2)).replace("$TOP", str(top)).replace("$HEIGHT", str(height))
    config["esphome"]["on_boot"][0]["then"] = [
        {"lambda": Lambda(check)},
        {"snapshot.take": {"id": "proof_display", "filename": "compact-contacts"}},
        {"lambda": Lambda("::exit(0);")},
    ]
    source = build / "host.yaml"
    source.write_text(yaml_util.dump(config))
    compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(source)], capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-5000:]
    result = subprocess.run([str(build / "build/.pioenvs/phone-list-proof/program")],
        capture_output=True, text=True, timeout=20, env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BOUNDARY_NAV top=pass" in result.stdout


@pytest.mark.parametrize("name", [
    "full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "voip-only/single-bus/spotpear-ball-v2-voip.yaml",
])
def test_spotpear_contact_geometry_has_four_complete_rows_without_mwi(name):
    from tests.native_profile import by_id, profile

    config = profile(name)
    view = by_id(config["phone_list_view"], "contacts_list_view")
    container = by_id(config["lvgl"], "phone_contacts_container")
    height = container["height"]
    height = int(float(height[:-1]) * 240 / 100) if isinstance(height, str) and height.endswith("%") else int(height)
    assert height == 4 * view["row_height"]
    assert view["visible_rows"] == 5
    assert config["voip_stack"]["blf"]["presence"] is True


def test_row_text_uses_no_temporary_heap_and_keeps_utf8_boundaries(tmp_path):
    header = (ROOT / "esphome/components/phone_list_view/phone_list_view.h").read_text()
    row = "struct RowData {" + header.split("struct RowData {", 1)[1].split("\n};", 1)[0] + "\n};"
    source = tmp_path / "row.cpp"
    source.write_text(r'''
#include <array>
#include <algorithm>
#include <string>
#include <string_view>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <cassert>
#include <new>
static bool tracking=false;static unsigned allocations=0;
void *operator new(std::size_t count){if(tracking)++allocations;if(auto*p=std::malloc(count))return p;std::abort();}
void operator delete(void*p)noexcept{std::free(p);}
void operator delete(void*p,std::size_t)noexcept{std::free(p);}
// ROW_DATA_DECLARATION
int main(){
 RowData row;
 tracking=true;
 row.set_name("A long caller name requiring more than small-string storage");
 row.set_detail("Ringing, tap to pick up");
 row.set_id("a-stable-contact-identity-longer-than-small-string-storage");
 tracking=false;std::fprintf(stderr,"ROW_ALLOCATIONS=%u\n",allocations);assert(allocations==0);
 std::string text(127,'x');text+="\xc3\xa0";
 row.set_name(text);assert(std::strlen(row.name.data())==127&&row.name[127]=='\0');
 row.set_detail({});assert(row.detail[0]=='\0');
 row.set_id(std::string(161,'x'));assert(!row.enabled);
}
'''.replace("// ROW_DATA_DECLARATION", row, 1))
    binary = tmp_path / "row"
    compiled = subprocess.run(["g++", "-std=c++17", str(source), "-o", str(binary)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    run = subprocess.run([binary], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr


def test_automatic_updates_coalesce_in_the_real_component_loop(tmp_path):
    document = yaml_util.load_yaml(ROOT / "packages/features/voip/presentation/phone_lists.yaml")
    action = document["defaults"]["_contacts_blf_updates"]["voip_stack"]["on_blf_state"][0]
    if "lambda" in action:
        update = str(action["lambda"]).replace("id(contacts_list_view)", "id(list_view)")
    else:
        assert str(action["phone_list_view.refresh"]) == "contacts_list_view"
        update = "esphome::phone_list_view::RefreshAction<> action; action.set_parent(id(list_view)); action.play();"
    build = Path(os.environ.get("PHONE_LIST_HOST_BUILD", str(tmp_path / "host"))).resolve()
    build.mkdir(parents=True, exist_ok=True)
    config = list_config(240, build, tmp_path)
    config["globals"].extend([
        {"id": "rendered_rows", "type": "unsigned", "initial_value": "0"},
        {"id": "proof_phase", "type": "unsigned", "initial_value": "0"},
        {"id": "proof_turns", "type": "unsigned", "initial_value": "0"},
        {"id": "proof_reenter", "type": "bool", "initial_value": "false"},
    ])
    config["phone_list_view"][0]["render_row"] = Lambda(
        "++id(rendered_rows);if(index==0&&id(proof_reenter)){id(proof_reenter)=false;id(list_view).request_refresh();}\n"
        + str(config["phone_list_view"][0]["render_row"]))
    config["esphome"]["on_boot"][0]["then"] = [{"lambda": Lambda(
        "lv_screen_load(id(list_page)->obj);id(list_view).refresh();lv_refr_now(nullptr);id(rendered_rows)=0;")}]
    config["esphome"]["on_loop"] = [{"then": [{"lambda": Lambda(r'''
        if(id(proof_phase)==0){
          id(rendered_rows)=0;
          for(unsigned i=0;i<20;++i){UPDATE}
          printf("BURST_IMMEDIATE_ROWS=%u\n",id(rendered_rows));
          if(id(rendered_rows)!=0)::exit(48);
          id(proof_phase)=1;return;
        }
        if(id(proof_phase)==1){
          if(id(rendered_rows)==0 && ++id(proof_turns)<10)return;
          if(id(rendered_rows)!=8)::exit(49);
          esphome::phone_list_view::RefreshAction<> explicit_action;
          explicit_action.set_parent(id(list_view));explicit_action.play();
          if(id(rendered_rows)!=16)::exit(50);
          id(proof_reenter)=true;id(proof_turns)=0;id(proof_phase)=2;
          id(list_view).request_refresh();return;
        }
        if(id(rendered_rows)<32 && ++id(proof_turns)<10)return;
        if(id(rendered_rows)!=32)::exit(51);
        printf("COALESCED_RENDER burst=20 redraws=1 explicit=immediate reentrant=retained\n");::exit(0);
    '''.replace("UPDATE", update, 1))}]}]
    source = build / "host.yaml"
    source.write_text(yaml_util.dump(config))
    compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(source)], capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-5000:]
    run = subprocess.run([str(build / "build/.pioenvs/phone-list-proof/program")], capture_output=True,
        text=True, timeout=20, env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(run.stdout + run.stderr)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "COALESCED_RENDER burst=20 redraws=1 explicit=immediate" in run.stdout
