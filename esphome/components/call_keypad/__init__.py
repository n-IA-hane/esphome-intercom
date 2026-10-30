"""Presentation clock for a borrowed LVGL call keypad."""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.lvgl.types import lv_obj_t
from esphome.components.lvgl.widgets import get_widgets
from esphome.const import CONF_ID

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["lvgl"]
ns = cg.esphome_ns.namespace("call_keypad")
CallKeypad = ns.class_("CallKeypad", cg.Component)
CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(CallKeypad),
    cv.Required("name_label_id"): cv.use_id(lv_obj_t),
    cv.Required("duration_label_id"): cv.use_id(lv_obj_t),
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    name = (await get_widgets([{CONF_ID: config["name_label_id"]}]))[0]
    duration = (await get_widgets([{CONF_ID: config["duration_label_id"]}]))[0]
    cg.add(var.set_labels(name.obj, duration.obj))
