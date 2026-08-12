"""Contracts for promoting only the archive qualified for the tag commit."""

from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.verify_release_candidate import verify_release


def _records(archive: Path) -> tuple[dict[str, object], dict[str, object]]:
    package = {
        "schema_version": 1,
        "head": "head",
        "candidate_id": "candidate",
        "archive": {
            "name": archive.name,
            "bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        },
    }
    qualification = {
        "schema_version": 1,
        "qualified": True,
        "head": "head",
        "candidate_id": "candidate",
        "jobs": {"release-package": {"status": "success"}},
    }
    return package, qualification


def test_exact_qualified_release_archive_is_accepted(tmp_path: Path) -> None:
    archive = tmp_path / "voip_stack.zip"
    archive.write_bytes(b"qualified archive")
    package, qualification = _records(archive)

    assert verify_release("head", package, qualification, archive) == []


def test_wrong_tag_or_modified_archive_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "voip_stack.zip"
    archive.write_bytes(b"qualified archive")
    package, qualification = _records(archive)
    archive.write_bytes(b"modified archive")

    errors = verify_release("other", package, qualification, archive)

    assert "release evidence does not match tag commit" in errors
    assert "release archive hash differs from package manifest" in errors
