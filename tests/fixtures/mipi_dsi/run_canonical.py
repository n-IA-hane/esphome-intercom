"""Run the local driver with the exact official source host-test orchestration."""

import argparse
from functools import partial
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import esphome

ESPHOME_REVISION = "373224c141dceadbb67ace263040cbfc802c5968"


def source_identity() -> dict:
    """A wheel lacks the runner and canonical main; require the real checkout."""
    root = Path(esphome.__file__).resolve().parents[1]
    required = ("script/build_helpers.py", "tests/components/main.cpp", "tests/testing_helpers.py")
    if any(not (root / name).is_file() for name in required):
        raise RuntimeError(
            f"Canonical MIPI tests require ESPHome source {ESPHOME_REVISION}; "
            "set PYTHONPATH to that checkout, not only the installed wheel"
        )
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if revision != ESPHOME_REVISION:
        raise RuntimeError(f"Canonical MIPI runner must be {ESPHOME_REVISION}, got {revision}")
    if subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], text=True,
    ).strip():
        raise RuntimeError("Canonical ESPHome source must have no tracked changes")
    driver = Path(__file__).resolve().parents[3] / "esphome/components/mipi_dsi"
    return {"root": str(root), "revision": revision,
            "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in required},
            "driver_sources": {name: hashlib.sha256((driver / name).read_bytes()).hexdigest()
                               for name in ("mipi_dsi.cpp", "mipi_dsi.h")}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-source", action="store_true", help="Verify source bootstrap without compiling")
    args = parser.parse_args()
    identity = source_identity()
    root = Path(identity["root"])
    sys.path[:0] = [str(root / "script"), str(root)]
    import build_helpers
    from build_helpers import PLATFORMIO_GOOGLE_TEST_LIB, build_and_run, load_test_manifest_overrides
    from tests import testing_helpers

    if Path(build_helpers.__file__).resolve() != root / "script/build_helpers.py":
        raise RuntimeError("The canonical build helper was shadowed by another package")
    if Path(testing_helpers.__file__).resolve() != root / "tests/testing_helpers.py":
        raise RuntimeError("The canonical tests package was shadowed by another package")
    if args.check_source:
        identity["official_orchestration_imported"] = True
        print(json.dumps(identity, sort_keys=True))
        return 0

    fixtures = Path(__file__).resolve().parents[1]
    return build_and_run(
        selected_components=["mipi_dsi"], tests_dir=fixtures,
        manifest_override_loader=partial(load_test_manifest_overrides, tests_dir=fixtures),
        config_prefix="mipi-owned", friendly_name="MIPI owned driver tests",
        libraries=PLATFORMIO_GOOGLE_TEST_LIB,
        build_flags=["-Og", "-g3", "-DESPHOME_DEBUG", "-fno-omit-frame-pointer",
                     "-fsanitize=address", "-fsanitize=undefined"],
        main_entry=str(root / "tests/components/main.cpp"),
        label="MIPI full-driver canonical tests",
    )


if __name__ == "__main__":
    raise SystemExit(main())
