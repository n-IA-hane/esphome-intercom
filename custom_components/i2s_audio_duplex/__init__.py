"""I2S Audio Duplex Component - Full duplex I2S for simultaneous mic+speaker"""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import pins
from esphome.const import CONF_ID

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = []
AUTO_LOAD = ["switch", "number"]

CONF_I2S_LRCLK_PIN = "i2s_lrclk_pin"
CONF_I2S_BCLK_PIN = "i2s_bclk_pin"
CONF_I2S_MCLK_PIN = "i2s_mclk_pin"
CONF_I2S_DIN_PIN = "i2s_din_pin"
CONF_I2S_DOUT_PIN = "i2s_dout_pin"
CONF_SAMPLE_RATE = "sample_rate"
CONF_AEC_ID = "aec_id"

i2s_audio_duplex_ns = cg.esphome_ns.namespace("i2s_audio_duplex")
I2SAudioDuplex = i2s_audio_duplex_ns.class_("I2SAudioDuplex", cg.Component)

# Forward declare esp_aec
esp_aec_ns = cg.esphome_ns.namespace("esp_aec")
EspAec = esp_aec_ns.class_("EspAec")

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(I2SAudioDuplex),
    cv.Required(CONF_I2S_LRCLK_PIN): pins.internal_gpio_output_pin_number,
    cv.Required(CONF_I2S_BCLK_PIN): pins.internal_gpio_output_pin_number,
    cv.Optional(CONF_I2S_MCLK_PIN, default=-1): cv.Any(
        cv.int_range(min=-1, max=-1),
        pins.internal_gpio_output_pin_number,
    ),
    cv.Optional(CONF_I2S_DIN_PIN, default=-1): cv.Any(
        cv.int_range(min=-1, max=-1),
        pins.internal_gpio_input_pin_number,
    ),
    cv.Optional(CONF_I2S_DOUT_PIN, default=-1): cv.Any(
        cv.int_range(min=-1, max=-1),
        pins.internal_gpio_output_pin_number,
    ),
    cv.Optional(CONF_SAMPLE_RATE, default=16000): cv.int_range(min=8000, max=48000),
    cv.Optional(CONF_AEC_ID): cv.use_id(EspAec),
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    # Define USE_I2S_AUDIO_DUPLEX so other components know it's available
    cg.add_define("USE_I2S_AUDIO_DUPLEX")

    cg.add(var.set_lrclk_pin(config[CONF_I2S_LRCLK_PIN]))
    cg.add(var.set_bclk_pin(config[CONF_I2S_BCLK_PIN]))
    cg.add(var.set_mclk_pin(config[CONF_I2S_MCLK_PIN]))
    cg.add(var.set_din_pin(config[CONF_I2S_DIN_PIN]))
    cg.add(var.set_dout_pin(config[CONF_I2S_DOUT_PIN]))
    cg.add(var.set_sample_rate(config[CONF_SAMPLE_RATE]))

    # Link AEC if configured
    if CONF_AEC_ID in config:
        aec = await cg.get_variable(config[CONF_AEC_ID])
        cg.add(var.set_aec(aec))
        # Enable AEC compilation in i2s_audio_duplex
        cg.add_define("USE_ESP_AEC")
