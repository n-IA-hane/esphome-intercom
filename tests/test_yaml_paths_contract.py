from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_checked_yaml_paths_are_consistent() -> None:
    subprocess.run(
        [str(ROOT / "scripts/yaml_paths.sh"), "check", "--expect", "remote"],
        cwd=ROOT,
        check=True,
    )
