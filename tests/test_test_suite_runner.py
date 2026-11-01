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
    path = "tests/test_esphome_provisioning_optional_ha.py"
    ha_path = tmp_path / "ha"
    software_path = tmp_path / "software"
    ha_path.mkdir()
    software_path.mkdir()
    assert path in _record_suite_arguments(ha_path, "ha")
    arguments = _record_suite_arguments(software_path, "software-full")
    assert f"--ignore={path}" in arguments


def test_native_trunk_validation_stays_in_the_catalogued_ha_module(tmp_path: Path) -> None:
    ha_path, software_path = tmp_path / "ha", tmp_path / "software"
    ha_path.mkdir()
    software_path.mkdir()
    ha_arguments = _record_suite_arguments(ha_path, "ha")
    software_arguments = _record_suite_arguments(software_path, "software-full")
    assert "tests/test_trunk_config_flow.py" in ha_arguments
    assert "--ignore=tests/test_trunk_config_flow.py" in software_arguments
    assert "tests/test_qualification_tooling_boundaries.py" not in ha_arguments
    assert "--ignore=tests/test_qualification_tooling_boundaries.py" not in software_arguments


def test_catalog_guard_detects_uncatalogued_function_level_ha_markers(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "source"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "test_known.py").write_text("import pytest\npytestmark = pytest.mark.ha\n")
    (tests / "test_uncatalogued.py").write_text(
        "import pytest\n@pytest.mark.ha\nasync def test_real_flow(hass):\n    pass\n")
    monkeypatch.setitem(globals(), "ROOT", project)
    monkeypatch.setitem(globals(), "_record_suite_arguments", lambda _path, mode: (
        ["tests/test_known.py"] if mode == "ha" else ["tests", "--ignore=tests/test_known.py"]))
    gate = tmp_path / "gate"
    gate.mkdir()
    with pytest.raises(AssertionError, match="test_uncatalogued.py"):
        test_all_ha_marked_modules_use_the_ha_interpreter(gate)


def test_all_ha_marked_modules_use_the_ha_interpreter(tmp_path: Path) -> None:
    ha_marked = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        module = ast.parse(path.read_text())
        assignments = list(module.body)
        marker_expressions = []
        for declaration in ast.walk(module):
            if isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                marker_expressions.extend(declaration.decorator_list)
            if isinstance(declaration, ast.ClassDef):
                assignments.extend(declaration.body)
        for assignment in assignments:
            if not isinstance(assignment, ast.Assign) or not any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in assignment.targets
            ):
                continue
            marker_expressions.append(assignment.value)
        if any(isinstance(node, ast.Attribute) and ast.unparse(node) == "pytest.mark.ha"
               for expression in marker_expressions for node in ast.walk(expression)):
            ha_marked.add(str(path.relative_to(ROOT)))

    assert ha_marked
    ha_path = tmp_path / "ha"
    software_path = tmp_path / "software"
    ha_path.mkdir()
    software_path.mkdir()
    ha_arguments = _record_suite_arguments(ha_path, "ha")
    software_arguments = _record_suite_arguments(software_path, "software-full")
    assert not (missing := ha_marked - set(ha_arguments)), f"HA suite omits: {sorted(missing)}"
    software_ignores = {
        argument.removeprefix("--ignore=")
        for argument in software_arguments if argument.startswith("--ignore=")
    }
    assert not (missing := ha_marked - software_ignores), (
        f"Regular interpreter collects HA modules: {sorted(missing)}"
    )
