"""NAT qualification owns its topology without adopting host/user resources."""

import json
import signal
from types import SimpleNamespace

import pytest

from tools.ha_voip_lab.sip_nat_lab import CLIENT_IP, NAT_IP, SERVER_IP, SipNatLab, private_json, validate_registration_captures


def test_invalid_mtu_is_rejected_before_network_ownership(tmp_path):
    with pytest.raises(ValueError, match="MTU"):
        SipNatLab(tmp_path, mtu=9000)


@pytest.mark.parametrize("change", [None, "client_socket", "client_contact", "no_external_rebind"])
def test_nat_rebind_oracle_requires_an_unchanged_real_client_flow(tmp_path, monkeypatch, change):
    def rows(side):
        source = CLIENT_IP if side == "rc0" else NAT_IP
        lines = []
        for index in (1, 2):
            port = "20600" if side == "rc0" or index == 1 or change == "no_external_rebind" else "42000"
            if side == "rc0" and index == 2 and change == "client_socket":
                port = "20602"
            contact_port = "20602" if index == 2 and change == "client_contact" else "20600"
            lines.append([source, SERVER_IP, port, "5060", "", "REGISTER", "", "udp-flow", str(index),
                f"<sip:7101@{CLIENT_IP}:{contact_port}>", "60", "", ""])
        lines.append([source, SERVER_IP, "", "", "50000", "REGISTER", "", "tcp-flow", "1",
            f"<sip:7102@{CLIENT_IP}:50000>", "60", "", ""])
        lines.append([SERVER_IP, source, "5060", "42000", "", "", "200", "udp-flow", "2", "", "60", NAT_IP, "42000"])
        return "\n".join("\t".join(items) for items in lines) + "\n"

    def run(command, **_kwargs):
        return SimpleNamespace(stdout="" if command[0] == "capinfos" else rows("rc0" if "rc0.pcap" in command[2] else "rs0"))

    monkeypatch.setattr("subprocess.run", run)
    if change is None:
        validate_registration_captures(tmp_path, require_rebind=True)
        record = json.loads((tmp_path / "native-registration-integrity.json").read_text())
        assert record["udp_rebindings"] == [{"private_source_port": "20600",
            "translated_source_ports": ["20600", "42000"], "same_private_contact": True}]
    else:
        with pytest.raises(RuntimeError, match="socket or Contact|No real NAT"):
            validate_registration_captures(tmp_path, require_rebind=True)


def test_existing_namespace_is_never_adopted_or_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 0)
    lab = SipNatLab(tmp_path / "fresh", identity="owned")
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="sipq-owned-client (id: 1)\nuser-lab\n")

    monkeypatch.setattr(lab, "run", run)
    with pytest.raises(RuntimeError, match="already exists"):
        lab.__enter__()
    assert commands == [["ip", "netns", "list"]]
    assert lab.created == []


def test_existing_evidence_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 0)
    path = tmp_path / "existing.json"
    path.write_text("preserved")
    with pytest.raises(FileExistsError):
        private_json(path, {"replacement": True})
    assert path.read_text() == "preserved"
    with pytest.raises(FileExistsError):
        SipNatLab(tmp_path).__enter__()


def test_traffic_counters_do_not_hide_or_invent_host_configuration_changes(tmp_path, monkeypatch):
    lab = SipNatLab(tmp_path)
    packets = 1
    verdict = "accept"

    def run(command, **_kwargs):
        value = [{"ifname": "eth0"}] if command[0] == "ip" else {
            "nftables": [{"rule": {"expr": [{"counter": {"packets": packets, "bytes": packets * 64}}, {verdict: None}]}}],
        }
        return SimpleNamespace(stdout=json.dumps(value))

    monkeypatch.setattr(lab, "run", run)
    before = lab.host_network()
    packets = 99
    assert lab.host_network() == before
    verdict = "drop"
    assert lab.host_network() != before


def test_actor_children_are_stopped_after_the_leader_exits(tmp_path, monkeypatch):
    lab = SipNatLab(tmp_path)
    children = [1234]
    stopped = []
    process = SimpleNamespace(pid=234, poll=lambda: 0, wait=lambda **_kwargs: 0)
    monkeypatch.setattr(lab, "_group_members", lambda _role, _group: children.copy())

    def kill(group, sig):
        stopped.append((group, sig))
        children.clear()

    monkeypatch.setattr("os.killpg", kill)
    lab._stop_actor("client", process)
    assert stopped == [(234, signal.SIGTERM)]
    assert children == []


def test_failed_final_snapshot_preserves_the_original_exception_and_receipt(tmp_path, monkeypatch):
    lab = SipNatLab(tmp_path)
    lab.before = {"interfaces": "original"}
    original = ValueError("original witness")

    def broken_snapshot():
        raise OSError("snapshot unavailable")

    monkeypatch.setattr(lab, "host_network", broken_snapshot)
    assert lab.__exit__(ValueError, original, None) is False
    result = json.loads((tmp_path / "topology.json").read_text())
    assert result["host_network_unchanged"] is None
    assert result["remaining_owned_namespaces"] == []
    assert result["cleanup_errors"][0]["stage"] == "host_snapshot_after"
    assert any("host_snapshot_after" in note for note in original.__notes__)
