"""ESP-Video camera platform for ESPHome (ESP32-P4).

Publishes the Espressif esp_video (V4L2) stream to Home Assistant as a native
``camera`` entity. Works with any auto-detected MIPI-CSI sensor through the
hardware JPEG encoder, and with USB-UVC cameras.

All Espressif sources are pulled through the IDF component manager as managed
components. Nothing is vendored.
"""

from pathlib import Path

import esphome.codegen as cg
from esphome.components import i2c
from esphome.components.esp32 import (
    add_extra_build_file,
    add_idf_component,
    add_idf_sdkconfig_option,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_DEVICE,
    CONF_I2C_ID,
    CONF_ID,
    CONF_RESOLUTION,
    CONF_ROTATION,
)
from esphome.core import CORE
from esphome.core.entity_helpers import setup_entity

CODEOWNERS = ["@youkorr"]
DEPENDENCIES = ["esp32", "i2c"]
AUTO_LOAD = ["camera"]

esp_video_camera_ns = cg.esphome_ns.namespace("esp_video_camera")
Camera = cg.esphome_ns.namespace("camera").class_(
    "Camera", cg.EntityBase, cg.Component
)
ESPVideoCamera = esp_video_camera_ns.class_(
    "ESPVideoCamera", Camera
)

CONF_JPEG_QUALITY = "jpeg_quality"
CONF_MAX_FRAMERATE = "max_framerate"
CONF_XCLK_PIN = "xclk_pin"
CONF_XCLK_FREQUENCY = "xclk_frequency"
CONF_ENABLE_XCLK = "enable_xclk"
CONF_ENABLE_UVC = "enable_uvc"

_RESOLUTION_ALIASES = ("QVGA", "VGA", "480P", "720P", "1080P")
_ROTATIONS = {0: 0, 90: 90, 180: 180, 270: 270}


def _validate_resolution(value):
    value = cv.string(value)
    if value.lower() == "auto":
        return "auto"
    if value.upper() in _RESOLUTION_ALIASES:
        return value.upper()
    parts = value.lower().split("x")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{int(parts[0])}x{int(parts[1])}"
    raise cv.Invalid(
        f"resolution '{value}' is invalid. Use 'auto', an alias "
        "(QVGA/VGA/480P/720P/1080P) or 'WIDTHxHEIGHT' (e.g. '1280x720')."
    )


def _validate_device(value):
    value = cv.string(value)
    low = value.lower()
    if low in ("jpeg", "uvc", "csi"):
        return low
    if low.startswith("uvc") and len(low) == 4 and low[3].isdigit():
        return low
    if value.startswith("/dev/video"):
        return value
    raise cv.Invalid(
        f"device '{value}' is invalid. Use 'jpeg' (hardware encoder, MIPI sensors), "
        "'uvc' / 'uvc0'..'uvc9' (USB-UVC camera), 'csi', or a '/dev/videoN' path."
    )


def _validate_rotation(value):
    value = cv.string(value).removesuffix("°")
    return cv.enum(_ROTATIONS, int=True)(value)


def _validate_config(config):
    if config[CONF_ROTATION] != 0 and config[CONF_DEVICE] not in (
        "jpeg",
        "csi",
        "/dev/video10",
    ):
        raise cv.Invalid(
            "rotation is supported only by a MIPI-CSI path (device: jpeg or "
            "device: csi). An already-compressed MJPEG/UVC frame cannot be "
            "rotated without decoding and re-encoding it."
        )
    return config


def _xclk_pin(value):
    if isinstance(value, str) and value.upper() in ("-1", "NO_CLOCK"):
        return -1
    return cv.int_range(min=-1, max=48)(value)


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(ESPVideoCamera),
            cv.Required(CONF_I2C_ID): cv.use_id(i2c.I2CBus),
            cv.Optional(CONF_DEVICE, default="jpeg"): _validate_device,
            cv.Optional(CONF_RESOLUTION, default="auto"): _validate_resolution,
            cv.Optional(CONF_JPEG_QUALITY, default=10): cv.int_range(min=1, max=63),
            cv.Optional(CONF_MAX_FRAMERATE, default=10): cv.float_range(
                min=0.1, max=60.0
            ),
            cv.Optional(CONF_ROTATION, default=0): _validate_rotation,
            cv.Optional(CONF_XCLK_PIN, default=36): _xclk_pin,
            cv.Optional(CONF_XCLK_FREQUENCY, default=24000000): cv.int_range(
                min=1000000, max=40000000
            ),
            cv.Optional(CONF_ENABLE_XCLK, default=False): cv.boolean,
            cv.Optional(CONF_ENABLE_UVC, default=False): cv.boolean,
        }
    )
    .extend(cv.ENTITY_BASE_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA),
    _validate_config,
)


