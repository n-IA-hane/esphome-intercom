#!/usr/bin/env python3
"""Resolve coordinated CI sources and record the exact reusable commit set."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORIES = {
    'esphome-voip-stack': 'n-IA-hane/esphome-voip-stack',
    'esphome-audio-stack': 'n-IA-hane/esphome-audio-stack',
    'esphome-runtime-controller': 'n-IA-hane/esphome-runtime-controller',
    'esphome-screenshot': 'n-IA-hane/esphome-screenshot',
    'esphome-video-renderer': 'n-IA-hane/esphome-video-renderer',
    'esphome-esp-video-camera': 'Psix-anp/esphome-esp-video-camera',
}


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def requested_refs(manifest: dict | None, default_ref: str) -> dict[str, str]:
    if manifest is None:
        return {name: 'main' if name == 'esphome-esp-video-camera' else default_ref
                for name in REPOSITORIES}
    if manifest.get('version') != 1 or set(manifest.get('components', {})) != set(REPOSITORIES):
        raise ValueError('Manifest must contain the complete coordinated component set')
    refs = {}
    for name, repository in REPOSITORIES.items():
        item = manifest['components'][name]
        sha = item.get('sha', '')
        if item.get('repository') != repository or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError(f'Invalid repository or immutable SHA for {name}')
        refs[name] = sha
    return refs


def checkout(root: Path, refs: dict[str, str]) -> dict:
    components = {}
    for name, repository in REPOSITORIES.items():
        path = root / name
        if path.exists():
            raise ValueError(f'Refusing to replace existing checkout: {path}')
        path.mkdir(parents=True)
        git(path, 'init', '--quiet')
        git(path, 'remote', 'add', 'origin', f'https://github.com/{repository}.git')
        git(path, 'fetch', '--depth', '1', 'origin', refs[name])
        git(path, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
        sha = git(path, 'rev-parse', 'HEAD')
        if re.fullmatch(r'[0-9a-f]{40}', refs[name]) and sha != refs[name]:
            raise ValueError(f'Fetched commit differs from manifest: {name}')
        components[name] = {'repository': repository, 'sha': sha}
    return components


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--backend', type=Path, default=Path.cwd())
    parser.add_argument('--ref', choices=('dev', 'main'), default='dev')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.manifest.read_text() if args.manifest else os.environ.get('COMPONENT_MANIFEST', '')
    manifest = json.loads(raw) if raw.strip() else None
    refs = requested_refs(manifest, args.ref)
    backend = git(args.backend, 'rev-parse', 'HEAD')
    if manifest is not None and manifest.get('backend_sha') != backend:
        raise ValueError('Select the manifest backend commit before replaying its components')
    components = checkout(args.root, refs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'version': 1, 'backend_sha': backend,
                                      'components': components}, indent=2) + '\n')


if __name__ == '__main__':
    main()
