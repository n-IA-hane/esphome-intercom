"""Configuration-only diagnostics for the native device package composition."""

import logging

import esphome.config_validation as cv

CODEOWNERS = ["@n-IA-hane"]

_LOGGER = logging.getLogger(__name__)
FEATURES = (
    "voice", "wake_word", "voip", "music", "sendspin", "timers",
    "display", "status_led", "diagnostics", "sensor_dock", "video",
    "directory", "blf", "mwi",
)
# Diagnostic descriptions of the native package conditions. This checker never
# enables a component, supplies a missing dependency or changes the selection.
REQUIRES = {
    "wake_word": ("voice",),
    "timers": ("voice",),
    "sendspin": ("music",),
    "video": ("voip",),
    "directory": ("voip",),
    "blf": ("voip",),
    "mwi": ("voip",),
}


def _report_selection(config):
    requested = config["requested"]
    _LOGGER.info("Firmware features requested: %s", ", ".join(requested) or "(none)")
    for feature in dict.fromkeys(requested):
        missing = [name for name in REQUIRES.get(feature, ()) if name not in requested]
        if missing:
            _LOGGER.warning(
                "Firmware feature '%s' is disabled: requires %s. "
                "Add the dependency or remove '%s' from firmware_features.",
                feature, ", ".join(missing), feature,
            )
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema({
        cv.Required("requested"): cv.ensure_list(cv.one_of(*FEATURES)),
        cv.Optional("audio_processor"): cv.one_of("aec", "afe", lower=True),
    }),
    _report_selection,
)
