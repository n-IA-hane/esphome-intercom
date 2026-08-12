#!/usr/bin/env python3
"""Compile exactly the firmware profiles selected by a qualification plan."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qualification.registry import FIRMWARE_PROFILES  # noqa: E402
from scripts.firmware_manifest import build_records, sha256  # noqa: E402


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
    parser.add_argument("--index", type=Path)
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
    staged: list[dict[str, object]] = []
    with _test_secrets(paths):
        for profile, path in zip(profiles, paths, strict=True):
            subprocess.run(
                [str(ROOT / ".venv/bin/esphome"), "compile", str(path)],
                cwd=ROOT,
                check=True,
            )
            if args.index is None:
                continue
            source = path.parent / profile.factory_path
            if not source.is_file():
                raise RuntimeError(
                    f"compiled factory firmware is missing: {profile.id}"
                )
            record = next(
                (
                    item
                    for item in build_records(ROOT)
                    if any(
                        ROOT / str(artifact.get("path") or "") == source
                        for artifact in item.get("artifacts", [])
                    )
                ),
                None,
            )
            if record is None:
                raise RuntimeError(f"firmware metadata is missing: {profile.id}")
            target = args.index.parent / "firmware-staging" / profile.id / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            staged_artifacts: list[dict[str, object]] = []
            for artifact in record.get("artifacts", []):
                artifact_source = ROOT / str(artifact.get("path") or "")
                if not artifact_source.is_file():
                    continue
                artifact_target = target.parent / artifact_source.name
                shutil.copy2(artifact_source, artifact_target)
                staged_artifacts.append(
                    {
                        "path": str(artifact_target.relative_to(args.index.parent)),
                        "bytes": artifact_target.stat().st_size,
                        "sha256": sha256(artifact_target),
                    }
                )
            staged.append(
                {
                    **record,
                    "profile": profile.id,
                    "artifacts": staged_artifacts,
                    "artifact": {
                        "path": str(target.relative_to(args.index.parent)),
                        "bytes": target.stat().st_size,
                        "sha256": sha256(target),
                    },
                }
            )
    if args.index is not None:
        args.index.write_text(
            json.dumps(
                {"schema_version": 1, "firmware": staged},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
