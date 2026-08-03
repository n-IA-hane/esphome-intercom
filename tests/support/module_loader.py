"""Minimal loader for production modules without importing Home Assistant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "custom_components.voip_stack"
PACKAGE_ROOT = ROOT / "custom_components" / "voip_stack"


def load_voip_stack_module(name: str):
    """Load one production module and its normal relative imports."""
    if "custom_components" not in sys.modules:
        root_package = types.ModuleType("custom_components")
        root_package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_package
    if PACKAGE_NAME not in sys.modules:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_ROOT)]
        sys.modules[PACKAGE_NAME] = package

    full_name = f"{PACKAGE_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(
        full_name,
        PACKAGE_ROOT / f"{name}.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module
