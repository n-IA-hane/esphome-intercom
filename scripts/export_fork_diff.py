#!/usr/bin/env python3
"""Export a local component's source delta against its recorded upstream commit."""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def source_file(path: str) -> bool:
    return Path(path).suffix in {'.py', '.h', '.cpp', '.c'} or Path(path).name == 'CMakeLists.txt'


def export_diff(component: str, upstream: Path, workspace: Path) -> str:
    manifest = json.loads((ROOT / 'esphome/upstream_sources.json').read_text())
    entry = manifest['components'][component]
    revision = entry.get('upstream_revision', manifest['revision'])
    prefix = f'esphome/components/{component}'
    local = workspace / entry['local_repository']
    directory = local / prefix
    if not directory.is_dir():
        raise ValueError(f'Component directory not found: {directory}')
    paths = subprocess.check_output(
        ['git', '-C', str(upstream), 'ls-tree', '-r', '--name-only', revision, prefix], text=True,
    ).splitlines()
    baseline = {
        name: subprocess.check_output(['git', '-C', str(upstream), 'show', f'{revision}:{name}'], text=True)
        for name in paths if source_file(name)
    }
    current = {
        path.relative_to(local).as_posix(): path.read_text()
        for path in directory.rglob('*') if path.is_file() and source_file(str(path))
    }
    output = []
    for name in sorted(baseline.keys() | current.keys()):
        output.extend(difflib.unified_diff(
            baseline.get(name, '').splitlines(keepends=True),
            current.get(name, '').splitlines(keepends=True),
            fromfile=f'a/{name}' if name in baseline else '/dev/null',
            tofile=f'b/{name}' if name in current else '/dev/null',
        ))
    return ''.join(output)


def main() -> None:
    manifest = json.loads((ROOT / 'esphome/upstream_sources.json').read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=manifest['components'])
    parser.add_argument('--upstream', required=True, type=Path, help='ESPHome Git checkout containing the recorded commit')
    parser.add_argument('--workspace', default=ROOT.parent, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.write_text(export_diff(args.component, args.upstream, args.workspace))


if __name__ == '__main__':
    main()
