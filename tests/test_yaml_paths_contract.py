import os
import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import subprocess
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_yaml_paths() -> ModuleType:
    loader = SourceFileLoader(
        "yaml_paths_contract", str(ROOT / "scripts/yaml_paths.sh")
    )
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def _expected_mode() -> str:
    mode = os.environ.get("YAML_PATH_MODE", "remote").strip().lower()
    if mode not in {"local", "remote"}:
        raise AssertionError("YAML_PATH_MODE must be local or remote")
    return mode


def test_checked_yaml_paths_are_consistent() -> None:
    subprocess.run(
        [
            str(ROOT / "scripts/yaml_paths.sh"),
            "check",
            "--expect",
            _expected_mode(),
        ],
        cwd=ROOT,
        check=True,
    )


def test_check_file_limits_the_expected_mode_gate() -> None:
    target = "yamls/voip-only/single-bus/generic-s3-voip.yaml"
    expected = _expected_mode()
    opposite = "remote" if expected == "local" else "local"
    result = subprocess.run(
        [
            str(ROOT / "scripts/yaml_paths.sh"),
            "check",
            "--expect",
            opposite,
            "--file",
            target,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    failures = [
        line for line in (result.stdout + result.stderr).splitlines() if line.startswith("FAIL:")
    ]
    assert failures == [f"FAIL: {target} ({expected}, expected {opposite})"]


def test_p4_camera_package_path_is_relative_to_the_top_level_yaml() -> None:
    yaml_paths = _load_yaml_paths()
    package = ROOT / "packages/voip/p4_video_h264.yaml"

    source_root = yaml_paths.camera_source_root(package)
    source = yaml_paths.relative(
        source_root, yaml_paths.CAMERA_ROOT / "components"
    )

    assert source == "../../../../esphome-esp-video-camera/components"


def test_remote_camera_source_uses_the_coordinated_owner(tmp_path: Path) -> None:
    yaml_paths = _load_yaml_paths()
    config = tmp_path / "camera.yaml"
    config.write_text(
        "external_components:\n"
        "  - source: placeholder\n"
        "    components: [esp_video_camera]\n"
    )

    for ref, owner in (("dev", "n-IA-hane"), ("main", "Psix-anp")):
        yaml_paths.rewrite_camera(config, ref)
        assert (
            f"source: github://{owner}/esphome-esp-video-camera@{ref}"
            in config.read_text()
        )


def test_global_selection_preserves_private_local_overrides(tmp_path: Path) -> None:
    tool = _load_yaml_paths()
    maintained = tmp_path / "phone.yaml"
    private = tmp_path / "phone-local.yaml"
    assert tool.selected([maintained, private], None) == [maintained]
    assert tool.selected([maintained, private], str(private)) == [private]


def test_remote_check_follows_package_includes(tmp_path: Path) -> None:
    tool = _load_yaml_paths()
    tool.ROOT = tmp_path
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'yamls').mkdir()
    (tmp_path / 'packages').mkdir()
    (tmp_path / 'yamls/device.yaml').write_text(
        'esphome:\n  name: package-check\npackages:\n'
        '  voice: github://n-IA-hane/esphome-intercom/packages/voice.yaml@main\n'
    )
    (tmp_path / 'packages/voice.yaml').write_text(
        'packages:\n  controller: !include ../../undeclared-repository/controller.yaml\n'
    )
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'packages/voice.yaml'], check=True)
    assert tool.check('remote', None) == 1


def test_single_device_entrypoint_gets_all_local_and_remote_source_overrides(tmp_path):
    tool = _load_yaml_paths()
    config = tmp_path / 'waveshare-p4-test.yaml'
    config.write_text('substitutions:\n  firmware_features: [voip, display, video]\n'
                      'packages:\n  device: github://n-IA-hane/esphome-intercom/packages/devices/waveshare-p4-phone-jpeg.yaml@main\n')
    tool.rewrite_yaml(config, 'local')
    text = config.read_text()
    for key in ('ext_components_source', 'voip_stack_components_source', 'audio_stack_components_source',
                'esp_screenshot_components_source', 'esp_video_renderer_components_source'):
        assert key + ':' in text
    assert str(tool.relative(config.parent, tool.ROOT.parent / 'esphome-screenshot/esphome/components')) in text
    assert str(tool.relative(config.parent, tool.ROOT.parent / 'esphome-video-renderer/esphome/components')) in text
    assert 'camera_components_source:' in text
    assert 'intercom_idf_root:' in text
    assert '!include ' in text
    tool.rewrite_yaml(config, 'dev')
    text = config.read_text()
    assert 'packages/devices/waveshare-p4-phone-jpeg.yaml@dev' in text
    assert 'github://n-IA-hane/esphome-audio-stack@dev' in text
    assert 'github://n-IA-hane/esphome-screenshot@dev' in text
    assert 'github://n-IA-hane/esphome-video-renderer@dev' in text
    assert 'github://n-IA-hane/esphome-esp-video-camera@dev' in text
    assert 'intercom_idf_ref: "dev"' in text
    assert 'intercom_idf_root: ""' in text


