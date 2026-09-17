"""WAV MIME compatibility for ESPHome's upstream HTTP media source."""

from pathlib import Path

from esphome.components.esp32 import add_idf_component
import esphome.config_validation as cv

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["esp32", "media_source.audio_http"]
CONFIG_SCHEMA = cv.Schema({})


async def to_code(config):
    add_idf_component(
        name="micro_decoder_mime",
        path=str(Path(__file__).parent / "micro_decoder_mime"),
    )
