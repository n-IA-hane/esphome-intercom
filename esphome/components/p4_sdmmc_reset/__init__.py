"""Build-local backport of the ESP32-P4 ESP-IDF 5.5.6 SDMMC reset order."""

import logging
from pathlib import Path

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import esp32
from esphome.core import CORE

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32"]
CONFIG_SCHEMA = cv.Schema({})

_LOGGER = logging.getLogger(__name__)
_SDK_VERSION = cv.Version(5, 5, 6)
_HOOK = "p4_sdmmc_reset.cmake"


async def to_code(config):
    if CORE.target_framework != "esp-idf" or esp32.get_esp32_variant() != esp32.VARIANT_ESP32P4:
        _LOGGER.warning("P4 SDMMC reset backport not applied: requires ESP32-P4 with ESP-IDF")
        return
    version = esp32.idf_version()
    if version != _SDK_VERSION:
        _LOGGER.warning(
            "P4 SDMMC reset backport not applied to SDK %s: only exact ESP-IDF %s is supported",
            version, _SDK_VERSION,
        )
        return
    esp32.add_extra_build_file(_HOOK, Path(__file__).parent / _HOOK)
    hook = str(CORE.relative_build_path(_HOOK))
    includes = CORE.cmake_args.get("CMAKE_PROJECT_INCLUDE", "").split(";")
    includes = [value for value in includes if value]
    if hook not in includes:
        includes.append(hook)
    cg.add_cmake_arg("CMAKE_PROJECT_INCLUDE", ";".join(includes))
