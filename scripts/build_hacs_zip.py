#!/usr/bin/env python3
"""Build the flat, reproducible release archive consumed by HACS."""

from __future__ import annotations

import argparse
from pathlib import Path
import stat
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "custom_components" / "voip_stack"
DEFAULT_OUTPUT = ROOT / "voip_stack.zip"
LICENSE_SOURCE = ROOT / "LICENSE"
ARCHIVE_LICENSE_NAME = "LICENSE"
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
IGNORED_GENERATED_DIRECTORY_NAMES = {"__pycache__"}
FORBIDDEN_FILE_NAMES = {".DS_Store", "Thumbs.db"}
FORBIDDEN_SUFFIXES = {
    ".bak",
    ".log",
    ".pcap",
    ".pyc",
    ".pyo",
    ".swp",
    ".tmp",
    ".wav",
}
ALLOWED_SUFFIXES = {".js", ".json", ".md", ".png", ".py", ".svg", ".txt", ".yaml"}


def _archive_files(source: Path) -> list[Path]:
    files: list[Path] = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise ValueError(f"release archive cannot contain symlinks: {relative}")
        if any(part in IGNORED_GENERATED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if any(part.startswith(".") for part in relative.parts):
            raise ValueError(f"release archive cannot contain hidden paths: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unsupported release filesystem entry: {relative}")
        if path.name in FORBIDDEN_FILE_NAMES:
            raise ValueError(f"release archive cannot contain junk file: {relative}")
        suffix = path.suffix.lower()
        if suffix in FORBIDDEN_SUFFIXES:
            raise ValueError(f"release archive cannot contain junk suffix: {relative}")
        if path.name != ARCHIVE_LICENSE_NAME and suffix not in ALLOWED_SUFFIXES:
            raise ValueError(f"unsupported release file type: {relative}")
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(source).as_posix())


def _archive_members(source: Path) -> list[tuple[str, bytes]]:
    members = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in _archive_files(source)
    }
    if LICENSE_SOURCE.is_symlink() or not LICENSE_SOURCE.is_file():
        raise ValueError(f"release license not found at {LICENSE_SOURCE}")
    license_data = LICENSE_SOURCE.read_bytes()
    existing_license = members.get(ARCHIVE_LICENSE_NAME)
    if existing_license is not None and existing_license != license_data:
        raise ValueError("integration LICENSE differs from the repository LICENSE")
    members[ARCHIVE_LICENSE_NAME] = license_data
    return sorted(members.items())


def build_archive(source: Path, output: Path) -> None:
    """Write a deterministic ZIP with integration files at the archive root."""
    if source.is_symlink():
        raise ValueError(f"release source cannot be a symlink: {source}")
    source = source.resolve()
    output = output.resolve()
    if not (source / "manifest.json").is_file():
        raise ValueError(f"integration manifest not found below {source}")
    if source == output or source in output.parents:
        raise ValueError("release archive output must be outside the integration tree")

    write_archive(_archive_members(source), output)


def write_archive(members: list[tuple[str, bytes]], output: Path) -> None:
    """Write already captured source bytes without reopening mutable inputs."""

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, data in members:
                info = zipfile.ZipInfo(name, date_time=ARCHIVE_TIMESTAMP)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, data)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build_archive(args.source, args.output)


if __name__ == "__main__":
    main()
