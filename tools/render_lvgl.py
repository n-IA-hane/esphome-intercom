#!/usr/bin/env python3
"""Render an expanded production LVGL page through ESPHome's host snapshot display.

The page, theme, fonts and settings synchronization scripts are imported, never
redrawn. Only hardware data owners are replaced with host data entities. Pass
the output of ``esphome config`` as --input, including generated owner defaults.
Build storage may be
placed on a RAM-backed parent with --build-parent.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from esphome import yaml_util
from esphome.core import Lambda
from esphome.components.lvgl.defines import LV_FONTS


ROOT = Path(__file__).resolve().parents[1]
SIZE_FIELDS = {"width", "height", "min_width", "max_width", "min_height", "max_height", "x", "y"}
WIDGET_KINDS = {"obj", "button", "buttonmatrix", "label", "slider", "switch", "dropdown", "bar", "arc", "image", "animimg", "textarea"}


def yaml_input(value, key=None):
    """Undo only validated YAML size encodings before using the same schema again.

    ESPHome's dump writes SIZE_CONTENT as LV_SIZE_CONTENT and percentages as
    floats. Feeding a whole float back to the size validator would turn 100%
    into one pixel. Reconstructing their public spellings preserves geometry.
    """
    if isinstance(value, dict):
        return {name: yaml_input(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [yaml_input(item, key) for item in value]
    if key in SIZE_FIELDS:
        if value == "LV_SIZE_CONTENT":
            return "SIZE_CONTENT"
        if isinstance(value, float):
            return f"{value * 100:g}%"
    return value


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def strings(value):
    if isinstance(value, Lambda):
        yield value.value
    elif isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def references(value):
    result = {match for source in strings(value)
              for match in re.findall(r"\bid\((\w+)\)", source)}
    for node in nodes(value):
        for key, item in node.items():
            if key in ("id", "text_font") and isinstance(item, str):
                result.add(item)
            if key in ("script.execute", "button.press") and isinstance(item, str):
                result.add(item)
    return result


def find_id(config, domain, name):
    for item in config.get(domain, []):
        if isinstance(item, dict) and str(item.get("id")) == name:
            return item
    if domain == "script":
        return None
    source = [config.get(domain, [])]
    if domain == "switch":
        ha = config.get("voip_stack", {}).get("ha_integration", {})
        if isinstance(ha, dict):
            source.append(ha.get("conference_ring", {}))
    return next((node for node in nodes(source) if str(node.get("id")) == name and len(node) > 1), None)


def overlay_policy(config):
    """Project the real status-overlay policy, rather than hiding labels ad hoc."""
    draw = find_id(config, "script", "draw_display")
    if draw is None:
        return ""
    for source in strings(draw):
        if "const bool show_status" in source:
            match = re.search(r"const bool show_status\s*=[\s\S]+?if \(show_status\)\s*\{[\s\S]+?\}\s*else\s*\{[\s\S]+?\}", source)
            if not match:
                raise ValueError("Status policy changed; refusing to replace it with a mock")
            return match.group(0)
    return ""


def page_transition(config):
    """Copy the balanced production page-change block, including its input policy."""
    draw = find_id(config, "script", "draw_display")
    for source in strings(draw):
        declaration = re.search(r"\b(?:const\s+)?bool\s+page_changed\s*=", source)
        if declaration is None:
            continue
        condition = re.search(r"if\s*\(page_changed\)\s*\{", source[declaration.start():])
        if condition is None:
            break
        opening = declaration.start() + condition.end() - 1
        tokens = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]')
        depth = 0
        for token in tokens.finditer(source, opening):
            if token.group() == "{":
                depth += 1
            elif token.group() == "}":
                depth -= 1
                if depth == 0:
                    block = source[declaration.start():token.end()]
                    resets = re.search(r"id\(nav_anim\)\s*=\s*0;\s*id\(nav_anim_ms\)\s*=\s*0;", source[token.end():])
                    if resets is None:
                        raise ValueError("Production animation-hint reset missing")
                    return block + "\n" + resets.group()
    raise ValueError("Production page-change block missing; refusing a replacement implementation")


def page_selector(config):
    """Keep the real arbitration, including the startup-populated page registry."""
    draw = find_id(config, "script", "draw_display")
    for source in strings(draw):
        start = source.find("// Determine target page")
        end = source.find("if (target == nullptr) return;", start)
        if start >= 0 and end >= start:
            return source[start:end + len("if (target == nullptr) return;")]
    raise ValueError("Production page selector missing; refusing a replacement implementation")


def settings_registry_boot(config):
    """Execute only the real UI registry action, at its real startup priority.

    Audio, network and delayed startup boundaries are not simulated by this
    display fixture. Composition tests retain and verify the complete startup.
    A missing registration remains missing, so the pointer test can expose it.
    """
    result = []
    for entry in config.get("esphome", {}).get("on_boot", []):
        actions = [copy.deepcopy(action) for action in entry.get("then", [])
                   if "g_settings_pages" in "\n".join(strings(action))]
        if actions:
            result.append({"priority": entry["priority"], "then": actions})
    return result


def widget_ids(pages, top_layer):
    result = {str(page["id"]) for page in pages}
    for node in nodes([pages, top_layer]):
        for kind, value in node.items():
            if kind in WIDGET_KINDS and isinstance(value, dict) and "id" in value:
                result.add(str(value["id"]))
    return result


def phone_geometry(config):
    reposition = find_id(config, "script", "reposition_widgets")
    for source in strings(reposition):
        if "call_dial_call_btn" not in source:
            continue
        for marker in ("const auto &gx = id(layout_x);", "lv_disp_t *disp = lv_disp_get_default();"):
            if source.count(marker) == 1:
                return source[source.index(marker):]
    raise ValueError("Production phone geometry changed; refusing to invent a dialer layout")


def make_host_config(config, page_id, width, height, build_parent, pointer=False):
    lvgl = config["lvgl"]
    if isinstance(lvgl, list):
        lvgl = next(item for item in lvgl if any(str(p.get("id")) == page_id for p in item.get("pages", [])))
    page = copy.deepcopy(next(p for p in lvgl["pages"] if str(p.get("id")) == page_id))
    page_hash = hashlib.sha256(yaml_util.dump(page).encode()).hexdigest()
    phone_pages = [copy.deepcopy(p) for p in lvgl["pages"] if str(p.get("id", "")).startswith("call_")] if pointer else []
    if pointer and not any(p["id"] == "call_dialer_page" for p in phone_pages):
        raise ValueError("Pointer witness requires the real production dialer")
    scripts = [copy.deepcopy(script) for script in config.get("script", [])
               if str(script.get("id", "")).startswith("settings_sync")]
    geometry = phone_geometry(config) if pointer else ""
    if pointer:
        scripts += [copy.deepcopy(find_id(config, "script", "compute_layout_metrics")),
                    {"id": "ui_phone_geometry", "then": [{"lambda": Lambda(geometry)}]}]
        for name in ("voip_dial_append", "voip_dial_backspace", "voip_dial_clear", "voip_dial_refresh", "voip_dial_call"):
            original = copy.deepcopy(find_id(config, "script", name))
            if original is None:
                raise ValueError(f"Production UI receiver missing: {name}")
            if name in ("voip_dial_append", "voip_dial_backspace"):
                arg = "dial_char" if name == "voip_dial_append" else "-1"
                before = "id(ui_digit_events)++; id(ui_before_buffer)=id(voip_dial_buffer); id(ui_before_length)=id(voip_dial_length); "
                before += 'printf("UI_RECEIVER op=' + name + ' arg=%d before=%u\\n",int(' + arg + '),unsigned(id(voip_dial_length)));'
                after = 'if(id(ui_before_buffer)!=id(voip_dial_buffer) || id(ui_before_length)!=id(voip_dial_length)) id(ui_buffer_mutations)++; '
                after += 'printf("UI_RECEIVER op=' + name + ' after=%u mutations=%u\\n",unsigned(id(voip_dial_length)),id(ui_buffer_mutations));'
                original["then"] = [{"lambda": Lambda(before)}, *original["then"], {"lambda": Lambda(after)}]
            scripts.append(original)
        for name in ("call_to_va", "swipe_to_va"):
            original = copy.deepcopy(find_id(config, "script", name))
            if name == "call_to_va":
                original["then"] = [{"lambda": Lambda("id(ui_assistant_events)++;")}, *original["then"]]
            scripts.append(original)
        for name in ("settings_open", "settings_close", "settings_open_transition", "settings_close_transition", "round_navigate"):
            original = copy.deepcopy(find_id(config, "script", name))
            if original is None:
                continue
            if name == "settings_open":
                original["then"] = [{"lambda": Lambda("id(ui_options_events)++;")}, *original["then"]]
            scripts.append(original)
        projected = page_selector(config) + "\n"
        projected += page_transition(config) + "\n" + overlay_policy(config)
        scripts.append({"id": "draw_display", "then": [{"lambda": Lambda(projected)}]})
    registrations = [copy.deepcopy(node) for node in config.get("esphome", {}).get("on_boot", [])
                     if "settings_sync" in "\n".join(strings(node))]
    if pointer:
        registrations += settings_registry_boot(config)
    top_layer = copy.deepcopy(lvgl.get("top_layer", {}))
    source = [page, phone_pages, scripts, registrations, top_layer]
    declared = widget_ids([page, *phone_pages], top_layer) | {str(script["id"]) for script in scripts}
    if pointer:
        declared |= {"idle_anim", "idle_page"}
    required = references(source) - declared
    # Action IDs appear among references too; the framework still validates them.
    required |= {name for name in references(source) if find_id(config, "number", name)
                 or find_id(config, "switch", name) or find_id(config, "select", name)
                 or find_id(config, "media_player", name) or find_id(config, "text_sensor", name)
                 or find_id(config, "button", name)}
    host = {
        "esphome": {"name": f"lvgl-render-{width}-{height}",
                    "build_path": str(build_parent / "build"), "compile_process_limit": 2},
        "host": {}, "logger": {"level": "INFO"},
        "globals": [
            {"id": "ui_owner_writes", "type": "uint32_t", "initial_value": "0"},
            {"id": "ui_accept_writes", "type": "bool", "initial_value": "true"},
            {"id": "ui_navigation_events", "type": "uint32_t", "initial_value": "0"},
            {"id": "settings_active", "type": "bool", "initial_value": "false"},
            *[{"id": name, "type": "uint32_t", "initial_value": "0"}
              for name in ("ui_digit_events", "ui_call_events", "ui_options_events", "ui_assistant_events", "ui_buffer_mutations")],
            {"id": "ui_before_buffer", "type": "std::array<char, 33>", "initial_value": "{0}"},
            {"id": "ui_before_length", "type": "uint8_t", "initial_value": "0"},
        ],
        "display": [{"platform": "snapshot", "id": "render_display",
                     "dimensions": {"width": width, "height": height}, "auto_clear_enabled": False}],
        "script": scripts,
    }
    target = {key: copy.deepcopy(value) for key, value in lvgl.items()
              if key in ("default_font", "theme", "style_definitions", "color_depth", "byte_order", "buffer_size")}
    policy = overlay_policy(config)
    context_policy = policy + (page_selector(config) if pointer else "")
    context_pages = sorted(references(Lambda(context_policy)) - declared - {page_id})
    context_pages = [name for name in context_pages if any(str(p.get("id")) == name for p in lvgl["pages"])]
    context_pages = sorted(set(context_pages) | {"main_page"})
    if pointer:
        context_pages = sorted(set(context_pages) | {"idle_page"})
    contextual = []
    for name in context_pages:
        original = next((item for item in lvgl["pages"] if str(item.get("id")) == name), {})
        contextual.append({"id": name, "bg_color": original.get("bg_color", 0x202020),
                           **{key: copy.deepcopy(value) for key, value in original.items()
                              if key in ("scrollable", "on_swipe_up", "on_swipe_down")}})
    required |= references(contextual) - declared
    target.update({"id": "render_lvgl", "displays": ["render_display"],
                   "pages": contextual
                            + [yaml_input(p) for p in phone_pages] + [yaml_input(page)]})
    if pointer:
        idle = next(p for p in target["pages"] if p["id"] == "idle_page")
        idle["widgets"] = [{"obj": {"id": "idle_anim", "width": 1, "height": 1, "hidden": True}}]
        host["external_components"] = [{"source": str(ROOT / "tests/fixtures/lvgl_host"), "components": ["ui_fixture"]}]
        host["ui_fixture"] = {}
    if top_layer:
        target["top_layer"] = yaml_input(top_layer)
    host["lvgl"] = target
    fonts = {str(font.get("id")): font for font in config.get("font", [])}
    used_fonts = set(strings(target)) & set(fonts)
    if used_fonts:
        host["font"] = [copy.deepcopy(fonts[name]) for name in sorted(used_fonts)]
    required -= set(fonts) | set(LV_FONTS) | {"settings_active", "main_page", "settings_close"}
    required -= set(context_pages)
    required -= {str(item["id"]) for item in host["globals"]}
    # Bind the native setting through its optional entity when the real profile
    # has one; PBX-only profiles retain the same authoritative phone interface.
    if pointer and "phone" in required and find_id(config, "switch", "voip_conference_ring"):
        required.add("voip_conference_ring")
        host["ui_fixture"]["conference_ring_id"] = "voip_conference_ring"
    for owner in sorted(required):
        if find_id(config, "number", owner):
            slider = next((node for node in nodes(page) if node.get("min_value") is not None
                           and owner in references(node)), {})
            low, high = float(slider.get("min_value", 0)), float(slider.get("max_value", 100))
            value = high if owner == "master_volume" else min(high, max(low, 12.0))
            host.setdefault("number", []).append({
                "platform": "template", "id": owner, "min_value": low, "max_value": high,
                "step": 1, "initial_value": value, "optimistic": False,
                "update_interval": "never",
                "set_action": [{"lambda": Lambda("id(ui_owner_writes)++; if (id(ui_accept_writes)) id(" + owner + ").publish_state(x);")}],
            })
        elif find_id(config, "switch", owner):
            def actions(state):
                return [{"lambda": Lambda("id(ui_owner_writes)++; if (id(ui_accept_writes)) id(" + owner + ").publish_state(" + state + ");")}]
            host.setdefault("switch", []).append({"platform": "template", "id": owner,
                "optimistic": False, "restore_mode": "ALWAYS_OFF",
                "turn_on_action": actions("true"), "turn_off_action": actions("false")})
        elif (select := find_id(config, "select", owner)) is not None:
            host.setdefault("select", []).append({"platform": "template", "id": owner,
                "options": copy.deepcopy(select["options"]), "optimistic": False,
                "update_interval": "never",
                "set_action": [{"lambda": Lambda("id(ui_owner_writes)++; if (id(ui_accept_writes)) id(" + owner + ").publish_state(x);")}]})
        elif find_id(config, "media_player", owner):
            host["external_components"] = [{"source": str(ROOT / "tests/fixtures/lvgl_host"), "components": ["ui_fixture"]}]
            host.setdefault("media_player", []).append({"platform": "ui_fixture", "id": owner})
        elif find_id(config, "text_sensor", owner):
            host.setdefault("text_sensor", []).append({"platform": "template", "id": owner, "update_interval": "never"})
        elif find_id(config, "button", owner):
            host.setdefault("button", []).append({"platform": "template", "id": owner,
                "on_press": [{"lambda": Lambda("id(ui_owner_writes)++;")}]})
        elif (global_value := find_id(config, "globals", owner)) is not None:
            item = copy.deepcopy(global_value)
            item["restore_value"] = False
            host["globals"].append(item)
        elif find_id(config, "script", owner):
            original = find_id(config, "script", owner)
            actions = [{"lambda": Lambda("id(ui_navigation_events)++;")}]
            if pointer and owner == "switch_to_va":
                actions = [{"lambda": Lambda("id(settings_active) = false;")}, {"script.execute": "draw_display"}]
            elif owner in ("voip_dial_append", "voip_dial_backspace"):
                actions = [{"lambda": Lambda("id(ui_digit_events)++;")}]
            elif owner == "voip_dial_call":
                actions = [{"lambda": Lambda("id(ui_call_events)++;")}]
            item = {"id": owner, "then": actions}
            if original.get("parameters"):
                item["parameters"] = copy.deepcopy(original["parameters"])
            host["script"].append(item)
        elif pointer and owner == "phone":
            host["ui_fixture"]["phone_id"] = owner
        elif pointer and owner == "runtime":
            host["ui_fixture"]["runtime_id"] = owner
        elif pointer and (find_id(config, "esp_video_renderer", owner) or find_id(config, "p4_video_renderer", owner)):
            host["ui_fixture"]["video_id"] = owner
        else:
            raise ValueError(f"No host data owner for {owner!r}; refusing to remove production callbacks")
    if not pointer:
        host["script"].append({"id": "settings_close", "then": [
            {"lambda": Lambda("id(ui_navigation_events)++; id(settings_active) = false;")},
            {"lvgl.page.show": "main_page"}]})
    capture = [{"lambda": Lambda("::setvbuf(stdout, nullptr, _IONBF, 0); id(ui_owner_writes) = 0;" +
                ("id(init_in_progress)=false; id(voice_assistant_phase)=1;" if pointer else "") +
                ("id(speaker_media_player).writes = 0;" if host.get("media_player") else ""))},
               {"lvgl.page.show": page_id}, {"lambda": Lambda("lv_refr_now(nullptr);")}]
    # Boundary tests use the actual generated handlers, callbacks and scripts.
    # These are host data owners, so none of the following can play audio.
    probe = r'''
      if (id(ui_owner_writes) != 0) { fprintf(stderr,"UI_PROBE open wrote owner\n"); ::exit(41); }
      id(master_volume).publish_state(17.0f);
      if (lv_slider_get_value(id(ui_speaker_vol_slider)) != 17 || id(ui_owner_writes) != 0) ::exit(42);
      lv_slider_set_value(id(ui_speaker_vol_slider), 73, LV_ANIM_OFF);
      lv_obj_send_event(id(ui_speaker_vol_slider), LV_EVENT_RELEASED, nullptr);
      if (id(ui_owner_writes) != 1 || id(master_volume).state != 73) ::exit(43);
      lv_obj_add_state(id(ui_speaker_vol_slider), LV_STATE_PRESSED);
      lv_slider_set_value(id(ui_speaker_vol_slider), 46, LV_ANIM_OFF);
      id(master_volume).publish_state(12.0f);
      if (lv_slider_get_value(id(ui_speaker_vol_slider)) != 46 || id(ui_owner_writes) != 1) ::exit(44);
      lv_obj_remove_state(id(ui_speaker_vol_slider), LV_STATE_PRESSED);
      lv_obj_send_event(id(ui_speaker_vol_slider), LV_EVENT_RELEASED, nullptr);
      if (id(ui_owner_writes) != 2 || id(master_volume).state != 46) ::exit(45);
      id(ui_accept_writes) = false;
      lv_slider_set_value(id(ui_speaker_vol_slider), 91, LV_ANIM_OFF);
      lv_obj_send_event(id(ui_speaker_vol_slider), LV_EVENT_RELEASED, nullptr);
      if (id(ui_owner_writes) != 3 || id(master_volume).state != 46 || lv_slider_get_value(id(ui_speaker_vol_slider)) != 46) ::exit(46);
      id(ui_accept_writes) = true;
      id(master_volume).publish_state(100.0f);
      printf("UI_PROBE owner_sync=pass user_write=pass drag=pass reject=pass\n");
    '''
    if host.get("media_player"):
        probe += 'if (id(speaker_media_player).writes != 0) ::exit(47);\n'
    capture.append({"lambda": Lambda(probe)})
    # Keep global overlays in the scene. Test data gives otherwise blank clock
    # and battery labels their realistic worst-case width, without restyling.
    overlay_ids = {str(n.get("id")) for n in nodes(top_layer)}
    overlay_values = {"top_clock_label": "12:34", "top_battery_label": "100%"}
    for label, value in overlay_values.items():
        if label in overlay_ids:
            capture.append({"lvgl.label.update": {"id": label, "text": value}})
    if policy:
        capture.append({"lambda": Lambda("auto *target = id(" + page_id + ")->obj;\n" + policy)})
    for position in ("top", "mid", "end"):
        amount = "0" if position == "top" else "lv_obj_get_scroll_bottom(id(settings_page_wrapper)) / 2" if position == "mid" else "LV_COORD_MAX"
        capture.extend([
            {"lambda": Lambda("lv_obj_scroll_to_y(id(settings_page_wrapper), " + amount + ", LV_ANIM_OFF); lv_refr_now(nullptr);")},
            {"snapshot.take": {"id": "render_display", "filename": position}},
        ])
    geometry_ids = [name for name in ("settings_page_wrapper", "settings_general_controls", "settings_audio_column", "settings_back_button")
                    if name in declared]
    geometry = "\n".join('printf("UI_BOUNDS ' + name + ' x=%d y=%d w=%d h=%d\\n", '
                         'int(lv_obj_get_x(id(' + name + '))), int(lv_obj_get_y(id(' + name + '))), '
                         'int(lv_obj_get_width(id(' + name + '))), int(lv_obj_get_height(id(' + name + '))));'
                         for name in geometry_ids)
    capture.append({"lambda": Lambda(geometry)})
    if policy:
        capture.append({"lambda": Lambda("auto *target = id(main_page)->obj;\n" + policy + '\nif (lv_obj_has_flag(id(top_clock_label), LV_OBJ_FLAG_HIDDEN) || lv_obj_has_flag(id(top_battery_label), LV_OBJ_FLAG_HIDDEN)) ::exit(48);\nprintf("UI_PROBE overlay_restore=pass\\n");')})
    if pointer:
        capture.extend([{"script.execute": "compute_layout_metrics"}, {"script.execute": "ui_phone_geometry"}])
        pointer_code = r'''
          esphome::ui_fixture::PointerProbe input(lv_display_get_default());
          struct Trace {const char *name;bool matrix;};
          Trace trace_data[]={{"digits",true},{"call",false},{"dialer",false}};
          lv_obj_t *trace_objects[]={id(call_dial_row_123)->obj,id(call_dial_call_btn),id(call_dialer_page)->obj};
          lv_event_dsc_t *trace_descriptors[3];
          for(unsigned i=0;i<3;i++) trace_descriptors[i]=lv_obj_add_event_cb(trace_objects[i],[](lv_event_t *event){
            auto code=lv_event_get_code(event);const char *name=nullptr;
            switch(code){case LV_EVENT_PRESSED:name="PRESSED";break;case LV_EVENT_PRESSING:name="PRESSING";break;
              case LV_EVENT_VALUE_CHANGED:name="VALUE_CHANGED";break;case LV_EVENT_RELEASED:name="RELEASED";break;
              case LV_EVENT_CLICKED:name="CLICKED";break;case LV_EVENT_SHORT_CLICKED:name="SHORT_CLICKED";break;
              case LV_EVENT_GESTURE:name="GESTURE";break;default:return;}
            auto *trace=static_cast<Trace*>(lv_event_get_user_data(event));
            auto *object=static_cast<lv_obj_t*>(lv_event_get_current_target(event));
            unsigned selected=trace->matrix?lv_buttonmatrix_get_selected_button(object):65535;
            printf("UI_EVENT tick=%u object=%s event=%s selected=%u anim=%u length=%u\n",unsigned(lv_tick_get()),trace->name,name,selected,
              unsigned(lv_display_get_screen_prev(lv_display_get_default())!=nullptr),unsigned(id(voip_dial_length)));
          },LV_EVENT_ALL,&trace_data[i]);
          auto enter_view = [&](int step) {
            id(settings_active)=step==3; id(current_mode)=step==0?0:1; id(voip_dialer_active)=step==2;
            id(phone).idle=true; id(phone).in_call=false;id(call_ended_active)=false;
            id(g_ota_in_progress)=false;id(runtime).timer_ringing=false;id(init_in_progress)=false;
            id(draw_display).execute(); input.pump(350);
            id(ui_digit_events)=id(ui_call_events)=id(ui_options_events)=id(ui_assistant_events)=0;
            id(ui_owner_writes)=0; esphome::ui_fixture::voip_action_writes=0;
            id(voip_dial_clear).execute();id(ui_buffer_mutations)=0;esphome::ui_fixture::dial_requests=0;
          };
          auto enter_dialer = [&] { enter_view(2); };
          auto gesture = [&](lv_obj_t *object, int delta, lv_obj_t *expected, bool header=false) {
            lv_obj_update_layout(lv_screen_active());
            lv_area_t bounds; lv_obj_get_coords(object,&bounds);
            int x=(bounds.x1+bounds.x2)/2;
            int y=header&&delta>0?bounds.y2-2:(bounds.y1+bounds.y2)/2;
            int end=std::max(0,std::min(int(lv_display_get_vertical_resolution(lv_display_get_default()))-1,y-delta*80));
            auto observed=input.watch(lv_screen_active(),LV_EVENT_GESTURE);
            input.swipe(x,y,x,end); input.pump(300);
            printf("UI_POINTER navigation delta=%d x=%d y=%d end=%d gestures=%llu matched=%u mutations=%u calls=%u writes=%u\n",delta,x,y,end,
              static_cast<unsigned long long>(input.count(observed)),unsigned(lv_screen_active()==expected),id(ui_buffer_mutations),esphome::ui_fixture::dial_requests,id(ui_owner_writes));
            if(input.count(observed)!=1 || lv_screen_active()!=expected) ::exit(61);
            if(id(ui_buffer_mutations)||esphome::ui_fixture::dial_requests||id(ui_owner_writes)||esphome::ui_fixture::voip_action_writes) ::exit(62);
          };
          printf("UI_BOOT registry=%u first_valid=%u\n",unsigned(id(g_settings_pages).size()),
            unsigned(!id(g_settings_pages).empty()&&id(g_settings_pages)[0]==id(SETTINGS_PAGE)->obj));
          if(id(g_settings_pages).size()!=1 || id(g_settings_pages)[0]!=id(SETTINGS_PAGE)->obj) ::exit(60);
          enter_view(0);
          gesture(id(idle_page)->obj,-1,id(idle_page)->obj);
          gesture(id(idle_page)->obj,1,id(call_idle_page)->obj);
          gesture(id(call_idle_page)->obj,1,id(call_dialer_page)->obj);
          gesture(id(call_dial_row_123)->obj,1,id(SETTINGS_PAGE)->obj);
          gesture(id(settings_page_header),1,id(SETTINGS_PAGE)->obj,true);
          gesture(id(settings_page_header),-1,id(call_dialer_page)->obj,true);
          gesture(id(call_dial_row_123)->obj,-1,id(call_idle_page)->obj);
          gesture(id(call_idle_page)->obj,-1,id(idle_page)->obj);
          printf("UI_POINTER ordered_navigation=pass bounded_ends=pass real_registry=pass\n");
          enter_dialer();
          id(voip_dial_buffer)[0]='4';id(voip_dial_buffer)[1]='2';id(voip_dial_buffer)[2]='\0';id(voip_dial_length)=2;
          lv_area_t call_swipe;lv_obj_get_coords(id(call_dial_call_btn),&call_swipe);
          int call_x=(call_swipe.x1+call_swipe.x2)/2,call_y=(call_swipe.y1+call_swipe.y2)/2;
          gesture(id(call_dial_call_btn),1,id(SETTINGS_PAGE)->obj);
          enter_dialer();
          lv_area_t digits; lv_obj_get_coords(id(call_dial_row_123)->obj,&digits);
          input.tap((digits.x1+digits.x2)/2,(digits.y1+digits.y2)/2);
          if(id(ui_buffer_mutations)!=1 || id(voip_dial_length)!=1) ::exit(63);
          enter_dialer();id(voip_dial_buffer)[0]='4';id(voip_dial_buffer)[1]='2';id(voip_dial_buffer)[2]='\0';id(voip_dial_length)=2;
          lv_area_t call;lv_obj_get_coords(id(call_dial_call_btn),&call);input.tap((call.x1+call.x2)/2,(call.y1+call.y2)/2);
          if(esphome::ui_fixture::dial_requests!=1 || esphome::ui_fixture::last_dial_target!="42") ::exit(70);
          enter_dialer();
          id(voip_dial_buffer)[0]='4';id(voip_dial_buffer)[1]='2';id(voip_dial_buffer)[2]='\0';id(voip_dial_length)=2;id(phone).idle=false;
          input.tap((call.x1+call.x2)/2,(call.y1+call.y2)/2);
          if(esphome::ui_fixture::dial_requests!=0) ::exit(72);
          id(phone).idle=true;
          printf("UI_POINTER source_dialer_gestures=pass suppressed_commands=pass positive_digit_tap=pass reads=%llu\n",static_cast<unsigned long long>(input.read_count()));
          enter_view(3);
          lv_obj_scroll_to_y(id(settings_page_wrapper),lv_obj_get_scroll_bottom(id(settings_page_wrapper))/2,LV_ANIM_OFF);
          input.pump(50);lv_area_t body;lv_obj_get_coords(id(settings_page_wrapper),&body);
          int scroll_before=lv_obj_get_scroll_y(id(settings_page_wrapper));
          input.swipe(body.x1+20,(body.y1+body.y2)/2,body.x1+20,(body.y1+body.y2)/2-60);input.pump(120);
          if(lv_screen_active()!=id(SETTINGS_PAGE)->obj || lv_obj_get_scroll_y(id(settings_page_wrapper))==scroll_before) ::exit(77);
          printf("UI_POINTER options_body_scroll=pass navigation_suppressed=pass\n");
          enter_view(1);id(phone).idle=false;id(phone).in_call=true;id(draw_display).execute();input.pump(60);
          if(lv_screen_active()!=id(call_in_call_page)->obj) ::exit(78);
          lv_obj_t *hangup=nullptr;
          for(unsigned i=0;i<lv_obj_get_child_count(id(call_in_call_page)->obj);++i){
            auto *child=lv_obj_get_child(id(call_in_call_page)->obj,i);
            if(lv_obj_check_type(child,&lv_button_class)) hangup=child;
          }
          if(hangup==nullptr) ::exit(79);
          gesture(hangup,1,id(call_in_call_page)->obj);
          gesture(hangup,-1,id(call_in_call_page)->obj);
          lv_area_t hangup_area;lv_obj_get_coords(hangup,&hangup_area);
          input.tap((hangup_area.x1+hangup_area.x2)/2,(hangup_area.y1+hangup_area.y2)/2);
          if(esphome::ui_fixture::voip_action_writes!=1) ::exit(80);
          printf("UI_POINTER in_call_gesture=pass hangup_suppressed=pass positive_hangup_tap=pass\n");
          enter_view(3);
          lv_screen_load(id(SETTINGS_PAGE)->obj); input.pump(80);
          id(master_volume).publish_state(50.0f); id(mic_gain).publish_state(5.0f);
          PLAYER_MIDPOINT
          const char *names[]={"master","playback","mic"};
          lv_obj_t *sliders[]={id(ui_speaker_vol_slider),PLAYBACK_SLIDER id(ui_mic_vol_slider)};
          int previous_center=-100000;
          for(auto *slider:sliders) {
            lv_area_t area; lv_obj_get_coords(slider,&area);
            int center=(area.y1+area.y2)/2;
            if(center-previous_center < EXPECTED_HIT_SIZE) ::exit(65);
            previous_center=center;
          }
          unsigned index=0;
          for(auto *slider:sliders) {
            lv_area_t viewport,area; lv_obj_get_coords(id(settings_page_wrapper),&viewport);
            lv_obj_get_coords(slider,&area);
            int offset=(area.y1+area.y2-viewport.y1-viewport.y2)/2;
            lv_obj_scroll_to_y(id(settings_page_wrapper),lv_obj_get_scroll_y(id(settings_page_wrapper))+offset,LV_ANIM_OFF);
            input.pump(40);lv_obj_update_layout(id(SETTINGS_PAGE)->obj);lv_obj_get_coords(slider,&area);
            int x=(area.x1+area.x2)/2,y=(area.y1+area.y2)/2;
            auto pressed=input.watch(slider,LV_EVENT_PRESSED);
            auto before=input.count(pressed);
            input.tap(x,y);
            if(input.count(pressed)!=before+1) ::exit(66);
            before=input.count(pressed);
            lv_obj_update_layout(id(SETTINGS_PAGE)->obj);lv_obj_get_coords(slider,&area);
            x=(area.x1+area.x2)/2;y=(area.y1+area.y2)/2;
            lv_area_t click_area;lv_obj_get_click_area(slider,&click_area);
            int pad=click_area.y2-area.y2;
            lv_point_t extended{x,area.y2+std::max(1,pad-1)};
            printf("UI_HIT slider=%s area=%d,%d,%d,%d point=%d,%d value=%d hit=%u\n",names[index],int(area.x1),int(area.y1),int(area.x2),int(area.y2),int(extended.x),int(extended.y),int(lv_slider_get_value(slider)),unsigned(lv_obj_hit_test(slider,&extended)));
            auto *top_hit=lv_indev_search_obj(lv_screen_active(),&extended);
            auto *next_sibling=lv_obj_get_child(lv_obj_get_parent(slider),lv_obj_get_index(slider)+1);
            lv_area_t next_area;lv_obj_get_coords(next_sibling,&next_area);
            printf("UI_HIT_TARGET slider=%s picked_slider=%u picked_next_sibling=%u next=%d,%d,%d,%d next_clickable=%u\n",names[index],unsigned(top_hit==slider),unsigned(top_hit==next_sibling),int(next_area.x1),int(next_area.y1),int(next_area.x2),int(next_area.y2),unsigned(lv_obj_has_flag(next_sibling,LV_OBJ_FLAG_CLICKABLE)));
            input.tap(x,area.y2+std::max(1,pad-1));
            if(input.count(pressed)!=before+1) ::exit(67);
            before=input.count(pressed);
            input.tap(area.x1+2,y);
            if(input.count(pressed)!=before) ::exit(68);
            printf("UI_POINTER slider=%s knob=pass extension=pass track_rejected=pass pad=%d\n",names[index++],pad);
          }
          for(int requested:{24,lv_slider_get_max_value(id(ui_mic_vol_slider))}) {
            id(mic_gain).publish_state(static_cast<float>(requested));input.pump(40);
            lv_obj_update_layout(id(SETTINGS_PAGE)->obj);
            lv_area_t area,viewport;lv_obj_get_coords(id(ui_mic_vol_slider),&area);lv_obj_get_coords(id(settings_page_wrapper),&viewport);
            int offset=(area.y1+area.y2-viewport.y1-viewport.y2)/2;
            lv_obj_scroll_to_y(id(settings_page_wrapper),lv_obj_get_scroll_y(id(settings_page_wrapper))+offset,LV_ANIM_OFF);input.pump(40);
            lv_obj_update_layout(id(SETTINGS_PAGE)->obj);lv_obj_get_coords(id(ui_mic_vol_slider),&area);
            int low=lv_slider_get_min_value(id(ui_mic_vol_slider)),high=lv_slider_get_max_value(id(ui_mic_vol_slider));
            int x=area.x1+(int64_t(area.x2-area.x1)*(requested-low))/(high-low);
            int y=(area.y1+area.y2)/2;
            auto pressed=input.watch(id(ui_mic_vol_slider),LV_EVENT_PRESSED);auto before=input.count(pressed);
            input.tap(x,y);if(input.count(pressed)!=before+1) ::exit(74);
            id(mic_gain).publish_state(static_cast<float>(requested));input.pump(40);lv_obj_update_layout(id(SETTINGS_PAGE)->obj);
            lv_obj_get_coords(id(ui_mic_vol_slider),&area);lv_obj_get_coords(id(settings_page_wrapper),&viewport);
            x=area.x1+(int64_t(area.x2-area.x1)*(requested-low))/(high-low);
            int bottom=area.y2;lv_point_t point{x,bottom+1};
            while(point.y<viewport.y2 && lv_obj_hit_test(id(ui_mic_vol_slider),&point)){bottom=point.y;point.y++;}
            point.y=bottom-1;
            if(bottom<=area.y2 || lv_indev_search_obj(lv_screen_active(),&point)!=id(ui_mic_vol_slider)) ::exit(75);
            bool mute_before=id(speaker_mute).state;before=input.count(pressed);
            input.tap(point.x,point.y);
            if(input.count(pressed)!=before+1 || id(speaker_mute).state!=mute_before) ::exit(76);
            printf("UI_POINTER mic_value=%d knob=pass extended_topmost=pass no_switch_overlap=pass extent=%d\n",requested,bottom-area.y2);
          }
          lv_area_t toggle,viewport;lv_obj_get_coords(id(ui_speaker_mute_sw),&toggle);lv_obj_get_coords(id(settings_page_wrapper),&viewport);
          int offset=(toggle.y1+toggle.y2-viewport.y1-viewport.y2)/2;
          lv_obj_scroll_to_y(id(settings_page_wrapper),lv_obj_get_scroll_y(id(settings_page_wrapper))+offset,LV_ANIM_OFF);input.pump(40);
          lv_obj_update_layout(id(SETTINGS_PAGE)->obj);lv_obj_get_coords(id(ui_speaker_mute_sw),&toggle);
          bool before_mute=id(speaker_mute).state;id(ui_owner_writes)=0;
          input.tap((toggle.x1+toggle.x2)/2,(toggle.y1+toggle.y2)/2);
          if(id(speaker_mute).state==before_mute || id(ui_owner_writes)!=1) ::exit(73);
          printf("UI_POINTER passive_row_child_switch=pass writes=1\n");
          for(unsigned i=0;i<3;i++)lv_obj_remove_event_dsc(trace_objects[i],trace_descriptors[i]);
        '''.replace("SETTINGS_PAGE", page_id).replace("APPLY_OVERLAY_POLICY", policy or "(void)target;")
        pointer_code = pointer_code.replace("PLAYER_MIDPOINT", "id(speaker_media_player).volume=0.5f;id(speaker_media_player).publish_state();" if host.get("media_player") else "")
        pointer_code = pointer_code.replace("PLAYBACK_SLIDER", "id(ui_playback_vol_slider)," if host.get("media_player") else "")
        pointer_code = pointer_code.replace("EXPECTED_HIT_SIZE", "40" if width <= 240 else "48")
        capture.append({"lambda": Lambda(pointer_code)})
    capture.append({"lambda": Lambda("::exit(0);")})
    host["esphome"]["on_boot"] = registrations + [{"priority": -300, "then": capture}]
    assert host["lvgl"]["pages"][-1] == yaml_input(page)
    return host, page_hash


def _header_widgets(value, names):
    """Retain production header leaves and their exact container geometry."""
    if isinstance(value, list):
        return [projected for item in value if (projected := _header_widgets(item, names)) is not None]
    if not isinstance(value, dict):
        return None
    if str(value.get("id", "")) in names:
        return copy.deepcopy(value)
    projected = copy.deepcopy(value)
    retained = False
    for key, item in value.items():
        if isinstance(item, (dict, list)):
            child = _header_widgets(item, names)
            if child:
                projected[key] = child
                retained = True
            elif key in ("widgets", "tiles") or key.startswith("on_"):
                projected.pop(key, None)
    if not retained:
        return None
    return {key: item for key, item in projected.items() if not key.startswith("on_")}


def make_status_config(config, page_id, width, height, build_parent):
    """Partial pixel proof: exact status widgets/fonts, with fixture input data."""
    lvgl = config["lvgl"][0] if isinstance(config["lvgl"], list) else config["lvgl"]
    original = next(page for page in lvgl["pages"] if str(page["id"]) == page_id)
    page_hash = hashlib.sha256(yaml_util.dump(original).encode()).hexdigest()
    header_names = {"weather_clock_label", "weather_date_label"}
    headers = _header_widgets(original.get("widgets", []), header_names) or []
    # A dynamic P4 landscape card needs its layout owner. Avoid projecting its
    # initial one-pixel placeholder as if it were the running device geometry.
    if any(item.get("width") == 1 or item.get("height") == 1 for item in nodes(headers)):
        headers = []
    status_names = {"top_clock_label", "top_battery_label", "top_voicemail_label"}
    top = [copy.deepcopy(widget) for widget in lvgl.get("top_layer", {}).get("widgets", [])
           if any(str(node.get("id", "")) in status_names for node in nodes(widget))]
    if not top:
        raise ValueError("Production status widgets missing")
    page = {"id": page_id, "bg_color": original.get("bg_color", 0), "widgets": yaml_input(headers)}
    host = {
        "esphome": {"name": f"lvgl-status-{width}-{height}", "build_path": str(build_parent / "build"),
                    "compile_process_limit": 2, "includes": [str(ROOT / "tests/fixtures/lvgl_host/status_inputs.h")]},
        "host": {}, "logger": {"level": "INFO"},
        "globals": [{"id": "ui_voicemail_waiting", "type": "bool", "initial_value": "false"}],
        "display": [{"platform": "snapshot", "id": "render_display",
                     "dimensions": {"width": width, "height": height}, "auto_clear_enabled": False}],
        "lvgl": {"id": "render_lvgl", "displays": ["render_display"], "pages": [page],
                 "top_layer": {"widgets": yaml_input(top)}, "color_depth": 16},
        "script": [],
    }
    ids = widget_ids([page], {"widgets": top})
    boot = [{"lvgl.page.show": page_id}]
    for name, value in (("top_clock_label", "12:34"), ("weather_clock_label", "12:34"),
                        ("weather_date_label", "Thursday 29 October")):
        if name in ids:
            boot.append({"lvgl.label.update": {"id": name, "text": value}})
    if "top_battery_label" in ids:
        owner = find_id(config, "script", "update_idle_labels")
        def updates_status(node):
            action = node.get("lvgl.label.update")
            if not isinstance(action, dict):
                return False
            targets = action.get("id", [])
            if not isinstance(targets, list):
                targets = [targets]
            return any(str(target.get("id", "") if isinstance(target, dict) else target) == "top_battery_label"
                       for target in targets)
        formatter = next(copy.deepcopy(node) for node in nodes(owner) if updates_status(node))
        for name in references(formatter)-ids-{"ui_voicemail_waiting"}:
            if find_id(config, "sensor", name):
                host.setdefault("sensor", []).append({"platform": "template", "id": name, "update_interval": "never"})
                boot.append({"sensor.template.publish": {"id": name, "state": 100.0}})
            elif find_id(config, "binary_sensor", name):
                host.setdefault("binary_sensor", []).append({"platform": "template", "id": name})
                boot.append({"binary_sensor.template.publish": {"id": name, "state": True}})
            else:
                raise ValueError(f"Unknown production status input {name}")
        host["script"].append({"id": "status_snapshot_refresh", "then": [formatter]})
        refresh = "status_snapshot_refresh"
    else:
        owner = copy.deepcopy(find_id(config, "script", "update_voicemail_status"))
        if owner is None:
            raise ValueError("Production voicemail visibility owner missing")
        host["script"].append(owner)
        refresh = "update_voicemail_status"
    for state in ("off", "on"):
        boot.extend([
            {"globals.set": {"id": "ui_voicemail_waiting", "value": "true" if state == "on" else "false"}},
            {"script.execute": refresh},
            {"lambda": Lambda("lv_obj_update_layout(id(" + page_id + ")->obj); lv_refr_now(nullptr);")},
            {"snapshot.take": {"id": "render_display", "filename": state}},
        ])
    bounds = []
    for name in sorted(ids & (status_names | header_names)):
        bounds.append('lv_area_t area_' + name + '; lv_obj_get_coords(id(' + name + '), &area_' + name + '); ' +
                      'printf("UI_STATUS_BOUNDS ' + name + ' %d %d %d %d\\n",int(area_' + name + '.x1),int(area_' + name + '.y1),int(area_' + name + '.x2),int(area_' + name + '.y2));')
    boot.append({"lambda": Lambda("\n".join(bounds) + "\n::exit(0);")})
    host["esphome"]["on_boot"] = [{"priority": -300, "then": boot}]
    fonts = {str(font["id"]): font for font in config["font"]}
    used = set(strings(host["lvgl"])) & set(fonts)
    host["font"] = [copy.deepcopy(fonts[name]) for name in sorted(used)]
    return host, page_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--page", required=True)
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--build-parent", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--round", action="store_true", help="Also show the physical circular viewport, retaining raw captures")
    parser.add_argument("--pointer", action="store_true", help="Exercise the real production dialer with a host LVGL pointer")
    parser.add_argument("--status-only", action="store_true", help="Render exact production status overlays off/on, without the full page or input qualification")
    args = parser.parse_args()
    args.input = args.input.resolve()
    args.output = args.output.resolve()
    args.build_parent = args.build_parent.resolve()
    if args.status_only and args.pointer:
        parser.error("--status-only and --pointer qualify different scopes")
    capture_names = ("off", "on") if args.status_only else ("top", "mid", "end")
    args.output.mkdir(parents=True, exist_ok=True)
    args.build_parent.mkdir(parents=True, exist_ok=True)
    production = yaml_util.load_yaml(args.input)
    config, page_hash = (make_status_config(production, args.page, args.width, args.height, args.build_parent)
                         if args.status_only else make_host_config(production, args.page, args.width, args.height, args.build_parent, args.pointer))
    path = args.build_parent / "host.yaml"
    path.write_text(yaml_util.dump(config))
    shutil.copyfile(path, args.output / "host.yaml")
    result = {"production_page_sha256": page_hash, "source": str(args.input),
              "width": args.width, "height": args.height, "scope": "Production page pixels with host data owners; no hardware or audio qualification."}
    if args.status_only:
        result["scope"] = "Status-overlay-only native LVGL pixels: production widgets/fonts/positions and adjacent static header containers; fixture battery/network/clock/MWI input. Avatar, media, other page content, gestures and hardware are outside this proof."
        result["states"] = {"off": False, "on": True}
    policy = overlay_policy(production)
    result["production_overlay_policy_sha256"] = hashlib.sha256(policy.encode()).hexdigest() if policy else None
    result["status"] = "prepared"
    if args.pointer:
        lvgl = production["lvgl"][0] if isinstance(production["lvgl"], list) else production["lvgl"]
        result["production_phone_pages_sha256"] = {
            str(page["id"]): hashlib.sha256(yaml_util.dump(page).encode()).hexdigest()
            for page in lvgl["pages"] if str(page.get("id", "")).startswith("call_")}
        result["production_phone_geometry_sha256"] = hashlib.sha256(phone_geometry(production).encode()).hexdigest()
        result["production_page_transition_sha256"] = hashlib.sha256(page_transition(production).encode()).hexdigest()
        result["production_page_selector_sha256"] = hashlib.sha256(page_selector(production).encode()).hexdigest()
        result["production_settings_registry_boot_sha256"] = hashlib.sha256(yaml_util.dump(settings_registry_boot(production)).encode()).hexdigest()
        result["pointer_scope"] = "Real LVGL pointer and production phone widgets/geometry; backend commands are counter-only boundaries, no SIP or hardware implementation."
    if not args.prepare_only:
        (args.output / "RESULTS.json").write_text(json.dumps(result, indent=2) + "\n")
        with (args.output / "compile.log").open("w") as stream:
            compiled = subprocess.run([sys.executable, "-m", "esphome", "compile", str(path)], stdout=stream, stderr=subprocess.STDOUT)
        if compiled.returncode:
            result.update(status="failed", phase="host_compile", returncode=compiled.returncode)
            (args.output / "RESULTS.json").write_text(json.dumps(result, indent=2) + "\n")
            raise RuntimeError(f"Host compilation failed; see {args.output / 'compile.log'}")
        executable = args.build_parent / "build/.pioenvs" / config["esphome"]["name"] / "program"
        captures = args.build_parent / ".esphome/snapshots" / config["esphome"]["name"]
        for position in capture_names:
            stale = captures / (position + ".bmp")
            if stale.exists():
                archive = args.output / "previous-incomplete-snapshots"
                archive.mkdir(exist_ok=True)
                shutil.move(stale, archive / stale.name)
        with (args.output / "run.log").open("w") as stream:
            ran = subprocess.run([str(executable)], stdout=stream, stderr=subprocess.STDOUT, timeout=30, cwd=args.output)
        if ran.returncode:
            result.update(status="failed", phase="host_runtime", returncode=ran.returncode)
            (args.output / "RESULTS.json").write_text(json.dumps(result, indent=2) + "\n")
            raise RuntimeError(f"Host runtime failed ({ran.returncode}); see {args.output / 'run.log'}")
        from PIL import Image, ImageDraw

        result["snapshots"] = []
        for position in capture_names:
            source = captures / (position + ".bmp")
            if not source.is_file():
                raise RuntimeError(f"Snapshot missing: {source}")
            destination = args.output / (position + ".bmp")
            shutil.move(source, destination)
            picture = Image.open(destination).convert("RGB")
            picture.save(args.output / (position + ".png"))
            if args.round:
                mask = Image.new("L", picture.size, 0)
                ImageDraw.Draw(mask).ellipse((0, 0, args.width - 1, args.height - 1), fill=255)
                panel = Image.new("RGB", picture.size, "black")
                panel.paste(picture, mask=mask)
                panel.save(args.output / (position + "-panel.png"))
            result["snapshots"].append(str(destination))
        result["status"] = "passed"
    (args.output / "RESULTS.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
