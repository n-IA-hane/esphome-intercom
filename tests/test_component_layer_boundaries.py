"""Keep protocol leaves and native-integration compatibility boundaries explicit."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "voip_stack"
pytestmark = pytest.mark.architecture

SDP_DEPENDENCIES = {
    "sdp_document": set(),
    "sdp_types": set(),
    "sdp_parser": {"sdp_document", "sdp_types"},
    "sdp_audio": {"sdp_document", "sdp_parser", "sdp_types"},
    "sdp_video": {"sdp_parser", "sdp_types"},
    "sdp_composer": {
        "sdp_document", "sdp_parser", "sdp_types", "sdp_audio", "sdp_video",
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
