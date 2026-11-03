#!/usr/bin/env python3
"""Run the real media sources through ESPHome's pinned C++ host-test runner."""
from __future__ import annotations

import argparse
from functools import partial
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ESPHOME_REVISION = "704551a6c93c29a311802f7ee97d67a9e9bce718"
DECODER_SOURCES = (
    "audio_decoder", "audio_reader", "decoder_source", "md_transfer_buffer", "ring_buffer", "types",
)
FLAC_SOURCES = (
    "crc", "decorrelation", "flac_decoder", "frame_header", "lpc", "lpc_analysis", "pcm_packing",
    "rice_partitions", "subframe_coding",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--esphome-source", required=True, type=Path)
    parser.add_argument("--micro-decoder", required=True, type=Path)
    parser.add_argument("--micro-flac", required=True, type=Path)
    parser.add_argument("--build-parent", required=True, type=Path)
    parser.add_argument("--junit", required=True, type=Path)
    args = parser.parse_args()
    upstream = args.esphome_source.resolve()
    decoder, flac = args.micro_decoder.resolve(), args.micro_flac.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True,
    ).strip()
    if revision != ESPHOME_REVISION:
        parser.error(f"ESPHome host runner must be {ESPHOME_REVISION}, got {revision}")
    for path in (upstream / "script/build_helpers.py", decoder / "src/decoder_source.cpp",
                 flac / "src/flac_decoder.cpp"):
        if not path.is_file():
            parser.error(f"Required host dependency is missing: {path}")

    build = args.build_parent.resolve()
    wrappers = build / "dependency-wrappers"
    wrappers.mkdir(parents=True, exist_ok=True)
    # Generated translation units reuse the pinned sources without copying libraries into this repository.
    for name, path, sources in (("decoder", decoder, DECODER_SOURCES), ("flac", flac, FLAC_SOURCES)):
        for source in sources:
            included = path / "src" / f"{source}.cpp"
            (wrappers / f"{name}-{source}.cpp").write_text(f"#include {json.dumps(str(included))}\n")

    # Import the official runner before this repository's tests package. The dependency is never patched.
    sys.path[:0] = [str(upstream / "script"), str(upstream)]
    import build_helpers
    import cpp_unit_test
    from esphome import loader

    loader.install_meta_finder(ROOT / "esphome/components", ["speaker_source", "audio_http"])
    for name in ("speaker_source", "audio_http"):
        module = importlib.import_module(f"esphome.components.{name}")
        if Path(module.__file__).resolve().parent != ROOT / "esphome/components" / name:
            raise RuntimeError(f"The host build selected the wrong provider for {name}")

    original_config = build_helpers.create_host_config

    def host_config(*positional, **keywords):
        config = original_config(*positional, **keywords)
        config["esphome"]["build_path"] = str(build / "host")
        return config

    build_helpers.create_host_config = host_config
    fixtures = ROOT / "tests/fixtures/assist_media"
    args.junit = args.junit.resolve()
    args.junit.parent.mkdir(parents=True, exist_ok=True)
    os.environ["ASSIST_MEDIA_FIXTURES"] = str(fixtures / "data")
    os.environ["GTEST_OUTPUT"] = f"xml:{args.junit}"
    os.environ["ASAN_OPTIONS"] = "detect_leaks=0:allocator_may_return_null=1"
    flags = cpp_unit_test.BUILD_FLAGS + [
        "-DASSIST_TEST_MICRO_DECODER", "-DMICRO_DECODER_CODEC_FLAC=1", "-DMICRO_FLAC_DISABLE_OGG",
        "-Wl,--wrap=pthread_create", "-Wl,--wrap=pthread_join",
        f"-I{decoder / 'include'}", f"-I{decoder / 'src'}", f"-I{flac / 'include'}",
    ]
    result = build_helpers.build_and_run(
        selected_components=["speaker_source"], tests_dir=fixtures,
        manifest_override_loader=partial(build_helpers.load_test_manifest_overrides, tests_dir=fixtures),
        config_prefix="assist-host", friendly_name="Assist Media Host",
        libraries=build_helpers.PLATFORMIO_GOOGLE_TEST_LIB, build_flags=flags,
        main_entry="main.cpp", label="Assist media lifecycle tests", extra_include_dirs=[wrappers],
    )
    if result:
        return result
    cases = ET.parse(args.junit).getroot().findall(".//testcase")
    if len(cases) != 12 or any(case.find("skipped") is not None for case in cases):
        raise RuntimeError(f"Expected all 12 media lifecycle cases, got {len(cases)} with possible skips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
