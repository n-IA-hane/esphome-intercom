"""Compile native providers and explicitly recorded local adaptations."""
from pathlib import Path
import base64
import hashlib
from importlib import metadata
import json
import os
import re
import subprocess
import sys

from esphome import yaml_util
import pytest

from tests.esphome_native import NATIVE_COMPONENTS
from tests.test_package_composition import FULL, device_config
from scripts.official_component_fixtures import verified_component_directory


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "esphome/components"
OFFICIAL = (
    "audio", "mixer",
    "ota", "resampler", "speaker", "sendspin", "audio_file",
    "media_source", "microphone",
)
RETIRED = (
    "audio", "mixer",
    "ota", "audio_http_compat", "sendspin_compat", "audio_dsp_iram", "speaker",
    "audio_http", "micro_wake_word",
)
# Only the player lifecycle and Assist idle still require a local runtime delta.
LIFECYCLE = ("speaker_source", "voice_assistant")
PINNED_OFFICIAL = ("audio_http", "micro_wake_word")
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(240)]


@pytest.mark.parametrize("component", RETIRED)
def test_retired_provider_is_not_shipped(component):
    assert not (COMPONENTS / component).exists()


def test_audio_stack_does_not_ship_a_shadow_resampler():
    source = Path(os.environ.get("AUDIO_STACK_REPO", ROOT.parent / "esphome-audio-stack"))
    assert not (source / "esphome/components/resampler").exists()


