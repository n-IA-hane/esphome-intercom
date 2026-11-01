"""Qualification fixtures obey native account and Home Assistant boundaries."""

from copy import deepcopy
import os
from types import SimpleNamespace

import pytest

from tests.test_tool_output_storage import load_tool


def test_baresip_codec_is_an_address_parameter_without_losing_other_accounts(monkeypatch, tmp_path):
    module = load_tool("ha_softphone_matrix")
    source = tmp_path / "source"
    source.mkdir()
    (source / "config").write_text("video_source fakevideo,nil\nvideo_display fakevideo,nil\n")
    accounts = (
        "# Preserve this comment with > and ;video_codecs=H264\n"
        '"First <sip:display>" <sip:first@example;transport=tcp;video_codecs=H264>;auth_pass="fixture-one;video_codecs=credential";video_codecs=H264\r\n'
        '"Second" <sip:second@example;transport=udp>;auth_pass=fixture-two\n'
    )
    (source / "accounts").write_bytes(accounts.encode())
    monkeypatch.delenv("VOIP_TEST_BARESIP_MODULE_PATH", raising=False)
    monkeypatch.setattr(module, "TEST_CAPTURE_DIR", tmp_path / "capture")
    monkeypatch.setattr(module.BareSip, "wait_for", lambda *_args: "ready")
    observed = []

    def start(command, **kwargs):
        from pathlib import Path
        observed.append((Path(command[2]) / "accounts").read_bytes().decode())
        return SimpleNamespace()

    monkeypatch.setattr(module.subprocess, "Popen", start)
    peer = module.BareSip(source, video_codec="VP8", wait_for_registration=False)
    try:
        serialized, = observed
        active = serialized.splitlines()[1:]
        assert len(active) == 2
        for line in active:
            uri, address = line.rsplit(">", 1)
            assert "video_codecs" not in uri
            assert address.count(";video_codecs=VP8") == 1
        assert 'auth_pass="fixture-one;video_codecs=credential"' in active[0]
        assert "auth_pass=fixture-two" in active[1]
        assert serialized.splitlines()[0] == accounts.splitlines()[0]
        assert (source / "accounts").read_bytes() == accounts.encode()
    finally:
        os.close(peer.master)
        peer._temporary_config.cleanup()


def form(step, values, flow_id="parent"):
    return {"type": "form", "flow_id": flow_id, "step_id": step,
            "data_schema": [{"name": key, "default": value} for key, value in values.items()]}


class NativeFlows:
    """Public serialized menu/forms, with baseline data kept outside the forms."""

    def __init__(self, *, legacy=False, fail=None):
        self.legacy, self.fail = legacy, fail
        self.requests, self.closed, self.flows = [], [], {}
        self.base = {"sip_port": 15060, "sip_video": True, "assist_endpoint_enabled": True}
        if legacy:
            self.base["trunk_enabled"] = True
        self.video, self.assist = {"video_camera_send": True}, {"assist_extension": "777"}
        self.trunk = {"trunk_server": "127.0.0.1", "trunk_port": 19999,
                      "trunk_inbound_mode": "direct", "automation_routing_enabled": False,
                      "trunk_inbound_default_target": "browser", "trunk_dtmf_timeout_ms": 3,
                      "trunk_dtmf_terminator": "", "trunk_password": "test-password"}
        self.records = [{"subentry_id": f"phone-{i}", "subentry_type": "phone", "title": f"Phone {i}"}
                        for i in range(5)]
        self.options, self.messages = {"baseline": True}, {"mailbox": ["recording-id"]}
        self.parent = {}
        self.original = deepcopy((self.records, self.options, self.messages))
        self.parent_commits = 0

    def get(self, path):
        if path.endswith("/entry"):
            return [{"domain": "voip_stack", "entry_id": "entry", "state": "loaded"}]
        if path == "/api/states":
            return [{"entity_id": "sensor.phone", "state": "idle", "attributes": {"endpoint_id": "browser:phone"}}]
        raise AssertionError(path)

    def ws(self, command):
        if command["type"] == "config_entries/subentries/list":
            return deepcopy(self.records)
        assert command["type"] == "config_entries/subentries/delete"
        identity = command["subentry_id"]
        assert identity == "owned-trunk"
        self.records = [item for item in self.records if item["subentry_id"] != identity]
        if self.parent.get("default_trunk_id") == identity:
            self.parent["default_trunk_id"] = ""
        return None

    def post(self, path, data):
        self.requests.append((path, deepcopy(data)))
        if path.endswith("/flow"):
            if "subentries" in path:
                self.reconfigure = bool(data.get("subentry_id"))
                self.flows["trunk"] = "trunk_user"
                fields = {"name": "", "trunk_register": True, "trunk_enabled": True}
                trunks = [item for item in self.records if item["subentry_type"] == "trunk"]
                if trunks and not (self.reconfigure and len(trunks) == 1
                                   and self.parent.get("default_trunk_id") == "owned-trunk"):
                    fields["make_default"] = False
                return form("user", fields, "trunk")
            self.flows["parent"] = "user" if self.legacy else "menu"
            return form("user", self.base) if self.legacy else {
                "type": "menu", "flow_id": "parent", "step_id": "reconfigure",
                "menu_options": ["user", "parking", "voicemail", "phone_services"]}
        identity = path.rsplit("/", 1)[1]
        step = self.flows[identity]
        if self.fail == step:
            raise RuntimeError("controlled flow failure")
        if step == "menu":
            assert data == {"next_step_id": "user"}
            self.flows[identity] = "user"
            return form("user", self.base)
        if step == "user":
            self.flows[identity] = "video"
            return form("video", self.video)
        if step == "video":
            self.flows[identity] = "assist"
            return form("assist", self.assist)
        if step == "assist":
            if self.legacy:
                self.flows[identity] = "legacy_trunk"
                return form("trunk", self.trunk)
            self.parent_commits += 1
            return {"type": "abort", "reason": "reconfigure_successful"}
        if step == "legacy_trunk":
            self.trunk = dict(data)
            self.flows.pop(identity)
            return {"type": "abort", "reason": "reconfigure_successful"}
        if step == "trunk_user":
            self.owned_name = data["name"]
            assert "make_default" not in data or data["make_default"] is False
            self.flows[identity] = "trunk_form"
            return form("trunk", self.trunk, "trunk")
        assert step == "trunk_form"
        self.trunk = dict(data)
        if not self.reconfigure:
            if not any(item["subentry_type"] == "trunk" for item in self.records):
                self.parent["default_trunk_id"] = "owned-trunk"
            self.records.append({"subentry_id": "owned-trunk", "subentry_type": "trunk", "title": self.owned_name})
        self.flows.pop(identity)
        if self.fail == "after_commit":
            raise RuntimeError("controlled failure after server commit")
        if self.reconfigure:
            return {"type": "abort", "reason": "reconfigure_successful"}
        return {"type": "create_entry", "title": self.owned_name}

    def delete(self, path, **kwargs):
        self.closed.append(path)
        self.flows.pop(path.rsplit("/", 1)[1], None)

    def service(self, *args):
        pass


