"""Static guard for the authoritative inbound answer boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_MODULES = (
    "call_forwarder.py",
    "ring_group_orchestrator.py",
    "softphone_answer.py",
    "trunk_inbound_router.py",
    "inbound_routing/bridge.py",
)


def test_application_answers_use_the_transaction_owner() -> None:
    violations: list[str] = []
    component = ROOT / "custom_components" / "voip_stack"
    for relative in APPLICATION_MODULES:
        path = component / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else ""
            )
            if "send_final_response" not in name:
                continue
            if any(
                isinstance(argument, ast.Constant) and argument.value == 200
                for argument in node.args
            ):
                violations.append(f"{relative}:{node.lineno}")

    assert not violations, "direct 2xx answer bypasses: " + ", ".join(violations)
