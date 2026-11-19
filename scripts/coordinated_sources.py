#!/usr/bin/env python3
"""Resolve coordinated CI sources and record the exact reusable commit set."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE_CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / 'qualification/sources.json').read_text()
)
REPOSITORIES = {
    name: source['repository']
    for name, source in SOURCE_CATALOG['repositories'].items()
    if name != 'esphome-intercom'
}
CAMERA = 'esphome-esp-video-camera'
CAMERA_DEV_REPOSITORY = 'n-IA-hane/esphome-esp-video-camera'
PHONE_REPOSITORY = SOURCE_CATALOG['phone_profiles']['repository']
PHONE_CATALOG = PHONE_REPOSITORY.rsplit('/', 1)[1]


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def requested_repositories(manifest: dict | None, default_ref: str) -> dict[str, str]:
    """Choose channel defaults, or preserve an explicitly recorded commit set."""
    if manifest is None:
        return {**REPOSITORIES, CAMERA: CAMERA_DEV_REPOSITORY if default_ref == 'dev' else REPOSITORIES[CAMERA]}
    if manifest.get('version') != 1 or set(manifest.get('components', {})) != set(REPOSITORIES):
        raise ValueError('Manifest must contain the complete coordinated component set')
    repositories = {}
    for name, repository in REPOSITORIES.items():
        item = manifest['components'][name]
        sha = item.get('sha', '')
        allowed = {repository, CAMERA_DEV_REPOSITORY} if name == CAMERA else {repository}
        if item.get('repository') not in allowed or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError(f'Invalid repository or immutable SHA for {name}')
        repositories[name] = item['repository']
    return repositories


def requested_refs(manifest: dict | None, default_ref: str) -> dict[str, str]:
    repositories = requested_repositories(manifest, default_ref)
    return ({name: default_ref for name in repositories} if manifest is None else
            {name: manifest['components'][name]['sha'] for name in repositories})


def checkout(root: Path, refs: dict[str, str], repositories: dict[str, str] | None = None) -> dict:
    components = {}
    repositories = repositories or requested_repositories(None, refs.get(CAMERA, 'main'))
    for name, repository in repositories.items():
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


def checkout_sources(root: Path, manifest: dict | None, channel: str, backend: str) -> dict:
    repositories = requested_repositories(manifest, channel)
    refs = requested_refs(manifest, channel)
    catalog_ref = channel
    if manifest is not None:
        catalog = manifest.get('phone_profiles')
        if (not isinstance(catalog, dict) or catalog.get('repository') != PHONE_REPOSITORY
                or not isinstance(catalog.get('sha'), str)
                or not re.fullmatch(r'[0-9a-f]{40}', catalog['sha'])):
            raise ValueError('Exact replay needs the phone_profiles repository and immutable SHA')
        catalog_ref = catalog['sha']
    repositories[PHONE_CATALOG] = PHONE_REPOSITORY
    refs[PHONE_CATALOG] = catalog_ref
    components = checkout(root, refs, repositories)
    phone_profiles = components.pop(PHONE_CATALOG)
    return {'version': 1, 'backend_sha': backend, 'components': components, 'phone_profiles': phone_profiles}


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
    backend = git(args.backend, 'rev-parse', 'HEAD')
    if manifest is not None and manifest.get('backend_sha') != backend:
        raise ValueError('Select the manifest backend commit before replaying its components')
    receipt = checkout_sources(args.root, manifest, args.ref, backend)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
