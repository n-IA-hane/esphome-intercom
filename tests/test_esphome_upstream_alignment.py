"""Contracts for the narrow ESPHome component forks shipped by this project."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "esphome" / "components"
pytestmark = pytest.mark.architecture


def test_audio_dependencies_match_esphome_2026_8() -> None:
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
    speaker = (COMPONENTS / "speaker" / "UPSTREAM.md").read_text()
    voice_assistant = (COMPONENTS / "voice_assistant" / "UPSTREAM.md").read_text()
    audio = (COMPONENTS / "audio" / "UPSTREAM.md").read_text()

    for document in (speaker, voice_assistant, audio):
        assert "ESPHome 2026.8.0" in document
    assert "pause_releases_pipeline" in speaker
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
    assert "ESPHome 2026.8.0" in upstream
    assert "micro-decoder 0.4.0" in upstream


def test_spi_uses_direct_psram_dma_without_internal_bounce_buffers() -> None:
    source = (COMPONENTS / "spi" / "spi_esp_idf.cpp").read_text()
    upstream = (COMPONENTS / "spi" / "UPSTREAM.md").read_text()

    assert "MAX_PSRAM_TRANSFER_SIZE = 4032" in source
    assert "SPI_TRANS_DMA_USE_PSRAM" in source
    assert "SPI_TRANS_DMA_BUFFER_ALIGN_MANUAL" in source
    assert "psram_tx_flags(txbuf, partial)" in source
    assert "psram_tx_flags(data, chunk_size)" in source
    assert "ESPHome 2026.8.0" in upstream
    assert "ESP-IDF 5.5" in upstream
