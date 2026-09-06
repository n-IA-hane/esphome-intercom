"""Runtime checks for the test-suite command interface."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "test_suite.sh"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(RUNNER), *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_help_exposes_fail_fast_override_and_reproducible_seed() -> None:
    result = _run("--help")

    assert result.returncode == 0
    assert "--keep-going" in result.stdout
    assert "--seed N" in result.stdout
    assert "coverage" in result.stdout
    assert "peer" in result.stdout
    assert "mutation" in result.stdout


def test_invalid_seed_fails_before_test_collection() -> None:
    result = _run("fast", "--seed", "not-a-number")

    assert result.returncode == 2
    assert "Seed must be numeric" in result.stderr


def test_mutation_is_refused_in_the_primary_checkout(tmp_path: Path) -> None:
    # Exercise a real primary checkout even when this suite runs in a linked
    # worktree. Never launch mutation testing in the checkout running the test.
    primary = tmp_path / "primary"
    scripts = primary / "scripts"
    scripts.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(primary)], check=True)
    runner = scripts / "test_suite.sh"
    shutil.copy2(RUNNER, runner)
    paths = scripts / "yaml_paths.sh"
    paths.write_text("#!/usr/bin/env bash\nprintf '%s\\n' 'fixture remote'\n")
    paths.chmod(0o755)
    result = subprocess.run(
        [str(runner), "mutation"], cwd=primary, check=False,
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHON": sys.executable},
    )

    assert result.returncode == 2
    assert "disposable git worktree" in result.stderr
