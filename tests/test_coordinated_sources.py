"""A recorded component set must replay exactly or fail before checkout."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

spec = importlib.util.spec_from_file_location('coordinated_sources', Path(__file__).resolve().parents[1] / 'scripts/coordinated_sources.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_default_and_exact_manifest():
    refs = module.requested_refs(None, 'dev')
    assert refs['esphome-voip-stack'] == 'dev'
    assert refs['esphome-video-renderer'] == 'dev'
    assert refs['esphome-screenshot'] == 'dev'
    assert refs['esphome-esp-video-camera'] == 'dev'
    components = {name: {'repository': repo, 'sha': str(i)*40}
                  for i, (name, repo) in enumerate(module.REPOSITORIES.items(), 1)}
    exact = module.requested_refs({'version': 1, 'components': components}, 'main')
    assert exact == {name: item['sha'] for name, item in components.items()}
    components['esphome-voip-stack']['sha'] = 'dev'
    with pytest.raises(ValueError, match='immutable SHA'):
        module.requested_refs({'version': 1, 'components': components}, 'dev')


def test_partial_manifest_is_rejected():
    with pytest.raises(ValueError, match='complete coordinated'):
        module.requested_refs({'version': 1, 'components': {}}, 'dev')


def test_phone_profile_catalog_is_recorded_and_replayed(tmp_path, monkeypatch):
    requests = []
    def fake_git(path, *args):
        if args[0] == 'fetch' and path.name == module.PHONE_CATALOG:
            requests.append(args[-1])
        return 'a' * 40 if args == ('rev-parse', 'HEAD') else ''
    monkeypatch.setattr(module, 'git', fake_git)
    first = module.checkout_sources(tmp_path / 'first', None, 'dev', 'b' * 40)
    assert first['phone_profiles'] == {'repository': module.PHONE_REPOSITORY, 'sha': 'a' * 40}
    assert module.checkout_sources(tmp_path / 'second', first, 'main', 'b' * 40) == first
    assert requests == ['dev', 'a' * 40]
    for invalid in (None, {'repository': module.PHONE_REPOSITORY, 'sha': 'dev'},
                    {'repository': 'other/catalog', 'sha': 'a' * 40}):
        with pytest.raises(ValueError, match='phone_profiles'):
            module.checkout_sources(tmp_path / 'unused', {**first, 'phone_profiles': invalid}, 'dev', 'b' * 40)
    assert not (tmp_path / 'unused').exists()


def test_existing_checkout_is_preserved(tmp_path):
    existing = tmp_path / 'esphome-voip-stack'
    existing.mkdir()
    work = existing / 'user-work'
    work.write_text('preserve')
    with pytest.raises(ValueError, match='existing checkout'):
        module.checkout(tmp_path, module.requested_refs(None, 'dev'))
    assert work.read_text() == 'preserve'


def test_resolved_manifest_replay_never_resolves_moving_branches(tmp_path, monkeypatch):
    resolved = []

    def remote_revision(command, **kwargs):
        assert command[:3] == ['git', 'ls-remote', '--exit-code']
        resolved.append(command[3])
        return 'a' * 40 + '\trefs/heads/dev\n'

    monkeypatch.setattr(module.subprocess, 'check_output', remote_revision)
    first = module.resolve_sources(None, 'dev', 'b' * 40)
    assert len(resolved) == len(module.REPOSITORIES) + 1
    assert len(set(resolved)) == len(resolved)
    assert module.resolve_sources(first, 'main', 'b' * 40) == first
    assert len(resolved) == len(module.REPOSITORIES) + 1
    assert first['phone_profiles']['sha'] == 'a' * 40


def test_unpublished_candidate_records_the_literal_public_profile_backend(monkeypatch):
    requests = []

    def remote_revision(command, **kwargs):
        requests.append(command[3])
        return 'a' * 40 + '\trefs/heads/dev\n'

    monkeypatch.setattr(module.subprocess, 'check_output', remote_revision)
    candidate = 'b' * 40
    receipt = module.resolve_sources(None, 'dev', candidate, public_backend_from_channel=True)
    assert receipt['backend_sha'] == candidate
    assert receipt['public_backend_sha'] == 'a' * 40
    assert set(receipt['components']) == set(module.REPOSITORIES)
    assert requests.count(f'https://github.com/{module.BACKEND_REPOSITORY}.git') == 1
    assert module.resolve_sources(receipt, 'main', candidate) == receipt
    assert len(requests) == len(module.REPOSITORIES) + 2


@pytest.mark.parametrize('catalog', [None, {'repository': module.PHONE_REPOSITORY, 'sha': int('1' * 40)}])
def test_resolver_rejects_a_catalog_that_checkout_cannot_replay(catalog):
    manifest = {'version': 1, 'components': {
        name: {'repository': repo, 'sha': 'a' * 40} for name, repo in module.REPOSITORIES.items()
    }, 'phone_profiles': catalog}
    with pytest.raises(ValueError, match='phone_profiles'):
        module.resolve_sources(manifest, 'dev', 'b' * 40)


@pytest.mark.parametrize('event,ref_type,ref_name,unpublished', [
    ('pull_request', 'branch', '123/merge', True),
    ('push', 'branch', 'feature-test', True),
    ('push', 'branch', 'dev', False),
    ('push', 'branch', 'main', False),
    ('push', 'tag', 'v2026.12.0-dev', False),
    ('push', 'tag', 'v2026.12.0', False),
])
def test_public_profile_backend_selection_uses_the_actual_workflow_condition(event, ref_type, ref_name, unpublished):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text())
    resolver = next(s for s in workflow['jobs']['coordinated-sources']['steps'] if s.get('id') == 'resolve')
    condition = resolver['run'].split('python3 scripts/coordinated_sources.py', 1)[0]
    result = subprocess.run(['bash', '-eu', '-c', condition + '\nprintf "%s" "${public_backend[*]}"'],
        env={**os.environ, 'GITHUB_EVENT_NAME': event, 'GITHUB_REF_TYPE': ref_type, 'GITHUB_REF_NAME': ref_name},
        capture_output=True, text=True, check=True)
    assert result.stdout == ('--public-backend-from-channel' if unpublished else '')


def test_component_jobs_consume_the_single_resolved_source_set():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text())
    jobs = workflow['jobs']
    resolver = jobs['coordinated-sources']
    step = next(s for s in resolver['steps'] if s.get('id') == 'resolve')
    assert '--resolve-only' in step['run']
    assert '--public-backend-from-channel' in step['run']
    assert step['env']['COMPONENT_MANIFEST'] == '${{ inputs.component_manifest }}'
    for job in ('software', 'home-assistant', 'box-v2-firmware', 'ws3-public-firmware'):
        depends = jobs[job]['needs']
        assert 'coordinated-sources' in ([depends] if isinstance(depends, str) else depends)
        consumers = [s for s in jobs[job]['steps'] if 'COMPONENT_MANIFEST' in s.get('env', {})]
        assert consumers
        assert all(s['env']['COMPONENT_MANIFEST'] == '${{ needs.coordinated-sources.outputs.manifest }}'
                   for s in consumers)


@pytest.mark.parametrize("owner", ["intercom", "renderer"])
def test_ci_component_refs_cover_incremental_preview_tags(owner):
    root = Path(__file__).resolve().parents[1]
    if owner == "renderer":
        root = Path(os.environ.get("VIDEO_RENDERER_SOURCE", root.parent / "esphome-video-renderer"))
    workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text())
    expressions = [step['env']['COMPONENT_REF'][3:-2].strip()
                   for job in workflow['jobs'].values() for step in job.get('steps', [])
                   if 'COMPONENT_REF' in step.get('env', {})]
    assert expressions
    cases = [
        ({'base_ref': '', 'ref_name': 'main', 'ref_type': 'branch'}, 'main'),
        ({'base_ref': '', 'ref_name': 'dev', 'ref_type': 'branch'}, 'dev'),
        ({'base_ref': 'main', 'ref_name': '153/merge', 'ref_type': 'branch'}, 'main'),
        ({'base_ref': 'dev', 'ref_name': '153/merge', 'ref_type': 'branch'}, 'dev'),
        ({'base_ref': '', 'ref_name': 'v2026.11.0', 'ref_type': 'tag'}, 'main'),
        ({'base_ref': '', 'ref_name': 'v2026.11.1-dev', 'ref_type': 'tag'}, 'dev'),
        ({'base_ref': '', 'ref_name': 'v2026.11.1-dev.1', 'ref_type': 'tag'}, 'dev'),
    ]
    # Evaluate the actual workflow conditions. These use only Boolean/string
    # operators shared by Actions and JavaScript, and the documented functions.
    script = """const vm=require('node:vm'), data=JSON.parse(process.argv[1]);
      const lower=value=>String(value).toLowerCase();
      const endsWith=(value,suffix)=>lower(value).endsWith(lower(suffix));
      const contains=(value,part)=>lower(value).includes(lower(part));
      console.log(JSON.stringify(data.expressions.map(expression=>data.cases.map(([github])=>
        vm.runInNewContext(expression,{github,endsWith,contains})))));"""
    result = subprocess.run(['node', '-e', script, json.dumps({'expressions': expressions, 'cases': cases})],
                            check=True, capture_output=True, text=True, timeout=10)
    assert json.loads(result.stdout) == [[expected for _, expected in cases]] * len(expressions)


@pytest.mark.parametrize("channel,owner", [("dev", "n-IA-hane"), ("main", "Psix-anp")])
def test_checkout_camera_uses_the_selected_repository_and_ref(tmp_path, monkeypatch, channel, owner):
    commands = []
    def fake_git(path, *args):
        commands.append((path.name, args))
        return "a" * 40 if args == ("rev-parse", "HEAD") else ""
    monkeypatch.setattr(module, "git", fake_git)
    result = module.checkout(tmp_path, module.requested_refs(None, channel))
    camera = "esphome-esp-video-camera"
    assert (camera, ("remote", "add", "origin", f"https://github.com/{owner}/{camera}.git")) in commands
    assert (camera, ("fetch", "--depth", "1", "origin", channel)) in commands
    assert result[camera]["repository"] == f"{owner}/{camera}"


@pytest.mark.parametrize("owner", ["n-IA-hane", "Psix-anp"])
def test_recorded_camera_repository_is_replayed_without_channel_substitution(tmp_path, monkeypatch, owner):
    components = {name: {'repository': repo, 'sha': 'a' * 40} for name, repo in module.REPOSITORIES.items()}
    camera = 'esphome-esp-video-camera'
    components[camera]['repository'] = f'{owner}/{camera}'
    manifest = {'version': 1, 'components': components}
    repositories = module.requested_repositories(manifest, 'dev')
    assert repositories[camera] == f'{owner}/{camera}'
    commands = []
    def fake_git(path, *args):
        commands.append((path.name, args))
        return 'a' * 40 if args == ('rev-parse', 'HEAD') else ''
    monkeypatch.setattr(module, 'git', fake_git)
    actual = module.checkout(tmp_path, module.requested_refs(manifest, 'dev'), repositories)
    assert actual == components
    assert (camera, ('remote', 'add', 'origin', f'https://github.com/{owner}/{camera}.git')) in commands
    assert (camera, ('fetch', '--depth', '1', 'origin', 'a' * 40)) in commands
    components[camera]['repository'] = 'unapproved/example'
    with pytest.raises(ValueError, match='Invalid repository'):
        module.requested_refs(manifest, 'dev')
