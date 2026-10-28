"""Exercise the maintained observer against public fake audio objects."""

from pathlib import Path
import json
import os
import re
import shlex
import importlib.util
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/playback_diagnostics"
FIXTURES = ROOT / "tests/fixtures/playback_diagnostics"


@pytest.mark.skipif(not os.environ.get("PLAYBACK_DIAGNOSTICS_COMPILE_COMMANDS"),
                    reason="Target assembly guard requires an explicit existing Xtensa compile database")
@pytest.mark.diagnostic_reference
def test_real_xtensa_progress_has_no_atomic_helper_or_lock(tmp_path):
    database = Path(os.environ["PLAYBACK_DIAGNOSTICS_COMPILE_COMMANDS"])
    entry = next((item for item in json.loads(database.read_text())
                  if item["file"].endswith("/playback_diagnostics/playback_diagnostics.cpp")), None)
    assert entry is not None, "Use an existing build with Playback Diagnostics enabled"
    original = entry.get("arguments") or shlex.split(entry["command"])
    assert "xtensa" in original[0], "This guard requires the actual Xtensa target compiler"
    arguments = []
    skip = False
    for argument in original:
        if skip:
            skip = False
            continue
        if argument in ("-o", "-MF", "-MT", "-MQ"):
            skip = True
            continue
        if argument in ("-c", "-MD", "-MMD", "-MP") or argument == entry["file"]:
            continue
        arguments.append(argument)
    assembly = tmp_path / "playback-diagnostics.s"
    result = subprocess.run(
        arguments + ["-S", "-Werror", "-o", str(assembly), str(COMPONENT / "playback_diagnostics.cpp")],
        cwd=entry["directory"], capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = "_ZN7esphome20playback_diagnostics19PlaybackDiagnostics8Progress7observeE"
    methods = dict(re.findall(
        r"^(" + re.escape(prefix) + r"[^\s:]+):\n(.*?)^\s*\.size\s+\1,",
        assembly.read_text(), re.MULTILINE | re.DOTALL,
    ))
    assert any(re.fullmatch(re.escape(prefix) + r"\w+", name) for name in methods), \
        "The actual production progress method must be checked"
    # GCC can split the method into a wrapper and $part$0. Inspect every part,
    # retaining the real build options instead of disabling that optimization.
    body = "\n".join(methods.values())
    calls = re.findall(r"^\s*call\w*\s+(\S+)", body, re.MULTILINE)
    assert all(call == "__divdi3" or call in methods for call in calls), body
    assert "__atomic_" not in body and "s32c1i" not in body, body
    assert re.search(r"^\s*l32i", body, re.MULTILINE), body
    assert re.search(r"^\s*s32i", body, re.MULTILINE), body
    assert not list(tmp_path.glob("*.o")) and not list(tmp_path.glob("*.obj"))


HARNESS = r'''
#include <iostream>
#include "esphome/components/playback_diagnostics/playback_diagnostics.h"

using namespace esphome;
using playback_diagnostics::PlaybackDiagnostics;

struct Rig {
  media_player::MediaPlayer player;
  media_source::MediaSource source;
  speaker::Speaker tts, mixer_input, hardware;
  mixer_speaker::MixerSpeaker mixer;
  esp_audio_stack::ESPAudioStack stack;
  voice_assistant::VoiceAssistant va;
  PlaybackDiagnostics observer;
  Rig() {
    observer.set_media_player(&player);
    observer.set_announcement_source(&source);
    observer.set_tts_speaker(&tts);
    observer.set_mixer_input(&mixer_input);
    observer.set_hardware_speaker(&hardware);
#ifdef USE_PLAYBACK_DIAGNOSTICS_MIXER
    observer.set_mixer(&mixer);
#endif
    observer.set_audio_stack(&stack);
#ifdef USE_PLAYBACK_DIAGNOSTICS_VOICE
    observer.set_voice_assistant(&va);
#endif
    observer.setup();
    observer.flush_deferred();
    test_logs.clear();
  }
  void flush() { observer.flush_deferred(); }
};

int main(int argc, char **argv) {
  assert(argc == 2);
  const std::string scenario(argv[1]);
  Rig r;
  assert(r.observer.loop_disabled);
  assert(r.tts.callbacks.size() == 1);
  assert(r.hardware.callbacks.size() == 1);
  if (scenario == "dump") {
    r.observer.dump("manual");
    r.flush();
    assert(!test_logs.empty());
    assert(r.observer.timer_count() == 0);
  } else if (scenario == "privacy") {
    r.observer.on_tts_start(17);
    r.observer.on_error("http://secret.test/token?auth=BEARER_123", "http://secret.test/token?auth=BEARER_123 PRIVATE_TEXT_5492");
    r.observer.dump("http://secret.test/token?auth=BEARER_123");
    r.flush();
    r.observer.advance(60000);
  } else if (scenario == "finite") {
    r.observer.watch_next_announcement(1000);
    assert(r.observer.timer_count() == 1);
    r.observer.on_tts_start(7);
    r.flush();
    assert(r.observer.timer_count() == 1);
    r.observer.advance(1000);
    assert(r.observer.timer_count() == 0);
    const auto count = test_logs.size();
    r.observer.advance(60000);
    assert(test_logs.size() == count);
  } else if (scenario == "cancel") {
    r.observer.watch_next_announcement(1000);
    r.observer.cancel_watch();
    r.flush();
    assert(r.observer.timer_count() == 0);
    const auto count = test_logs.size();
    r.observer.advance(60000);
    assert(test_logs.size() == count);
  } else if (scenario == "waiting") {
    r.observer.watch_next_announcement(1000);
    r.flush();
    r.observer.advance(1000);
    assert(r.observer.timer_count() == 0);
    const auto count = test_logs.size();
    r.observer.advance(60000);
    assert(test_logs.size() == count);
  } else if (scenario == "finish") {
    r.observer.watch_next_announcement(1000);
    r.observer.on_tts_start(7);
    r.player.publish(media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
    r.flush();
    r.player.publish(media_player::MEDIA_PLAYER_STATE_IDLE);
    r.flush();
    assert(r.observer.timer_count() == 0);
    const auto count = test_logs.size();
    r.observer.advance(60000);
    assert(test_logs.size() == count);
  } else if (scenario == "standalone_finish" || scenario == "preserve_va_baseline") {
    r.observer.watch_next_announcement(1000);
    r.flush();
    if (scenario == "preserve_va_baseline") {
      r.observer.on_tts_start(7);
      r.flush();
    }
    r.tts.emit(16, 100000);
    r.hardware.emit(48, 100000);
    r.player.publish(media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
    r.flush();
    r.tts.emit(160, 200000);
    r.hardware.emit(480, 200000);
    r.player.publish(media_player::MEDIA_PLAYER_STATE_IDLE);
    r.flush();
    assert(r.observer.timer_count() == 0);
    const auto count = test_logs.size();
    r.observer.advance(60000);
    assert(test_logs.size() == count);
  } else if (scenario == "known_error") {
    r.observer.on_error("tts-start-timeout", "TTS playback did not start before the timeout");
    r.flush();
  } else if (scenario == "bounded") {
    for (unsigned i = 0; i < 1000; ++i) r.observer.dump("manual");
    r.observer.on_error("tts-start-timeout", "TTS playback did not start before the timeout");
    r.flush();
    assert(test_logs.size() <= 28);
    test_logs.clear();
    r.observer.dump("manual");
    r.flush();
  } else if (scenario == "report_ids") {
    r.observer.dump("before");
    r.observer.on_tts_start(17);
    r.observer.dump("after");
    r.flush();
  } else if (scenario == "bounded_ids") {
    for (unsigned i = 0; i < 1000; ++i) r.observer.dump("manual");
    r.flush();
    assert(test_logs.size() == 28);
    r.observer.dump("after");
    r.flush();
  } else if (scenario == "progress") {
    r.observer.watch_next_announcement(1000);
    r.observer.on_tts_start(7);
    r.flush();
    test_logs.clear();
    r.tts.info = audio::AudioStreamInfo(16, 1, 16000);
    r.hardware.info = audio::AudioStreamInfo(16, 1, 48000);
    r.tts.emit(160, 100000);
    r.hardware.emit(480, 100000);
    assert(test_logs.empty());
    r.observer.dump("progress");
    r.flush();
  } else if (scenario == "public_states") {
    r.source.state = media_source::MediaSourceState::PLAYING;
    r.tts.stopped = false;
    r.tts.running = true;
    r.mixer_input.buffered = true;
    r.hardware.stopped = false;
    r.hardware.running = true;
    r.mixer.frames = 321;
    r.stack.queued = 654;
    r.stack.pending = true;
    r.player.publish(media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
    r.flush();
    r.observer.dump("states");
    r.flush();
    assert(!test_logs.empty());
  } else if (scenario == "no_watch_idle") {
    r.observer.advance(60000);
    assert(test_logs.empty());
    assert(r.observer.timer_count() == 0);
  } else {
    return 2;
  }
  assert(test_mutations == 0 && "diagnostics changed playback lifecycle or controls");
  assert(test_app_description_reads == test_identity_reports && "identity metadata must be read once per emitted report");
  for (const auto &line : test_logs) std::cout << line << '\n';
}
'''


@pytest.fixture(scope="module", params=["full", "minimal"])
def observer_binary(tmp_path_factory, request):
    compiler = shutil.which("g++")
    assert compiler, "The production observer contract requires g++"
    build = tmp_path_factory.mktemp("playback-diagnostics")
    shutil.copyfile(FIXTURES / "public_api.h", build / "public_api.h")
    headers = (
        "esphome/core/component.h", "esphome/core/hal.h", "esphome/core/helpers.h",
        "esphome/core/log.h", "esphome/core/defines.h", "esphome/core/automation.h",
        "esphome/core/version.h", "esp_app_desc.h",
        "esphome/components/speaker/speaker.h", "esphome/components/media_source/media_source.h",
        "esphome/components/media_player/media_player.h",
        "esphome/components/speaker_source/speaker_source_media_player.h",
        "esphome/components/mixer/speaker/mixer_speaker.h",
        "esphome/components/esp_audio_stack/esp_audio_stack.h",
        "esphome/components/voice_assistant/voice_assistant.h",
    )
    for name in headers:
        path = build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "public_api.h"\n')
    cpp = build / "observer_contract.cpp"
    cpp.write_text(HARNESS)
    binary = build / "observer_contract"
    defines = ["-DUSE_PLAYBACK_DIAGNOSTICS_MIXER", "-DUSE_PLAYBACK_DIAGNOSTICS_VOICE"] if request.param == "full" else []
    result = subprocess.run(
        [compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
         "-fsanitize=undefined", "-fno-omit-frame-pointer", "-DUSE_ESP32",
         *defines,
         "-I", str(build), "-I", str(ROOT), str(cpp),
         str(COMPONENT / "playback_diagnostics.cpp"), "-o", str(binary)],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


def run_observer(binary, scenario):
    result = subprocess.run(
        ["bash", "-c", 'ulimit -c 0; exec "$@"', "bash", str(binary), scenario],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize("scenario", ["dump", "finite", "cancel", "waiting", "finish", "public_states", "no_watch_idle"])
def test_public_observation_has_no_control_side_effects(observer_binary, scenario):
    run_observer(observer_binary, scenario)


def test_private_payloads_never_enter_diagnostic_logs(observer_binary):
    output = run_observer(observer_binary, "privacy")
    assert "PRIVATE_TEXT_5492" not in output
    assert "BEARER_123" not in output
    assert "secret.test" not in output
    assert "http://" not in output


def test_progress_is_aggregated_without_realtime_logging(observer_binary):
    output = run_observer(observer_binary, "progress")
    assert "16000" in output
    assert "48000" in output
    assert "160" in output
    assert "480" in output


def test_known_error_retains_verified_code_and_message(observer_binary):
    output = run_observer(observer_binary, "known_error")
    assert "code=tts-start-timeout" in output
    assert "message=TTS playback did not start before the timeout" in output


def test_bounded_reports_preserve_error_for_next_explicit_dump(observer_binary):
    output = run_observer(observer_binary, "bounded")
    assert "code=tts-start-timeout" in output


def complete_reports(output):
    reports = {}
    for line in output.splitlines():
        match = re.search(r"\breport_id=(\d+)\s+part=(\d+)/(\d+)\b", line)
        assert match is not None, line
        report_id, part, total = map(int, match.groups())
        assert total == 7
        reports.setdefault(report_id, []).append((part, line))
    assert reports
    for parts in reports.values():
        assert [part for part, _ in parts] == list(range(1, 8))
        assert "BEGIN" in parts[0][1]
        assert "END" in parts[-1][1]
        begin_seq = re.search(r"\bseq=(\d+)\b", parts[0][1])
        end_seq = re.search(r"\bseq=(\d+)\b", parts[-1][1])
        assert begin_seq is not None and end_seq is not None
        assert begin_seq.group(1) == end_seq.group(1)
    return reports


@pytest.mark.parametrize(
    "scenario, high_frames, low_frames",
    [("standalone_finish", 160, 480), ("preserve_va_baseline", 176, 528)],
)
def test_watch_uses_player_without_tts_and_preserves_earlier_va_baseline(
    observer_binary, scenario, high_frames, low_frames
):
    reports = complete_reports(run_observer(observer_binary, scenario))
    finished = [parts for parts in reports.values() if "reason=watch_player_finished" in parts[0][1]]
    assert len(finished) == 1
    assert "watch=2" in finished[0][0][1]
    assert f"watch_frames={high_frames}" in finished[0][2][1]
    assert f"watch_frames={low_frames}" in finished[0][3][1]


def test_report_contains_complete_firmware_identity(observer_binary):
    reports = complete_reports(run_observer(observer_binary, "dump"))
    assert len(reports) == 1
    identity = next(iter(reports.values()))[1][1]
    assert "2026.10.0" in identity
    assert "2026-10-26" in identity
    assert "23:30:00" in identity
    assert "v5.5.6" in identity
    assert bytes(range(32)).hex() in identity


@pytest.mark.parametrize("scenario, expected_reports", [("report_ids", 3), ("bounded_ids", 5)])
def test_report_ids_distinguish_snapshots_and_account_for_coalesced_requests(observer_binary, scenario, expected_reports):
    reports = complete_reports(run_observer(observer_binary, scenario))
    assert len(reports) == expected_reports
    identifiers = list(reports)
    assert identifiers == sorted(set(identifiers))
    if scenario == "report_ids":
        assert identifiers == list(range(identifiers[0], identifiers[0] + expected_reports))
    else:
        assert identifiers[:4] == [1, 2, 3, 4]
        assert identifiers[-1] == 1001


@pytest.mark.parametrize("enabled,has_voice", [("true", True), ("false", False), ('"false"', False)])
def test_real_package_parser_preserves_overrides_and_optional_voice(tmp_path, enabled, has_voice):
    from esphome import yaml_util
    from esphome.components.packages import do_packages_pass, merge_packages
    from esphome.components.substitutions import do_substitution_pass

    config = tmp_path / "package.yaml"
    config.write_text(
        "substitutions:\n"
        f"  playback_diagnostics_voice_enabled: {enabled}\n"
        "  playback_diagnostics_player_id: alternate_player\n"
        f"  ext_components_source: {ROOT / 'esphome/components'}\n"
        "packages:\n"
        f"  observation: !include {ROOT / 'packages/diagnostics/playback.yaml'}\n"
    )
    expanded = do_packages_pass(yaml_util.load_yaml(config))
    expanded = merge_packages(do_substitution_pass(expanded))
    assert expanded["playback_diagnostics"]["media_player"] == "alternate_player"
    assert ("voice_assistant" in expanded) is has_voice
    assert ("voice_assistant" in expanded["playback_diagnostics"]) is has_voice
    assert "runtime_controller" not in expanded


def test_real_package_parser_keeps_existing_va_actions(tmp_path):
    from esphome import yaml_util
    from esphome.components.packages import do_packages_pass, merge_packages
    from esphome.components.substitutions import do_substitution_pass

    config = tmp_path / "package.yaml"
    config.write_text(
        "substitutions:\n"
        f"  ext_components_source: {ROOT / 'esphome/components'}\n"
        "voice_assistant:\n  id: va\n  on_tts_start:\n    - logger.log: existing UI action\n"
        "packages:\n"
        f"  observation: !include {ROOT / 'packages/diagnostics/playback.yaml'}\n"
    )
    expanded = do_packages_pass(yaml_util.load_yaml(config))
    expanded = merge_packages(do_substitution_pass(expanded))
    actions = expanded["voice_assistant"]["on_tts_start"]
    assert any(item.get("logger.log") == "existing UI action" for item in actions)
    assert any("on_tts_start(x.size())" in str(item.get("lambda", "")) for item in actions)


@pytest.fixture(scope="module")
def topology_module():
    spec = importlib.util.spec_from_file_location(
        "playback_diagnostics_validation_test", COMPONENT / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicTopology:
    """A small final-validation config with ordinary nested declaration paths."""

    def __init__(self):
        self.tree = {
            "speaker": [
                {"id": "tts", "platform": "resampler", "output_speaker": "tts_input"},
                {"id": "mix", "platform": "mixer", "output_speaker": "hardware",
                 "source_speakers": [{"id": "tts_input"}]},
                {"id": "hardware", "platform": "esp_audio_stack", "esp_audio_stack_id": "stack"},
            ],
            "media_player": [
                {"id": "player", "platform": "speaker_source",
                 "announcement_pipeline": {"speaker": "tts", "sources": ["announcement_http"]}},
            ],
        }
        self.paths = {
            "tts": ["speaker", 0, "id"],
            "tts_input": ["speaker", 1, "source_speakers", 0, "id"],
            "mix": ["speaker", 1, "id"],
            "hardware": ["speaker", 2, "id"],
            "player": ["media_player", 0, "id"],
        }

    def get_path_for_id(self, identifier):
        return self.paths[str(identifier)]

    def get_config_for_path(self, path):
        value = self.tree
        for item in path:
            value = value[item]
        return value


def public_diagnostic_bindings():
    from esphome.core import ID

    return {
        key: ID(value, is_declaration=False)
        for key, value in {
            "id": "diagnostic", "audio_stack": "stack", "media_player": "player",
            "announcement_source": "announcement_http", "tts_speaker": "tts",
            "mixer_input": "tts_input", "hardware_speaker": "hardware", "mixer": "mix",
        }.items()
    }


@pytest.mark.parametrize("bind_optional_mixer", [False, True])
def test_native_serialized_completion_topology_is_accepted(topology_module, bind_optional_mixer):
    full = PublicTopology()
    config = public_diagnostic_bindings()
    if not bind_optional_mixer:
        config.pop("mixer")
    token = topology_module.fv.full_config.set(full)
    try:
        assert topology_module.FINAL_VALIDATE_SCHEMA(config) is config
    finally:
        topology_module.fv.full_config.reset(token)


@pytest.mark.parametrize(
    "location, replacement, error_field",
    [
        (("speaker", 0, "platform"), "template", "tts_speaker"),
        (("speaker", 0, "output_speaker"), "other_input", "tts_speaker"),
        (("speaker", 1, "platform"), "template", "mixer_input"),
        (("speaker", 1, "output_speaker"), "other_hardware", "mixer_input"),
        (("speaker", 2, "platform"), "i2s_audio", "hardware_speaker"),
        (("speaker", 2, "esp_audio_stack_id"), "other_stack", "hardware_speaker"),
        (("speaker", 1, "id"), "other_mixer", "mixer"),
        (("media_player", 0, "announcement_pipeline", "sources"), ["other_http"], "media_player"),
        (("media_player", 0, "announcement_pipeline", "speaker"), "other_tts", "media_player"),
        (("media_player", 0, "announcement_pipeline"), {}, "media_player"),
    ],
    ids=["tts-platform", "tts-output", "mixer-platform", "mixer-output", "hardware-platform",
         "hardware-stack", "mixer-owner", "player-source", "player-speaker", "missing-pipeline"],
)
def test_wrong_completion_topology_is_rejected(topology_module, location, replacement, error_field):
    full = PublicTopology()
    target = full.tree
    for item in location[:-1]:
        target = target[item]
    target[location[-1]] = replacement
    config = public_diagnostic_bindings()
    token = topology_module.fv.full_config.set(full)
    try:
        with pytest.raises(topology_module.cv.Invalid) as failure:
            topology_module.FINAL_VALIDATE_SCHEMA(config)
        assert failure.value.path == [error_field]
    finally:
        topology_module.fv.full_config.reset(token)
