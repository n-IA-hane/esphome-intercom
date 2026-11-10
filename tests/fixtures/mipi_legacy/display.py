"""Representative pre-owned-transform provider for the codegen handshake only.

The legacy contract publishes software rotation metadata and never emits the
owned-transform define. Keep this independent of Git ancestry and of the new
provider's capability detection. Native tests own legacy C++ interoperability.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import display
from esphome.components.const import BYTE_ORDER_LITTLE
from esphome.const import CONF_ID, CONF_MODEL, CONF_ROTATION

from . import mipi_dsi_ns


DEPENDENCIES = ["esp32", "esp_ldo", "psram"]
MipiDsi = mipi_dsi_ns.class_("MipiDsi", display.Display, cg.Component)
ColorBitness = display.display_ns.enum("ColorBitness")


def legacy_metadata(config):
    display.add_metadata(
        config[CONF_ID], 800, 1280,
        has_hardware_rotation=False,
        byte_order=BYTE_ORDER_LITTLE,
        has_writer=False,
        rotation=config.get(CONF_ROTATION, 0),
        draw_rounding=2,
    )
    return config


CONFIG_SCHEMA = cv.All(
    display.FULL_DISPLAY_SCHEMA.extend({
        cv.GenerateID(): cv.declare_id(MipiDsi),
        cv.Required(CONF_MODEL): cv.one_of("WAVESHARE-10.1-DSI-TOUCH-A"),
    }).extend(cv.COMPONENT_SCHEMA),
    legacy_metadata,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID], 800, 1280, ColorBitness.COLOR_BITNESS_565, 16)
    await display.register_display(var, config)