def test_capture_uses_native_menu_and_aborts_without_committing_preferences():
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows()
    snapshot = module.FlowSnapshot.capture(api)
    assert snapshot.base == api.base and snapshot.video == api.video and snapshot.assist == api.assist
    assert api.parent_commits == 0
    assert not api.flows
    assert api.original == (api.records, api.options, api.messages)


def test_native_trunk_validation_reports_error_codes_without_form_defaults():
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows()
    snapshot = module.FlowSnapshot.capture(api)
    post = api.post
    def invalid_form(path, data):
        if api.flows.get("trunk") == "trunk_form":
            return {**form("trunk", {"trunk_password": "private-default-value"}, "trunk"),
                    "errors": {"trunk_allowed_ips": "trunk_allowed_ips_invalid"}}
        return post(path, data)
    api.post = invalid_form
    with pytest.raises(RuntimeError, match="trunk_allowed_ips_invalid") as failure:
        snapshot.apply(api, trunk_override={"trunk_register": False})
    assert "private-default-value" not in str(failure.value)
    assert "data_schema" not in str(failure.value)
    assert not api.flows
    assert api.original == (api.records, api.options, api.messages)


def test_native_trunk_roundtrip_changes_only_owned_fixture(monkeypatch):
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows()
    snapshot = module.FlowSnapshot.capture(api)
    snapshot.apply(api, mode="direct", automation=True, default_target="sink",
                   trunk_override={"trunk_server": "127.0.0.1", "trunk_password": "local-only"})
    assert len(api.records) == 6
    assert api.trunk["trunk_server"] == "127.0.0.1"
    snapshot.apply(api, mode="dtmf", timeout_seconds=5, terminator="#")
    assert len(api.records) == 6
    assert api.trunk["trunk_inbound_mode"] == "dtmf"
    assert api.trunk["trunk_dtmf_timeout_ms"] == 5
    snapshot.apply(api)
    snapshot.apply(api)
    assert not api.flows
    assert api.parent_commits == 0
    assert api.original == (api.records, api.options, api.messages)


@pytest.mark.parametrize("failure", ["trunk_user", "trunk_form", "after_commit"])
def test_native_trunk_failure_aborts_flow_and_deletes_only_owned_fixture(failure):
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows(fail=failure)
    snapshot = module.FlowSnapshot.capture(api)
    with pytest.raises(RuntimeError, match="controlled"):
        snapshot.apply(api, trunk_override={"trunk_server": "127.0.0.1"})
    snapshot.apply(api)
    assert not api.flows
    assert api.original == (api.records, api.options, api.messages)
    assert api.parent_commits == 0


def test_native_flow_never_edits_a_baseline_trunk():
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows()
    api.records.append({"subentry_id": "baseline-trunk", "subentry_type": "trunk", "title": "Existing provider"})
    original = deepcopy(api.records)
    snapshot = module.FlowSnapshot.capture(api)
    with pytest.raises(RuntimeError, match="test-owned"):
        snapshot.apply(api, mode="direct")
    assert api.records == original
    assert not api.flows


