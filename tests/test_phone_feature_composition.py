"""The three standard phone services compose independently before firmware builds."""
import itertools
import json
import hashlib
import re
from pathlib import Path
import subprocess
import sys

import pytest
from esphome import yaml_util

from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ("directory", "blf", "mwi")
PROFILES = (
    "yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml",
    "yamls/voip-only/single-bus/spotpear-ball-v2-voip.yaml",
)


@pytest.fixture(scope="module")
def profile_workspaces(tmp_path_factory):
    # Font/model downloads belong to the profile data directory. Keep that
    # cache for the matrix, while the official cleaner removes each build.
    return {profile: tmp_path_factory.mktemp("phone-feature-profile") for profile in PROFILES}


def keep_application_source(directory, main):
    """Keep the code oracle and full identity without duplicating generated assets."""
    lines = main.splitlines(keepends=True)
    body = [line for line in lines if not re.match(r"\s*static constexpr uint8_t \w+\[\] PROGMEM = \{", line)]
    (directory / "main-application.cpp").write_text("// Generated asset declarations omitted; regenerate before compiling.\n" + "".join(body))
    (directory / "source-identity.json").write_text(json.dumps({"full_source_sha256": hashlib.sha256(main.encode()).hexdigest(),
        "full_source_bytes": len(main.encode()), "omitted_asset_declarations": len(lines) - len(body)}) + "\n")


@pytest.mark.parametrize("profile", PROFILES, ids=("full", "phone-only"))
@pytest.mark.parametrize("selection", list(itertools.product((False, True), repeat=3)),
                         ids=lambda flags: "".join(str(int(flag)) for flag in flags))
def test_three_phone_services_generate_independent_components(tmp_path, profile, selection, profile_workspaces):
    shipped = yaml_util.load_yaml(ROOT / profile)
    requested = [name for name in shipped["substitutions"]["firmware_features"] if name not in FEATURES]
    requested += [name for name, selected in zip(FEATURES, selection) if selected]
    work = profile_workspaces[profile]
    config = device_config(work, requested, profile)
    run = subprocess.run([sys.executable, "-m", "esphome", "compile", str(config), "--only-generate"],
                         capture_output=True, text=True, timeout=180)
    (tmp_path / "generate.log").write_text(run.stdout + run.stderr)
    if run.returncode != 0:
        pytest.fail((run.stdout + run.stderr)[-7000:])
    definitions_path = next(work.rglob("src/esphome/core/defines.h"))
    generated = definitions_path.parents[2]
    main = (generated / "main.cpp").read_text()
    definitions = definitions_path.read_text()
    components = generated / "esphome/components"
    keep_application_source(tmp_path, main)
    (tmp_path / "defines.h").write_text(definitions)
    (tmp_path / "components.json").write_text(json.dumps(sorted(p.name for p in components.iterdir() if p.is_dir())) + "\n")
    clean = subprocess.run([sys.executable, "-m", "esphome", "clean", str(config)],
                           capture_output=True, text=True, timeout=120)
    (tmp_path / "official-clean.log").write_text(clean.stdout + clean.stderr)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    directory, blf, mwi = selection
    for component, enabled in (("ldap_directory", directory), ("phone_client", directory)):
        copied = json.loads((tmp_path / "components.json").read_text())
        assert (component in copied) is enabled
    for define, enabled in (("USE_ESPHOME_VOIP_DIRECTORY", directory),
                            ("USE_ESPHOME_VOIP_BLF", blf), ("USE_ESPHOME_VOIP_PRESENCE", blf),
                            ("USE_ESPHOME_VOIP_MWI", mwi)):
        assert (f"#define {define}" in definitions) is enabled, define
    assert "phone_voicemail_page" not in main
    assert "ui_voicemail_active" not in main
    assert "imap_voicemail" not in copied
    assert ("top_voicemail_label" in main) is mwi
    assert ("get_contact_blf(" in main) is blf
    assert "contacts_list_view" in main
    if "voip-only" in profile:
        assert "runtime_controller" not in copied
        assert "runtime_controller::" not in main


