"""ESP32-P4 hardware H.264 source for the optional VoIP video media line."""

import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components import esp_video_camera, voip_stack
from esphome.components.esp32 import (
    VARIANT_ESP32P4,
    add_idf_component,
    only_on_variant,
)
from esphome.const import CONF_DEVICE, CONF_ID
from esphome.core import CORE

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32", "esp_video_camera", "voip_stack"]

CONF_CAMERA_ID = "camera_id"
CONF_WIDTH = "width"
CONF_HEIGHT = "height"
CONF_FRAMERATE = "framerate"
CONF_BITRATE = "bitrate"
CONF_GOP = "gop"

esp_h264_video_source_ns = cg.esphome_ns.namespace("esp_h264_video_source")
EspH264VideoSource = esp_h264_video_source_ns.class_(
    "EspH264VideoSource",
    cg.Component,
    voip_stack.EncodedVideoSource,
)


def _validate_dimensions(config):
    if config[CONF_WIDTH] % 16 != 0 or config[CONF_HEIGHT] % 16 != 0:
        raise cv.Invalid(
            "esp_h264_video_source width and height must be multiples of 16"
        )
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(EspH264VideoSource),
            cv.Required(CONF_CAMERA_ID): cv.use_id(
                esp_video_camera.ESPVideoCamera
            ),
            cv.Optional(CONF_WIDTH, default=400): cv.int_range(
                min=160, max=1280
            ),
            cv.Optional(CONF_HEIGHT, default=400): cv.int_range(
                min=120, max=720
            ),
            cv.Optional(CONF_FRAMERATE, default=10): cv.int_range(
                min=1, max=30
            ),
            cv.Optional(CONF_BITRATE, default=400000): cv.int_range(
                min=64000, max=4000000
            ),
            cv.Optional(CONF_GOP, default=30): cv.int_range(min=1, max=255),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    _validate_dimensions,
    cv.only_on_esp32,
    only_on_variant(supported=[VARIANT_ESP32P4]),
)


def _validate_raw_camera(config):
    if config[CONF_DEVICE] != "csi":
        raise cv.Invalid(
            "esp_h264_video_source requires camera_id to use device: csi"
        )
    return config


FINAL_VALIDATE_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_CAMERA_ID): fv.id_declaration_match_schema(
            _validate_raw_camera
        ),
    },
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    if not CORE.using_toolchain_esp_idf:
        raise cv.Invalid("esp_h264_video_source requires the esp-idf framework")

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    camera = await cg.get_variable(config[CONF_CAMERA_ID])
    cg.add(var.set_camera(camera))
    cg.add(var.set_width(config[CONF_WIDTH]))
    cg.add(var.set_height(config[CONF_HEIGHT]))
    cg.add(var.set_framerate(config[CONF_FRAMERATE]))
    cg.add(var.set_bitrate(config[CONF_BITRATE]))
    cg.add(var.set_gop(config[CONF_GOP]))

    # The implementation uses only Espressif's public P4 hardware encoder API.
    # Keeping the managed-component dependency here lets builds without this
    # source omit every H.264 symbol and buffer.
    add_idf_component(name="espressif/esp_h264", ref="1.3.6")
    # Espressif's encoder prefers its large deblocking buffer in internal RAM
    # and falls back to PSRAM only when the allocation does not fit. At small
    # resolutions that policy can consume nearly all P4 internal RAM. Keep the
    # component unchanged and override only that large-allocation preference.
    cg.add_build_flag("-Wl,--wrap=esp_h264_calloc_prefer")
    cg.add_define("USE_ESPHOME_VOIP_STACK_VIDEO_H264")
