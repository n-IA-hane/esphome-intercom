#!/usr/bin/env python3
"""Resolve the immutable multi-repository source identity of a candidate."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "qualification/sources.json"


def candidate_id(payload: dict[str, object]) -> str:
    """Return the content identity without trusting a stored digest."""

    canonical_payload = {
        key: value for key, value in payload.items() if key != "candidate_id"
    }
    canonical = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _environment_digest(python: str) -> str:
    frozen = _run(
        python,
        "-c",
        (
            "import importlib.metadata as m; "
            "print('\\n'.join(sorted(f'{d.metadata.get(\"Name\", d.name)}=={d.version}' "
            "for d in m.distributions())))"
        ),
    )
    canonical = "\n".join(sorted(filter(None, frozen.splitlines()))) + "\n"
    return _sha256_bytes(canonical.encode())


def _input_hashes() -> dict[str, str]:
    return {
        path.name: _sha256_bytes(path.read_bytes())
        for path in sorted(ROOT.glob("requirements*.txt"))
    }


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
    payload: dict[str, object] = {
        "schema_version": 1,
        "repositories": repositories,
        "toolchain": {
            "python": platform.python_version(),
            "node": _run("node", "--version"),
            "esphome": _package_version("esphome"),
            "home_assistant": home_assistant,
            "python_environment_sha256": _environment_digest(sys.executable),
            "ha_environment_sha256": _environment_digest(ha_python)
            if ha_python
            else "unavailable",
            "requirement_inputs": _input_hashes(),
        },
    }
    payload["candidate_id"] = candidate_id(payload)
    return payload


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
