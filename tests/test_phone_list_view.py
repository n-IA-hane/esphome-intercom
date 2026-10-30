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
                    using esphome::phone_list_view::message_time;
                    if(message_time("30-Oct-2026 01:35:20 +0200")!="29 Oct 23:35") ::exit(37);
                    if(message_time("bad date")!="bad date" || message_time("31-Feb-2026 01:00:00 +0000")!="31-Feb-2026 01:00:00 +0000") ::exit(38);
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


@pytest.mark.parametrize("view_id,count", [("contacts_list_view", 64), ("voicemail_list_view", 16)])
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
