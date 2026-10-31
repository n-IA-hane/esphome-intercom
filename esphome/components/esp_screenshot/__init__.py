"""On-demand display screenshots with bounded JPEG storage."""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import display as display_component, text_sensor, web_server_base
from esphome.components.esp32 import add_idf_component, add_idf_sdkconfig_option
from esphome.components.lvgl.lvcode import LvglComponent
from esphome.components.mipi_dsi import display as mipi_dsi_display
from esphome.const import CONF_ID, CONF_ROTATION
from esphome.core import CORE
from esphome.final_validate import full_config

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32", "network"]
AUTO_LOAD = ["web_server_base", "text_sensor"]
MULTI_CONF = True

CONF_QUALITY = "quality"
CONF_MIPI_DSI_ID = "mipi_dsi_id"
CONF_LVGL_ID = "lvgl_id"
CONF_CAPTURE_ID = "capture_id"
CONF_CAPTURE_STATUS = "capture_status"
CONF_MAX_JPEG_SIZE = "max_jpeg_size"
CONF_PATH = "path"
CONF_TASK_CORE = "task_core"
CONF_TASK_PRIORITY = "task_priority"
CONF_TASK_STACK_SIZE = "task_stack_size"
CONF_TASK_STACK_IN_PSRAM = "task_stack_in_psram"
CONF_BUFFER_IN_PSRAM = "buffer_in_psram"


def _task_core(value):
    if isinstance(value, str) and value.lower() == "any":
        return "any"
    if isinstance(value, bool):
        raise cv.Invalid("task_core must be any, 0 or 1")
    return cv.int_range(min=0, max=1)(value)


def _stack_size(value):
    size = cv.All(cv.validate_bytes, cv.Range(min=4096, max=65536))(value)
    if size % 16:
        raise cv.Invalid("task_stack_size must be a multiple of 16 bytes")
    return size


ns = cg.esphome_ns.namespace("esp_screenshot")
EspScreenshot = ns.class_("EspScreenshot", cg.Component)
CaptureAction = ns.class_("CaptureAction", automation.Action)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(EspScreenshot),
            cv.GenerateID(web_server_base.CONF_WEB_SERVER_BASE_ID): cv.use_id(
                web_server_base.WebServerBase
            ),
            cv.Optional(CONF_MIPI_DSI_ID): cv.use_id(mipi_dsi_display.MipiDsi),
            cv.Optional(CONF_LVGL_ID): cv.use_id(LvglComponent),
            cv.Optional(CONF_ROTATION, default=0): display_component.validate_rotation,
            cv.Optional(CONF_CAPTURE_ID): text_sensor.text_sensor_schema(
                icon="mdi:camera"
            ),
            cv.Optional(CONF_CAPTURE_STATUS): text_sensor.text_sensor_schema(
                icon="mdi:monitor-screenshot"
            ),
            cv.Optional(CONF_QUALITY, default=85): cv.int_range(min=1, max=100),
            cv.Optional(CONF_MAX_JPEG_SIZE, default=262144): cv.All(
                cv.validate_bytes, cv.Range(min=4096, max=4 * 1024 * 1024)
            ),
            cv.Optional(CONF_PATH, default="screen"): cv.valid_name,
            cv.Optional(CONF_TASK_CORE, default="any"): _task_core,
            cv.Optional(CONF_TASK_PRIORITY, default=1): cv.int_range(min=1, max=24),
            cv.Optional(CONF_TASK_STACK_SIZE, default=6144): _stack_size,
            cv.Optional(CONF_TASK_STACK_IN_PSRAM, default=True): cv.boolean,
            cv.Optional(CONF_BUFFER_IN_PSRAM, default=True): cv.boolean,
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.has_exactly_one_key(CONF_MIPI_DSI_ID, CONF_LVGL_ID),
    cv.only_on_esp32,
)


def _validate_paths(config):
    full = full_config.get()
    instances = full.get("esp_screenshot", [])
    if CONF_LVGL_ID in config and config[CONF_ROTATION] != 0:
        raise cv.Invalid("rotation applies only to mipi_dsi_id; LVGL capture already uses its logical orientation")
    if (
        config[CONF_TASK_STACK_IN_PSRAM] or config[CONF_BUFFER_IN_PSRAM]
    ) and "psram" not in full:
        raise cv.Invalid(
            "PSRAM placement requires the psram component; choose internal placement explicitly otherwise"
        )
    if sum(instance[CONF_PATH] == config[CONF_PATH] for instance in instances) != 1:
        raise cv.Invalid("Each esp_screenshot instance requires a unique path")
    return config


FINAL_VALIDATE_SCHEMA = _validate_paths


async def to_code(config):
    if not CORE.using_toolchain_esp_idf:
        raise cv.Invalid("esp_screenshot requires the esp-idf framework")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    server = await cg.get_variable(config[web_server_base.CONF_WEB_SERVER_BASE_ID])
    cg.add(var.set_web_server(server))
    cg.add(var.set_quality(config[CONF_QUALITY]))
    cg.add(var.set_max_jpeg_size(config[CONF_MAX_JPEG_SIZE]))
    cg.add(var.set_path(config[CONF_PATH]))
    cg.add(
        var.set_task_core(
            -1 if config[CONF_TASK_CORE] == "any" else config[CONF_TASK_CORE]
        )
    )
    cg.add(var.set_task_priority(config[CONF_TASK_PRIORITY]))
    cg.add(var.set_task_stack_size(config[CONF_TASK_STACK_SIZE]))
    cg.add(var.set_task_stack_in_psram(config[CONF_TASK_STACK_IN_PSRAM]))
    cg.add(var.set_buffer_in_psram(config[CONF_BUFFER_IN_PSRAM]))
    if CONF_MIPI_DSI_ID in config:
        if CORE.data["esp32"]["variant"] != "ESP32P4":
            raise cv.Invalid("mipi_dsi_id requires ESP32-P4")
        display = await cg.get_variable(config[CONF_MIPI_DSI_ID])
        cg.add(var.set_mipi_display(display))
        cg.add(var.set_rotation(display_component.DISPLAY_ROTATIONS[config[CONF_ROTATION]]))
        cg.add_define("USE_ESP_SCREENSHOT_MIPI_DSI")
    else:
        display = await cg.get_variable(config[CONF_LVGL_ID])
        cg.add(var.set_lvgl(display))
        cg.add_define("USE_ESP_SCREENSHOT_LVGL")
    if CONF_CAPTURE_ID in config:
        sensor = await text_sensor.new_text_sensor(config[CONF_CAPTURE_ID])
        cg.add(var.set_capture_id_sensor(sensor))
    if CONF_CAPTURE_STATUS in config:
        sensor = await text_sensor.new_text_sensor(config[CONF_CAPTURE_STATUS])
        cg.add(var.set_capture_status_sensor(sensor))
    cg.add_define("USE_ESP_SCREENSHOT")
    add_idf_component(name="espressif/esp_new_jpeg", ref="1.0.2")
    if config[CONF_TASK_STACK_IN_PSRAM]:
        add_idf_sdkconfig_option("CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM", True)


@automation.register_action(
    "esp_screenshot.capture",
    CaptureAction,
    cv.Schema({cv.Required(CONF_ID): cv.use_id(EspScreenshot)}),
    synchronous=True,
)
async def capture_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, parent)
