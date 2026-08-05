"""Architecture boundary for Home Assistant independent protocol code."""

from __future__ import annotations

import ast
from pathlib import Path


CORE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "voip_stack"
    / "core"
)


def test_core_does_not_import_home_assistant() -> None:
    for path in CORE.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names = (node.module or "",)
            else:
                continue
            assert all(
                name != "homeassistant" and not name.startswith("homeassistant.")
                for name in names
            ), f"{path.name} imports Home Assistant"
