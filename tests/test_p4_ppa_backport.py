"""Configure the real backport hook against byte-exact official SDK sources."""

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/p4_ppa_backport"
HOOK = COMPONENT / "p4_ppa_backport.cmake"
FIXTURE = ROOT / "tests/fixtures/p4_default_sdk/esp-idf"
MANIFEST = json.loads((COMPONENT / "p4_ppa_backport.json").read_text())


def sdk_base(version):
    return MANIFEST["additional_bases"].get(version, MANIFEST)


def configure(tmp_path, *, target="esp32p4", version="5.5.6", already_fixed=(),
              corrupt=False, source_count=1, bootloader=False):
    # Deliberately nest both SDK and build beneath a Git checkout. A git apply
    # accidentally using the parent's prefix must not silently skip this patch.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sdk = tmp_path / "sdk"
    fixture = FIXTURE.with_name("esp-idf-6.0.3") if version == "6.0.3" else FIXTURE
    shutil.copytree(fixture, sdk)
    for fix in already_fixed:
        patch = "9da8ff53.patch" if fix == "engine" else sdk_base(version)["rotation_patch_file"]
        subprocess.run(["git", "apply", str(COMPONENT / patch)], cwd=sdk,
                       env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)}, check=True)
    original = sdk / "components/esp_driver_ppa/src/ppa_core.c"
    if corrupt:
        original.write_bytes(original.read_bytes() + b"\n/* unrecognized SDK */\n")
    version_file = sdk / "tools/cmake/version.cmake"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("\n".join(
        f"set(IDF_VERSION_{part} {value})"
        for part, value in zip(("MAJOR", "MINOR", "PATCH"), version.split("."), strict=True)
    ))
    project = tmp_path / "project"
    child = project / "driver"
    child.mkdir(parents=True)
    sources = [str(sdk / item["path"]) for item in sdk_base(version)["files"] if item["path"].endswith(".c")]
    target_sources = [path for path in sources for _ in range(source_count)]
    (child / "unchanged.c").write_text("int unchanged(void) { return 0; }\n")
    for directory in ("target_include", "source_include"):
        (project / directory).mkdir()
    (project / "dependency").write_text("preserved source dependency\n")
    source_text = " ".join(f'"{path}"' for path in target_sources)
    (child / "CMakeLists.txt").write_text(f'''
add_library(esp_driver_ppa STATIC {source_text} unchanged.c)
target_include_directories(esp_driver_ppa PRIVATE "{project}/target_include")
target_compile_definitions(esp_driver_ppa PRIVATE TARGET_SENTINEL=1)
target_compile_options(esp_driver_ppa PRIVATE -fno-strict-aliasing)
set_source_files_properties({" ".join(f'"{path}"' for path in sources)} PROPERTIES
    COMPILE_DEFINITIONS SOURCE_SENTINEL=1 COMPILE_OPTIONS -fno-inline
    COMPILE_FLAGS -fno-omit-frame-pointer INCLUDE_DIRECTORIES "{project}/source_include"
    OBJECT_DEPENDS "{project}/dependency" SKIP_PRECOMPILE_HEADERS TRUE LANGUAGE C)
''')
    (project / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.19)
project(p4_ppa_contract C)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(BOOTLOADER_BUILD {"TRUE" if bootloader else "FALSE"})
function(idf_build_get_property result property)
    if(property STREQUAL "IDF_TARGET")
        set(${{result}} "{target}" PARENT_SCOPE)
    elseif(property STREQUAL "IDF_PATH")
        set(${{result}} "{sdk}" PARENT_SCOPE)
    else()
        message(FATAL_ERROR "Unexpected build property")
    endif()
endfunction()
function(idf_component_get_property result component property)
    if(NOT component STREQUAL "esp_driver_ppa" OR NOT property STREQUAL "COMPONENT_LIB")
        message(FATAL_ERROR "Unexpected component property")
    endif()
    set(${{result}} esp_driver_ppa PARENT_SCOPE)
