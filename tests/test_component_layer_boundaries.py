"""Keep protocol leaves and native-integration compatibility boundaries explicit."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "voip_stack"
pytestmark = pytest.mark.architecture

SDP_DEPENDENCIES = {
    "sdp_document": set(),
    "sdp_types": set(),
    "sdp_parser": {"sdp_document", "sdp_types"},
    "sdp_audio_facts": {"sdp_parser", "sdp_types"},
    "sdp_audio": {"sdp_document", "sdp_parser", "sdp_types", "sdp_audio_facts"},
    "sdp_video": {"sdp_parser", "sdp_types"},
    "sdp_composer": {
        "sdp_document", "sdp_parser", "sdp_types", "sdp_audio", "sdp_video",
        "sdp_audio_facts",
    },
}


@pytest.mark.parametrize("module,allowed", SDP_DEPENDENCIES.items())
def test_sdp_implementations_only_depend_on_lower_protocol_layers(module, allowed):
    tree = ast.parse((ROOT / "core" / f"{module}.py").read_text())
    dependencies = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module is None:
            dependencies.update(item.name for item in node.names if item.name.startswith("sdp"))
        elif node.module.startswith("sdp"):
            dependencies.add(node.module)
    assert dependencies <= allowed
    assert "sdp" not in dependencies


def test_audio_facts_transitive_dependencies_remain_acyclic_protocol_leaves():
    prefix = "custom_components.voip_stack.core."
    allowed = {
        "sdp_audio_facts", "sdp_parser", "sdp_document", "sdp_types",
        "audio_format", "imageattr", "media.codecs",
    }

    def module_path(module):
        path = ROOT / "core" / module.replace(".", "/")
        return path.with_suffix(".py") if path.with_suffix(".py").is_file() else path / "__init__.py"

    def check(module, chain=()):
        assert module in allowed, f"audio facts depend on higher layer: {chain + (module,)}"
        assert module not in chain, f"protocol dependency cycle: {chain + (module,)}"
        package = (prefix + module).rpartition(".")[0]
        for node in ast.walk(ast.parse(module_path(module).read_text())):
            if isinstance(node, ast.Import):
                imports = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), package)
                imports = [
                    f"{base}.{item.name}"
                    if (base == prefix[:-1] or base.startswith(prefix))
                    and module_path(f"{base}.{item.name}"[len(prefix):]).is_file()
                    else base
                    for item in node.names
                ]
            else:
                continue
            for imported in imports:
                if imported.startswith(prefix):
                    check(imported[len(prefix):], chain + (module,))
                else:
                    assert not imported.startswith("custom_components.voip_stack"), imported

    check("sdp_audio_facts")


def test_shared_call_events_do_not_import_the_websocket_command_adapter():
    tree = ast.parse((ROOT / "call_events.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.level == 1
        and (
            node.module == "websocket_api"
            or node.module is None and any(item.name == "websocket_api" for item in node.names)
        )
        for node in ast.walk(tree)
    )


def test_native_service_execution_is_confined_to_the_esphome_adapter():
    unexpected = []
    for path in ROOT.rglob("*.py"):
        if path.name == "esphome_native.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "execute_service":
                    unexpected.append((str(path.relative_to(ROOT)), node.lineno))
    assert not unexpected


def test_saved_dashboard_exports_are_confined_to_the_lovelace_adapter():
    unexpected = []
    for path in ROOT.rglob("*.py"):
        if path.name == "lovelace_adapter.py":
            continue
        if any(
            isinstance(node, ast.Name) and node.id == "LOVELACE_DATA"
            for node in ast.walk(ast.parse(path.read_text()))
        ):
            unexpected.append(str(path.relative_to(ROOT)))
    assert not unexpected
