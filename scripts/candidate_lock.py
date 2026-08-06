#!/usr/bin/env python3
"""Resolve the immutable multi-repository source identity of a candidate."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "qualification/sources.json"


def _run(*command: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _local_repository(path: Path) -> dict[str, object] | None:
    if not (path / ".git").exists() and not _is_worktree(path):
        return None
    return {
        "commit": _run("git", "rev-parse", "HEAD", cwd=path),
        "dirty": bool(_run("git", "status", "--porcelain", cwd=path)),
        "source": "local",
    }


def _is_worktree(path: Path) -> bool:
    try:
        return _run("git", "rev-parse", "--is-inside-work-tree", cwd=path) == "true"
    except (OSError, subprocess.CalledProcessError):
        return False


def _remote_repository(url: str, ref: str) -> dict[str, object]:
    rows = _run("git", "ls-remote", url, ref).splitlines()
    if len(rows) != 1:
        raise RuntimeError(f"could not resolve exactly one commit for {url} {ref}")
    commit, resolved_ref = rows[0].split(maxsplit=1)
    return {
        "commit": commit,
        "dirty": False,
        "source": "remote",
        "url": url,
        "ref": resolved_ref,
    }


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def build_lock(config: dict[str, Any], *, allow_dirty: bool) -> dict[str, object]:
    repositories: dict[str, object] = {}
    for name, source in config["repositories"].items():
        path = (ROOT / source["path"]).resolve()
        resolved = _local_repository(path)
        if resolved is None:
            resolved = _remote_repository(source["url"], source["ref"])
        if resolved["dirty"] and not allow_dirty:
            raise RuntimeError(f"candidate repository is dirty: {name}")
        repositories[name] = resolved

    return {
        "schema_version": 1,
        "repositories": repositories,
        "toolchain": {
            "python": platform.python_version(),
            "node": _run("node", "--version"),
            "esphome": _package_version("esphome"),
            "home_assistant": _package_version("homeassistant"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output", type=Path, default=Path("candidate-lock.json"))
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()

    config = json.loads(args.sources.read_text())
    lock = build_lock(config, allow_dirty=args.allow_dirty)
    args.output.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
