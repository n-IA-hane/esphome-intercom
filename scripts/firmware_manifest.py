#!/usr/bin/env python3
"""Record immutable metadata for every compiled ESPHome firmware artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    # ``**`` may discover the same nested ESPHome build through more than one
    # expansion.  A build artifact is evidence once, irrespective of how the
    # glob reached it.
    descriptions = sorted(
        set(root.glob("yamls/**/.esphome/**/build/*/build/project_description.json"))
    )
    for description_path in descriptions:
        build_dir = description_path.parent
        binaries = sorted(build_dir.glob("firmware*.bin"))
        if not binaries:
            continue
        description = json.loads(description_path.read_text(encoding="utf-8"))
        build_info_path = build_dir.parent / "build_info.json"
        build_info = (
            json.loads(build_info_path.read_text(encoding="utf-8"))
            if build_info_path.is_file()
            else {}
        )
        records.append(
            {
                "node": str(description.get("project_name") or build_dir.parent.name),
                "target": str(description.get("target") or ""),
                "esphome_version": str(
                    build_info.get("esphome_version")
                    or description.get("project_version")
                    or ""
                ),
                "esp_idf_version": str(description.get("git_revision") or ""),
                "config_hash": build_info.get("config_hash"),
                "artifacts": [
                    {
                        "path": str(binary.relative_to(root)),
                        "bytes": binary.stat().st_size,
                        "sha256": sha256(binary),
                    }
                    for binary in binaries
                ],
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=Path("firmware-manifest.json"))
    parser.add_argument("--expected", type=int, default=6)
    args = parser.parse_args()

    root = args.root.resolve()
    records = build_records(root)
    if len(records) != args.expected:
        raise RuntimeError(
            f"expected {args.expected} compiled firmware profiles, found {len(records)}"
        )
    payload = {"schema_version": 1, "firmware": records}
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
