"""ESP32-P4 encoded-video-to-LVGL renderer."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import voip_stack
from esphome.components.mipi_dsi import display as mipi_dsi_display
from esphome.components.esp32 import (
    VARIANT_ESP32P4,
    add_idf_component,
    add_idf_sdkconfig_option,
    only_on_variant,
)
from esphome.components.lvgl.defines import add_lv_use
from esphome.const import CONF_DISPLAY_ID, CONF_ID
from esphome.core import CORE

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32", "voip_stack", "lvgl"]

CONF_TASK_STACKS_IN_PSRAM = "task_stacks_in_psram"
CONF_CODEC = "codec"
CONF_WIDTH = "width"
CONF_HEIGHT = "height"
CONF_FRAMERATE = "framerate"
CONF_MAX_DECODE_WIDTH = "max_decode_width"
CONF_MAX_DECODE_HEIGHT = "max_decode_height"
CONF_ON_FIRST_FRAME = "on_first_frame"
CONF_ON_VIDEO_ENDED = "on_video_ended"
CONF_DISPLAY_ROTATION = "display_rotation"

p4_video_renderer_ns = cg.esphome_ns.namespace("p4_video_renderer")
P4VideoRenderer = p4_video_renderer_ns.class_(
    "P4VideoRenderer",
    cg.Component,
    voip_stack.EncodedVideoSink,
)

_CODECS = {
    "jpeg": "jpeg",
    "h264": "h264",
}


def _validate_dimensions(config):
    if config[CONF_CODEC] == "h264":
        if CONF_DISPLAY_ID not in config:
            raise cv.Invalid(
                "p4_video_renderer H.264 requires display_id for direct "
                "presentation"
            )
        for key in (CONF_WIDTH, CONF_HEIGHT):
            if config[key] % 16 != 0:
                raise cv.Invalid(
                    f"p4_video_renderer {key} must be a multiple of 16 "
                    "for H.264"
                )
        for key in (CONF_MAX_DECODE_WIDTH, CONF_MAX_DECODE_HEIGHT):
            if config[key] % 2 != 0:
                raise cv.Invalid(
                    f"p4_video_renderer {key} must be even for H.264 I420"
                )
        macroblocks = (
            ((config[CONF_WIDTH] + 15) // 16)
            * ((config[CONF_HEIGHT] + 15) // 16)
        )
        if macroblocks > 1620:
            raise cv.Invalid(
                "p4_video_renderer preferred H.264 resolution exceeds "
                "the advertised Level 3.0 limit of 1620 macroblocks"
            )
        if (
            config[CONF_MAX_DECODE_WIDTH] > 1280
            or config[CONF_MAX_DECODE_HEIGHT] > 800
        ):
            raise cv.Invalid(
                "p4_video_renderer H.264 decode bounds exceed the "
                "ESP32-P4 PPA surface envelope of 1280x800"
            )
    return config

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(P4VideoRenderer),
            cv.Required(CONF_CODEC): cv.enum(_CODECS, lower=True),
            # Decoder control flow is latency-sensitive while camera, PPA and
            # display DMA contend for PSRAM. Keep its stack internal by
            # default; the multi-megabyte frame buffers remain in PSRAM.
            cv.Optional(CONF_TASK_STACKS_IN_PSRAM, default=False): cv.boolean,
            # Preferred receive envelope advertised to SIP peers.
            cv.Optional(CONF_WIDTH, default=640): cv.int_range(
                min=160, max=1280
            ),
            cv.Optional(CONF_HEIGHT, default=480): cv.int_range(
                min=120, max=800
            ),
            cv.Optional(CONF_FRAMERATE, default=10): cv.int_range(
                min=1, max=30
            ),
            cv.Optional(CONF_MAX_DECODE_WIDTH, default=1280): cv.int_range(
                min=160, max=2040
            ),
            cv.Optional(CONF_MAX_DECODE_HEIGHT, default=800): cv.int_range(
                min=120, max=2040
            ),
            cv.Optional(CONF_ON_FIRST_FRAME): automation.validate_automation(
                single=True
            ),
            cv.Optional(CONF_ON_VIDEO_ENDED): automation.validate_automation(
                single=True
            ),
            # JPEG may use LVGL directly. H.264 requires this direct P4 path so
            # decoded surfaces never pass through a second LVGL scaling path.
            cv.Optional(CONF_DISPLAY_ID): cv.use_id(mipi_dsi_display.MipiDsi),
            cv.Optional(CONF_DISPLAY_ROTATION, default=0): cv.one_of(
                0, 90, 180, 270, int=True
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    _validate_dimensions,
    cv.only_on_esp32,
    only_on_variant(supported=[VARIANT_ESP32P4]),
)


async def to_code(config):
    if not CORE.using_toolchain_esp_idf:
        raise cv.Invalid("p4_video_renderer requires the esp-idf framework")

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_task_stacks_in_psram(config[CONF_TASK_STACKS_IN_PSRAM]))
    cg.add(var.set_width(config[CONF_WIDTH]))
    cg.add(var.set_height(config[CONF_HEIGHT]))
    cg.add(var.set_framerate(config[CONF_FRAMERATE]))
    cg.add(var.set_max_decode_width(config[CONF_MAX_DECODE_WIDTH]))
    cg.add(var.set_max_decode_height(config[CONF_MAX_DECODE_HEIGHT]))
    if display_id := config.get(CONF_DISPLAY_ID):
        display_var = await cg.get_variable(display_id)
        cg.add(var.set_direct_display(display_var))
        cg.add(var.set_display_rotation(config[CONF_DISPLAY_ROTATION]))
        cg.add_define("USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY")

    if on_first_frame := config.get(CONF_ON_FIRST_FRAME):
        await automation.build_automation(
            var.get_first_frame_trigger(), [], on_first_frame
        )
    if on_video_ended := config.get(CONF_ON_VIDEO_ENDED):
        await automation.build_automation(
            var.get_video_ended_trigger(), [], on_video_ended
        )

    if config[CONF_CODEC] == "jpeg":
        # The P4 driver serializes encoder and decoder transactions on the
        # physical JPEG codec. Keep both directions hardware accelerated and
        # let the driver's codec mutex time-slice them frame by frame.
        cg.add_define("USE_P4_VIDEO_RENDERER_JPEG")
    else:
        # This component creates the video image from C++, so it is invisible
        # to ESPHome's YAML widget scanner. Register the same transitive LVGL
        # uses as the built-in image widget before lv_conf.h is generated.
        add_lv_use("image", "label")
        cg.add_define("USE_P4_VIDEO_RENDERER_H264")
        cg.add_define("USE_ESPHOME_VOIP_STACK_VIDEO_H264")
        add_idf_component(name="espressif/esp_h264", ref="1.3.6")
        # esp_h264 decodes to planar I420. Use Espressif's cache-aligned P4
        # conversion kernel for the PPA-native O_UYY_E_VYY layout instead of
        # walking every luma/chroma byte in application C++.
        add_idf_component(name="espressif/esp_image_effects", ref="1.1.0")
        add_idf_sdkconfig_option("CONFIG_ESP_H264_DECODER_IRAM", True)
        # Espressif's dual-task tinyH264 path can lose its filter-task
        # notification on otherwise valid live Baseline streams
        # ("Fail to sync decoder(529)").  The P4 mono-task decoder is rated
        # above this profile's 10 fps receive envelope and avoids that
        # synchronization failure entirely.
        add_idf_sdkconfig_option("CONFIG_ESP_H264_DUAL_TASK", False)
