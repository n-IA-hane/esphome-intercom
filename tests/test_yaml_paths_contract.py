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
    assert failures == [f"FAIL: {target} ({expected}, expected {opposite})"]


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
