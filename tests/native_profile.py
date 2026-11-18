"""Read shipped profiles through native ESPHome composition, once per test process."""
from functools import lru_cache
from pathlib import Path
import subprocess
import sys
import tempfile

from esphome import yaml_util
import yaml

from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]


class PackageLoader(yaml.SafeLoader):
    pass


def _tagged(loader, _tag, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


PackageLoader.add_multi_constructor('!', _tagged)


@lru_cache(maxsize=None)
def profile(path: str | Path, **substitutions) -> dict:
    path = Path(path)
    if path.is_absolute():
        path = path.relative_to(ROOT)
    if not str(path).startswith('yamls/'):
        path = Path('yamls') / path
    original = yaml_util.load_yaml(ROOT / path)
    with tempfile.TemporaryDirectory(prefix='voip-native-contract-') as directory:
        config = device_config(Path(directory), original['substitutions']['firmware_features'], str(path),
                               overrides=substitutions)
        data = yaml_util.load_yaml(config)
        data.update({k: v for k, v in original.items() if k not in ('substitutions', 'packages')})
        config.write_text(yaml_util.dump(data))
        result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                                capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'Could not resolve substitution' not in result.stderr, result.stderr
        return yaml.load(result.stdout, Loader=PackageLoader)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


def by_id(value, name):
    matches = [node for node in nodes(value) if node.get('id') == name]
    assert matches, f'Missing {name}'
    return matches[0]


def action_id(action, kind):
    value = action.get(kind)
    return value.get('id') if isinstance(value, dict) else value


def text(value):
    """Human values/code, independent of scalar quoting and list indentation."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return '\n'.join(str(k) + '\n' + text(v) for k, v in value.items())
    if isinstance(value, list):
        return '\n'.join(text(v) for v in value)
    return str(value)
