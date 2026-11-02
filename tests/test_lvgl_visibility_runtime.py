"""Real LVGL contracts, mandatory in the Box firmware job after SDK resolution.

This suite never downloads dependencies. CI provides LVGL_HOST_SOURCE from the
firmware's managed components. A local identified host archive can be reused by
setting LVGL_HOST_LIBRARY and its corresponding LVGL_HOST_CONFIG.
"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/lvgl_visibility"
LVGL_COMPONENT_SHA = "184e532558c1c45fefed631f3e235423d22582aafb4630f3e8885c35281a49ae"
CASES = ("matrix", "hidden", "script_only", "bootstrap", "transition", "top", "generic", "owner_paused",
         "unrelated_events")
pytestmark = [pytest.mark.diagnostic_reference, pytest.mark.integration, pytest.mark.timeout(180)]


def _run(command, *, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def visibility_binary(tmp_path_factory):
    source_value = os.environ.get("LVGL_HOST_SOURCE", "")
    assert source_value, "Provide LVGL_HOST_SOURCE from the Box build's managed_components/lvgl__lvgl"
    source = Path(source_value).resolve()
    identity = source / ".component_hash"
    assert identity.is_file(), f"Missing managed LVGL source identity: {identity}"
    assert identity.read_text().strip() == LVGL_COMPONENT_SHA, "Expected the reviewed official LVGL 9.5.0 source"
    compiler = shutil.which("g++")
    assert compiler, "The real LVGL host contract requires g++"
    work = tmp_path_factory.mktemp("lvgl-visibility")
    library_value = os.environ.get("LVGL_HOST_LIBRARY", "")
    if library_value:
        library = Path(library_value).resolve()
        config_value = os.environ.get("LVGL_HOST_CONFIG", "")
        assert library.is_file() and config_value, "Provide an existing host archive and its matching LVGL_HOST_CONFIG"
        config = Path(config_value).resolve()
        assert config.is_file(), f"Missing matching host LVGL config: {config}"
        binary = work / "visibility_oracle"
        _run([
            compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
            f'-DLV_CONF_PATH="{config}"', "-DLV_KCONFIG_IGNORE", "-DLV_LVGL_H_INCLUDE_SIMPLE",
            f"-I{source}", f"-I{ROOT / 'esphome/components/lvgl_visibility'}",
            str(FIXTURE / "runtime.cpp"), str(library), "-lm", "-o", str(binary),
        ])
        return binary
    cmake = shutil.which("cmake")
    assert cmake, "The real LVGL host contract requires CMake"
    build_root = Path(os.environ.get("LVGL_HOST_BUILD_ROOT", work)) / "visibility"
    _run([
        cmake, "-S", str(FIXTURE), "-B", str(build_root),
        f"-DLVGL_SOURCE={source}", "-DCMAKE_BUILD_TYPE=Release",
    ])
    _run([cmake, "--build", str(build_root), "--target", "visibility_oracle", "--parallel", "2"])
    binary = build_root / "visibility_oracle"
    assert binary.is_file(), "CMake did not produce the real LVGL oracle"
    return binary


@pytest.mark.parametrize("case", CASES)
def test_real_lvgl_visibility_contract(visibility_binary, case):
    result = subprocess.run([str(visibility_binary), case], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"PASS real_lvgl_9_5 {case}"
