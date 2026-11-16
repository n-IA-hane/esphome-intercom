"""Shared host compiler support, without loading another repository's tests."""

import importlib.util
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def runtime_native_core_headers(request, tmp_path_factory):
    # HA-only lanes do not need ESPHome or this compiler adapter.
    consumers = {"test_primary_voice_controls_runtime.py", "test_lvgl_settings_composition.py"}
    if not any(item.path.name in consumers for item in request.session.items):
        yield
        return
    runtime = Path(os.environ.get(
        "RUNTIME_CONTROLLER_REPO", Path(__file__).resolve().parents[2] / "esphome-runtime-controller"
    ))
    utility = runtime / "tests/native_core_headers.py"
    spec = importlib.util.spec_from_file_location("_runtime_native_core_headers", utility)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = module.write_static_ring_buffer_header(
        tmp_path_factory.mktemp("runtime-native-core-headers")
    )
    previous = os.environ.get("CPLUS_INCLUDE_PATH")
    os.environ["CPLUS_INCLUDE_PATH"] = str(directory) + (os.pathsep + previous if previous else "")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CPLUS_INCLUDE_PATH", None)
        else:
            os.environ["CPLUS_INCLUDE_PATH"] = previous
