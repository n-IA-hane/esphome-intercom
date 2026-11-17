"""Exercise the real streaming model owner against an input prefilled with 0x55."""
from pathlib import Path
import subprocess

import pytest

from tests.esphome_native import NATIVE_COMPONENTS


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/mww_reload_contract"
pytestmark = pytest.mark.timeout(60)


@pytest.mark.parametrize("provider,expected_failures", [("upstream", 8), ("local", 0)])
def test_new_interpreter_waits_for_three_new_features(
    tmp_path: Path, provider: str, expected_failures: int
) -> None:
    component = (
        NATIVE_COMPONENTS / "micro_wake_word"
        if provider == "upstream"
        else ROOT / "esphome/components/micro_wake_word"
    )
    for name in (
        "esphome/core/helpers.h",
        "esphome/core/preferences.h",
        "esphome/core/log.h",
        "tensorflow/lite/core/c/common.h",
        "tensorflow/lite/micro/micro_interpreter.h",
        "tensorflow/lite/micro/micro_mutable_op_resolver.h",
    ):
        header = tmp_path / "stubs" / name
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text('#pragma once\n#include "stubs.h"\n')
    executable = tmp_path / "reload-contract"
    command = [
        "g++", "-std=c++20", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
        "-Wno-unused-parameter", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-DUSE_ESP32",
        "-I" + str(tmp_path / "stubs"), "-I" + str(FIXTURE), "-I" + str(component),
        str(component / "streaming_model.cpp"), str(FIXTURE / "contract.cpp"),
        "-o", str(executable),
    ]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert result.returncode == int(expected_failures != 0), result.stdout + result.stderr
    assert f"SUMMARY passed={11 - expected_failures} failed={expected_failures}" in result.stdout
    assert "AddressSanitizer" not in result.stderr and "runtime error:" not in result.stderr
    if provider == "local":
        assert "new-slice Invoke counts 0,0,1; first input row heads 17,34,51" in result.stdout
    else:
        assert "first input row heads 85,17,34" in result.stdout
        assert "first input row heads 85,85,17" in result.stdout
