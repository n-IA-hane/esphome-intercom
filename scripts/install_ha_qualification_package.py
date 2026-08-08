#!/usr/bin/env python3
"""Install the canonical real-automation package into an isolated HA lab."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "qualification/home_assistant/voip_qualification.yaml"
PACKAGE_INCLUDE = "packages: !include_dir_named packages"


def install(config_dir: Path, *, check: bool) -> tuple[Path, str]:
    config_dir = config_dir.resolve()
    configuration = config_dir / "configuration.yaml"
    if not configuration.is_file():
        raise RuntimeError(f"Home Assistant configuration is unavailable: {configuration}")
    if PACKAGE_INCLUDE not in configuration.read_text(encoding="utf-8"):
        raise RuntimeError(
            "isolated HA lab must enable homeassistant packages with "
            f"{PACKAGE_INCLUDE!r}"
        )
    target = config_dir / "packages/voip_qualification.yaml"
    source_bytes = SOURCE.read_bytes()
    if check:
        if not target.is_file() or target.read_bytes() != source_bytes:
            raise RuntimeError("installed HA qualification package differs from source")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE, target)
    return target, hashlib.sha256(source_bytes).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/config"),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target, digest = install(args.config_dir, check=args.check)
    print(f"package={target}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

