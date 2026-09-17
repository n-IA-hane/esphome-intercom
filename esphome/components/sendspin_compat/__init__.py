"""Sendspin worker configuration and partial-frame compatibility fixes."""

from pathlib import Path

from esphome.components.esp32 import add_idf_component
import esphome.config_validation as cv

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32", "sendspin"]
CONFIG_SCHEMA = cv.Schema({})


async def to_code(config):
    add_idf_component(
        name="voip_sendspin_compat",
        path=str(Path(__file__).parent / "voip_sendspin_compat"),
    )
