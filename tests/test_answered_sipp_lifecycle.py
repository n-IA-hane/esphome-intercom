"""Owned fake resources exercise failure cleanup without starting SIP peers."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


class FakePipe:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.error = None

    def close(self):
        self.events.append("pipe.close")
        self.closed = True
        if self.error is not None:
            raise self.error


class FakeCaller:
    def __init__(self, events):
        self.events = events
        self.stdout = FakePipe(events)
        self.outcomes = []
        self.running = True
        self.reaped = False
        self.returncode = None
        self.terminate_error = None
        self.kill_error = None
        self.signalled = 0

    def poll(self):
        return None if self.running else self.returncode

    def terminate(self):
        self.events.append("caller.terminate")
        if self.terminate_error is not None:
            raise self.terminate_error
        self.signalled = 15

    def kill(self):
        self.events.append("caller.kill")
        if self.kill_error is not None:
            raise self.kill_error
        self.signalled = 9

    def communicate(self, *, timeout):
        self.events.append(("caller.communicate", timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else ("call complete", None)
        if isinstance(outcome, BaseException):
            raise outcome
        self.running = False
        self.reaped = True
        self.returncode = -self.signalled if self.signalled else 0
        return outcome


class FakeCallee:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.wait_error = None
        self.close_error = None
        self.incoming = False
        self.waits = 0

    def wait_for_any(self, states, timeout):
        self.events.append(("callee.wait", states, timeout))
        self.waits += 1
        if self.wait_error is not None:
            raise self.wait_error
        return "Incoming call" if self.incoming and self.waits == 1 else "Call established"

    def wait_for(self, text, timeout):
        self.events.append(("callee.wait_for", text, timeout))
        return text

    def command(self, command):
        self.events.append(("callee.command", command))

    def hangup(self):
        self.events.append("callee.hangup")

    def close(self):
        self.events.append("callee.close")
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def case(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("answered_sipp_lifecycle", ROOT / "scripts/run_answered_sipp_lab.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    events = []
    callee, caller = FakeCallee(events), FakeCaller(events)

    def acquire_callee(config, *, headless_audio):
        assert config == tmp_path / "private-config"
        assert headless_audio is True
        events.append("callee.acquired")
        return callee

    def acquire_caller(command, **kwargs):
        assert command[:2] == ["sipp", "127.0.0.1:15060"]
        assert kwargs["cwd"] == tmp_path
        assert kwargs["text"] is True
        events.append("caller.acquired")
        return caller

    async def quiescence(url, token):
        assert url == "http://127.0.0.1:18123" and token == "test-only"
        events.append("quiescence")
        return {"state": "idle", "active_dialogs": 0, "call_scoped_quiescent": True}

    monkeypatch.setattr(runner, "BareSip", acquire_callee)
    monkeypatch.setattr(runner, "subprocess", SimpleNamespace(
        Popen=acquire_caller, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT,
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    monkeypatch.setattr(runner, "runtime_quiescence", quiescence)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    kwargs = {"target_host": "127.0.0.1", "target_port": 15060, "extension": "2502",
              "local_port": 16062, "callee_config": tmp_path / "private-config",
              "capture_dir": tmp_path, "ha_url": "http://127.0.0.1:18123", "token": "test-only"}
    return SimpleNamespace(runner=runner, callee=callee, caller=caller, events=events, kwargs=kwargs)


def run_case(case, name="caller_bye"):
    return case.runner.run_answered_case(name, Path("scenario.xml"), **case.kwargs)


def assert_closed(case):
    assert case.callee.closed
    assert case.caller.stdout.closed
    assert case.events[-1] == "callee.close"


@pytest.mark.parametrize("name", ["caller_bye", "callee_bye"])
def test_successful_case_keeps_call_flow_and_closes_resources(case, name):
    case.callee.incoming = True
    result = run_case(case, name)
    assert result["status"] == "passed"
    assert result["cleanup"]["call_scoped_quiescent"] is True
    assert ("callee.command", "/accept") in case.events
    assert ("callee.hangup" in case.events) is (name == "callee_bye")
    assert case.caller.reaped
    assert "caller.terminate" not in case.events
    assert "caller.kill" not in case.events
    assert_closed(case)


def test_popen_failure_closes_already_acquired_callee(case, monkeypatch):
    def fail_popen(*_args, **_kwargs):
        case.events.append("caller.spawn_failed")
        raise FileNotFoundError("isolated missing SIPp")

    monkeypatch.setattr(case.runner.subprocess, "Popen", fail_popen)
    with pytest.raises(FileNotFoundError, match="isolated missing SIPp"):
        run_case(case)
    assert case.events == ["callee.acquired", "caller.spawn_failed", "callee.close"]


def test_constructor_failure_does_not_attempt_caller_acquisition(case, monkeypatch):
    def fail_callee(*_args, **_kwargs):
        raise RuntimeError("callee constructor owns its failure cleanup")

    monkeypatch.setattr(case.runner, "BareSip", fail_callee)
    with pytest.raises(RuntimeError, match="constructor"):
        run_case(case)
    assert case.events == []


def test_callee_failure_terminates_and_reaps_only_acquired_caller(case):
    case.callee.wait_error = RuntimeError("callee wait failed")
    with pytest.raises(RuntimeError, match="callee wait failed"):
        run_case(case)
    assert case.events[-4:] == ["caller.terminate", ("caller.communicate", 3), "pipe.close", "callee.close"]
    assert case.caller.reaped
    assert "caller.kill" not in case.events
    assert_closed(case)


def test_communicate_timeout_escalates_to_kill_and_reaps_before_callee_close(case):
    primary = subprocess.TimeoutExpired("sipp", 12, output="partial trace")
    case.caller.outcomes = [primary, subprocess.TimeoutExpired("sipp", 3), ("final trace", None)]
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_case(case)
    assert raised.value is primary
    assert case.events[-6:] == ["caller.terminate", ("caller.communicate", 3), "caller.kill",
                               ("caller.communicate", 3), "pipe.close", "callee.close"]
    assert case.caller.reaped and case.caller.returncode == -9
    assert_closed(case)


def test_terminate_error_still_finishes_communication_and_closes_callee(case):
    case.callee.wait_error = RuntimeError("case failed")
    case.caller.terminate_error = OSError("terminate failed")
    with pytest.raises(OSError, match="terminate failed"):
        run_case(case)
    assert case.caller.reaped
    assert_closed(case)


def test_kill_error_still_attempts_reap_and_closes_callee(case):
    case.callee.wait_error = RuntimeError("case failed")
    case.caller.outcomes = [subprocess.TimeoutExpired("sipp", 3), ("reaped after race", None)]
    case.caller.kill_error = ProcessLookupError("already exited")
    with pytest.raises(ProcessLookupError, match="already exited"):
        run_case(case)
    assert case.caller.reaped
    assert_closed(case)


def test_reap_error_cannot_skip_callee_close(case):
    case.callee.wait_error = RuntimeError("case failed")
    case.caller.outcomes = [subprocess.TimeoutExpired("sipp", 3), OSError("reap failed")]
    with pytest.raises(OSError, match="reap failed"):
        run_case(case)
    assert "caller.kill" in case.events
    assert_closed(case)


def test_pipe_error_cannot_skip_callee_close(case):
    case.caller.stdout.error = OSError("pipe close failed")
    with pytest.raises(OSError, match="pipe close failed"):
        run_case(case)
    assert_closed(case)


def test_callee_cleanup_failure_cannot_return_a_pass(case):
    case.callee.close_error = RuntimeError("callee cleanup failed")
    with pytest.raises(RuntimeError, match="callee cleanup failed"):
        run_case(case)
    assert case.caller.reaped
    assert_closed(case)


def configure_main(case, monkeypatch, tmp_path):
    args = argparse.Namespace(host="127.0.0.1", port=15060, extension="2502",
                              ha_url="http://127.0.0.1:18123", credentials=tmp_path / "unused",
                              callee_config=tmp_path / "private-config", out_dir=tmp_path / "results",
                              quiescence_only=False)
    monkeypatch.setattr(case.runner, "parse_args", lambda: args)
    monkeypatch.setattr(case.runner, "lab_token", lambda *_args: "test-only")
    monkeypatch.setattr(case.runner, "candidate_revision", lambda: {"commit": "isolated-fixture"})
    return args


@pytest.mark.parametrize("raises", [False, True])
def test_first_failure_preserves_summary_and_never_starts_second_case(case, monkeypatch, tmp_path, raises):
    args = configure_main(case, monkeypatch, tmp_path)
    calls = []

    def fail_first(name, _scenario, **_kwargs):
        calls.append(name)
        assert name == "caller_bye", "A later live case must not run after failure"
        if raises:
            raise RuntimeError("original failing witness")
        return {"scenario": name, "status": "failed", "error": "original failing witness"}

    monkeypatch.setattr(case.runner, "run_answered_case", fail_first)
    assert case.runner.main() == 1
    artifact = json.loads((args.out_dir / "summary.json").read_text())
    assert calls == ["caller_bye"]
    assert artifact["results"][0]["error"] == "original failing witness"
    assert artifact["results"][1] == {"scenario": "callee_bye", "status": "not_run",
                                      "reason": "previous_case_failed", "blocked_by": "caller_bye"}
    assert artifact["aborted_after"] == "caller_bye"


@pytest.mark.parametrize("second_fails", [False, True])
def test_summary_preserves_preceding_pass_and_reports_overall_exit(case, monkeypatch, tmp_path, second_fails):
    args = configure_main(case, monkeypatch, tmp_path)
    calls = []

    def answered(name, _scenario, **_kwargs):
        calls.append(name)
        if second_fails and name == "callee_bye":
            raise RuntimeError("second case failed")
        return {"scenario": name, "status": "passed", "cleanup": {"call_scoped_quiescent": True}}

    monkeypatch.setattr(case.runner, "run_answered_case", answered)
    assert case.runner.main() == (1 if second_fails else 0)
    artifact = json.loads((args.out_dir / "summary.json").read_text())
    assert calls == ["caller_bye", "callee_bye"]
    assert artifact["results"][0]["status"] == "passed"
    assert artifact["results"][1]["status"] == ("failed" if second_fails else "passed")
    assert artifact["aborted_after"] == ("callee_bye" if second_fails else None)
