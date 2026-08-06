#!/usr/bin/env python3
"""Create the fail-closed qualification plan for a source diff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_PROFILES = (
    "generic-s3-voip",
    "waveshare-s3-full-afe",
    "spotpear-ball-v2-full-afe",
    "waveshare-p4-jpeg",
    "waveshare-p4-h264",
    "waveshare-p4-full-landscape-jpeg",
)
DOC_PREFIXES = ("docs/",)
DOC_FILES = {"README.md", "LICENSE", "CODE_OF_CONDUCT.md"}


def changed_files(base: str, head: str) -> tuple[str, ...]:
    output = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    return tuple(line for line in output.splitlines() if line)


def make_plan(files: tuple[str, ...], *, full: bool = False) -> dict[str, object]:
    docs_only = bool(files) and all(
        path in DOC_FILES or path.startswith(DOC_PREFIXES) for path in files
    )
    if docs_only and not full:
        required = ("quick",)
        skipped = {
            "software-full": "documentation-only diff",
            "home-assistant-runtime": "documentation-only diff",
            "peer": "documentation-only diff",
            "firmware": "documentation-only diff",
        }
        profiles: tuple[str, ...] = ()
        risk = "low"
    else:
        required = (
            "quick",
            "software-full",
            "home-assistant-runtime",
            "peer",
            "firmware",
        )
        skipped = {}
        profiles = FIRMWARE_PROFILES
        risk = "critical"
    return {
        "schema_version": 1,
        "changed_files": files,
        "risk": risk,
        "required_jobs": required,
        "required_firmware_profiles": profiles,
        "skipped_jobs": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("qualification-plan.json"))
    args = parser.parse_args()
    files = changed_files(args.base, args.head) if args.base else ("forced-full",)
    plan = make_plan(files, full=args.full or not args.base)
    args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
