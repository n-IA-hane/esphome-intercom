"""Bind release archive members to one immutable integration Git tree."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from scripts.build_hacs_zip import ALLOWED_SUFFIXES, _archive_files


COMPONENT = Path("custom_components/voip_stack")


def git_bytes(repository: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "--no-replace-objects", "-C", str(repository), *arguments])


def committed_members(repository: Path, commit: str) -> list[tuple[str, bytes]]:
    """Read every package byte, including LICENSE, from Git objects."""
    listing = git_bytes(repository, "ls-tree", "-rz", commit, "--", str(COMPONENT), "LICENSE")
    members = {}
    for row in listing.split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        path = Path(raw_path.decode("utf-8"))
        name = "LICENSE" if path == Path("LICENSE") else path.relative_to(COMPONENT).as_posix()
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError(f"unsupported committed release member: {path}")
        if name != "LICENSE" and (path.suffix.lower() not in ALLOWED_SUFFIXES
                                  or any(part.startswith(".") for part in Path(name).parts)):
            raise ValueError(f"unsupported committed release member: {path}")
        data = git_bytes(repository, "cat-file", "blob", object_id.decode("ascii"))
        if name in members and members[name] != data:
            raise ValueError(f"conflicting committed release member: {name}")
        members[name] = data
    if "manifest.json" not in members or "LICENSE" not in members:
        raise ValueError("committed integration or LICENSE is missing")
    return sorted(members.items())


def source_members(source: Path, commit: str) -> list[tuple[str, bytes]]:
    """Reject foreign or modified directories, then return immutable bytes."""
    if source.is_symlink():
        raise ValueError("release source cannot be a symlink")
    source = source.resolve()
    repository = Path(git_bytes(source, "rev-parse", "--show-toplevel").decode().strip())
    if source != repository / COMPONENT:
        raise ValueError("release source must be the committed integration directory")
    if git_bytes(repository, "rev-parse", "HEAD").decode().strip() != commit:
        raise ValueError("release source HEAD differs from the candidate")
    expected = committed_members(repository, commit)
    actual = {path.relative_to(source).as_posix(): path.read_bytes() for path in _archive_files(source)}
    license_path = repository / "LICENSE"
    if license_path.is_symlink():
        raise ValueError("release LICENSE cannot be a symlink")
    license_bytes = license_path.read_bytes()
    if "LICENSE" in actual and actual["LICENSE"] != license_bytes:
        raise ValueError("integration LICENSE differs from the repository LICENSE")
    actual["LICENSE"] = license_bytes
    if actual != dict(expected):
        raise ValueError("release source bytes differ from the candidate commit")
    return expected


def members_digest(members: list[tuple[str, bytes]]) -> str:
    index = [(name, len(data), hashlib.sha256(data).hexdigest()) for name, data in members]
    return hashlib.sha256(json.dumps(index, separators=(",", ":")).encode()).hexdigest()