async def to_code(config):
    if not CORE.using_toolchain_esp_idf:
        raise cv.Invalid("esp_video_camera requires the esp-idf framework.")

    cg.add_define("USE_CAMERA")

    var = cg.new_Pvariable(config[CONF_ID])
    await setup_entity(var, config, "camera")
    await cg.register_component(var, config)

    i2c_bus = await cg.get_variable(config[CONF_I2C_ID])
    cg.add(var.set_i2c_bus(i2c_bus))
    cg.add(
        var.set_xclk_pin(
            cg.RawExpression(f"static_cast<gpio_num_t>({config[CONF_XCLK_PIN]})")
        )
    )
    cg.add(var.set_xclk_freq(config[CONF_XCLK_FREQUENCY]))
    cg.add(var.set_enable_xclk_init(config[CONF_ENABLE_XCLK]))
    cg.add(var.set_enable_uvc(config[CONF_ENABLE_UVC]))

    cg.add(var.set_device(config[CONF_DEVICE]))
    cg.add(var.set_resolution(config[CONF_RESOLUTION]))
    cg.add(var.set_jpeg_quality(config[CONF_JPEG_QUALITY]))
    cg.add(var.set_max_framerate(config[CONF_MAX_FRAMERATE]))
    cg.add(var.set_rotation(config[CONF_ROTATION]))

    # Managed Espressif components (no vendored sources). Espressif's esp_video
    # (V4L2) framework transitively pulls the rest of the camera stack at
    # compatible versions: esp_cam_sensor (MIPI sensor drivers), esp_sccb_intf
    # (camera I2C/SCCB) and esp_ipa (ISP/IPA tuning).
    jpeg_enabled = config[CONF_DEVICE] in ("jpeg", "/dev/video10")
    # Pin the Espressif follow-up to esp_video 2.3.0 that widens its esp_h264
    # dependency to 1.3.*. Both codec profiles then use the same V4L2 camera,
    # ISP and sensor fixes while the generated build directories keep their
    # compile-time codec graphs isolated.
    add_idf_component(
        name="espressif/esp_video",
        repo="https://github.com/espressif/esp-video-components.git",
        ref="50d258a34938014b5f43277573880d96bd8ed669",
        path="esp_video",
    )
    if jpeg_enabled:
        # esp_video 2.3.0's manifest pulls esp_h264 on every P4 build although
        # both its H.264 source and CMake requirement are correctly Kconfig
        # gated. IDF local-component precedence supplies an empty dependency
        # only for JPEG firmware; H.264 profiles resolve the real library.
        add_extra_build_file(
            "components/esp_h264/CMakeLists.txt",
            Path(__file__).parent
            / "build_stubs"
            / "esp_h264"
            / "CMakeLists.txt",
        )
    if config[CONF_ENABLE_UVC]:
        # USB-UVC host driver, aligned with esp_video 2.x's own dependency.
        add_idf_component(name="espressif/usb_host_uvc", ref="2.5.*")

    # Pipeline features. Kconfig keys verified against esp_video 2.3.
    # ENABLE_ISP_PIPELINE_CONTROLLER (default n) is what pulls in esp_ipa and
    # runs the AWB/AE/CCM/gamma automation that applies the sensor IPA JSON
    # tuning; without it the MIPI image is unprocessed (washed-out / green cast).
    for opt in (
        "CONFIG_ESP_VIDEO_ENABLE_MIPI_CSI_VIDEO_DEVICE",
        "CONFIG_ESP_VIDEO_ENABLE_ISP",
        "CONFIG_ESP_VIDEO_ENABLE_ISP_VIDEO_DEVICE",
        "CONFIG_ESP_VIDEO_ENABLE_ISP_PIPELINE_CONTROLLER",
    ):
        add_idf_sdkconfig_option(opt, True)
    add_idf_sdkconfig_option(
        "CONFIG_ESP_VIDEO_ENABLE_JPEG_ENC_VIDEO_DEVICE", jpeg_enabled
    )
    add_idf_sdkconfig_option(
        "CONFIG_ESP_VIDEO_ENABLE_HW_JPEG_ENC_VIDEO_DEVICE", jpeg_enabled
    )

    # esp_video's prebuilt IPA libraries emit per-frame DEBUG telemetry. Runtime
    # tag filtering is disabled by ESPHome's size-oriented IDF defaults, making
    # esp_log_level_set() a no-op. Use the lower-memory linked-list backend so
    # this component can suppress only those hot tags while retaining normal
    # application logging.
    add_idf_sdkconfig_option("CONFIG_LOG_DYNAMIC_LEVEL_CONTROL", True)
    add_idf_sdkconfig_option("CONFIG_LOG_TAG_LEVEL_IMPL_NONE", False)
    add_idf_sdkconfig_option("CONFIG_LOG_TAG_LEVEL_IMPL_LINKED_LIST", True)

    if config[CONF_ENABLE_UVC]:
        add_idf_sdkconfig_option("CONFIG_ESP_VIDEO_ENABLE_USB_UVC_VIDEO_DEVICE", True)

    # Auto-detect the MIPI-CSI sensors shipped with espressif/esp_cam_sensor over
    # the shared I2C bus. Kconfig keys verified against esp_cam_sensor 2.2.0.
    for sensor in ("SC202CS", "OV5647", "SC2336"):
        add_idf_sdkconfig_option(f"CONFIG_CAMERA_{sensor}", True)
        add_idf_sdkconfig_option(
            f"CONFIG_CAMERA_{sensor}_AUTO_DETECT_MIPI_INTERFACE_SENSOR", True
        )

    # SC202CS colour tuning. The SC2356 module is the same SC202CS silicon
    # (PID 0xeb52 @ SCCB 0x36) but ships an IPA JSON with noticeably better
    # colour than the stock default, validated on the M5Stack Tab5 / reTerminal.
    # Override the sensor's default IPA configuration with it. esp_cam_sensor's
    # project_include.cmake embeds the file pointed to by the CUSTOMIZED path.
    ipa_json = "esp_video_camera/sc202cs_ipa.json"
    add_extra_build_file(ipa_json, Path(__file__).parent / "cfg" / "sc202cs.json")
    add_idf_sdkconfig_option(
        "CONFIG_CAMERA_SC202CS_DEFAULT_IPA_JSON_CONFIGURATION_FILE", False
    )
    add_idf_sdkconfig_option(
        "CONFIG_CAMERA_SC202CS_CUSTOMIZED_IPA_JSON_CONFIGURATION_FILE", True
    )
    add_idf_sdkconfig_option(
        "CONFIG_CAMERA_SC202CS_CUSTOMIZED_IPA_JSON_CONFIGURATION_FILE_PATH",
        str(CORE.relative_build_path(ipa_json)),
    )
