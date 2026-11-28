"""Native ESPHome scheduling lets package YAML override the official MWW pin."""

import ast
from pathlib import Path

from esphome import yaml_util
from esphome.components import esp32
from esphome.core import CORE
import pytest

from scripts.official_component_fixtures import verified_component_directory


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("with_override, expected_tflite", [(False, "1.4.1"), (True, "1.4.3")])
def test_native_yaml_final_priority_overrides_only_tflite(with_override, expected_tflite):
    # Execute the provider's actual dependency declarations without running model
    # downloads or unrelated codegen. Hash verification keeps these statements
    # tied to the selected official MWW revision.
    source = verified_component_directory("micro_wake_word") / "__init__.py"
    tree = ast.parse(source.read_text())
    to_code = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "to_code")
    declarations = [
        node for node in to_code.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "esp32"
        and node.value.func.attr == "add_idf_component"
    ]
    assert len(declarations) == 3
    provider_dependencies = compile(ast.Module(body=declarations, type_ignores=[]), str(source), "exec")
    package = yaml_util.load_yaml(ROOT / "packages/features/wake_word/main.yaml")
    framework = esp32.FRAMEWORK_SCHEMA(package["esp32"]["framework"])

    async def add_official_provider_dependencies():
        exec(provider_dependencies, {"esp32": esp32})

    CORE.reset()
    try:
        registry = {}
        CORE.data[esp32.KEY_ESP32] = {esp32.KEY_COMPONENTS: registry}
        # Queue YAML first to prove priority, rather than insertion order, wins.
        if with_override:
            CORE.add_job(esp32._add_yaml_idf_components, framework["components"])
        CORE.add_job(add_official_provider_dependencies)
        CORE.flush_tasks()
        assert registry == {
            "espressif/esp-tflite-micro": {"repo": None, "ref": expected_tflite, "path": None},
            "espressif/esp-nn": {"repo": None, "ref": "1.5.0", "path": None},
            "esphome/esp-micro-speech-features": {"repo": None, "ref": "1.2.3", "path": None},
        }
    finally:
        CORE.reset()
