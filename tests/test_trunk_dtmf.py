"""Behavioral tests for bounded inbound trunk DTMF collection."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "trunk_dtmf.py"


@pytest.fixture
def trunk_dtmf(monkeypatch):
    package_name = "voip_stack_trunk_dtmf_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    core_package = types.ModuleType(f"{package_name}.core")
    core_package.__path__ = []
    monkeypatch.setitem(sys.modules, core_package.__name__, core_package)
    sdp = types.ModuleType(f"{package_name}.core.sdp")
    sdp.offered_dtmf_formats = lambda _offer: []
    monkeypatch.setitem(sys.modules, sdp.__name__, sdp)

    dtmf = types.ModuleType(f"{package_name}.dtmf")
    dtmf.DtmfCollector = object

    async def _empty_info(*_args, **_kwargs):
        return "", ""

    dtmf.collect_info_digits = _empty_info
    monkeypatch.setitem(sys.modules, dtmf.__name__, dtmf)

    module_name = f"{package_name}.trunk_dtmf"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _invite():
    return SimpleNamespace(
        call_id="call-1",
        remote_sdp="offer",
        remote_rtp_host="10.0.0.2",
    )


def test_info_only_returns_exact_route(trunk_dtmf) -> None:
    async def collect_info(*_args, **_kwargs):
        return "667", "Test"

    trunk_dtmf.collect_info_digits = collect_info

    result = asyncio.run(
        trunk_dtmf.collect_trunk_dtmf(
            _invite(),
            info_queue=asyncio.Queue(),
            source_rtp_port=40000,
            routes={"667": "Test"},
            timeout=1.0,
        )
    )

    assert result == trunk_dtmf.TrunkDtmfSelection("667", "Test")


def test_rfc4733_winner_cancels_sip_info_collector(trunk_dtmf) -> None:
    info_cancelled = False

    async def collect_info(*_args, **_kwargs):
        nonlocal info_cancelled
        try:
            await asyncio.Event().wait()
        finally:
            info_cancelled = True

    class Collector:
        def __init__(self, **kwargs):
            assert kwargs["payload_type"] == 101
            assert kwargs["remote_host"] == "10.0.0.2"

        async def collect(self):
            return "418", "Casa"

    trunk_dtmf.collect_info_digits = collect_info
    trunk_dtmf.DtmfCollector = Collector
    trunk_dtmf.sdp.offered_dtmf_formats = lambda _offer: [
        SimpleNamespace(payload_type=101)
    ]

    result = asyncio.run(
        trunk_dtmf.collect_trunk_dtmf(
            _invite(),
            info_queue=asyncio.Queue(),
            source_rtp_port=40000,
            routes={"418": "Casa"},
            timeout=1.0,
            terminator="#",
        )
    )

    assert result == trunk_dtmf.TrunkDtmfSelection("418", "Casa")
    assert info_cancelled is True


def test_failed_channels_return_no_selection(trunk_dtmf) -> None:
    async def failed_info(*_args, **_kwargs):
        raise OSError("INFO unavailable")

    class FailedCollector:
        def __init__(self, **_kwargs):
            pass

        async def collect(self):
            raise OSError("RTP unavailable")

    trunk_dtmf.collect_info_digits = failed_info
    trunk_dtmf.DtmfCollector = FailedCollector
    trunk_dtmf.sdp.offered_dtmf_formats = lambda _offer: [
        SimpleNamespace(payload_type=101)
    ]

    result = asyncio.run(
        trunk_dtmf.collect_trunk_dtmf(
            _invite(),
            info_queue=asyncio.Queue(),
            source_rtp_port=40000,
            routes={},
            timeout=1.0,
        )
    )

    assert result == trunk_dtmf.TrunkDtmfSelection()
