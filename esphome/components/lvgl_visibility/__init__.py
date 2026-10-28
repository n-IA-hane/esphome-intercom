"""Event-driven visibility for boot-created LVGL presentation widgets."""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.lvgl.lvcode import LvglComponent
from esphome.const import CONF_ID
from esphome.core import CORE

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["lvgl"]
CONF_LVGL_ID = "lvgl_id"
CONF_ON_CHANGE = "on_change"
ns = cg.esphome_ns.namespace("lvgl_visibility")
LvglVisibility = ns.class_("LvglVisibility", cg.Component)
CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(LvglVisibility),
    cv.GenerateID(CONF_LVGL_ID): cv.use_id(LvglComponent),
    cv.Optional(CONF_ON_CHANGE): automation.validate_automation(single=True),
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    parent = await cg.get_variable(config[CONF_LVGL_ID])
    cg.add(var.set_lvgl(parent))
    for display in CORE.config["lvgl"]:
        if str(display[CONF_ID]) != str(config[CONF_LVGL_ID]):
            continue
        for page in display.get("pages", []):
            cg.add(var.add_page(await cg.get_variable(page[CONF_ID])))
    if CONF_ON_CHANGE in config:
        await automation.build_automation(var.get_change_trigger(), [], config[CONF_ON_CHANGE])