@pytest.mark.parametrize("video,assist", [(False, False), (False, True), (True, False), (True, True)])
def test_capture_stops_before_the_last_native_form(video, assist):
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows()
    api.base.update(sip_video=video, assist_endpoint_enabled=assist)
    # Match the actual dynamic native form transitions.
    original_post = api.post

    def post(path, data):
        result = original_post(path, data)
        if api.flows.get("parent") == "video" and not video:
            if assist:
                api.flows["parent"] = "assist"
                return form("assist", api.assist)
            pytest.fail("Terminal user form was submitted during capture")
        return result

    api.post = post
    captured = module.FlowSnapshot.capture(api)
    assert (captured.video is not None) is video
    assert (captured.assist is not None) is assist
    assert not api.flows and api.parent_commits == 0


def test_supported_legacy_forms_restore_original_values_and_abort_capture(monkeypatch):
    module = load_tool("inbound_routing_qualification")
    monkeypatch.setattr(module.time, "sleep", lambda *_args: None)
    api = NativeFlows(legacy=True)
    original = deepcopy(api.trunk)
    snapshot = module.FlowSnapshot.capture(api)
    assert snapshot.trunk == original and not api.flows
    snapshot.apply(api, mode="dtmf", timeout_seconds=5)
    assert api.trunk["trunk_inbound_mode"] == "dtmf"
    snapshot.apply(api)
    assert api.trunk == original and not api.flows


def test_failed_legacy_apply_aborts_its_owned_flow():
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows(legacy=True)
    snapshot = module.FlowSnapshot.capture(api)
    api.fail = "video"
    with pytest.raises(RuntimeError, match="controlled"):
        snapshot.apply(api, mode="dtmf")
    assert not api.flows


def test_requested_native_controls_cannot_be_silently_ignored():
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows()
    api.trunk.pop("trunk_inbound_mode")
    snapshot = module.FlowSnapshot.capture(api)
    with pytest.raises(RuntimeError, match="requested controls"):
        snapshot.apply(api, mode="dtmf", trunk_override={"trunk_server": "127.0.0.1"})
    assert not api.flows
    assert api.original == (api.records, api.options, api.messages)


@pytest.mark.parametrize("failure", ["menu", "user", "video"])
def test_capture_failure_aborts_without_changing_baseline(failure):
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows(fail=failure)
    with pytest.raises(RuntimeError, match="controlled"):
        module.FlowSnapshot.capture(api)
    assert not api.flows and api.parent_commits == 0
    assert api.original == (api.records, api.options, api.messages)


def test_cleanup_refuses_a_trunk_that_was_renamed_outside_the_runner():
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows()
    snapshot = module.FlowSnapshot.capture(api)
    snapshot.apply(api, trunk_override={"trunk_server": "127.0.0.1"})
    api.records[-1]["title"] = "Changed by another owner"
    with pytest.raises(RuntimeError, match="ownership changed"):
        snapshot.apply(api)
    assert len(api.records) == 6


@pytest.mark.parametrize("baseline_trunk", [False, True])
def test_native_default_is_preserved_or_only_explicitly_normalized_to_empty(baseline_trunk):
    module = load_tool("inbound_routing_qualification")
    api = NativeFlows()
    if baseline_trunk:
        api.records.append({"subentry_id": "provider", "subentry_type": "trunk", "title": "Provider"})
        api.parent = {"default_trunk_id": "provider", "unrelated": "unchanged"}
    before = deepcopy(api.parent)
    snapshot = module.FlowSnapshot.capture(api)
    snapshot.apply(api, trunk_override={"trunk_server": "127.0.0.1"})
    snapshot.apply(api)
    if baseline_trunk:
        assert api.parent == before
    else:
        # Native HA trunk lifecycle selects its first line and clears a removed
        # default. Only this exact absent-to-empty normalization is equivalent.
        assert before == {} and api.parent == {"default_trunk_id": ""}


@pytest.mark.parametrize("fail_restore,fail_runtime", [(False, False), (True, False), (False, True), (True, True)])
def test_matrix_cleanup_attempts_both_owners_and_reports_failures_without_secrets(monkeypatch, fail_restore, fail_runtime):
    from tests.test_ha_real_matrix import load_runner
    runner = load_runner()
    completed = []

    def restore(_api):
        completed.append("restore")
        if fail_restore:
            raise RuntimeError("flow payload with private fixture password")

    def cleanup(_url, _token):
        completed.append("runtime")
        if fail_runtime:
            raise ValueError("private fixture authorization value")

    monkeypatch.setattr(runner, "_cleanup", cleanup)
    failures = runner._restore_matrix_state(object(), SimpleNamespace(apply=restore), "http://127.0.0.1", "fixture")
    assert completed == ["restore", "runtime"]
    assert len(failures) == int(fail_restore) + int(fail_runtime)
    assert all(item["status"] == "failed" for item in failures)
    assert "private fixture" not in str(failures)
