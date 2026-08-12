#!/usr/bin/env python3
"""Verify that a release uses the archive qualified for its exact tag commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.package_release_candidate import sha256


def verify_release(
    tag_sha: str,
    package: dict[str, object],
    qualification: dict[str, object],
    archive: Path,
) -> list[str]:
    errors: list[str] = []
    archive_record = package.get("archive")
    if package.get("schema_version") != 1:
        errors.append("release package schema is unsupported")
    if qualification.get("schema_version") != 1:
        errors.append("qualification manifest schema is unsupported")
    if qualification.get("qualified") is not True:
        errors.append("candidate is not qualified")
    if package.get("head") != tag_sha or qualification.get("head") != tag_sha:
        errors.append("release evidence does not match tag commit")
    if package.get("candidate_id") != qualification.get("candidate_id"):
        errors.append("release package and qualification candidate differ")
    package_job = qualification.get("jobs", {}).get("release-package", {})
    if not isinstance(package_job, dict) or package_job.get("status") != "success":
        errors.append("release-package job did not succeed")
    if not isinstance(archive_record, dict) or not archive.is_file():
        errors.append("qualified release archive is missing")
        return errors
    if archive_record.get("name") != archive.name:
        errors.append("release archive name differs from package manifest")
    if archive_record.get("bytes") != archive.stat().st_size:
        errors.append("release archive size differs from package manifest")
    if archive_record.get("sha256") != sha256(archive):
        errors.append("release archive hash differs from package manifest")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag-sha", required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    errors = verify_release(
        args.tag_sha,
        json.loads(args.package.read_text(encoding="utf-8")),
        json.loads(args.qualification.read_text(encoding="utf-8")),
        args.archive,
    )
    for error in errors:
        print(f"release_error={error}")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
