"""Optional, read-only playback diagnostics using public audio interfaces."""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import media_source, speaker, voice_assistant
from esphome.components.audio_http.media_source import AudioHTTPMediaSource
from esphome.components.mixer.speaker import MixerSpeaker
from esphome.components.speaker_source.media_player import SpeakerSourceMediaPlayer
from esphome.const import CONF_ID
import esphome.final_validate as fv

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32", "esp_audio_stack", "media_player", "media_source", "speaker"]

ns = cg.esphome_ns.namespace("playback_diagnostics")
PlaybackDiagnostics = ns.class_("PlaybackDiagnostics", cg.Component)
ESPAudioStack = cg.esphome_ns.namespace("esp_audio_stack").class_("ESPAudioStack", cg.Component)
DumpAction = ns.class_("DumpAction", automation.Action)
WatchAction = ns.class_("WatchAction", automation.Action)
CancelWatchAction = ns.class_("CancelWatchAction", automation.Action)

CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(PlaybackDiagnostics),
    cv.GenerateID("audio_stack"): cv.use_id(ESPAudioStack),
    cv.Required("media_player"): cv.use_id(SpeakerSourceMediaPlayer),
    cv.Required("announcement_source"): cv.use_id(AudioHTTPMediaSource),
    cv.Optional("media_source"): cv.use_id(media_source.MediaSource),
    cv.Required("tts_speaker"): cv.use_id(speaker.Speaker),
    cv.Required("mixer_input"): cv.use_id(speaker.Speaker),
    cv.Required("hardware_speaker"): cv.use_id(speaker.Speaker),
    cv.Optional("mixer"): cv.use_id(MixerSpeaker),
    cv.Optional("voice_assistant"): cv.use_id(voice_assistant.VoiceAssistant),
}).extend(cv.COMPONENT_SCHEMA)


def _validate_topology(config):
    """Require the serialized completion owners used by the progress counters."""
    full = fv.full_config.get()

    def bound(key):
        return full.get_config_for_path(full.get_path_for_id(config[key])[:-1])

    tts = bound("tts_speaker")
    hardware = bound("hardware_speaker")
    input_path = full.get_path_for_id(config["mixer_input"])
    mixer = full.get_config_for_path(input_path[:-3])
    if (tts.get("platform") != "resampler"
            or str(tts.get("output_speaker")) != str(config["mixer_input"])):
        raise cv.Invalid("tts_speaker must be the native resampler feeding mixer_input", path=["tts_speaker"])
    if (mixer.get("platform") != "mixer"
            or str(mixer.get("output_speaker")) != str(config["hardware_speaker"])):
        raise cv.Invalid("mixer_input must belong to the native mixer feeding hardware_speaker", path=["mixer_input"])
    if (hardware.get("platform") != "esp_audio_stack"
            or str(hardware.get("esp_audio_stack_id")) != str(config["audio_stack"])):
        raise cv.Invalid("hardware_speaker must belong to the bound Audio Stack", path=["hardware_speaker"])
    if "mixer" in config and str(mixer[CONF_ID]) != str(config["mixer"]):
        raise cv.Invalid("mixer must own mixer_input", path=["mixer"])
    pipeline = bound("media_player").get("announcement_pipeline", {})
    if (str(pipeline.get("speaker")) != str(config["tts_speaker"])
            or str(config["announcement_source"]) not in [str(value) for value in pipeline.get("sources", [])]):
        raise cv.Invalid("Bind the player's actual announcement source and speaker", path=["media_player"])
    return config


FINAL_VALIDATE_SCHEMA = _validate_topology


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    for key in ("audio_stack", "media_player", "announcement_source", "media_source",
                "tts_speaker", "mixer_input", "hardware_speaker", "mixer", "voice_assistant"):
        if key not in config:
            continue
        if key in ("mixer", "voice_assistant"):
            cg.add_define("USE_PLAYBACK_DIAGNOSTICS_MIXER" if key == "mixer" else "USE_PLAYBACK_DIAGNOSTICS_VOICE")
        cg.add(getattr(var, f"set_{key}")(await cg.get_variable(config[key])))


@automation.register_action("playback_diagnostics.dump", DumpAction, cv.Schema({
    cv.GenerateID(): cv.use_id(PlaybackDiagnostics),
    cv.Optional("reason", default="manual"): cv.templatable(cv.string_strict),
}), synchronous=True)
async def dump_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_reason(await cg.templatable(config["reason"], args, cg.std_string)))
    return var


@automation.register_action("playback_diagnostics.watch_next_announcement", WatchAction, cv.Schema({
    cv.GenerateID(): cv.use_id(PlaybackDiagnostics),
    cv.Optional("timeout", default="45s"): cv.templatable(cv.positive_time_period_milliseconds),
}), synchronous=True)
async def watch_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_timeout(await cg.templatable(config["timeout"], args, cg.uint32)))
    return var


@automation.register_action("playback_diagnostics.cancel_watch", CancelWatchAction,
                            cv.Schema({cv.GenerateID(): cv.use_id(PlaybackDiagnostics)}), synchronous=True)
async def cancel_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var
