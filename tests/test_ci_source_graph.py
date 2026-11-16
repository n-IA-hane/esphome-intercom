"""CI source wiring must use real pinned runners and preserve suite isolation."""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_mipi_gate_uses_the_exact_core_source_and_all_boundaries():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["mipi-driver-host"]
    assert job["strategy"]["matrix"]["sdk_boundary"] == [5, 6]
    steps = job["steps"]
    checkout = next(s for s in steps if s.get("with", {}).get("repository") == "esphome/esphome")
    expected = json.loads((ROOT / "qualification/sources.json").read_text())["official_sources"]["esphome"]["revision"]
    assert checkout["with"]["ref"] == expected
    sparse = checkout["with"]["sparse-checkout"]
    assert all(path in sparse.splitlines() for path in ("/esphome/", "/script/", "/tests/components/main.cpp", "/tests/testing_helpers.py"))
    execution = next(s for s in steps if "run_canonical.py --check-source" in s.get("run", ""))
    assert execution["env"]["PYTHONPATH"].endswith("/.test-deps/esphome")
    assert execution["env"]["ESPHOME_DEFAULT_COMPILE_PROCESS_LIMIT"] == "1"
    assert execution["env"]["ASAN_OPTIONS"] == "detect_leaks=0:allocator_may_return_null=1"
    assert "set -o pipefail" in execution["run"]
    assert execution["env"]["ESPHOME_BUILD_PATH"].startswith("${{ runner.temp }}")
    assert "--check-source" in execution["run"]
    assert "len(cases) == 29" in execution["run"]
    assert "GTEST_OUTPUT" in execution["env"]
    # Upstream's tests package must not shadow this repo's Python software suite.
    software = workflow["jobs"]["software"]
    assert not any("PYTHONPATH" in s.get("env", {}) for s in software["steps"])


def test_assist_gate_runner_matches_the_declared_core_revision():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    expected = json.loads((ROOT / "qualification/sources.json").read_text())["official_sources"]["esphome"]["revision"]
    checkout = next(s for s in workflow["jobs"]["assist-media-host"]["steps"]
                    if s.get("with", {}).get("repository") == "esphome/esphome")
    assert checkout["with"]["ref"] == expected


def test_wheel_only_bootstrap_fails_before_a_compile(tmp_path):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    runner = ROOT / "tests/fixtures/mipi_dsi/run_canonical.py"
    result = subprocess.run([sys.executable, str(runner), "--check-source"], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "set PYTHONPATH to that checkout" in result.stderr
    assert not list(tmp_path.rglob("CMakeCache.txt"))


def test_importing_runner_never_starts_a_build(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: (_ for _ in ()).throw(AssertionError("source check only belongs in main")))
    path = ROOT / "tests/fixtures/mipi_dsi/run_canonical.py"
    spec = importlib.util.spec_from_file_location("mipi_runner_import_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.source_identity) and callable(module.main)
