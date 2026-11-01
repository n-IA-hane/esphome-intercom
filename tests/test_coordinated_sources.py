"""A recorded component set must replay exactly or fail before checkout."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location('coordinated_sources', Path(__file__).resolve().parents[1] / 'scripts/coordinated_sources.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_default_and_exact_manifest():
    refs = module.requested_refs(None, 'dev')
    assert refs['esphome-voip-stack'] == 'dev'
    assert refs['esphome-screenshot'] == 'dev'
    assert refs['esphome-video-renderer'] == 'dev'
    assert refs['esphome-esp-video-camera'] == 'dev'
    assert module.repositories_for(None, 'dev')[module.CAMERA] == 'n-IA-hane/esphome-esp-video-camera'
    assert module.repositories_for(None, 'main')[module.CAMERA] == module.LEGACY_CAMERA_REPOSITORY
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


def test_existing_checkout_is_preserved(tmp_path):
    existing = tmp_path / 'esphome-voip-stack'
    existing.mkdir()
    work = existing / 'user-work'
    work.write_text('preserve')
    with pytest.raises(ValueError, match='existing checkout'):
        module.checkout(tmp_path, module.requested_refs(None, 'dev'))
    assert work.read_text() == 'preserve'


def _manifest():
    return {'version': 1, 'backend_sha': 'a' * 40,
            'components': {name: {'repository': repository, 'sha': str(index) * 40}
                           for index, (name, repository) in enumerate(module.REPOSITORIES.items(), 1)}}


def test_historical_camera_manifest_keeps_its_repository():
    manifest = _manifest()
    manifest['components'][module.CAMERA]['repository'] = module.LEGACY_CAMERA_REPOSITORY
    assert module.repositories_for(manifest, 'dev')[module.CAMERA] == module.LEGACY_CAMERA_REPOSITORY
    assert module.requested_refs(manifest, 'dev')[module.CAMERA] == '6' * 40
    manifest['components'][module.CAMERA]['repository'] = 'unrelated/esphome-esp-video-camera'
    with pytest.raises(ValueError, match='Invalid repository'):
        module.repositories_for(manifest, 'dev')


def test_resolved_manifest_does_not_query_moving_branches(monkeypatch):
    manifest = _manifest()
    monkeypatch.setattr(module.subprocess, 'check_output',
                        lambda *args, **kwargs: pytest.fail('Immutable replay queried a branch'))
    result = module.resolve(module.requested_refs(manifest, 'dev'),
                            module.repositories_for(manifest, 'dev'))
    assert result == manifest['components']


def test_resolver_records_each_qualified_dev_repository(monkeypatch):
    calls = []

    def ls_remote(command, **kwargs):
        calls.append(command)
        return f'{len(calls):040x}\trefs/heads/dev\n'

    monkeypatch.setattr(module.subprocess, 'check_output', ls_remote)
    resolved = module.resolve(module.requested_refs(None, 'dev'), module.repositories_for(None, 'dev'))
    assert len(calls) == len(module.REPOSITORIES)
    assert calls[-1][-2:] == ['https://github.com/n-IA-hane/esphome-esp-video-camera.git', 'refs/heads/dev']
    assert resolved[module.CAMERA]['sha'] == '0000000000000000000000000000000000000006'


def test_checkout_uses_the_recorded_camera_repository(tmp_path, monkeypatch):
    manifest = _manifest()
    manifest['components'][module.CAMERA]['repository'] = module.LEGACY_CAMERA_REPOSITORY
    commands = []

    def git(path, *args):
        commands.append((path.name, args))
        if args[:2] == ('rev-parse', 'HEAD'):
            return manifest['components'][path.name]['sha']
        return ''

    monkeypatch.setattr(module, 'git', git)
    result = module.checkout(tmp_path, module.requested_refs(manifest, 'dev'),
                             module.repositories_for(manifest, 'dev'))
    assert result == manifest['components']
    assert (module.CAMERA, ('remote', 'add', 'origin',
                           'https://github.com/Psix-anp/esphome-esp-video-camera.git')) in commands


def test_replay_requires_its_backend_before_checkout(tmp_path, monkeypatch):
    manifest = _manifest()
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    output = tmp_path / 'output.json'
    monkeypatch.setattr(module, 'git', lambda *args: 'b' * 40)
    monkeypatch.setattr('sys.argv', ['coordinated_sources.py', '--manifest', str(path),
                                   '--root', str(tmp_path / 'sources'), '--output', str(output)])
    with pytest.raises(ValueError, match='Select the manifest backend'):
        module.main()
    assert not output.exists()
    assert not (tmp_path / 'sources').exists()


def test_coordinator_selects_the_historical_backend_without_refetch(tmp_path, monkeypatch):
    manifest = _manifest()
    manifest['components'][module.CAMERA]['repository'] = module.LEGACY_CAMERA_REPOSITORY
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    output = tmp_path / 'output.json'
    monkeypatch.setattr(module, 'git', lambda *args: 'b' * 40)
    monkeypatch.setattr('sys.argv', ['coordinated_sources.py', '--resolve-only', '--manifest', str(path),
                                   '--output', str(output)])
    module.main()
    assert json.loads(output.read_text()) == manifest


def test_ci_lanes_share_one_manifest_and_its_backend():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text())
    jobs = workflow['jobs']
    assert jobs['coordinated-sources']['outputs']['manifest_sha256']
    for name, job in jobs.items():
        if name == 'coordinated-sources':
            continue
        assert job['needs'] == 'coordinated-sources'
        checkout = next(step for step in job['steps'] if str(step.get('uses', '')).startswith('actions/checkout@'))
        assert checkout['with']['ref'] == '${{ needs.coordinated-sources.outputs.backend_sha }}'
        for step in job['steps']:
            if 'scripts/coordinated_sources.py' in step.get('run', ''):
                assert step['env']['COMPONENT_MANIFEST'] == '${{ needs.coordinated-sources.outputs.manifest }}'
