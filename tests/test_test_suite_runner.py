"""Runtime checks for the test-suite command interface."""

from __future__ import annotations

from pathlib import Path
import subprocess

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
    assert "mutation" in result.stdout


def test_invalid_seed_fails_before_test_collection() -> None:
    result = _run("fast", "--seed", "not-a-number")

    assert result.returncode == 2
    assert "Seed must be numeric" in result.stderr


def test_mutation_is_refused_in_the_primary_checkout() -> None:
    result = _run("mutation")

    assert result.returncode == 2
    assert "disposable git worktree" in result.stderr