def test_camera_source_variable_is_owned_by_the_top_level_profile(tmp_path):
    tool = _load_yaml_paths()
    config = tmp_path / 'camera.yaml'
    text = 'external_components:\n  - source: ${camera_components_source}\n    components: [esp_video_camera]\n'
    config.write_text(text)
    tool.rewrite_camera(config, 'local')
    assert config.read_text() == text


def test_tracked_public_package_defaults_are_online_and_coordinated():
    tool = _load_yaml_paths()
    version = json.loads((ROOT / 'custom_components/voip_stack/manifest.json').read_text())['version']
    expected = 'dev' if version.endswith('-dev') else 'main'
    wrong = {str(path.relative_to(ROOT)): refs for path in tool.tracked_package_files()
             if (refs := tool.owned_remote_refs(path)) and set(refs) != {expected}}
    assert not wrong, wrong


def test_remote_default_rewrite_preserves_upstream_and_local_mode_inputs(tmp_path):
    tool = _load_yaml_paths()
    path = tmp_path / 'device.yaml'
    path.write_text('substitutions:\n'
        '  voip_stack_components_source: github://n-IA-hane/esphome-voip-stack@main\n'
        '  assets_base: https://github.com/n-IA-hane/esphome-intercom/raw/main/\n'
        '  camera_components_source: github://Psix-anp/esphome-esp-video-camera@main\n'
        '  playback_diagnostics_source: \'${ source | default("github://n-IA-hane/esphome-intercom@main") }\'\n')
    tool.rewrite_remote_defaults(path, 'dev')
    text = path.read_text()
    assert set(tool.owned_remote_refs(path)) == {'dev'}
    assert 'github://Psix-anp/esphome-esp-video-camera@main' in text
    tool.rewrite_remote_defaults(path, 'main')
    assert set(tool.owned_remote_refs(path)) == {'main'}


def test_public_camera_default_round_trip_preserves_an_explicit_upstream_override(tmp_path):
    tool = _load_yaml_paths()
    path = tmp_path / 'device.yaml'
    path.write_text('substitutions:\n'
        '  camera_components_source: \'${ camera_components_source | default("github://n-IA-hane/esphome-esp-video-camera@dev") }\'\n'
        '  custom_source: github://Psix-anp/esphome-esp-video-camera@main\n')
    tool.rewrite_remote_defaults(path, 'main')
    assert 'default("github://Psix-anp/esphome-esp-video-camera@main")' in path.read_text()
    tool.rewrite_remote_defaults(path, 'dev')
    assert 'default("github://n-IA-hane/esphome-esp-video-camera@dev")' in path.read_text()
    assert 'custom_source: github://Psix-anp/esphome-esp-video-camera@main' in path.read_text()


def test_package_selection_ignores_untracked_private_overlays(tmp_path):
    tool = _load_yaml_paths()
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'packages').mkdir()
    public = tmp_path / 'packages/public.yaml'
    private = tmp_path / 'packages/private-local.yaml'
    public.write_text('substitutions: {}\n')
    private.write_text('substitutions: {}\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'packages/public.yaml'], check=True)
    tool.ROOT = tmp_path
    assert tool.tracked_package_files() == [public]
    private.write_text('external_components:\n  - source: private-camera\n    components: [esp_video_camera]\n')
    (tmp_path / 'yamls').mkdir()
    assert tool.camera_files() == []
    assert tool.selected(tool.tracked_package_files(), str(private)) == [private]


def test_local_mode_keeps_public_remote_defaults(tmp_path, monkeypatch):
    tool = _load_yaml_paths()
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'yamls').mkdir()
    (tmp_path / 'packages').mkdir()
    path = tmp_path / 'packages/device.yaml'
    text = 'substitutions:\n  voip_stack_components_source: github://n-IA-hane/esphome-voip-stack@dev\n'
    path.write_text(text)
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'packages/device.yaml'], check=True)
    tool.ROOT = tmp_path
    monkeypatch.setattr('sys.argv', [str(ROOT / 'scripts/yaml_paths.sh'), 'local'])
    assert tool.main() == 0
    assert path.read_text() == text


def test_unquoted_assets_follow_the_selected_remote_ref(tmp_path):
    tool = _load_yaml_paths()
    path = tmp_path / 'device.yaml'
    path.write_text('substitutions:\n  assets_base: https://github.com/n-IA-hane/esphome-intercom/raw/main/\n')
    tool.rewrite_yaml(path, 'dev')
    assert 'https://github.com/n-IA-hane/esphome-intercom/raw/dev/' in path.read_text()
