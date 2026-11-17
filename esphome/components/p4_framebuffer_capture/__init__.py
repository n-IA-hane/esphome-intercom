"""One-shot ESP32-P4 rendered-frame capture for HIL diagnostics."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.esp32 import VARIANT_ESP32P4, only_on_variant
from esphome.components.mipi_dsi import display as mipi_dsi_display
from esphome.const import CONF_DISPLAY_ID, CONF_ID, CONF_PORT
from esphome.core import CORE
from esphome.types import ConfigType

CODEOWNERS = ["@n-IA-hane"]
CONF_HOST = "host"
CONF_DOWNSAMPLE = "downsample"

p4_framebuffer_capture_ns = cg.esphome_ns.namespace("p4_framebuffer_capture")
P4FramebufferCapture = p4_framebuffer_capture_ns.class_(
    "P4FramebufferCapture", cg.Component
)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(P4FramebufferCapture),
            cv.Required(CONF_DISPLAY_ID): cv.use_id(mipi_dsi_display.MipiDsi),
            cv.Required(CONF_HOST): cv.ipv4address,
            cv.Optional(CONF_PORT, default=19090): cv.port,
            cv.Optional(CONF_DOWNSAMPLE, default=1): cv.one_of(1, 2, 4, int=True),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
    only_on_variant(supported=[VARIANT_ESP32P4]),
)


def _require_rendered_frame_copy(config: ConfigType) -> ConfigType:
    readback_ids = {
        str(id_)
        for id_ in CORE.data.get("mipi_dsi", {}).get("framebuffer_readback_ids", [])
    }
    if str(config[CONF_DISPLAY_ID]) not in readback_ids:
        raise cv.Invalid(
            "p4_framebuffer_capture requires the selected RGB565 MIPI display to provide "
            "copy_frame_buffer_rgb565. This ESPHome source does not provide rendered-frame "
            "readback; disable this optional raw diagnostic or select the qualified driver."
        )
    return config


FINAL_VALIDATE_SCHEMA = _require_rendered_frame_copy


async def to_code(config: ConfigType) -> None:
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    display = await cg.get_variable(config[CONF_DISPLAY_ID])
    cg.add(var.set_display(display))
    cg.add(var.set_host(str(config[CONF_HOST])))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_downsample(config[CONF_DOWNSAMPLE]))
