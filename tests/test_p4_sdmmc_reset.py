"""Configure the real CMake hook against the exact upstream IDF source."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/p4_sdmmc_reset"
HOOK = COMPONENT / "p4_sdmmc_reset.cmake"
FIXTURE = ROOT / "tests/fixtures/esp_idf_5_5_6/p4_system_internal.c"
ORIGINAL_SHA = "7bd2af3da2a9c1592c8d9ff6f65ad7fe320690b2e480076685df068f231e40cc"
PATCHED_SHA = "38aca3174d6ac05afa37c87e9c9d33533c3772fb136b17c44d6ee20bcdad4946"


def configure(tmp_path, *, target="esp32p4", version="5.5.6", corrupt=False,
              source_count=1, repeat_include=False):
    sdk = tmp_path / "sdk"
    original = sdk / "components/esp_system/port/soc/esp32p4/system_internal.c"
    original.parent.mkdir(parents=True)
    original.write_bytes(FIXTURE.read_bytes() + (b"\n/* changed SDK */\n" if corrupt else b""))
    version_file = sdk / "tools/cmake/version.cmake"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("\n".join(
        f"set(IDF_VERSION_{part} {value})"
        for part, value in zip(("MAJOR", "MINOR", "PATCH"), version.split("."), strict=True)
    ))
    source = tmp_path / "project"
    source.mkdir()
    child = source / "system"
    child.mkdir()
    for name in ("target_include", "source_include"):
        (source / name).mkdir()
    (source / "dependency").write_text("source dependency\n")
    (child / "other.c").write_text("int other(void) {return 0;}\n")
    sources = " ".join(f'"{original}"' for _ in range(source_count)) or '"other.c"'
    (child / "CMakeLists.txt").write_text(f'''
add_library(esp_system STATIC {sources})
target_include_directories(esp_system PRIVATE "{source}/target_include")
target_compile_options(esp_system PRIVATE -fno-strict-aliasing)
target_compile_definitions(esp_system PRIVATE TARGET_SENTINEL=1)
set_source_files_properties("{original}" PROPERTIES
    COMPILE_DEFINITIONS SOURCE_SENTINEL=1
    COMPILE_OPTIONS -fno-inline
    COMPILE_FLAGS -fno-omit-frame-pointer
    INCLUDE_DIRECTORIES "{source}/source_include"
    OBJECT_DEPENDS "{source}/dependency"
    SKIP_PRECOMPILE_HEADERS TRUE
    LANGUAGE C)
''')
    # Inclusion deliberately precedes target creation. A direct target edit
    # instead of DEFER cannot pass this real CMake configure.
    (source / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.19)
project(p4_backport_contract C)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
function(idf_build_get_property result property)
    if(property STREQUAL "IDF_TARGET")
        set(${{result}} "{target}" PARENT_SCOPE)
    elseif(property STREQUAL "IDF_PATH")
        set(${{result}} "{sdk}" PARENT_SCOPE)
    else()
        message(FATAL_ERROR "Unexpected build property: ${{property}}")
    endif()
endfunction()
function(idf_component_get_property result component property)
    if(NOT component STREQUAL "esp_system" OR NOT property STREQUAL "COMPONENT_LIB")
        message(FATAL_ERROR "Unexpected component property")
    endif()
    set(${{result}} esp_system PARENT_SCOPE)
endfunction()
if(EXISTS "{HOOK}")
    include("{HOOK}")
    {f'include("{HOOK}")' if repeat_include else ''}
endif()
add_subdirectory(system)
function(probe)
    get_target_property(sources esp_system SOURCES)
    file(WRITE "${{CMAKE_BINARY_DIR}}/sources.txt" "${{sources}}")
    list(GET sources 0 selected)
    foreach(property COMPILE_DEFINITIONS COMPILE_OPTIONS COMPILE_FLAGS
                     INCLUDE_DIRECTORIES OBJECT_DEPENDS SKIP_PRECOMPILE_HEADERS LANGUAGE)
        get_source_file_property(value "${{selected}}" TARGET_DIRECTORY esp_system ${{property}})
        file(WRITE "${{CMAKE_BINARY_DIR}}/source-${{property}}.txt" "${{value}}")
    endforeach()
    foreach(property INCLUDE_DIRECTORIES COMPILE_OPTIONS COMPILE_DEFINITIONS)
        get_target_property(value esp_system ${{property}})
        file(WRITE "${{CMAKE_BINARY_DIR}}/target-${{property}}.txt" "${{value}}")
    endforeach()
endfunction()
cmake_language(DEFER CALL probe)
''')
    build = tmp_path / "build"
    command = ["cmake", "-S", str(source), "-B", str(build)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40, check=False)
    return result, original, build, command


def test_official_source_is_copied_with_only_the_early_sdmmc_reset(tmp_path):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == ORIGINAL_SHA
    result, original, build, _ = configure(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    selected = Path((build / "sources.txt").read_text())
    # Check the independently required reset ordering before the exact hash.
    contents = selected.read_text()
    assert contents.index("LP_CLKRST_RST_EN_SDMMC") < contents.index("HP_SYS_CLKRST_REG_RST_EN_MSPI_AXI")
    assert selected == build / "p4_sdmmc_reset/system_internal.c"
    assert hashlib.sha256(selected.read_bytes()).hexdigest() == PATCHED_SHA
    assert hashlib.sha256(original.read_bytes()).hexdigest() == ORIGINAL_SHA
    assert len(selected.read_bytes()) == len(original.read_bytes())
    commands = json.loads((build / "compile_commands.json").read_text())
    assert [entry["file"] for entry in commands] == [str(selected)]
    for value in ("TARGET_SENTINEL=1", "SOURCE_SENTINEL=1", "-fno-inline",
                  "-fno-strict-aliasing", "-fno-omit-frame-pointer", "source_include", "target_include"):
        assert value in commands[0]["command"]


def test_source_and_target_properties_survive_replacement(tmp_path):
    result, _, build, _ = configure(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = {"COMPILE_DEFINITIONS": "SOURCE_SENTINEL=1", "COMPILE_OPTIONS": "-fno-inline",
                "COMPILE_FLAGS": "-fno-omit-frame-pointer", "INCLUDE_DIRECTORIES": "source_include",
                "OBJECT_DEPENDS": "dependency", "SKIP_PRECOMPILE_HEADERS": "TRUE", "LANGUAGE": "C"}
    for property_name, value in expected.items():
        assert (build / f"source-{property_name}.txt").read_text().endswith(value)
    assert (build / "target-COMPILE_DEFINITIONS.txt").read_text() == "TARGET_SENTINEL=1"
    assert (build / "target-COMPILE_OPTIONS.txt").read_text() == "-fno-strict-aliasing"
    assert (build / "target-INCLUDE_DIRECTORIES.txt").read_text().endswith("target_include")


def test_reconfigure_and_repeated_include_are_idempotent(tmp_path):
    first, original, build, command = configure(tmp_path, repeat_include=True)
    assert first.returncode == 0, first.stdout + first.stderr
    selected = build / "p4_sdmmc_reset/system_internal.c"
    stamp = selected.stat().st_mtime_ns
    second = subprocess.run(command, capture_output=True, text=True, timeout=40, check=False)
    assert second.returncode == 0, second.stdout + second.stderr
    assert selected.stat().st_mtime_ns == stamp
    assert hashlib.sha256(original.read_bytes()).hexdigest() == ORIGINAL_SHA
    assert (build / "sources.txt").read_text() == str(selected)


@pytest.mark.parametrize("target,version,reason", [
    ("esp32s3", "5.5.6", "target esp32s3"),
    ("esp32p4", "5.5.5", "SDK 5.5.5"),
    ("esp32p4", "5.5.7", "SDK 5.5.7"),
])
def test_other_targets_and_versions_are_explicitly_unmodified(tmp_path, target, version, reason):
    result, original, build, _ = configure(tmp_path, target=target, version=version)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not applied" in result.stdout + result.stderr
    assert reason in result.stdout + result.stderr
    assert (build / "sources.txt").read_text() == str(original)
    assert not (build / "p4_sdmmc_reset").exists()


def test_changed_sdk_556_fails_before_writing_the_replacement(tmp_path):
    result, original, build, _ = configure(tmp_path, corrupt=True)
    assert result.returncode != 0
    assert "unrecognized SDK 5.5.6 source SHA256" in result.stderr
    assert original.read_bytes() == FIXTURE.read_bytes() + b"\n/* changed SDK */\n"
    assert not (build / "p4_sdmmc_reset").exists()


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_original_source_fails_before_copy(tmp_path, count):
    result, _, build, _ = configure(tmp_path, source_count=count)
    assert result.returncode != 0
    assert f"expected exactly one original source, found {count}" in result.stderr
    assert not (build / "p4_sdmmc_reset").exists()


def component(monkeypatch, tmp_path, target="ESP32P4", version="5.5.6"):
    from esphome import config_validation as cv
    from esphome.components import esp32
    from esphome.const import KEY_CORE, KEY_TARGET_FRAMEWORK
    from esphome.core import CORE

    spec = importlib.util.spec_from_file_location("p4_sdmmc_codegen_contract", COMPONENT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(CORE, "data", {KEY_CORE: {KEY_TARGET_FRAMEWORK: "esp-idf"},
        esp32.KEY_ESP32: {esp32.KEY_EXTRA_BUILD_FILES: {}}})
    monkeypatch.setattr(CORE, "build_path", str(tmp_path))
    monkeypatch.setattr(CORE, "cmake_args", {})
    monkeypatch.setattr(esp32, "get_esp32_variant", lambda: target)
    monkeypatch.setattr(esp32, "idf_version", lambda: cv.Version(*map(int, version.split("."))))
    return module, CORE, esp32


def test_codegen_uses_existing_native_idf_copy_and_cmake_interfaces(monkeypatch, tmp_path):
    module, core, esp32 = component(monkeypatch, tmp_path)
    core.cmake_args["CMAKE_PROJECT_INCLUDE"] = "/existing/project-hook.cmake"
    asyncio.run(module.to_code({}))
    asyncio.run(module.to_code({}))
    expected = str(tmp_path / "p4_sdmmc_reset.cmake")
    assert core.cmake_args == {"CMAKE_PROJECT_INCLUDE": "/existing/project-hook.cmake;" + expected}
    files = core.data[esp32.KEY_ESP32][esp32.KEY_EXTRA_BUILD_FILES]
    assert list(files) == ["p4_sdmmc_reset.cmake"]
    assert files["p4_sdmmc_reset.cmake"]["path"] == HOOK
    assert not (tmp_path / "p4_sdmmc_reset.cmake").exists(), "Codegen registers the normal writer, not its own file owner"


@pytest.mark.parametrize("target,version", [("ESP32S3", "5.5.6"), ("ESP32P4", "5.5.7")])
def test_codegen_does_not_claim_other_sdk_or_targets(monkeypatch, tmp_path, caplog, target, version):
    module, core, esp32 = component(monkeypatch, tmp_path, target, version)
    asyncio.run(module.to_code({}))
    assert not core.cmake_args
    assert not core.data[esp32.KEY_ESP32][esp32.KEY_EXTRA_BUILD_FILES]
    assert "not applied" in caplog.text
