from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_checked_yaml_paths_are_consistent() -> None:
    subprocess.run(
        [str(ROOT / "scripts/yaml_paths.sh"), "check", "--expect", "remote"],
        cwd=ROOT,
        check=True,
    )


def test_check_file_limits_the_expected_mode_gate() -> None:
    target = "yamls/voip-only/single-bus/generic-s3-voip.yaml"
    result = subprocess.run(
        [
            str(ROOT / "scripts/yaml_paths.sh"),
            "check",
            "--expect",
            "local",
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
    assert failures == [f"FAIL: {target} (remote, expected local)"]
