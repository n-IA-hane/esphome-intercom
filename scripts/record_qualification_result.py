#!/usr/bin/env python3
"""Create one hashed qualification job result for final verification."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_result(job: str, status: str, artifacts: list[Path], root: Path) -> dict[str, object]:
    root = root.resolve()
    records: list[dict[str, object]] = []
    for artifact in artifacts:
        path = artifact.resolve()
        if not path.is_file():
            raise RuntimeError(f"qualification artifact is unavailable: {artifact}")
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise RuntimeError(f"qualification artifact is outside evidence root: {artifact}") from error
        records.append(
            {
                "path": str(relative),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return {"schema_version": 1, "jobs": {job: {"status": status, "artifacts": records}}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--status", choices=("success", "failure", "cancelled", "skipped"), required=True)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_result(args.job, args.status, args.artifact, args.root)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

