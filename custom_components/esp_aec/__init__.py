"""
ESP AEC Component for ESPHome
Acoustic Echo Cancellation using ESP-SR library

Requirements:
- ESP32-S3 with PSRAM (octal mode recommended)
- ESP-IDF framework
- esp-sr component added via yaml:
    esp32:
      framework:
        type: esp-idf
        components:
          - espressif/esp-sr
"""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32"]

CONF_SAMPLE_RATE = "sample_rate"
CONF_FILTER_LENGTH = "filter_length"

esp_aec_ns = cg.esphome_ns.namespace("esp_aec")
EspAec = esp_aec_ns.class_("EspAec", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(EspAec),
        cv.Optional(CONF_SAMPLE_RATE, default=16000): cv.int_range(min=8000, max=48000),
        cv.Optional(CONF_FILTER_LENGTH, default=4): cv.int_range(min=1, max=10),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    cg.add(var.set_sample_rate(config[CONF_SAMPLE_RATE]))
    cg.add(var.set_filter_length(config[CONF_FILTER_LENGTH]))

    # Enable AEC compilation (requires esp-sr in framework.components)
    cg.add_define("USE_ESP_AEC")
