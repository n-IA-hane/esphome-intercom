import os
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
    assert f"FAIL: {target} ({expected}, expected {opposite})" in failures
    # One selected file may fail multiple guards, but no other file may leak
    # into this scoped mode/source check.
    assert all(line.startswith(f"FAIL: {target} (") for line in failures)


def test_p4_camera_package_path_is_relative_to_the_top_level_yaml() -> None:
    yaml_paths = _load_yaml_paths()
    package = ROOT / "packages/voip/p4_video_h264.yaml"

    source_root = yaml_paths.camera_source_root(package)
    source = yaml_paths.relative(
        source_root, yaml_paths.CAMERA_ROOT / "components"
    )

    assert source == "../../../../esphome-esp-video-camera/components"


def test_remote_camera_source_uses_upstream_main(tmp_path: Path) -> None:
    yaml_paths = _load_yaml_paths()
    config = tmp_path / "camera.yaml"
    config.write_text(
        "external_components:\n"
        "  - source: placeholder\n"
        "    components: [esp_video_camera]\n"
    )

    for ref in ("dev", "main"):
        yaml_paths.rewrite_camera(config, ref)
        assert (
            "source: github://Psix-anp/esphome-esp-video-camera@main"
            in config.read_text()
        )


def test_global_selection_preserves_private_local_overrides(tmp_path: Path) -> None:
    tool = _load_yaml_paths()
    maintained = tmp_path / "phone.yaml"
    private = tmp_path / "phone-local.yaml"
    assert tool.selected([maintained, private], None) == [maintained]
    assert tool.selected([maintained, private], str(private)) == [private]


def test_standalone_native_example_checks_exact_channel_and_roundtrips(tmp_path: Path) -> None:
    tool = _load_yaml_paths()
    example = tmp_path / 'native-phone.yaml'
    example.write_text('esphome:\n  name: native-phone\nexternal_components:\n'
                       '  - source: github://n-IA-hane/esphome-voip-stack@main\n'
                       '    components: [voip_stack]\n')
    assert tool.mode(example) == 'remote'
    assert list(tool.source_errors(example, 'dev', True)) == ['source channel main, expected dev']
    tool.rewrite_sources(example, 'dev')
    assert '@dev' in example.read_text()
    assert list(tool.source_errors(example, 'dev', True)) == []
    tool.rewrite_sources(example, 'local')
    assert tool.mode(example) == 'local'
    assert '@dev' not in example.read_text()
    tool.rewrite_sources(example, 'main')
    assert tool.mode(example) == 'remote'
    assert list(tool.source_errors(example, 'main', True)) == []


def test_coordinated_sources_include_the_tracked_native_example() -> None:
    tool = _load_yaml_paths()
    repository = tool.REPOS['voip_stack_components_source'][1]
    expected = repository / 'examples/native-full-duplex.yaml'
    if not expected.is_file():
        return  # An Intercom-only checkout has no sibling examples to update.
    assert expected in tool.standalone_examples()


def test_remote_check_follows_package_includes(tmp_path: Path) -> None:
    tool = _load_yaml_paths()
    tool.ROOT = tmp_path
    (tmp_path / 'yamls').mkdir()
    (tmp_path / 'packages').mkdir()
    (tmp_path / 'yamls/device.yaml').write_text(
        'esphome:\n  name: package-check\npackages:\n'
        '  voice: github://n-IA-hane/esphome-intercom/packages/voice.yaml@main\n'
    )
    (tmp_path / 'packages/voice.yaml').write_text(
        'packages:\n  controller: !include ../../undeclared-repository/controller.yaml\n'
    )
    assert tool.check('remote', None) == 1


def test_single_device_entrypoint_gets_all_local_and_remote_source_overrides(tmp_path):
    tool = _load_yaml_paths()
    config = tmp_path / 'waveshare-p4-test.yaml'
    config.write_text('substitutions:\n  firmware_features: [voip, display, video]\n'
                      'packages:\n  device: github://n-IA-hane/esphome-intercom/packages/devices/waveshare-p4-phone-jpeg.yaml@main\n')
    tool.rewrite_yaml(config, 'local')
    text = config.read_text()
    for key in ('ext_components_source', 'voip_stack_components_source', 'audio_stack_components_source',
                'runtime_controller_components_source', 'esp_video_renderer_components_source',
                'esp_screenshot_components_source'):
        assert key + ':' in text
    assert 'camera_components_source:' in text
    assert 'intercom_idf_root:' in text
    assert '!include ' in text
    tool.rewrite_yaml(config, 'dev')
    text = config.read_text()
    assert 'packages/devices/waveshare-p4-phone-jpeg.yaml@dev' in text
    assert 'github://n-IA-hane/esphome-audio-stack@dev' in text
    assert 'github://n-IA-hane/esphome-video-renderer@dev' in text
    assert 'github://n-IA-hane/esphome-screenshot@dev' in text
    assert 'github://Psix-anp/esphome-esp-video-camera@main' in text
    assert 'intercom_idf_ref: "dev"' in text
    assert 'intercom_idf_root: ""' in text


