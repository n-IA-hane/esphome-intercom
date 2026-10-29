"""Runtime checks for the test-suite command interface."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import json
import ast

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


def _record_suite_arguments(tmp_path: Path, mode: str, *options: str) -> list[str]:
    """Exercise the public runner without launching pytest recursively."""
    recorder = tmp_path / "python"
    recorded = tmp_path / "arguments.json"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        "    open(os.environ['RECORDED_ARGS'], 'w').write(json.dumps(sys.argv[3:]))\n"
    )
    recorder.chmod(0o755)
    result = subprocess.run(
        [str(RUNNER), mode, *options], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHON": str(recorder), "HA_PYTHON": str(recorder),
             "RECORDED_ARGS": str(recorded)},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(recorded.read_text())


@pytest.mark.parametrize("mode", ["software-full", "ha", "browser", "peer"])
def test_direct_suites_forward_reproduction_options(tmp_path: Path, mode: str) -> None:
    arguments = _record_suite_arguments(tmp_path, mode, "--seed", "123", "--keep-going")
    assert "--hypothesis-seed" in arguments
    assert arguments[arguments.index("--hypothesis-seed") + 1] == "123"
    assert arguments.count("--maxfail=0") == 1


def test_browser_suite_selects_all_real_browser_regressions(tmp_path: Path) -> None:
    arguments = _record_suite_arguments(tmp_path, "browser")
    assert arguments[arguments.index("-m") + 1] == "browser_real"
    assert "tests/test_browser_audio_startup.py" in arguments
    assert "tests/test_frontend_media_devices_runtime.py" in arguments
    assert "tests/test_frontend_card_browser_layout.py" in arguments
    assert "tests/test_frontend_video_browser.py" in arguments
    # New Playwright tests must be assigned to this same executable CI suite.
    playwright_files = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            imported = node.module if isinstance(node, ast.ImportFrom) else ""
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "importorskip" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                imported = node.args[0].value
            if isinstance(imported, str) and imported.startswith("playwright."):
                playwright_files.add(str(path.relative_to(ROOT)))
    assert playwright_files <= set(arguments)


def test_optional_esphome_mailbox_regression_uses_the_ha_interpreter(tmp_path: Path) -> None:
    path = "tests/test_esphome_mailbox_optional_ha.py"
    ha_path = tmp_path / "ha"
    software_path = tmp_path / "software"
    ha_path.mkdir()
    software_path.mkdir()
    assert path in _record_suite_arguments(ha_path, "ha")
    arguments = _record_suite_arguments(software_path, "software-full")
    assert f"--ignore={path}" in arguments
