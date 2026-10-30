"""Bounded, event-driven LVGL rows shared by phonebook and voicemail pages."""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.lvgl import lv_validation
from esphome.components.lvgl.types import lv_obj_t
from esphome.components.lvgl.widgets import get_widgets
from esphome.components.lvgl.defines import add_lv_use
from esphome.const import CONF_ID

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["lvgl"]
MULTI_CONF = True
ns = cg.esphome_ns.namespace("phone_list_view")
PhoneListView = ns.class_("PhoneListView", cg.Component)
RowData = ns.class_("RowData")
RefreshAction = ns.class_("RefreshAction", automation.Action)

def _features(config):
    # LVGL emits lv_conf.h before dependent components' to_code callbacks.
    add_lv_use("button", "label")
    return config


CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(PhoneListView),
    cv.Required("parent_id"): cv.use_id(lv_obj_t),
    cv.Required("row_count"): cv.lambda_,
    cv.Required("render_row"): cv.lambda_,
    cv.Optional("row_height", default=48): cv.int_range(min=36, max=160),
    cv.Optional("visible_rows", default=8): cv.int_range(min=2, max=32),
    cv.Optional("buffer_in_psram", default=True): cv.boolean,
    cv.Optional("empty_text", default="No entries"): cv.string_strict,
    cv.Optional("text_font", default="montserrat_14"): lv_validation.lv_font,
    cv.Optional("icon_font"): lv_validation.lv_font,
    cv.Optional("on_select"): automation.validate_automation(single=True),
}).extend(cv.COMPONENT_SCHEMA), _features)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    widget = (await get_widgets([{CONF_ID: config["parent_id"]}]))[0]
    cg.add(var.set_parent_widget(widget.obj))
    cg.add(var.set_row_height(config["row_height"]))
    cg.add(var.set_visible_rows(config["visible_rows"]))
    cg.add(var.set_buffer_in_psram(config["buffer_in_psram"]))
    cg.add(var.set_empty_text(config["empty_text"]))
    cg.add(var.set_text_font(await lv_validation.lv_font.process(config["text_font"])))
    cg.add(var.set_icon_font(await lv_validation.lv_font.process(config.get("icon_font", config["text_font"]))))
    count = await cg.process_lambda(config["row_count"], [], return_type=cg.uint32)
    render = await cg.process_lambda(config["render_row"], [(cg.uint32, "index"), (RowData.operator("ref"), "row")], return_type=cg.void)
    cg.add(var.set_count_callback(count))
    cg.add(var.set_render_callback(render))
    if "on_select" in config:
        await automation.build_automation(var.get_select_trigger(), [(cg.std_string, "row_id"), (cg.uint8, "action")], config["on_select"])


@automation.register_action("phone_list_view.refresh", RefreshAction, cv.maybe_simple_value(
    cv.Schema({cv.GenerateID(): cv.use_id(PhoneListView)}), key=CONF_ID), synchronous=True)
async def refresh_action(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var