@pytest.fixture(scope="module")
def native_full_build(tmp_path_factory):
    directory = tmp_path_factory.mktemp("official-provider-contract")
    config = device_config(directory, FULL)
    expanded = subprocess.run(
        [sys.executable, "-m", "esphome", "config", str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert expanded.returncode == 0, expanded.stdout + expanded.stderr
    assert "Could not resolve substitution" not in expanded.stderr
    document_path = directory / "expanded.yaml"
    document_path.write_text(expanded.stdout)
    document = yaml_util.load_yaml(document_path)
    generated = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    source = next(directory.rglob("src/main.cpp")).parent
    return document, source


def test_real_parser_selects_only_recorded_lifecycle_overrides(native_full_build):
    document, source = native_full_build
    providers = {
        component for provider in document.get("external_components", [])
        for component in provider.get("components", [])
    }
    assert set(OFFICIAL).isdisjoint(providers)
    assert set(LIFECYCLE) <= providers
    assert set(PINNED_OFFICIAL) <= providers
    assert {"esp_audio_stack", "esp_afe", "voip_stack", "runtime_controller"} <= providers
    assert not any(name in document for name in ("audio_http_compat", "sendspin_compat", "audio_dsp_iram"))
    generated = (source / "main.cpp").read_text()
    for class_name in ("ESPAudioStack", "EspAfe", "VoipStack", "RuntimeController"):
        assert class_name in generated


def test_real_voip_only_configuration_does_not_load_or_generate_mww(tmp_path: Path) -> None:
    config = device_config(tmp_path, features=["voip"])
    expanded = subprocess.run(
        [sys.executable, "-m", "esphome", "config", str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert expanded.returncode == 0, expanded.stdout + expanded.stderr
    assert "Could not resolve substitution" not in expanded.stderr
    document_path = tmp_path / "expanded.yaml"
    document_path.write_text(expanded.stdout)
    document = yaml_util.load_yaml(document_path)
    providers = {
        component for provider in document.get("external_components", [])
        for component in provider.get("components", [])
    }
    assert "voip_stack" in document
    assert "micro_wake_word" not in document
    assert "micro_wake_word" not in providers

    generated = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    assert "Could not resolve substitution" not in generated.stderr
    source = next(tmp_path.rglob("src/main.cpp")).parent
    assert not (source / "esphome/components/micro_wake_word").exists()
    assert "micro_wake_word::MicroWakeWord" not in (source / "main.cpp").read_text()


@pytest.mark.parametrize("component", OFFICIAL)
def test_generated_native_provider_is_byte_identical_to_installed_esphome(native_full_build, component):
    _, source = native_full_build
    generated = source / "esphome/components" / component
    files = [path for path in generated.rglob("*") if path.suffix in (".cpp", ".h")]
    assert files, f"The FULL composition did not load {component}"
    distribution = metadata.distribution("esphome")
    records = {str(path): path for path in distribution.files}
    for path in files:
        original = NATIVE_COMPONENTS / component / path.relative_to(generated)
        assert original.is_file(), path
        assert path.read_bytes() == original.read_bytes(), path
        relative = Path("esphome/components") / component / path.relative_to(generated)
        record = records[str(relative)]
        assert record.hash.mode == "sha256"
        digest = base64.urlsafe_b64encode(hashlib.sha256(original.read_bytes()).digest()).rstrip(b"=").decode()
        assert digest == record.hash.value, f"Installed provider was edited after installation: {original}"


def test_owned_audio_formats_reach_official_consumers(native_full_build):
    document, source = native_full_build
    stack = document["esp_audio_stack"]
    assert stack["sample_rate"] == 48000
    assert stack["output_sample_rate"] == 16000
    assert document["esp_afe"]["mic_num"] == 2
    assert stack["tdm_mic_slots"] == [0, 2]
    mic = document["microphone"][0]
    assert mic["platform"] == "esp_audio_stack"
    speaker = next(item for item in document["speaker"] if item["platform"] == "esp_audio_stack")
    assert speaker["sample_rate"] == 48000
    assert speaker["num_channels"] == 1
    generated = (source / "main.cpp").read_text()
    # ESPHome omits generated IDs from its YAML dump even with defaults enabled.
    stack_ids = re.findall(r"\besp_audio_stack::ESPAudioStack \*const (\w+) =", generated)
    assert len(stack_ids) == 1
    stack_id = stack_ids[0]
    assert f'{stack_id}->set_output_sample_rate(16000);' in generated
    assert f'{mic["id"]}->set_parent({stack_id});' in generated
    assert f'{speaker["id"]}->set_parent({stack_id});' in generated
    for name in ("voice_assistant", "micro_wake_word", "voip_stack"):
        assert str(mic["id"]) in str(document[name]["microphone"])


@pytest.mark.parametrize("component", LIFECYCLE)
def test_recorded_lifecycle_provider_reaches_generated_cpp(native_full_build, component):
    _, source = native_full_build
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    entry = manifest["components"][component]
    local = COMPONENTS / component
    for name, digest in entry["source_sha256"].items():
        assert hashlib.sha256((local / name).read_bytes()).hexdigest() == digest
    generated = source / "esphome/components" / component
    files = [path for path in generated.rglob("*") if path.suffix in (".cpp", ".h")]
    assert files, f"The FULL composition did not load {component}"
    distribution = metadata.distribution("esphome")
    records = {str(path): path for path in distribution.files}
    local_only = set(entry["source_sha256"]) - set(
        entry.get("upstream_source_sha256", entry["source_sha256"])
    )
    for path in files:
        relative = path.relative_to(generated)
        assert path.read_bytes() == (local / relative).read_bytes()
        # A local adaptation must not mutate the installed ESPHome provider.
        installed = NATIVE_COMPONENTS / component / relative
        if relative.as_posix() in local_only:
            # Recorded local helpers have no file in the official distribution.
            assert not installed.exists(), installed
            continue
        record = records[str(Path("esphome/components") / component / relative)]
        assert record.hash.mode == "sha256"
        digest = base64.urlsafe_b64encode(hashlib.sha256(installed.read_bytes()).digest()).rstrip(b"=").decode()
        assert digest == record.hash.value, f"Installed provider was edited: {installed}"


@pytest.mark.parametrize("component", PINNED_OFFICIAL)
def test_official_pinned_provider_reaches_generated_cpp(native_full_build, component):
    document, source = native_full_build
    official = verified_component_directory(component)
    selected = [entry for entry in document["external_components"] if component in entry["components"]]
    assert len(selected) == 1
    assert Path(selected[0]["source"]["path"]).resolve() == official.parent
    generated = source / "esphome/components" / component
    files = [path for path in generated.rglob("*") if path.suffix in (".cpp", ".h")]
    assert files
    for path in files:
        assert path.read_bytes() == (official / path.relative_to(generated)).read_bytes()


def test_production_official_pins_match_catalog_and_fixture_provenance():
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    catalog = json.loads((ROOT / "qualification/sources.json").read_text())
    packages = {
        "audio_http": "packages/media_player/playback_compat.yaml",
        "micro_wake_word": "packages/runtime/wake_word_events.yaml",
    }
    for component, package in packages.items():
        entry = manifest["official_component_pins"][component]
        assert entry["repository"] == "esphome/esphome"
        assert re.fullmatch(r"[0-9a-f]{40}", entry["revision"])
        assert entry["source_kind"] == "official_pinned_component" and not entry["adaptations"]
        assert entry["proposal_status"] == "merged" and entry["removal_condition"]
        document = yaml_util.load_yaml(ROOT / package)
        assert f'"github://esphome/esphome@{entry["revision"]}"' in (
            document["defaults"][entry["source_substitution"]])
        assert catalog["official_sources"]["esphome_" + component]["revision"] == entry["revision"]
        assert component in manifest["retired_local_providers"]
        assert not (COMPONENTS / component).exists()
        assert verified_component_directory(component).is_dir()


def test_recorded_audio_dependency_versions_match_the_real_generator(native_full_build):
    _, source = native_full_build
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    dependencies = yaml_util.load_yaml(source / "idf_component.yml")["dependencies"]
    for key, dependency in (("audio_libs", "esphome/esp-audio-libs"),
                            ("micro_decoder", "esphome/micro-decoder"),
                            ("sendspin", "sendspin/sendspin-cpp")):
        assert manifest["build_baseline"][key] == dependencies[dependency]["version"]


def test_all_local_upstream_providers_are_documented():
    assert not (COMPONENTS / "spi").exists()
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    assert set(manifest["components"]) == set(LIFECYCLE) | {"mipi_dsi"}
    for component, entry in manifest["components"].items():
        document = (COMPONENTS / component / "UPSTREAM.md").read_text()
        assert entry.get("upstream_revision", manifest["revision"]) in document
    assert set(manifest["native_components"]) == set(OFFICIAL)
    assert metadata.version("esphome") == manifest["native_provider_baseline"]["version"]


def test_sdk_manifest_records_qualification_without_overriding_framework():
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    baseline = manifest["build_baseline"]
    assert baseline["esp_idf_selection"] == "esphome_recommended"
    assert "esp_idf_p4_revision" not in baseline
    reference = baseline["esp_idf_qualification_reference"]
    assert reference["repository"] == "espressif/esp-idf"
    assert reference["tag"] == "v5.5.6"
    assert reference["revision"] == "880a203ae603d08a94d06aa01e0d6761958e5e9e"
    assert reference["framework_override"] is False


def test_official_build_backports_are_separate_from_runtime_provider_overrides():
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    fixes = {"p4_sdmmc_reset": "dd47ea3696b419fdb19cf747a6b96e2c4f6abcc5",
             "p4_ppa_backport": "9da8ff53f751f6682f69897d886f3a9946deed31"}
    assert set(manifest["build_backports"]) == set(fixes)
    assert set(fixes).isdisjoint(manifest["components"])
    for name, fix in fixes.items():
        entry = manifest["build_backports"][name]
        assert entry["official_fix_revision"] == fix
        assert entry["apply_when"] == {"variant": "ESP32P4", "framework": "esp-idf", "versions": ["5.5.6", "6.0.3"]}
        assert entry["scope"] == "build_directory_only"
        assert entry["installed_sdk_modified"] is False
        assert entry["removal_condition"]
        for filename, digest in entry["source_sha256"].items():
            assert hashlib.sha256((COMPONENTS / name / filename).read_bytes()).hexdigest() == digest
    entry = manifest["components"]["voice_assistant"]
    assert entry["public_proposal"] is True
    assert entry["proposal_url"] == "https://github.com/esphome/esphome/pull/22782"
    assert manifest["official_component_pins"]["micro_wake_word"]["proposal_url"] == "https://github.com/esphome/esphome/pull/22781"


@pytest.fixture(scope="module")
def official_p4_build(tmp_path_factory):
    directory = tmp_path_factory.mktemp("official-p4-provider-contract")
    profile = "yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml"
    document = yaml_util.load_yaml(ROOT / profile)
    config = device_config(directory, document["substitutions"]["firmware_features"], profile)
    expanded = subprocess.run([sys.executable, "-m", "esphome", "config", str(config)],
                              capture_output=True, text=True, timeout=180)
    assert expanded.returncode == 0, expanded.stdout + expanded.stderr
    path = directory / "expanded.yaml"
    path.write_text(expanded.stdout)
    generated = subprocess.run([sys.executable, "-m", "esphome", "compile", "--only-generate", str(config)],
                               capture_output=True, text=True, timeout=180)
    assert generated.returncode == 0, generated.stdout + generated.stderr
    return yaml_util.load_yaml(path), next(directory.rglob("src/main.cpp")).parent


def test_p4_resolves_declared_mipi_and_esphome_recommended_sdk(official_p4_build):
    from esphome.components.esp32 import ESP_IDF_FRAMEWORK_VERSION_LOOKUP

    document, source = official_p4_build
    providers = {name for item in document.get("external_components", [])
                 for name in item.get("components", [])}
    assert {"mipi_dsi", "p4_sdmmc_reset", "p4_ppa_backport"} <= providers
    framework = document["esp32"]["framework"]
    assert framework["version"] == str(ESP_IDF_FRAMEWORK_VERSION_LOOKUP["recommended"])
    assert not framework.get("source")
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    assert set(manifest["build_backports"]) <= providers
    # Recommended selection stays authoritative; a recorded source qualification
    # must not grow into an implicit framework override in the expanded YAML.
    assert manifest["build_baseline"]["esp_idf_selection"] == "esphome_recommended"
    assert not any("-DLV_USE_PPA" in flag for flag in document["esphome"].get("build_flags", []))
    assert not {"esp_driver_ppa", "esp_video_csi_compat"} & {
        item["name"] for item in framework.get("components", [])}
    for name in ["mipi_dsi.h", "mipi_dsi.cpp"]:
        assert (source / "esphome/components/mipi_dsi" / name).read_bytes() == (
            COMPONENTS / "mipi_dsi" / name).read_bytes()


def test_loaded_upstream_exceptions_have_an_explicit_exit(native_full_build):
    document, _ = native_full_build
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    providers = {name for item in document.get("external_components", [])
                 for name in item.get("components", [])}
    shadowed = {name for name in providers if (NATIVE_COMPONENTS / name).is_dir()}
    assert shadowed <= set(manifest["components"]) | set(manifest["official_component_pins"])
    for name in shadowed:
        entry = manifest["components"].get(name, manifest["official_component_pins"].get(name))
        assert re.fullmatch(r"[0-9a-f]{40}", entry.get("upstream_revision", entry.get("revision")))
        assert entry["removal_condition"]
        assert entry.get("proposal_status") or entry.get("upstream_pr")