def test_static_contact_minimum_has_no_optional_phone_clients_or_players(tmp_path):
    source = ROOT.parent / "esphome-voip-stack"
    text = (source / "tests/minimal_no_text.yaml").read_text()
    text = text.replace("../esphome/components", str(source / "esphome/components"))
    text = text.replace("voip_stack:\n", "voip_stack:\n  ha_integration: false\n  static_contacts:\n    - name: Reception\n      ip: 192.0.2.5\n      port: 5060\n")
    config = tmp_path / "static.yaml"
    config.write_text(text)
    run = subprocess.run([sys.executable, "-m", "esphome", "compile", str(config), "--only-generate"],
                         capture_output=True, text=True, timeout=180)
    (tmp_path / "generate.log").write_text(run.stdout + run.stderr)
    assert run.returncode == 0, (run.stdout + run.stderr)[-5000:]
    definitions_path = next(tmp_path.rglob("src/esphome/core/defines.h"))
    generated = definitions_path.parents[2]
    main = (generated / "main.cpp").read_text()
    definitions = definitions_path.read_text()
    components = generated / "esphome/components"
    for name in ("ldap_directory", "imap_voicemail", "phone_client", "media_source", "speaker_source", "runtime_controller", "call_keypad", "phone_list_view", "lvgl_visibility"):
        assert not (components / name).exists(), name
    for token in ("USE_ESPHOME_VOIP_DIRECTORY", "USE_ESPHOME_VOIP_BLF", "USE_ESPHOME_VOIP_MWI", "USE_ESPHOME_VOIP_REGISTRATION"):
        assert f"#define {token}" not in definitions
    assert 'add_contact("Reception|192.0.2.5|5060|' in main


@pytest.fixture(scope="module")
def boundary_workspace(tmp_path_factory):
    return tmp_path_factory.mktemp("phone-service-boundaries")


BOUNDARIES = [
    ("yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml", value) for value in (False, True)
] + [
    ("yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml", value) for value in (False, True)
] + [
    ("yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml", value) for value in (False, True)
] + [
    ("yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-full-duplex.yaml", True),
    ("yamls/voip-only/single-bus/generic-s3-voip.yaml", True),
    ("yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-mic-only.yaml", True),
    ("yamls/full-experience/single-bus/generic-s3-full-lite-aec.yaml", True),
]


@pytest.mark.parametrize("profile,enabled", BOUNDARIES)
@pytest.mark.timeout(240)
def test_phone_service_hardware_boundaries_generate_without_hidden_dependencies(tmp_path, boundary_workspace, profile, enabled):
    original = yaml_util.load_yaml(ROOT / profile)
    requested = [name for name in original["substitutions"]["firmware_features"] if name not in FEATURES]
    if enabled:
        requested += list(FEATURES)
    config = device_config(boundary_workspace, requested, profile)
    data = yaml_util.load_yaml(config)
    data.update({key: value for key, value in original.items() if key not in ("packages", "substitutions")})
    config.write_text(yaml_util.dump(data))
    with (tmp_path / "generate.log").open("w") as output:
        run = subprocess.run([sys.executable, "-m", "esphome", "compile", str(config), "--only-generate"],
                             stdout=output, stderr=subprocess.STDOUT, text=True, timeout=180)
    assert run.returncode == 0, (tmp_path / "generate.log").read_text()[-7000:]
    definitions_path = next(boundary_workspace.rglob("src/esphome/core/defines.h"))
    generated = definitions_path.parents[2]
    main = (generated / "main.cpp").read_text()
    definitions = definitions_path.read_text()
    names = {path.name for path in (generated / "esphome/components").iterdir() if path.is_dir()}
    keep_application_source(tmp_path, main)
    (tmp_path / "defines.h").write_text(definitions)
    (tmp_path / "components.json").write_text(json.dumps(sorted(names)) + "\n")
    clean = subprocess.run([sys.executable, "-m", "esphome", "clean", str(config)], capture_output=True, text=True, timeout=120)
    (tmp_path / "official-clean.log").write_text(clean.stdout + clean.stderr)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert ("ldap_directory" in names) is enabled
    assert "imap_voicemail" not in names
    if "voip-only" in profile:
        assert "runtime_controller" not in names
    assert "USE_IMAP_VOICEMAIL_PLAYBACK" not in definitions
    assert "phone_voicemail_page" not in main
    if "mic-only" in profile:
        assert "media_source" not in names and "speaker_source" not in names
        assert "speaker_media_player" not in main
