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
BACKEND_REPOSITORY = SOURCE_CATALOG['repositories']['esphome-intercom']['repository']


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def requested_repositories(manifest: dict | None, default_ref: str) -> dict[str, str]:
    """Choose channel defaults, or preserve an explicitly recorded commit set."""
    if manifest is None:
        return {**REPOSITORIES, CAMERA: CAMERA_DEV_REPOSITORY if default_ref == 'dev' else REPOSITORIES[CAMERA]}
    if manifest.get('version') != 1 or set(manifest.get('components', {})) != set(REPOSITORIES):
        raise ValueError('Manifest must contain the complete coordinated component set')
    if 'public_backend_sha' in manifest and (
            not isinstance(manifest['public_backend_sha'], str) or
            not re.fullmatch(r'[0-9a-f]{40}', manifest['public_backend_sha'])):
        raise ValueError('Public profile replay needs an immutable backend SHA')
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
    receipt = {'version': 1, 'backend_sha': backend, 'components': components, 'phone_profiles': phone_profiles}
    if manifest is not None and 'public_backend_sha' in manifest:
        receipt['public_backend_sha'] = manifest['public_backend_sha']
    return receipt


def resolve_sources(manifest: dict | None, channel: str, backend: str,
                    *, public_backend_from_channel: bool = False) -> dict:
    """Freeze one source set before independent CI jobs start their checkouts."""
    repositories = requested_repositories(manifest, channel)
    if manifest is not None:
        catalog = manifest.get('phone_profiles')
        if (not isinstance(catalog, dict) or catalog.get('repository') != PHONE_REPOSITORY or
                not isinstance(catalog.get('sha'), str) or
                not re.fullmatch(r'[0-9a-f]{40}', catalog['sha'])):
            raise ValueError('Exact replay needs the phone_profiles repository and immutable SHA')
        return manifest
    if public_backend_from_channel:
        repositories['esphome-intercom'] = BACKEND_REPOSITORY
    repositories[PHONE_CATALOG] = PHONE_REPOSITORY
    resolved = {}
    for name, repository in repositories.items():
        output = subprocess.check_output(
            ['git', 'ls-remote', '--exit-code', f'https://github.com/{repository}.git', f'refs/heads/{channel}'],
            text=True,
        ).strip().split()
        if len(output) != 2 or not re.fullmatch(r'[0-9a-f]{40}', output[0]):
            raise ValueError(f'Could not resolve immutable source: {name}')
        resolved[name] = {'repository': repository, 'sha': output[0]}
    catalog = resolved.pop(PHONE_CATALOG)
    receipt = {'version': 1, 'backend_sha': backend, 'components': resolved, 'phone_profiles': catalog}
    if public_backend_from_channel:
        receipt['public_backend_sha'] = resolved.pop('esphome-intercom')['sha']
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--resolve-only', action='store_true')
    parser.add_argument('--public-backend-from-channel', action='store_true',
                        help='Record the public profile backend separately from an unpublished candidate')
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
    if args.resolve_only:
        receipt = resolve_sources(manifest, args.ref, backend,
                                  public_backend_from_channel=args.public_backend_from_channel)
    else:
        if args.root is None:
            parser.error('--root is required for source checkout')
        receipt = checkout_sources(args.root, manifest, args.ref, backend)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
