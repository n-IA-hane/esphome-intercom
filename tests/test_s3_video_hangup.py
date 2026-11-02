"""Real LVGL hit testing for the S3 video-page stop control."""

import os
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util
from esphome.components.substitutions import do_substitution_pass
from esphome.config import resolve_extend_remove
from esphome.core import Lambda
import pytest

from tests.native_profile import by_id
from tests.test_phone_list_view import list_config

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(480)]


@pytest.mark.parametrize("size", [240, 360])
def test_video_hangup_is_a_compact_stop_target_above_the_video_surface(tmp_path, size):
    shipped = yaml_util.load_yaml(ROOT / "packages/voip/s3_video_call_page.yaml")
    page = do_substitution_pass({"substitutions": {"display_width": str(size)},
                                "page": shipped["defaults"]["_round_video_pages"][0]}, {})["page"]
    button = by_id(page, "call_video_hangup")
    assert button["on_click"] == [{"voip_stack.stop": "phone"}]
    assert button["bg_opa"] == "TRANSP"
    assert (button["width"], button["height"]) == (size // 6, size // 6)
    icon = button["widgets"][0]["label"]
    assert icon["text"] == "\U000F03F5"
    assert icon["text_color"] == 0xEF4444
    layout = yaml_util.load_yaml(ROOT / f"packages/features/presentation/layouts/round_{size}.yaml")
    font = by_id(layout["font"], str(icon["text_font"]))
    previous_glyphs = list(font["extras"][0]["glyphs"])
    fonts = {"font": [font] + shipped["font"]}
    resolve_extend_remove(fonts)
    font = by_id(fonts["font"], str(icon["text_font"]))
    assert len(fonts["font"]) == 1 and font["size"] == size // 10
    assert font["extras"][0]["glyphs"] == previous_glyphs
    assert font["extras"][-1]["glyphs"] == [icon["text"]]
    build = Path(os.environ.get("PHONE_LIST_HOST_BUILD", str(tmp_path / "host"))).resolve()
    build.mkdir(parents=True, exist_ok=True)
    config = list_config(size, build, tmp_path)
    config["substitutions"] = {"font_family": "Figtree"}
    config["font"] = [font]
    # Keep the shipped action too: exercise its actual schema and parent binding.
    config["external_components"].append({"source": str(ROOT / "tests/fixtures/lvgl_host"),
                                          "components": ["ui_fixture"]})
    config["ui_fixture"] = {"phone_id": "phone"}
    config["esphome"]["name"] = f"lvgl-render-{size}-{size}"
    config["lvgl"]["pages"] = [page]
    config.pop("phone_list_view")
    config["esphome"]["on_boot"][0]["then"] = [{"lambda": Lambda(r'''
        id(phone).idle=false;id(phone).in_call=true;
        lv_screen_load(id(call_video_page)->obj);lv_refr_now(nullptr);
        assert(lv_obj_get_child_count(id(call_video_page)->obj)==2);
        assert(lv_obj_get_child(id(call_video_page)->obj,0)==id(call_video_surface));
        assert(lv_obj_get_child(id(call_video_page)->obj,1)==id(call_video_hangup));
        // The decoder may create a full-surface image after the page exists.
        // A sibling button must stay above that subtree for drawing and input.
        auto*image=lv_obj_create(id(call_video_surface));
        lv_obj_set_size(image,LV_PCT(100),LV_PCT(100));lv_obj_center(image);
        lv_obj_remove_flag(image,LV_OBJ_FLAG_CLICKABLE);lv_refr_now(nullptr);
        lv_area_t page,button,glyph;
        lv_obj_get_coords(id(call_video_page)->obj,&page);
        lv_obj_get_coords(id(call_video_hangup),&button);
        lv_obj_get_coords(lv_obj_get_child(id(call_video_hangup),0),&glyph);
        assert(button.x1>=page.x1&&button.x2<=page.x2&&button.y1>=page.y1&&button.y2<=page.y2);
        assert(glyph.x1>=button.x1&&glyph.x2<=button.x2&&glyph.y1>=button.y1&&glyph.y2<=button.y2);
        esphome::ui_fixture::PointerProbe input(lv_display_get_default());
        const auto clicks=input.watch(id(call_video_hangup),LV_EVENT_CLICKED);
        input.tap((button.x1+button.x2)/2,(button.y1+button.y2)/2);
        assert(input.count(clicks)==1);
        assert(esphome::ui_fixture::stop_requests==1&&id(phone).is_idle());
        assert(!id(phone).is_in_call()&&esphome::ui_fixture::dial_requests==0);
        input.tap((button.x1+button.x2)/2,(button.y1+button.y2)/2);
        assert(input.count(clicks)==2);
        assert(esphome::ui_fixture::stop_requests==1&&id(phone).is_idle());
        assert(esphome::ui_fixture::voip_action_writes==1&&esphome::ui_fixture::dial_requests==0);
        printf("S3_VIDEO_HANGUP stop_only=pass red_glyph=pass above_video=pass repeated_stop=pass\n");
    ''')}, {"snapshot.take": {"id": "proof_display", "filename": "s3-video-hangup"}},
        {"lambda": Lambda("::exit(0);")}]
    source = build / "host.yaml"
    source.write_text(yaml_util.dump(config))
    compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(source)],
                              capture_output=True, text=True, timeout=360)
    (tmp_path / "compile.log").write_text(compiled.stdout + compiled.stderr)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr)[-7000:]
    result = subprocess.run([build / f"build/.pioenvs/lvgl-render-{size}-{size}/program"],
                            capture_output=True, text=True, timeout=15,
                            env={**os.environ, "ESPHOME_SNAPSHOT_DIR": str(tmp_path)}, cwd=tmp_path)
    (tmp_path / "run.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "above_video=pass repeated_stop=pass" in result.stdout
