#!/usr/bin/env python3
"""Build and attest the HACS archive for one qualified candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.build_hacs_zip import build_archive
from scripts.candidate_lock import candidate_id as compute_candidate_id


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_candidate(
    source: Path,
    archive: Path,
    candidate: dict[str, object],
    plan: dict[str, object],
) -> dict[str, object]:
    candidate_value = str(candidate.get("candidate_id") or "")
    head = str(plan.get("head") or "")
    repositories = candidate.get("repositories")
    intercom = (
        repositories.get("esphome-intercom", {})
        if isinstance(repositories, dict)
        else {}
    )
    if candidate_value != compute_candidate_id(candidate):
        raise RuntimeError("candidate identity is invalid")
    if not head or intercom.get("commit") != head:
        raise RuntimeError("release package does not match candidate head")
    build_archive(source, archive)
    return {
        "schema_version": 1,
        "head": head,
        "candidate_id": candidate_value,
        "archive": {
            "name": archive.name,
            "bytes": archive.stat().st_size,
            "sha256": sha256(archive),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = package_candidate(
        args.source,
        args.archive,
        json.loads(args.candidate.read_text(encoding="utf-8")),
        json.loads(args.plan.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
