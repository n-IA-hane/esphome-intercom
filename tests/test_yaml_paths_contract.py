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


def test_remote_camera_source_follows_selected_ref(tmp_path: Path) -> None:
    yaml_paths = _load_yaml_paths()
    config = tmp_path / "camera.yaml"
    config.write_text(
        "external_components:\n"
        "  - source: placeholder\n"
        "    components: [esp_video_camera]\n"
    )

    yaml_paths.rewrite_camera(config, "dev")

    assert (
        "source: github://n-IA-hane/esphome-esp-video-camera@dev"
        in config.read_text()
    )