def test_camera_source_variable_is_owned_by_the_top_level_profile(tmp_path):
    tool = _load_yaml_paths()
    config = tmp_path / 'camera.yaml'
    text = 'external_components:\n  - source: ${camera_components_source}\n    components: [esp_video_camera]\n'
    config.write_text(text)
    tool.rewrite_camera(config, 'local')
    assert config.read_text() == text


def test_remote_ref_check_rejects_mixed_component_channels(tmp_path):
    tool = _load_yaml_paths()
    tool.ROOT = tmp_path
    (tmp_path / 'yamls').mkdir()
    (tmp_path / 'packages').mkdir()
    config = tmp_path / 'yamls/device.yaml'
    text = ('substitutions:\n'
            '  ext_components_source: "github://n-IA-hane/esphome-intercom@dev"\n'
            '  voip_stack_components_source: "github://n-IA-hane/esphome-voip-stack@main"\n'
            '  camera_components_source: "github://Psix-anp/esphome-esp-video-camera@main"\n'
            'packages:\n  device: github://n-IA-hane/esphome-intercom/packages/devices/phone.yaml@dev\n')
    config.write_text(text)
    assert tool.check('remote', None, 'dev') == 1
    config.write_text(text.replace('esphome-voip-stack@main', 'esphome-voip-stack@dev'))
    assert tool.check('remote', None, 'dev') == 0


def test_package_defaults_and_direct_sources_follow_channel_without_changing_includes(tmp_path):
    tool = _load_yaml_paths()
    package = tmp_path / "fragment.yaml"
    original = ('substitutions:\n'
                '  esp_video_renderer_components_source: ${ esp_video_renderer_components_source | default("github://n-IA-hane/esphome-video-renderer@main") }\n'
                'packages:\n  audio: !include ../audio/profile.yaml\n'
                'external_components:\n  - source: github://n-IA-hane/esphome-esp-video-camera@main\n'
                '  - source:\n      type: git\n      url: https://github.com/n-IA-hane/esphome-screenshot.git\n      ref: main\n'
                '  - source: github://Psix-anp/esphome-esp-video-camera@main\n')
    package.write_text(original)
    tool.rewrite_sources(package, "dev")
    text = package.read_text()
    assert 'default("github://n-IA-hane/esphome-video-renderer@dev")' in text
    assert 'audio: !include ../audio/profile.yaml' in text
    assert 'github://n-IA-hane/esphome-esp-video-camera@dev' in text
    assert '      ref: dev' in text
    assert 'github://Psix-anp/esphome-esp-video-camera@main' in text
    assert list(tool.source_errors(package, "dev", True)) == []
    package.write_text(text.replace('      ref: dev', '      ref: main'))
    assert list(tool.source_errors(package, "dev", True)) == ['git source channel main, expected dev']


def test_package_default_round_trip_preserves_expression(tmp_path):
    tool = _load_yaml_paths()
    tool.ROOT = tmp_path
    directory = tmp_path / 'packages'
    directory.mkdir()
    package = directory / 'source.yaml'
    original = 'substitutions:\n  ext_components_source: ${ ext_components_source | default("github://n-IA-hane/esphome-intercom@dev") }\n'
    package.write_text(original)
    tool.rewrite_sources(package, 'local')
    assert 'default("' + str(tool.URLS['github://n-IA-hane/esphome-intercom'] / 'esphome/components') + '")' in package.read_text()
    assert list(tool.source_errors(package, 'dev', True)) == ['absolute local source path in public YAML']
    tool.rewrite_sources(package, 'dev')
    assert package.read_text() == original


def test_global_managed_files_exclude_debug_not_ready_and_private(tmp_path):
    tool = _load_yaml_paths()
    tool.ROOT = tmp_path
    directory = tmp_path / 'yamls'
    (directory / 'debug').mkdir(parents=True)
    for relative in ('phone.yaml', 'phone-local.yaml', 'secrets.yaml', 'future_NOT_READY.yaml', 'debug/test.yaml'):
        (directory / relative).write_text('substitutions: {}\n')
    assert tool.yaml_files() == [directory / 'phone.yaml']


def test_relative_sources_do_not_require_optional_checkouts(tmp_path):
    tool = _load_yaml_paths()
    assert tool.relative(tmp_path / 'project/yamls', tmp_path / 'native/esphome/components') == '../../native/esphome/components'


def test_global_ref_gate_checks_package_defaults(tmp_path):
    tool = _load_yaml_paths()
    tool.ROOT = tmp_path
    (tmp_path / 'yamls').mkdir()
    (tmp_path / 'packages').mkdir()
    (tmp_path / 'yamls/device.yaml').write_text('packages:\n  device: github://n-IA-hane/esphome-intercom/packages/phone.yaml@dev\n')
    package = tmp_path / 'packages/phone.yaml'
    package.write_text('substitutions:\n  ext_components_source: github://n-IA-hane/esphome-intercom@main\n')
    assert tool.check('remote', None, 'dev') == 1
    tool.rewrite_sources(package, 'dev')
    assert tool.check('remote', None, 'dev') == 0
