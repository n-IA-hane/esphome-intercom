"""Apply one official PPA fix to an exact SDK's build-local sources."""

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
_SDK_VERSIONS = (cv.Version(5, 5, 6), cv.Version(6, 0, 3))
_HOOK = "p4_ppa_backport.cmake"


async def to_code(config):
    if CORE.target_framework != "esp-idf" or esp32.get_esp32_variant() != esp32.VARIANT_ESP32P4:
        return
    if esp32.idf_version() not in _SDK_VERSIONS:
        _LOGGER.info("P4 PPA backport not applied to unsupported SDK %s", esp32.idf_version())
        return
    for filename in (_HOOK, "9da8ff53.patch", "p4_ppa_backport.json"):
        esp32.add_extra_build_file(filename, Path(__file__).parent / filename)
    hook = str(CORE.relative_build_path(_HOOK))
    includes = [value for value in CORE.cmake_args.get("CMAKE_PROJECT_INCLUDE", "").split(";") if value]
    if hook not in includes:
        includes.append(hook)
    cg.add_cmake_arg("CMAKE_PROJECT_INCLUDE", ";".join(includes))
