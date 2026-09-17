"""Contracts for the narrow ESPHome component forks shipped by this project."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "esphome" / "components"
pytestmark = pytest.mark.architecture


UPSTREAM_DEV_SHA = "5f6a910e2d6e41d3716668a66e5dff8cca25f2ea"


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
    assert 'timeout = this->tts_playback_start_timeout_;' in source
    assert 'this->set_timeout("playing", timeout,' in source


def test_local_forks_remain_narrow_and_documented() -> None:
    speaker = (COMPONENTS / "speaker" / "UPSTREAM.md").read_text()
    voice_assistant = (COMPONENTS / "voice_assistant" / "UPSTREAM.md").read_text()
    audio = (COMPONENTS / "audio" / "UPSTREAM.md").read_text()
    mipi_dsi = (COMPONENTS / "mipi_dsi" / "UPSTREAM.md").read_text()

    for document in (speaker, voice_assistant, audio, mipi_dsi):
        assert UPSTREAM_DEV_SHA in document
    assert "pause_releases_pipeline" in speaker
    assert "tts_playback_start_timeout" in voice_assistant
    assert "host simulator" in audio



def test_integrated_spi_and_http_components_are_not_shadowed() -> None:
    assert not (COMPONENTS / "spi").exists()
    assert not (COMPONENTS / "audio_http").exists()
    package = (ROOT / "packages/audio/http_media_codecs.yaml").read_text()
    assert "components: [audio_http_compat]" in package
    assert "min_version: 2026.9.0" in package
