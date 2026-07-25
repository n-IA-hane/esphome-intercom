"""Cross-repository contracts for maintained ESPHome audio profiles."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_profiles_do_not_use_removed_blocking_audio_stack_action() -> None:
    offenders: list[str] = []
    for base in (ROOT / "packages", ROOT / "yamls"):
        for path in base.rglob("*.yaml"):
            if ".esphome" in path.parts:
                continue
            if "esp_audio_stack.stop_and_wait" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_ota_waits_for_audio_stack_idle_without_blocking_main_loop() -> None:
    for relative in (
        "packages/ota/full_audio_maintenance.yaml",
        "packages/ota/full_audio_lvgl_maintenance.yaml",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "- esp_audio_stack.stop: audio_stack" in text
        assert "esp_audio_stack.is_idle: audio_stack" in text
        assert "timeout: 2s" in text
