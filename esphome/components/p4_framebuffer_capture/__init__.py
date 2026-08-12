"""One-shot ESP32-P4 framebuffer capture for HIL diagnostics."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.mipi_dsi import display as mipi_dsi_display
from esphome.const import CONF_DISPLAY_ID, CONF_ID, CONF_PORT

CODEOWNERS = ["@n-IA-hane"]

CONF_HOST = "host"

p4_framebuffer_capture_ns = cg.esphome_ns.namespace("p4_framebuffer_capture")
P4FramebufferCapture = p4_framebuffer_capture_ns.class_(
    "P4FramebufferCapture", cg.Component
)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(P4FramebufferCapture),
        cv.Required(CONF_DISPLAY_ID): cv.use_id(mipi_dsi_display.MipiDsi),
        cv.Required(CONF_HOST): cv.ipv4address,
        cv.Optional(CONF_PORT, default=19090): cv.port,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    cg.add_define("USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    display = await cg.get_variable(config[CONF_DISPLAY_ID])
    cg.add(var.set_display(display))
    cg.add(var.set_host(str(config[CONF_HOST])))
    cg.add(var.set_port(config[CONF_PORT]))
