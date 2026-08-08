#!/usr/bin/env python3
"""Compile exactly the firmware profiles selected by a qualification plan."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qualification.registry import FIRMWARE_PROFILES  # noqa: E402


TEST_SECRETS = "wifi_ssid: qualification\nwifi_password: qualification\n"


@contextmanager
def _test_secrets(paths: list[Path]):
    created: list[Path] = []
    for directory in {path.parent for path in paths}:
        secret = directory / "secrets.yaml"
        if not secret.exists():
            secret.write_text(TEST_SECRETS, encoding="utf-8")
            created.append(secret)
    try:
        yield
    finally:
        for secret in created:
            secret.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    selected = {
        str(profile["id"])
        for profile in json.loads(args.plan.read_text(encoding="utf-8"))[
            "firmware_profiles"
        ]
    }
    profiles = [profile for profile in FIRMWARE_PROFILES if profile.id in selected]
    if selected != {profile.id for profile in profiles}:
        raise RuntimeError("qualification plan contains an unknown firmware profile")
    if not profiles:
        raise RuntimeError("qualification plan selected no firmware profiles")
    paths = [ROOT / profile.path for profile in profiles]
    with _test_secrets(paths):
        for path in paths:
            subprocess.run(
                [str(ROOT / ".venv/bin/esphome"), "compile", str(path)],
                cwd=ROOT,
                check=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
