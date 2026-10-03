"""Opt-in internal instruction placement for ESP32-S3 full audio profiles."""
from pathlib import Path

from esphome.components import esp32
import esphome.config_validation as cv

DEPENDENCIES = ["esp32", "esp_afe"]
CONFIG_SCHEMA = cv.All(
    cv.Schema({}),
    esp32.only_on_variant(supported=["ESP32S3"], msg_prefix="audio_dsp_iram"),
)

async def to_code(config):
    esp32.add_idf_component(
        name="audio_dsp_iram",
        path=str(Path(__file__).parent / "idf_components" / "audio_dsp_iram"),
    )
