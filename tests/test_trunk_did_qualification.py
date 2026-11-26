"""Configured direct-DID producers do not claim retired menu evidence."""
import json

import pytest

from tests.test_ha_real_matrix import load_dtmf_runner


def test_native_trunk_preflight_aborts_without_saving_or_legacy_options():
    runner = load_dtmf_runner()
    calls = []
    class Api:
        def post(self, url, data):
            calls.append((url, data))
            fields = ({"trunk_enabled": True, "trunk_register": True, "name": "Fusion"}
                if len(calls) == 1 else {"automation_routing_enabled": False, "trunk_inbound_default_target": "Welcome"})
            return {"flow_id": "inspect", "step_id": "user" if len(calls) == 1 else "trunk",
                "data_schema": [{"name": key, "default": value} for key, value in fields.items()]}
        def delete(self, url):
            calls.append(("DELETE", url))
    result = runner._read_trunk_policy(Api(), {"entry_id": "entry", "subentry_id": "trunk"})
    assert result["automation_routing_enabled"] is False
    assert len(calls) == 3 and calls[-1] == ("DELETE", "/api/config/config_entries/subentries/flow/inspect")
    assert not any(runner.RETIRED_MENU_FIELDS.intersection(data) for _, data in calls if isinstance(data, dict))
    assert calls[1][1]["trunk_enabled"] is True
    assert "trunk_inbound_default_target" not in calls[1][1]


@pytest.mark.parametrize("field,value", [("automation_routing_enabled", True), ("trunk_inbound_mode", "dtmf")])
def test_native_preflight_blocks_unsupported_policy_and_aborts(field, value):
    runner = load_dtmf_runner()
    class Api:
        count = 0
        closed = False
        def post(self, url, data):
            self.count += 1
            fields = {"trunk_enabled": True} if self.count == 1 else {field: value}
            return {"flow_id": "inspect", "step_id": "user" if self.count == 1 else "trunk",
                "data_schema": [{"name": k, "default": v} for k, v in fields.items()]}
        def delete(self, url):
            self.closed = True
    api = Api()
    with pytest.raises(RuntimeError):
        runner._read_trunk_policy(api, {"entry_id": "entry", "subentry_id": "trunk"})
    assert api.closed


@pytest.mark.parametrize("called_number,override,passes", [("401", False, True), ("9999", False, False), ("401", True, False)])
def test_case_dials_exact_configured_did_without_postanswer_digits(monkeypatch, tmp_path, called_number, override, passes):
    runner = load_dtmf_runner()
    actions = []
    class Caller:
        def __init__(self, config, **kwargs):
            pass
        def dial(self, target):
            actions.append(("dial", target))
        def close(self):
            actions.append(("close",))
    class Trace:
        def __init__(self, api):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def for_call(self, call_id):
            return [{"call_id": call_id, "called_number": called_number, "inbound_trunk_id": "fusion",
                "event_type": "route_requested" if override else "ringing"}]
    monkeypatch.setattr(runner, "_require_wire", lambda *args: {"caller": {}, "callee": {}})
    monkeypatch.setattr(runner, "BareSip", Caller)
    monkeypatch.setattr(runner, "EventTrace", Trace)
    monkeypatch.setattr(runner, "wait_event_state", lambda *args, **kwargs: {"call_id": "call"})
    case = {"name": runner.SCENARIO_IDS[0], "dial_target": "sip:401@fusion.test", "called_number": "401", "callee": "Office"}
    if passes:
        assert runner._run_case(None, config=tmp_path, case=case, trunk_id="fusion")["status"] == "passed"
    else:
        with pytest.raises(RuntimeError):
            runner._run_case(None, config=tmp_path, case=case, trunk_id="fusion")
    assert actions == [("dial", "sip:401@fusion.test"), ("close",)]


def test_missing_configured_path_writes_blocked_without_claiming_pass(monkeypatch, tmp_path):
    runner = load_dtmf_runner()
    output = tmp_path / "blocked.json"
    monkeypatch.setattr(runner.sys, "argv", ["run_trunk_did_lab.py", "--profile", str(tmp_path / "missing"), "--out", str(output)])
    monkeypatch.setattr(runner, "candidate_revision", lambda: {})
    assert runner.main() == 1
    results = json.loads(output.read_text())["results"]
    assert results and all(row["status"] == "blocked" for row in results)


def test_wire_proof_requires_both_current_dialog_ids_and_exact_invite_targets(tmp_path):
    runner = load_dtmf_runner()
    caller, callee = tmp_path / "caller.log", tmp_path / "callee.log"
    caller.write_text("INVITE sip:401@fusion.test SIP/2.0\r\nCall-ID: source-now\r\n\r\n")
    callee.write_text("INVITE sip:office@ha.test SIP/2.0\r\nCall-ID: destination-now\r\n\r\n")
    case = {"wire": {"caller_trace": str(caller), "callee_trace": str(callee),
        "caller_request_uri": "sip:401@fusion.test", "callee_request_uri": "sip:office@ha.test"}}
    observed = [{"dest_call_id": "destination-now"}]
    assert runner._require_wire(case, "source-now", observed)
    assert runner._require_wire(case, "older-source", observed) is None
    assert runner._require_wire(case, "source-now", [{"dest_call_id": "older-destination"}]) is None
    case["wire"]["caller_request_uri"] = "sip:9999@fusion.test"
    assert runner._require_wire(case, "source-now", observed) is None
