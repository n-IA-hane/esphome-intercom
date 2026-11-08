"""Compile native providers and the four recorded lifecycle adaptations."""
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
)
# HTTP retirement, player lifecycle, Assist idle and MWW model reload.
LIFECYCLE = ("audio_http", "speaker_source", "voice_assistant", "micro_wake_word")
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
    for path in files:
        relative = path.relative_to(generated)
        assert path.read_bytes() == (local / relative).read_bytes()
        # A local adaptation must not mutate the installed ESPHome provider.
        installed = NATIVE_COMPONENTS / component / relative
        record = records[str(Path("esphome/components") / component / relative)]
        assert record.hash.mode == "sha256"
        digest = base64.urlsafe_b64encode(hashlib.sha256(installed.read_bytes()).digest()).rstrip(b"=").decode()
        assert digest == record.hash.value, f"Installed provider was edited: {installed}"


def test_all_local_upstream_providers_are_documented():
    assert not (COMPONENTS / "spi").exists()
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    assert set(manifest["components"]) == {"mipi_dsi", *LIFECYCLE}
    for component, entry in manifest["components"].items():
        document = (COMPONENTS / component / "UPSTREAM.md").read_text()
        assert entry.get("upstream_revision", manifest["revision"]) in document
    assert set(manifest["native_components"]) == set(OFFICIAL)
    assert metadata.version("esphome") == manifest["native_provider_baseline"]["version"]