endfunction()
include("{HOOK}")
include("{HOOK}")
add_subdirectory(driver)
function(probe)
    get_target_property(sources esp_driver_ppa SOURCES)
    file(WRITE "${{CMAKE_BINARY_DIR}}/sources.txt" "${{sources}}")
    foreach(property INCLUDE_DIRECTORIES COMPILE_OPTIONS COMPILE_DEFINITIONS)
        get_target_property(value esp_driver_ppa ${{property}})
        file(WRITE "${{CMAKE_BINARY_DIR}}/target-${{property}}.txt" "${{value}}")
    endforeach()
endfunction()
cmake_language(DEFER CALL probe)
''')
    build = tmp_path / "build"
    command = ["cmake", "-S", str(project), "-B", str(build)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    return result, sdk, build, command


@pytest.mark.parametrize("version", ["5.5.6", "6.0.3"])
def test_official_fix_provenance_and_patch_are_exact(version):
    assert MANIFEST["base_commit"] == "880a203ae603d08a94d06aa01e0d6761958e5e9e"
    assert MANIFEST["official_fix_commit"] == "9da8ff53f751f6682f69897d886f3a9946deed31"
    assert hashlib.sha256((COMPONENT / "9da8ff53.patch").read_bytes()).hexdigest() == MANIFEST["patch_sha256"]
    base = sdk_base(version)
    fixture = FIXTURE.with_name("esp-idf-6.0.3") if version == "6.0.3" else FIXTURE
    if version == "6.0.3":
        assert base["base_commit"] == "76f5dedd9950a3012fee8fb7d5586df21fc67802"
    expected = {"5.5.6": "e106b16b9adf5af60785a3ee8032cf6e877edfe4",
                "6.0.3": "8e4e73b0ddd98f2423490c45423d54fe2370d576"}
    assert base["rotation_fix_commit"] == expected[version]
    assert hashlib.sha256((COMPONENT / base["rotation_patch_file"]).read_bytes()).hexdigest() == base["rotation_patch_sha256"]
    assert len(base["files"]) == 5
    for item in base["files"]:
        data = (fixture / item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data,
                            usedforsecurity=False).hexdigest() == item["git_blob"]
        assert b"SPDX-License-Identifier: Apache-2.0" in data
    assert "Apache License" in (FIXTURE / "LICENSE").read_text()


@pytest.mark.parametrize("version", ["5.5.6", "6.0.3"])
def test_real_cmake_preserves_target_properties_and_only_replaces_official_sources(tmp_path, version):
    result, sdk, build, _ = configure(tmp_path, version=version)
    assert result.returncode == 0, result.stdout + result.stderr
    selected = (build / "sources.txt").read_text().split(";")
    expected = [str(build / "p4_ppa_backport" / item["path"])
                for item in sdk_base(version)["files"] if item["path"].endswith(".c")]
    assert selected == [*expected, "unchanged.c"]
    for item in sdk_base(version)["files"]:
        assert hashlib.sha256((sdk / item["path"]).read_bytes()).hexdigest() == item["sha256"]
        assert hashlib.sha256((build / "p4_ppa_backport" / item["path"]).read_bytes()).hexdigest() == item["patched_sha256"]
    commands = json.loads((build / "compile_commands.json").read_text())
    assert [entry["file"] for entry in commands[:4]] == expected
    for entry in commands[:4]:
        for flag in ["TARGET_SENTINEL=1", "SOURCE_SENTINEL=1", "-fno-inline",
                     "-fno-strict-aliasing", "-fno-omit-frame-pointer", "source_include", "target_include"]:
            assert flag in entry["command"]
    assert (build / "target-COMPILE_DEFINITIONS.txt").read_text() == "TARGET_SENTINEL=1"
    assert (build / "target-COMPILE_OPTIONS.txt").read_text() == "-fno-strict-aliasing"


@pytest.mark.parametrize("version", ["5.5.6", "6.0.3"])
@pytest.mark.parametrize("present", [("engine",), ("rotation",)])
def test_partial_official_backport_applies_only_missing_fix(tmp_path, version, present):
    result, _, build, _ = configure(tmp_path, version=version, already_fixed=present)
    assert result.returncode == 0, result.stdout + result.stderr
    for item in sdk_base(version)["files"]:
        output = build / "p4_ppa_backport" / item["path"]
        assert hashlib.sha256(output.read_bytes()).hexdigest() == item["patched_sha256"]


@pytest.mark.parametrize("version", ["5.5.6", "6.0.3"])
def test_reconfigure_reuses_verified_copies_without_rewriting(tmp_path, version):
    first, _, build, command = configure(tmp_path, version=version)
    assert first.returncode == 0, first.stdout + first.stderr
    paths = [build / "p4_ppa_backport" / item["path"] for item in MANIFEST["files"]]
    before = [path.stat().st_mtime_ns for path in paths]
    second = subprocess.run(command, capture_output=True, text=True, timeout=40)
    assert second.returncode == 0, second.stdout + second.stderr
    assert [path.stat().st_mtime_ns for path in paths] == before


@pytest.mark.parametrize("options", [
    {"target": "esp32s3"}, {"version": "6.0.4"}, {"version": "5.5.7"},
    {"bootloader": True}, {"already_fixed": ("engine", "rotation")},
    {"version": "6.0.3", "already_fixed": ("engine", "rotation")},
])
def test_other_targets_versions_and_existing_fix_make_no_copies(tmp_path, options):
    result, sdk, build, _ = configure(tmp_path, **options)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (build / "p4_ppa_backport").exists()
    assert str(sdk / "components/esp_driver_ppa/src/ppa_core.c") in (build / "sources.txt").read_text()
    if options.get("already_fixed"):
        assert "both official fixes already present" in result.stdout


@pytest.mark.parametrize("version", ["5.5.6", "6.0.3"])
@pytest.mark.parametrize("options,message", [
    ({"corrupt": True}, "unrecognized SDK source"),
    ({"source_count": 0}, "expected one ppa_blend.c source, found 0"),
    ({"source_count": 2}, "expected one ppa_blend.c source, found 2"),
])
def test_unknown_source_or_target_shape_fails_closed_before_copy(tmp_path, options, message, version):
    result, _, build, _ = configure(tmp_path, version=version, **options)
    assert result.returncode != 0
    assert message in result.stderr
    assert not (build / "p4_ppa_backport").exists()


@pytest.mark.parametrize("target,version", [("ESP32P4", "5.5.6"), ("ESP32P4", "6.0.3"),
                                           ("ESP32P4", "6.0.4"), ("ESP32S3", "5.5.6")])
def test_codegen_uses_normal_extra_files_without_replacing_other_hooks(monkeypatch, tmp_path, target, version):
    fixture_spec = importlib.util.spec_from_file_location(
        "_sdmmc_codegen_fixture", ROOT / "tests/test_p4_sdmmc_backport.py"
    )
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    _, core, esp32 = fixture.component(monkeypatch, tmp_path, target, version)
    spec = importlib.util.spec_from_file_location("p4_ppa_contract", COMPONENT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    core.cmake_args["CMAKE_PROJECT_INCLUDE"] = "/existing/p4_sdmmc_reset.cmake"
    asyncio.run(module.to_code({}))
    asyncio.run(module.to_code({}))
    files = core.data[esp32.KEY_ESP32][esp32.KEY_EXTRA_BUILD_FILES]
    if target == "ESP32P4" and version in ("5.5.6", "6.0.3"):
        assert set(files) == {"p4_ppa_backport.cmake", "9da8ff53.patch", sdk_base(version)["rotation_patch_file"], "p4_ppa_backport.json"}
        assert core.cmake_args["CMAKE_PROJECT_INCLUDE"] == (
            "/existing/p4_sdmmc_reset.cmake;" + str(tmp_path / "p4_ppa_backport.cmake")
        )
    else:
        assert not files
        assert core.cmake_args["CMAKE_PROJECT_INCLUDE"] == "/existing/p4_sdmmc_reset.cmake"
