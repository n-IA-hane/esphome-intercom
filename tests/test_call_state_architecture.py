"""Prevent call-state writes and ownership decisions from leaking into views."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'voip_stack'
OWNERS = {'endpoint_session.py', 'pbx_runtime.py', 'call_registry.py'}


def test_call_lifecycle_fields_are_written_only_by_their_owners():
    violations = []
    for path in ROOT.glob('*.py'):
        if path.name in OWNERS:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Store):
                continue
            target = node.value.id if isinstance(node.value, ast.Name) else ''
            lifecycle = node.attr == 'phase' or (node.attr == '_state' and path.name != 'condition.py')
            participant = node.attr == 'state' and (target.endswith('_leg') or target in {
                'session', 'leg', 'original', 'consultation', 'goal',
            })
            if lifecycle or participant:
                violations.append(f'{path.name}:{node.lineno}: {ast.unparse(node)}')
    assert not violations, '\n'.join(violations)


def test_publishing_and_reading_events_cannot_commit_a_call():
    forbidden = {'transition', 'upsert', 'observe_leg', 'observe_external_state',
                 'ensure_session', 'release_endpoint_claim', 'apply_observation'}
    targets = {'call_registry.py': 'event_fields', 'call_projection.py': 'publish_call_projection',
               'call_events.py': 'fire_call_event'}
    for file, function in targets.items():
        tree = ast.parse((ROOT / file).read_text())
        body = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function)
        writes = [node.func.attr for node in ast.walk(body)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in forbidden]
        assert not writes, (file, function, writes)


def test_phone_observer_does_not_require_a_presentation_event_to_read_state():
    tree = ast.parse((ROOT / 'phone_state.py').read_text())
    start = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_start')
    assert not any(isinstance(node, ast.Name) and node.id == 'CALL_EVENT' for node in ast.walk(start))
