"""A recorded component set must replay exactly or fail before checkout."""
import importlib.util
import json
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


def test_ci_component_refs_cover_incremental_preview_tags():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text())
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
