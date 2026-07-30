"""Zero-copy ESP-Video JPEG source for the VoIP RTP adapter."""

import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components import esp_video_camera, voip_stack
from esphome.const import CONF_DEVICE, CONF_ID

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32", "esp_video_camera", "voip_stack"]

CONF_CAMERA_ID = "camera_id"
CONF_WIDTH = "width"
CONF_HEIGHT = "height"
CONF_FRAMERATE = "framerate"

esp_jpeg_video_source_ns = cg.esphome_ns.namespace("esp_jpeg_video_source")
EspJpegVideoSource = esp_jpeg_video_source_ns.class_(
    "EspJpegVideoSource",
    cg.Component,
    voip_stack.EncodedVideoSource,
)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(EspJpegVideoSource),
        cv.Required(CONF_CAMERA_ID): cv.use_id(
            esp_video_camera.ESPVideoCamera
        ),
        cv.Optional(CONF_WIDTH, default=400): cv.int_range(
            min=8, max=2040
        ),
        cv.Optional(CONF_HEIGHT, default=400): cv.int_range(
            min=8, max=2040
        ),
        cv.Optional(CONF_FRAMERATE, default=10): cv.int_range(
            min=1, max=60
        ),
    }
).extend(cv.COMPONENT_SCHEMA)


def _validate_encoded_camera(config):
    if config[CONF_DEVICE] == "csi":
        raise cv.Invalid(
            "esp_jpeg_video_source requires a JPEG-producing camera; "
            "device: csi exposes only raw frames"
        )
    return config


FINAL_VALIDATE_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_CAMERA_ID): fv.id_declaration_match_schema(
            _validate_encoded_camera
        ),
    },
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    camera = await cg.get_variable(config[CONF_CAMERA_ID])
    cg.add(var.set_camera(camera))
    cg.add(var.set_width(config[CONF_WIDTH]))
    cg.add(var.set_height(config[CONF_HEIGHT]))
    cg.add(var.set_framerate(config[CONF_FRAMERATE]))
    cg.add_define("USE_ESPHOME_VOIP_STACK_VIDEO_JPEG")
