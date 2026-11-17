"""Optional coordinate taps through the existing LVGL pointer pipeline."""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.lvgl.lvcode import LvglComponent
from esphome.const import CONF_ID, CONF_X, CONF_Y

DEPENDENCIES = ["lvgl"]
CONF_LVGL_ID = "lvgl_id"
ns = cg.esphome_ns.namespace("lvgl_touch_diagnostic")
LvglTouchDiagnostic = ns.class_("LvglTouchDiagnostic", cg.Component)
TapAction = ns.class_("TapAction", automation.Action)
SwipeAction = ns.class_("SwipeAction", automation.Action)

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(LvglTouchDiagnostic),
    cv.GenerateID(CONF_LVGL_ID): cv.use_id(LvglComponent),
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_lvgl(await cg.get_variable(config[CONF_LVGL_ID])))


@automation.register_action("lvgl_touch_diagnostic.tap", TapAction, cv.Schema({
    cv.Required(CONF_ID): cv.use_id(LvglTouchDiagnostic),
    cv.Required(CONF_X): cv.templatable(cv.int_),
    cv.Required(CONF_Y): cv.templatable(cv.int_),
}), synchronous=True)
async def tap_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    cg.add(var.set_parent(await cg.get_variable(config[CONF_ID])))
    cg.add(var.set_x(await cg.templatable(config[CONF_X], args, cg.int32)))
    cg.add(var.set_y(await cg.templatable(config[CONF_Y], args, cg.int32)))
    return var


@automation.register_action("lvgl_touch_diagnostic.swipe", SwipeAction, cv.Schema({
    cv.Required(CONF_ID): cv.use_id(LvglTouchDiagnostic),
    **{cv.Required(key): cv.templatable(cv.int_) for key in ("x1", "y1", "x2", "y2")},
    cv.Required("duration_ms"): cv.templatable(cv.int_range(min=80, max=2000)),
}), synchronous=True)
async def swipe_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    cg.add(var.set_parent(await cg.get_variable(config[CONF_ID])))
    for key in ("x1", "y1", "x2", "y2", "duration_ms"):
        cg.add(getattr(var, "set_" + key)(await cg.templatable(config[key], args, cg.int32)))
    return var
