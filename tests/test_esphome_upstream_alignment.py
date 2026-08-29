"""Contracts for the narrow ESPHome component forks shipped by this project."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "esphome" / "components"
pytestmark = pytest.mark.architecture


UPSTREAM_DEV_SHA = "5f6a910e2d6e41d3716668a66e5dff8cca25f2ea"
SPI_UPSTREAM_MERGE_SHA = "e458a38f89e7ccfc3d186d6d2f41708168e6f492"


def test_audio_dependencies_match_recorded_esphome_dev() -> None:
    source = (COMPONENTS / "audio" / "__init__.py").read_text()

    assert 'name="esphome/micro-decoder", ref="0.4.0"' in source
    assert 'name="esphome/micro-mp3", ref="0.4.0"' in source
    assert 'name="esphome/micro-opus", ref="0.4.1"' in source
    assert '"CONFIG_MICRO_DECODER_CODEC_VORBIS", False' in source
    assert '"CONFIG_MICRO_MP3_PREFER_PSRAM"' in source
    assert '"CONFIG_MICRO_MP3_PREFER_INTERNAL"' in source
    assert "CONFIG_MP3_DECODER_PREFER_PSRAM" not in source


def test_voice_assistant_keeps_upstream_backpressure_and_speaker_drain() -> None:
    source = (COMPONENTS / "voice_assistant" / "voice_assistant.cpp").read_text()

    assert "if (!this->api_client_->send_message(msg))" in source
    assert "this->speaker_buffer_index_ + msg.data_len <= SPEAKER_BUFFER_SIZE" in source
    assert "this->write_speaker_();" in source
    assert 'this->set_timeout("playing", this->tts_playback_start_timeout_' in source


def test_local_forks_remain_narrow_and_documented() -> None:
    voice_assistant = (COMPONENTS / "voice_assistant" / "UPSTREAM.md").read_text()
    audio = (COMPONENTS / "audio" / "UPSTREAM.md").read_text()
    audio_http = (COMPONENTS / "audio_http" / "UPSTREAM.md").read_text()
    mipi_dsi = (COMPONENTS / "mipi_dsi" / "UPSTREAM.md").read_text()
    spi = (COMPONENTS / "spi" / "UPSTREAM.md").read_text()

    for document in (voice_assistant, audio, audio_http, mipi_dsi):
        assert UPSTREAM_DEV_SHA in document
    assert SPI_UPSTREAM_MERGE_SHA in spi
    assert not (COMPONENTS / "speaker").exists()
    assert "tts_playback_start_timeout" in voice_assistant
    assert "host simulator" in audio


def test_audio_http_exposes_micro_decoder_persistent_ring_policy() -> None:
    schema = (COMPONENTS / "audio_http" / "media_source.py").read_text()
    source = (COMPONENTS / "audio_http" / "audio_http_media_source.cpp").read_text()
    header = (COMPONENTS / "audio_http" / "audio_http_media_source.h").read_text()
    upstream = (COMPONENTS / "audio_http" / "UPSTREAM.md").read_text()

    assert 'CONF_PERSISTENT_RING_BUFFER = "persistent_ring_buffer"' in schema
    assert "default=False" in schema
    assert "set_persistent_ring_buffer(config[CONF_PERSISTENT_RING_BUFFER])" in schema
    assert "config.persistent_ring_buffer = this->persistent_ring_buffer_;" in source
    assert "bool persistent_ring_buffer_{false};" in header
    assert UPSTREAM_DEV_SHA in upstream
    assert "micro-decoder 0.4.0" in upstream


def test_spi_matches_the_upstream_opt_in_psram_dma_contract() -> None:
    source = (COMPONENTS / "spi" / "spi_esp_idf.cpp").read_text()
    schema = (COMPONENTS / "spi" / "__init__.py").read_text()
    upstream = (COMPONENTS / "spi" / "UPSTREAM.md").read_text()

    assert "SPI_TRANS_DMA_USE_PSRAM" in source
    assert "SPI_TRANS_DMA_BUFFER_ALIGN_MANUAL" not in source
    assert "rxbuf == nullptr ? get_psram_dma_flags" in source
    assert 'CONF_PSRAM_DMA = "psram_dma"' in schema
    assert 'cg.add_define("USE_SPI_PSRAM_DMA")' in schema
    assert SPI_UPSTREAM_MERGE_SHA in upstream
    assert "ESP-IDF 5.5" in upstream
