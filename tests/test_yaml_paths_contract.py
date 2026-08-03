import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


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
