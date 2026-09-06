"""Opt-in hardware audio capture for qualification, never a media source."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import esp_audio_stack
from esphome.const import CONF_ID, CONF_PORT

DEPENDENCIES = ["esp_audio_stack"]
CONF_AUDIO_STACK_ID = "audio_stack_id"
CONF_HOST = "host"
CONF_DURATION_MS = "duration_ms"
CONF_BUFFER_BYTES = "buffer_bytes"
CONF_DEFERRED_TRANSFER = "deferred_transfer"

capture_ns = cg.esphome_ns.namespace("audio_stack_capture")
AudioStackCapture = capture_ns.class_("AudioStackCapture", cg.Component)

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(AudioStackCapture),
    cv.Required(CONF_AUDIO_STACK_ID): cv.use_id(esp_audio_stack.ESPAudioStack),
    cv.Required(CONF_HOST): cv.ipv4address,
    cv.Optional(CONF_PORT, default=19091): cv.port,
    cv.Optional(CONF_DURATION_MS, default=10000): cv.int_range(min=1000, max=3600000),
    cv.Optional(CONF_BUFFER_BYTES, default=128 * 1024): cv.int_range(min=32768, max=1024 * 1024),
    cv.Optional(CONF_DEFERRED_TRANSFER, default=False): cv.boolean,
}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    cg.add_define("USE_ESP_AUDIO_STACK_CAPTURE")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    stack = await cg.get_variable(config[CONF_AUDIO_STACK_ID])
    cg.add(var.set_audio_stack(stack))
    cg.add(var.set_host(str(config[CONF_HOST])))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_duration_ms(config[CONF_DURATION_MS]))
    cg.add(var.set_buffer_bytes(config[CONF_BUFFER_BYTES]))
    cg.add(var.set_deferred_transfer(config[CONF_DEFERRED_TRANSFER]))
