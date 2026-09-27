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
        from esphome import yaml_util

        config = yaml_util.load_yaml(ROOT / relative)
        steps = config["ota"][0]["on_begin"]
        stop_index = next(i for i, step in enumerate(steps) if "esp_audio_stack.stop" in step)
        wait_index = next(
            i for i, step in enumerate(steps)
            if "esp_audio_stack.is_idle" in step.get("wait_until", {}).get("condition", {})
        )
        assert stop_index < wait_index
        assert steps[stop_index]["esp_audio_stack.stop"] is None
        wait = steps[wait_index]["wait_until"]
        assert wait["condition"]["esp_audio_stack.is_idle"] is None
        assert wait["timeout"] == "2s"
