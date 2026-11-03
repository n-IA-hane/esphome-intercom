"""Capture owner/control tests do not claim a live or physical capture."""
import asyncio
import json
import shlex

import pytest

from tools.ssh_pcap_capture import SshPcapWriter, capture_filter, ssh_command


def test_ssh_command_quotes_bpf_and_never_bypasses_hostkeys():
    argv = ['python3', '-c', 'print("$(touch /tmp/should-not-run)")']
    command = ssh_command('capture-host', ['pct', 'exec', '231', '--'], argv)
    assert shlex.split(command[-1]) == ['pct', 'exec', '231', '--', *argv]
    assert 'StrictHostKeyChecking=yes' in command
    assert 'BatchMode=yes' in command


@pytest.mark.parametrize('host', ['-oProxyCommand=bad', 'host;exit', 'host\nother'])
def test_untrusted_host_syntax_is_rejected(host):
    with pytest.raises(RuntimeError):
        ssh_command(host, [], ['true'])


def test_capture_cannot_record_its_ssh_stream_or_plain_api_credentials():
    bpf = capture_filter('udp or tcp port 5060')
    assert '(udp or tcp port 5060)' in bpf
    for port in [22, 80, 443, 6053, 8123, 18123]:
        assert f'port {port}' in bpf
    assert 'and not (' in bpf


def test_stop_identity_mismatch_never_claims_valid_capture(tmp_path):
    writer = SshPcapWriter(host='hass', bpf='udp', output=tmp_path/'call.pcap')
    writer.identity = {'pid': 42, 'starttime': '77', 'cmdline': writer.argv, 'stdout': 'pipe:[1]'}
    calls = []
    async def remote(argv, timeout=15):
        calls.append(argv)
        raise RuntimeError('Owned capture process identity changed; no signal sent')
    writer.remote = remote
    with pytest.raises(RuntimeError, match='no signal sent'):
        asyncio.run(writer.stop())
    result = json.loads((tmp_path/'call.pcap.writer.json').read_text())
    assert result['status'] == 'capture_cleanup_failed'
    assert 'pcap_sha256' not in result
    assert len(calls) == 1


def test_missing_owned_identity_stops_without_sending_any_signal(tmp_path):
    writer = SshPcapWriter(host='hass', bpf='udp', output=tmp_path/'call.pcap')
    with pytest.raises(RuntimeError, match='No verified'):
        asyncio.run(writer.stop())
    assert writer.receipt['status'] == 'capture_cleanup_failed'


# Actual Linux tcpdump final counter shapes from the retained0/188-drop runs.
COUNTERS_ZERO = ["7709 packets captured", "7734 packets received by filter", "0 packets dropped by kernel"]
COUNTERS_LOSSY = ["8926 packets captured", "9706 packets received by filter", "188 packets dropped by kernel"]


def closed_writer(tmp_path, counters, *, require_zero=False):
    """Only SSH control is replaced; real capinfos validates a header-only fixture.

    These are collector software tests, never media/transport qualification.
    """
    import inspect
    import struct
    options = dict(host="capture-host", bpf="udp", output=tmp_path / "call.pcap")
    if "require_zero_kernel_drops" in inspect.signature(SshPcapWriter).parameters:
        options["require_zero_kernel_drops"] = require_zero
    writer = SshPcapWriter(**options)
    writer.require_zero_kernel_drops = require_zero
    writer.output.write_bytes(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    writer.stream = writer.output.open("ab")
    writer.identity = dict(pid=42, starttime="77", cmdline=writer.argv, stdout="pipe:[1]")
    writer.stderr = list(counters)
    class FinishedProcess:
        returncode = 0
        async def wait(self):
            return 0
    writer.process = FinishedProcess()
    async def remote(argv, timeout=15):
        return json.dumps({"signalled_owned_pid": 42}).encode()
    writer.remote = remote
    return writer


@pytest.mark.parametrize(("lines", "drops", "quality"), [
    (COUNTERS_LOSSY, 188, "kernel_drops_reported"),
    (COUNTERS_ZERO, 0, "zero_kernel_drops_reported"),
    ([], None, "unknown"),
])
def test_observation_reports_actual_or_unknown_capture_quality(tmp_path, lines, drops, quality):
    writer = closed_writer(tmp_path, lines)
    before = writer.output.read_bytes()
    result = asyncio.run(writer.stop())
    assert result["tcpdump_counts"]["dropped_by_kernel"] == drops
    assert result["capture_quality"] == quality
    assert result["capture_file_valid"] is True
    assert result["status"] == "capture_closed_capinfos_accepted"
    assert writer.output.read_bytes() == before
    saved = json.loads(writer.output.with_suffix(".pcap.writer.json").read_text())
    assert saved["stderr"] == lines and saved["media_qualified"] is False


@pytest.mark.parametrize(("lines", "accepted"), [(COUNTERS_LOSSY, False), (COUNTERS_ZERO, True), ([], False)])
def test_zero_drop_requirement_is_separate_from_hardware_cleanup(tmp_path, lines, accepted):
    writer = closed_writer(tmp_path, lines, require_zero=True)
    before = writer.output.read_bytes()
    if accepted:
        result = asyncio.run(writer.stop())
        assert result["capture_quality"] == "zero_kernel_drops_reported"
    else:
        with pytest.raises(RuntimeError, match="Capture quality"):
            asyncio.run(writer.stop())
        assert writer.receipt["status"] == "capture_quality_failed"
        assert writer.receipt["capture_file_valid"] is True
        assert writer.receipt["pcap_sha256"]
        assert writer.receipt["status"] != "capture_cleanup_failed"
        assert writer.stream.closed and writer.closed
        assert asyncio.run(writer.stop())["status"] == "capture_quality_failed"
    assert writer.output.read_bytes() == before
    saved = json.loads(writer.output.with_suffix(".pcap.writer.json").read_text())
    assert saved["stderr"] == lines
    assert saved["require_zero_kernel_drops"] is True


def test_singular_stats_and_os_dependent_received_counts(tmp_path):
    writer = closed_writer(tmp_path, ["1 packet captured", "100 packets received by filter", "0 packets dropped by kernel"], require_zero=True)
    result = asyncio.run(writer.stop())
    assert result["tcpdump_counts"] == {"captured": 1, "received_by_filter": 100, "dropped_by_kernel": 0}
    assert result["capture_quality"] == "zero_kernel_drops_reported"


def test_duplicate_stats_are_unknown_and_never_assumed_zero(tmp_path):
    writer = closed_writer(tmp_path, [*COUNTERS_ZERO, "188 packets dropped by kernel"], require_zero=True)
    with pytest.raises(RuntimeError, match="Capture quality"):
        asyncio.run(writer.stop())
    assert writer.receipt["capture_quality"] == "unknown"
    assert writer.receipt["tcpdump_counts"]["dropped_by_kernel"] is None
    assert writer.receipt["capture_quality_issues"] == ["duplicate dropped_by_kernel counter"]
