#!/usr/bin/env python3
"""Resolve the immutable multi-repository source identity of a candidate."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess


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


def _repository(path: Path) -> dict[str, object]:
    if not _is_worktree(path):
        raise RuntimeError(f"candidate repository is unavailable: {path}")
    return {
        "commit": _run("git", "rev-parse", "HEAD", cwd=path),
        "dirty": bool(_run("git", "status", "--porcelain", cwd=path)),
    }


def _is_worktree(path: Path) -> bool:
    try:
        return _run("git", "rev-parse", "--is-inside-work-tree", cwd=path) == "true"
    except (OSError, subprocess.CalledProcessError):
        return False


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def build_lock(
    config: dict[str, dict[str, dict[str, str]]],
    *,
    allow_dirty: bool,
    ha_python: str = "",
) -> dict[str, object]:
    repositories: dict[str, object] = {}
    for name, source in config["repositories"].items():
        path = (ROOT / source["path"]).resolve()
        resolved = _repository(path)
        if resolved["dirty"] and not allow_dirty:
            raise RuntimeError(f"candidate repository is dirty: {name}")
        repositories[name] = resolved

    home_assistant = (
        _run(
            ha_python,
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('homeassistant'))",
        )
        if ha_python
        else _package_version("homeassistant")
    )
    return {
        "schema_version": 1,
        "repositories": repositories,
        "toolchain": {
            "python": platform.python_version(),
            "node": _run("node", "--version"),
            "esphome": _package_version("esphome"),
            "home_assistant": home_assistant,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output", type=Path, default=Path("candidate-lock.json"))
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--ha-python", default="")
    args = parser.parse_args()

    config = json.loads(args.sources.read_text())
    lock = build_lock(
        config,
        allow_dirty=args.allow_dirty,
        ha_python=args.ha_python,
    )
    args.output.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
