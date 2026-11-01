#!/usr/bin/env python3
"""Resolve coordinated CI sources and record the exact reusable commit set."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

CAMERA = 'esphome-esp-video-camera'
LEGACY_CAMERA_REPOSITORY = 'Psix-anp/esphome-esp-video-camera'
REPOSITORIES = {
    'esphome-voip-stack': 'n-IA-hane/esphome-voip-stack',
    'esphome-audio-stack': 'n-IA-hane/esphome-audio-stack',
    'esphome-runtime-controller': 'n-IA-hane/esphome-runtime-controller',
    'esphome-screenshot': 'n-IA-hane/esphome-screenshot',
    'esphome-video-renderer': 'n-IA-hane/esphome-video-renderer',
    CAMERA: 'n-IA-hane/esphome-esp-video-camera',
}


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def repositories_for(manifest: dict | None, default_ref: str) -> dict[str, str]:
    """New dev runs use our qualified Camera; old locks retain their owner."""
    if manifest is None:
        repositories = dict(REPOSITORIES)
        if default_ref != 'dev':
            repositories[CAMERA] = LEGACY_CAMERA_REPOSITORY
        return repositories
    if manifest.get('version') != 1 or set(manifest.get('components', {})) != set(REPOSITORIES):
        raise ValueError('Manifest must contain the complete coordinated component set')
    repositories = {}
    for name, repository in REPOSITORIES.items():
        item = manifest['components'][name]
        sha = item.get('sha', '')
        allowed = {repository, LEGACY_CAMERA_REPOSITORY} if name == CAMERA else {repository}
        if item.get('repository') not in allowed or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError(f'Invalid repository or immutable SHA for {name}')
        repositories[name] = item['repository']
    return repositories


def source_url(name: str, ref: str) -> str:
    if name == CAMERA and ref != 'dev':
        return f'github://{LEGACY_CAMERA_REPOSITORY}@main'
    return f'github://{repositories_for(None, ref)[name]}@{ref}'


def requested_refs(manifest: dict | None, default_ref: str) -> dict[str, str]:
    repositories_for(manifest, default_ref)
    if manifest is None:
        return dict.fromkeys(REPOSITORIES, default_ref)
    return {name: item['sha'] for name, item in manifest['components'].items()}


def resolve(refs: dict[str, str], repositories: dict[str, str]) -> dict:
    """Resolve moving branches once, before any parallel CI job starts."""
    components = {}
    for name, repository in repositories.items():
        ref = refs[name]
        if re.fullmatch(r'[0-9a-f]{40}', ref):
            sha = ref
        else:
            result = subprocess.check_output(
                ['git', 'ls-remote', '--exit-code', f'https://github.com/{repository}.git',
                 f'refs/heads/{ref}'], text=True,
            ).split()
            if len(result) != 2 or not re.fullmatch(r'[0-9a-f]{40}', result[0]):
                raise ValueError(f'Cannot resolve one immutable commit for {name}')
            sha = result[0]
        components[name] = {'repository': repository, 'sha': sha}
    return components


def checkout(root: Path, refs: dict[str, str], repositories: dict[str, str] | None = None) -> dict:
    components = {}
    for name, repository in (repositories or REPOSITORIES).items():
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
    parser.add_argument('--root', type=Path)
    parser.add_argument('--resolve-only', action='store_true')
    parser.add_argument('--backend', type=Path, default=Path.cwd())
    parser.add_argument('--ref', choices=('dev', 'main'), default='dev')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.manifest.read_text() if args.manifest else os.environ.get('COMPONENT_MANIFEST', '')
    manifest = json.loads(raw) if raw.strip() else None
    refs = requested_refs(manifest, args.ref)
    repositories = repositories_for(manifest, args.ref)
    backend = git(args.backend, 'rev-parse', 'HEAD')
    if manifest is not None:
        recorded_backend = manifest.get('backend_sha', '')
        if not re.fullmatch(r'[0-9a-f]{40}', recorded_backend):
            raise ValueError('Manifest must record an immutable backend SHA')
        if args.resolve_only:
            # The coordinator selects this backend for every downstream job.
            backend = recorded_backend
        elif recorded_backend != backend:
            raise ValueError('Select the manifest backend commit before replaying its components')
    if args.resolve_only:
        components = resolve(refs, repositories)
    else:
        if args.root is None:
            parser.error('--root is required unless --resolve-only is selected')
        components = checkout(args.root, refs, repositories)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'version': 1, 'backend_sha': backend,
                                      'components': components}, indent=2) + '\n')


if __name__ == '__main__':
    main()
