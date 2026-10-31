"""Capture defaults stay disposable while explicit paths and SIP cwd remain usable."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
CASES = [
    ("ha_softphone_matrix", "main", "--out", "out"),
    ("inbound_routing_qualification", "main", "--out", "out"),
    ("live_voip_qualification", "parse_args", "--out-dir", "out_dir"),
    ("qualify_voip_dashboard", "main", "--out", "out"),
    ("jtag_snapshots", "main", "--out-dir", "out_dir"),
    ("ring_group_live_matrix", "_parse_args", "--out", "out"),
    ("local_softphone_live_matrix", "_parse_args", "--out", "out"),
    ("ha_softphone_card_trace", "main", "--out", "out"),
]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"output_storage_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_without_running(monkeypatch, module, entry, arguments):
    # Stop at the actual parser, before auth, browser, network or debugger setup.
    original = argparse.ArgumentParser.parse_args
    parsed = []

    class Parsed(Exception):
        pass

    def capture(parser, args=None, namespace=None):
        parsed.append(original(parser, arguments, namespace))
        raise Parsed

    with monkeypatch.context() as context:
        context.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(Parsed):
            getattr(module, entry)()
    return parsed[0]


@pytest.mark.parametrize("name,entry,option,field", CASES)
def test_capture_defaults_ignore_cwd_and_build_tmpdir(monkeypatch, tmp_path, name, entry, option, field):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", "/var/tmp/voip-builds/storage-test-sentinel")
    monkeypatch.delenv("RING_GROUP_MATRIX_OUT", raising=False)
    monkeypatch.delenv("LOCAL_SOFTPHONE_MATRIX_OUT", raising=False)
    args = parse_without_running(monkeypatch, load_tool(name), entry, [])
    output = Path(getattr(args, field))
    assert output.is_absolute() and output.is_relative_to(Path("/tmp/voip-tests"))
    assert not (tmp_path / "test_captures").exists()


@pytest.mark.parametrize("name,entry,option,field", CASES)
def test_explicit_capture_path_remains_unchanged(monkeypatch, tmp_path, name, entry, option, field):
    chosen = tmp_path / "chosen report path" / "result"
    args = parse_without_running(monkeypatch, load_tool(name), entry, [option, str(chosen)])
    assert Path(getattr(args, field)) == chosen


@pytest.mark.parametrize("name,variable", [
    ("ring_group_live_matrix", "RING_GROUP_MATRIX_OUT"),
    ("local_softphone_live_matrix", "LOCAL_SOFTPHONE_MATRIX_OUT"),
])
def test_matrix_environment_output_override_remains_available(monkeypatch, tmp_path, name, variable):
    chosen = tmp_path / "custom-report.json"
    monkeypatch.setenv(variable, str(chosen))
    args = parse_without_running(monkeypatch, load_tool(name), "_parse_args", [])
    assert Path(args.out) == chosen


@pytest.mark.parametrize("name,arguments", [
    ("ha_softphone_matrix", {"wait_for_registration": False}),
    ("inbound_routing_qualification", {"wait_registered": False}),
])
def test_baresip_explicit_capture_cwd_and_relative_wav_stay_coherent(monkeypatch, tmp_path, name, arguments):
    from tools.ha_voip_lab.phone_services_hil import baresip_audio_source_path

    module = load_tool(name)
    capture = tmp_path / "isolated-capture"
    monkeypatch.setattr(module, "TEST_CAPTURE_DIR", capture)
    monkeypatch.delenv("VOIP_TEST_BARESIP_MODULE_PATH", raising=False)
    monkeypatch.setattr(module.BareSip, "wait_for", lambda *args: "ready")
    calls = []

    def start(command, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(module.subprocess, "Popen", start)
    peer = module.BareSip(tmp_path / "peer-config", **arguments)
    try:
        assert calls[0]["cwd"] == capture
        assert capture.stat().st_mode & 0o777 == 0o700
        voice = capture / "peer-source.wav"
        voice.write_bytes(b"fixture")
        source = baresip_audio_source_path(voice, capture)
        assert (Path(calls[0]["cwd"]) / source).resolve() == voice.resolve()
        assert len(os.fsencode(source)) < 128
    finally:
        os.close(peer.master)


@pytest.mark.parametrize("name,entry,required", [
    ("run_sip_extensions_lab", "parse_args", []),
    ("run_answered_sipp_lab", "parse_args", []),
    ("run_ha_real_matrix", "parse_args", []),
    ("run_sip_transport_lab", "_parse_args", ["--ca-cert", "ca", "--server-cert", "cert", "--server-key", "key"]),
    ("run_sipp_rfc_lab", "parse_args", []),
])
@pytest.mark.parametrize("explicit", [False, True])
def test_protocol_lab_output_parser_preserves_storage_boundary(monkeypatch, tmp_path, name, entry, required, explicit):
    import ast

    # Exercise the maintained parser itself without importing live protocol
    # servers or requiring Home Assistant to test a filesystem argument.
    path = ROOT / "scripts" / f"{name}.py"
    tree = ast.parse(path.read_text())
    parser = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == entry)
    scope = {"argparse": argparse, "Path": Path, "ROOT": ROOT,
             "CAPTURE_ROOT": Path("/tmp/voip-tests"), "__doc__": "parser storage contract"}
    exec(compile(ast.Module(body=[parser], type_ignores=[]), str(path), "exec"), scope)
    chosen = tmp_path / "custom-protocol-output"
    args = [name, *required, *(["--out-dir", str(chosen)] if explicit else [])]
    monkeypatch.setattr(sys, "argv", args)
    result = scope[entry]()
    assert result.out_dir == chosen if explicit else result.out_dir.is_relative_to(Path("/tmp/voip-tests"))
