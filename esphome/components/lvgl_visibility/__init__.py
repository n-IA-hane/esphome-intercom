"""Event-driven visibility for boot-created LVGL presentation widgets."""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.lvgl.lvcode import LvglComponent
from esphome.const import CONF_ID
from esphome.core import CORE, Lambda

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["lvgl"]
CONF_LVGL_ID = "lvgl_id"
CONF_ON_CHANGE = "on_change"
ns = cg.esphome_ns.namespace("lvgl_visibility")
LvglVisibility = ns.class_("LvglVisibility", cg.Component)
VisibilityPolicy = ns.class_("VisibilityPolicy")
CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(LvglVisibility),
    cv.GenerateID(CONF_LVGL_ID): cv.use_id(LvglComponent),
    cv.Optional(CONF_ON_CHANGE): automation.validate_automation(single=True),
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    parent = await cg.get_variable(config[CONF_LVGL_ID])
    pages = []
    for display in CORE.config["lvgl"]:
        if str(display[CONF_ID]) != str(config[CONF_LVGL_ID]):
            continue
        for page in display.get("pages", []):
            pages.append(await cg.get_variable(page[CONF_ID]))
    setup = [f"policy.reserve_roots({max(1, len(pages)) + 3});\n"]
    setup.extend(f"policy.add_root({page}->obj);\n" for page in pages)
    if not pages:
        setup.append(f"policy.add_root({parent}->get_screen_active());\n")
    setup.append(f"policy.attach({parent}->get_disp());\n")
    initializer = await cg.process_lambda(
        Lambda("".join(setup)), [(VisibilityPolicy.operator("ref"), "policy")],
        capture="", return_type=cg.void,
    )
    cg.add(var.set_initializer(initializer))
    if CONF_ON_CHANGE in config:
        await automation.build_automation(var.get_change_trigger(), [], config[CONF_ON_CHANGE])
