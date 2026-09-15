import esphome.config_validation as cv
import esphome.codegen as cg
from esphome.const import CONF_ID

CODEOWNERS = ["@n-IA-hane"]

voip_simulator_ns = cg.esphome_ns.namespace("voip_simulator")
VoipSimulator = voip_simulator_ns.class_("VoipSimulator", cg.Component)

CONF_SOURCE_PROFILE = "source_profile"
CONF_DEVICE_PROFILE = "device_profile"
CONF_SOCKET_PATH = "socket_path"
CONF_SPEAKER_OUTPUT_PATH = "speaker_output_path"
CONF_MICROPHONE_INPUT_PATH = "microphone_input_path"
CONF_FRAMEBUFFER_PATH = "framebuffer_path"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(VoipSimulator),
        cv.Optional(CONF_SOURCE_PROFILE, default=""): cv.string,
        cv.Optional(CONF_DEVICE_PROFILE, default="generic"): cv.string,
        cv.Optional(CONF_SOCKET_PATH, default="test_captures/simulator/voip-sim.sock"): cv.string,
        cv.Optional(CONF_SPEAKER_OUTPUT_PATH, default="test_captures/simulator/audio/speaker_output.pcm"): cv.string,
        cv.Optional(CONF_MICROPHONE_INPUT_PATH, default="tests/simulator/audio/mic_input.pcm"): cv.string,
        cv.Optional(CONF_FRAMEBUFFER_PATH, default="test_captures/simulator/framebuffer.png"): cv.string,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_source_profile(config[CONF_SOURCE_PROFILE]))
    cg.add(var.set_device_profile(config[CONF_DEVICE_PROFILE]))
    cg.add(var.set_socket_path(config[CONF_SOCKET_PATH]))
    cg.add(var.set_speaker_output_path(config[CONF_SPEAKER_OUTPUT_PATH]))
    cg.add(var.set_microphone_input_path(config[CONF_MICROPHONE_INPUT_PATH]))
    cg.add(var.set_framebuffer_path(config[CONF_FRAMEBUFFER_PATH]))
