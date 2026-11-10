"""Reusable text storage and wrapping for the ten-row round presentation."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["lvgl"]
CONF_BUFFERS_IN_PSRAM = "buffers_in_psram"
CONF_DEBUG = "debug"
ns = cg.esphome_ns.namespace("lvgl_round_text")
LvglRoundText = ns.class_("LvglRoundText", cg.Component)
CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(LvglRoundText),
    cv.Optional(CONF_BUFFERS_IN_PSRAM, default=True): cv.boolean,
    cv.Optional(CONF_DEBUG, default=False): cv.boolean,
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_buffers_in_psram(config[CONF_BUFFERS_IN_PSRAM]))
    if config[CONF_DEBUG]:
        cg.add_define("USE_LVGL_ROUND_TEXT_DEBUG")
