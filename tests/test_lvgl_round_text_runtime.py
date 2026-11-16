"""Real reviewed LVGL text layout, borrowed-pointer lifetime and bounded caches."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.lvgl_host_source import lvgl_source_tag

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/lvgl_round_text"
CASES = ("differential", "pointer_cache", "allocation_failure", "work", "wrapper", "setup_failure",
         "oversized_empty_clear")
pytestmark = [pytest.mark.diagnostic_reference, pytest.mark.integration, pytest.mark.timeout(180)]


def _run(command, *, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(scope="module", params=[False, True], ids=["plain", "arabic"])
def round_text_binary(request, tmp_path_factory):
    value = os.environ.get("LVGL_HOST_SOURCE", "")
    assert value, "Provide LVGL_HOST_SOURCE from the identified managed LVGL component"
    source = Path(value).resolve()
    lvgl_source_tag(source)
    cmake = shutil.which("cmake")
    assert cmake, "The real LVGL round text contract requires CMake"
    work = tmp_path_factory.mktemp("lvgl-round-text")
    build = Path(os.environ.get("LVGL_HOST_BUILD_ROOT", work)) / ("round-text-arabic" if request.param else "round-text")
    _run([cmake, "-S", str(FIXTURE), "-B", str(build), f"-DLVGL_SOURCE={source}",
          "-DCMAKE_BUILD_TYPE=Release", f"-DROUND_TEXT_ARABIC={'ON' if request.param else 'OFF'}"])
    _run([cmake, "--build", str(build), "--target", "round_text_oracle", "--parallel", "2"])
    return build / "round_text_oracle"


@pytest.mark.parametrize("case", CASES)
def test_real_lvgl_round_text_contract(round_text_binary, case):
    output = _run([str(round_text_binary), case], timeout=10)
    assert f"PASS {lvgl_source_tag(Path(os.environ['LVGL_HOST_SOURCE']))} {case}" in output
    if case == "work":
        assert "METRICS allocations=0 " in output
