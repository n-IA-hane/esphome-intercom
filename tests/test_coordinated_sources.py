"""A recorded component set must replay exactly or fail before checkout."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('coordinated_sources', Path(__file__).resolve().parents[1] / 'scripts/coordinated_sources.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_default_and_exact_manifest():
    refs = module.requested_refs(None, 'dev')
    assert refs['esphome-voip-stack'] == 'dev'
    assert refs['esphome-screenshot'] == 'dev'
    assert refs['esphome-video-renderer'] == 'dev'
    assert refs['esphome-esp-video-camera'] == 'main'
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
