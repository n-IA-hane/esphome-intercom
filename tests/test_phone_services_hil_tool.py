"""Safety boundaries of the existing native phone-service HIL operation."""
import json

import pytest

from tools.ha_voip_lab.phone_services_hil import HomeFixture, MwiTrace, contact_page_offsets, dialog_still_ringing, notification_record, parse_rtp_sample, parse_snapshot, write_private


@pytest.mark.parametrize("count,expected", [
    (0, [0]), (3, [0, 48]), (10, [0, 96, 192, 288, 384]),
])
def test_contact_overview_covers_the_actual_bounded_scroll_extent(count, expected):
    assert contact_page_offsets(count) == expected


def test_invalid_geometry_cannot_produce_a_pointer_action():
    with pytest.raises(ValueError):
        contact_page_offsets(2, row_height=0)


def test_snapshot_reads_only_the_canonical_contact_count():
    assert parse_snapshot("[I][phone.hil] contacts=8") == {"contacts": 8}
    with pytest.raises(ValueError):
        parse_snapshot("IMAP is not an ESP phone service")


@pytest.mark.parametrize("enabled,new,old,total", [(True, 0, 0, 0), (False, 1, 0, 1), (False, 0, 1, 1), (False, 0, 0, 1)])
async def test_sip_menu_fixture_never_changes_an_existing_mailbox(monkeypatch, enabled, new, old, total):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tools.ha_voip_lab import phone_services_hil as hil
    fixture = SimpleNamespace(phone=AsyncMock(return_value={"mailbox": {
        "enabled": enabled, "new": new, "old": old}}), action=AsyncMock())
    ws = SimpleNamespace(command=AsyncMock())
    witness = SimpleNamespace(service=AsyncMock())
    monkeypatch.setattr(hil, "message_uid", AsyncMock(return_value={"total": total}))
    with pytest.raises(RuntimeError, match="disabled, empty mailbox"):
        await hil.mailbox_menu_scenario(witness, SimpleNamespace(mailbox_device="existing"), fixture, ws, {}, "192.0.2.1", "192.0.2.2")
    fixture.action.assert_not_awaited()
    ws.command.assert_not_awaited()
    witness.service.assert_not_awaited()


async def test_menu_operation_witness_rejects_another_mailbox_or_preceding_message():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tools.ha_voip_lab.phone_services_hil import mailbox_event
    def packet(mailbox, identity):
        return {"type": "event", "event": {"event_type": "voip_stack_voicemail_operation",
            "data": {"mailbox_id": mailbox, "message_id": identity, "operation": "deleted"}}}
    ws = SimpleNamespace(events=[packet("someone_else", "owned"), packet("ours", "preceding")],
        ws=SimpleNamespace(recv=AsyncMock(return_value=json.dumps(packet("ours", "owned")))))
    event = await mailbox_event(ws, "voip_stack_voicemail_operation",
        lambda d: d.get("mailbox_id") == "ours" and d.get("message_id") == "owned")
    assert event["message_id"] == "owned"
    ws.ws.recv.assert_awaited_once()


async def test_replay_witness_requires_a_fresh_backend_play_completion():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tools.ha_voip_lab.phone_services_hil import mailbox_event
    def packet(timestamp):
        return {"type": "event", "event": {"event_type": "voip_stack_voicemail_changed",
            "time_fired": timestamp, "data": {"endpoint_id": "ours"}}}
    ws = SimpleNamespace(events=[packet("2026-10-30T10:00:00+00:00")],
        ws=SimpleNamespace(recv=AsyncMock(return_value=json.dumps(packet("2026-10-30T10:00:02+00:00")))))
    from datetime import datetime
    since = datetime.fromisoformat("2026-10-30T10:00:01+00:00").timestamp()
    await mailbox_event(ws, "voip_stack_voicemail_changed", lambda d: d.get("endpoint_id") == "ours", since=since)
    ws.ws.recv.assert_awaited_once()


def native_audio_dump(uptime=1000):
    bodies = [
        f"BEGIN v=1 uptime_ms={uptime} state=in_call call_hash=aabbccdd transition=NO failed=NO",
        "configured: transport=udp sip_port=5060 rtp_port=40000 capability=full_duplex registration=enabled",
        "local TX: codec=pcm rate=16000 channels=1 ptime_ms=10",
        "local RX: codec=pcm rate=16000 channels=1 ptime_ms=10",
        "sip: running=YES peer_transport=udp invite_pending=NO terminal_pending=NO status=200 event=answer",
        "rtp: running=YES remote_sip_port=5060 remote_rtp_port=40002 tx_packets=100 rx_packets=90 tx_bytes=32000 rx_bytes=28800",
        "negotiated TX: codec=pcm rate=16000 channels=1 ptime_ms=10",
        "negotiated RX: codec=pcm rate=16000 channels=1 ptime_ms=10",
        "queues: tx_depth=1 tx_drops=0 rx_depth=2 rx_drops=0",
        "rx_jitter: depth=2 capacity=16 missing=0 late=1 duplicates=2 drops=0 discarded_frames=0",
        "audio: active=YES buffers_psram=YES task_stacks_psram=YES audio_stacks_psram=YES route=direct",
        "last_termination=none", f"END v=1 uptime_ms={uptime} untracked_metrics=not_available",
    ]
    return ["[I][voip_stack.diagnostics:123]: " + body for body in bodies]


def test_complete_native_audio_dump_retains_the_authoritative_identity_and_counters():
    from tools.ha_voip_lab.phone_services_hil import parse_full_audio_snapshot
    value = parse_full_audio_snapshot(native_audio_dump())
    assert value["uptime_ms"] == 1000 and value["call_hash"] == "aabbccdd" and value["state"] == "in_call"
    assert value["rtp"] == {"running": True, "tx_packets": 100, "rx_packets": 90, "tx_bytes": 32000, "rx_bytes": 28800}
    assert value["queues"] == {"tx_depth": 1, "tx_drops": 0, "rx_depth": 2, "rx_drops": 0}
    assert value["jitter"] == {"depth": 2, "capacity": 16, "missing": 0, "late": 1,
                               "duplicates": 2, "drops": 0, "discarded_frames": 0}


def test_standard_sip_call_does_not_require_optional_ha_route_metadata():
    from tools.ha_voip_lab.phone_services_hil import parse_full_audio_snapshot
    lines = [line.replace("route=direct", "route=") for line in native_audio_dump()]
    value = parse_full_audio_snapshot(lines)
    assert value["route"] == "" and value["state"] == "in_call" and value["audio_active"]


async def test_full_snapshot_waits_through_every_deferred_section_until_fresh_end():
    from tools.ha_voip_lab.phone_services_hil import full_audio_snapshot
    class Witness:
        log = [{"message": message} for message in ["[I][phone.hil]: contacts=8", *native_audio_dump(100)]]
        async def service(self, name):
            assert name == "phone_runtime_snapshot"
            self.log += [{"message": "[I][phone.hil]: contacts=9"},
                         {"message": native_audio_dump(100)[-1]}]
        async def wait(self, predicate, timeout):
            assert timeout == 5 and not predicate()
            for line in native_audio_dump(1000)[:-1]:
                self.log.append({"message": line})
                assert not predicate()
            self.log.append({"message": "[I][audio_stack.diagnostics]: END v=1 uptime_ms=1000"})
            assert not predicate()
            self.log.append({"message": native_audio_dump(1000)[-1]})
            assert predicate()
    value = await full_audio_snapshot(Witness())
    assert value["uptime_ms"] == 1000


async def test_rtp_query_drains_previous_idle_dump_and_respects_native_throttle(monkeypatch):
    import time
    from tools.ha_voip_lab import phone_services_hil as hil
    sleeps = []
    async def scheduled_delay(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(hil.asyncio, "sleep", scheduled_delay)
    old = [line.replace("state=in_call", "state=idle").replace("running=YES", "running=NO")
           for line in native_audio_dump(100)]
    class Witness:
        drained = False
        log = [{"message": old[0], "time": time.monotonic() - .9}]
        async def wait(self, predicate, timeout):
            if not self.drained:
                assert not predicate()
                self.log += [{"message": line} for line in old[1:]]
                self.drained = True
            assert predicate()
        async def service(self, name):
            assert self.drained and sleeps
            self.log += [{"message": "[I][phone.hil]: contacts=9"}]
            self.log += [{"message": line} for line in native_audio_dump(1000)]
    value = await hil.rtp_sample(Witness())
    assert value == {"running": True, "tx_packets": 100, "rx_packets": 90}
    assert len(sleeps) == 1 and .05 < sleeps[0] < .15


@pytest.mark.parametrize("fault", ["no-marker", "no-begin", "no-end", "wrong-end"])
async def test_partial_or_stale_native_snapshots_never_satisfy_the_wait(fault):
    from tools.ha_voip_lab.phone_services_hil import full_audio_snapshot
    class Witness:
        log = []
        async def service(self, name):
            lines = native_audio_dump()
            if fault == "no-begin":
                lines = lines[1:]
            elif fault == "no-end":
                lines = lines[:-1]
            elif fault == "wrong-end":
                lines[-1] = lines[-1].replace("uptime_ms=1000", "uptime_ms=999")
            if fault != "no-marker":
                self.log.append({"message": "[I][phone.hil]: contacts=9"})
            self.log += [{"message": line} for line in lines]
        async def wait(self, predicate, timeout):
            assert not predicate()
            raise TimeoutError("controlled incomplete trace")
    with pytest.raises(TimeoutError, match="incomplete trace"):
        await full_audio_snapshot(Witness())


@pytest.mark.parametrize("fault", ["no-jitter", "duplicate-rtp", "wrong-end"])
def test_complete_but_malformed_native_dump_is_rejected(fault):
    from tools.ha_voip_lab.phone_services_hil import parse_full_audio_snapshot
    lines = native_audio_dump()
    if fault == "no-jitter":
        lines = [line for line in lines if "rx_jitter:" not in line]
    elif fault == "duplicate-rtp":
        lines.append(next(line for line in lines if "rtp: running=" in line))
    else:
        lines[-1] = lines[-1].replace("uptime_ms=1000", "uptime_ms=999")
    with pytest.raises(ValueError):
        parse_full_audio_snapshot(lines)


def advancing_audio_pair():
    from copy import deepcopy
    from tools.ha_voip_lab.phone_services_hil import parse_full_audio_snapshot
    before = parse_full_audio_snapshot(native_audio_dump())
    after = deepcopy(before)
    after["uptime_ms"] = 2000
    for key, delta in {"tx_packets": 100, "rx_packets": 99, "tx_bytes": 32000, "rx_bytes": 31680}.items():
        after["rtp"][key] += delta
    return before, after


def test_same_call_audio_gate_keeps_late_and_duplicate_observations_separate():
    from tools.ha_voip_lab.phone_services_hil import check_audio_continuity
    before, after = advancing_audio_pair()
    after["jitter"]["late"] += 1
    after["jitter"]["duplicates"] += 2
    delta = check_audio_continuity(before, after)
    assert delta["rtp"]["tx_packets"] == 100 and delta["rtp"]["rx_packets"] == 99
    assert delta["jitter"] == {"missing": 0, "late": 1, "duplicates": 2, "drops": 0, "discarded_frames": 0}


@pytest.mark.parametrize("group,name", [("queues", "tx_drops"), ("queues", "rx_drops"),
    ("jitter", "missing"), ("jitter", "drops"), ("jitter", "discarded_frames")])
def test_any_true_audio_loss_rejects_the_physical_gate(group, name):
    from tools.ha_voip_lab.phone_services_hil import check_audio_continuity
    before, after = advancing_audio_pair()
    after[group][name] += 1
    with pytest.raises(RuntimeError, match="loss counters increased"):
        check_audio_continuity(before, after)


@pytest.mark.parametrize("fault", ["different-call", "inactive", "transition", "stopped-rtp", "stale",
    "rtp-reset", "jitter-reset", "queue-reset", "no-tx", "no-rx-bytes"])
def test_counter_resets_and_other_call_generations_cannot_turn_loss_into_zero(fault):
    from tools.ha_voip_lab.phone_services_hil import check_audio_continuity
    before, after = advancing_audio_pair()
    if fault == "different-call":
        after["call_hash"] = "11223344"
    elif fault == "inactive":
        after["audio_active"] = False
    elif fault == "transition":
        after["transition"] = True
    elif fault == "stopped-rtp":
        after["rtp"]["running"] = False
    elif fault == "stale":
        after["uptime_ms"] = before["uptime_ms"]
    elif fault == "rtp-reset":
        after["rtp"]["tx_packets"] = 0
    elif fault == "jitter-reset":
        after["jitter"]["duplicates"] = 0
    elif fault == "queue-reset":
        before["queues"]["tx_drops"] = 1
    elif fault == "no-tx":
        after["rtp"]["tx_packets"] = before["rtp"]["tx_packets"]
    else:
        after["rtp"]["rx_bytes"] = before["rtp"]["rx_bytes"]
    with pytest.raises(RuntimeError):
        check_audio_continuity(before, after)


@pytest.mark.parametrize("fault", [None, "wrong-digit", "missing-end", "missing-final"])
async def test_menu_wire_oracle_decodes_negotiated_events_with_real_tshark(tmp_path, fault):
    import shutil
    import socket
    import struct
    from tools.ha_voip_lab.phone_services_hil import menu_wire_dtmf
    if not shutil.which("tshark") or not shutil.which("capinfos"):
        pytest.skip("Wireshark command-line tools are required")
    packets = []
    def udp(source, target, source_port, target_port, payload):
        header = struct.pack("!HHHH", source_port, target_port, 8 + len(payload), 0)
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 28 + len(payload), 0, 0, 64, 17, 0,
            socket.inet_aton(source), socket.inet_aton(target))
        packets.append(bytes.fromhex("0200000000010200000000020800") + ip + header + payload)
    def sip(first, host, port):
        sdp = (f"v=0\r\no=- 1 1 IN IP4 {host}\r\ns=wire\r\nc=IN IP4 {host}\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP 96 120\r\na=rtpmap:96 L16/16000/1\r\n"
            "a=rtpmap:120 telephone-event/8000\r\na=fmtp:120 0-15\r\n").encode()
        return (first + "\r\nVia: SIP/2.0/UDP 192.0.2.3:5060;branch=z9hG4bK-wire\r\n"
            "From: <sip:phone@192.0.2.3>;tag=local\r\nTo: <sip:mailbox@192.0.2.1>;tag=peer\r\n"
            "Call-ID: wire-test\r\nCSeq: 1 INVITE\r\nContent-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n\r\n").encode() + sdp
    udp("192.0.2.3", "192.0.2.1", 5060, 5060, sip("INVITE sip:mailbox@192.0.2.1 SIP/2.0", "192.0.2.3", 40000))
    udp("192.0.2.1", "192.0.2.3", 5060, 5060, sip("SIP/2.0 200 OK", "192.0.2.1", 40002))
    for index, event in enumerate([1, 3, 2, 0, 1, 11, 2, 11]):
        if fault == "missing-final" and index == 7:
            continue
        if fault == "wrong-digit" and index == 1:
            event = 4
        for report in range(1 if index == 7 else 4):
            end = report != 0 and not (fault == "missing-end" and index == 0)
            packet = struct.pack("!BBHII", 0x80, 120 | (0x80 if report == 0 else 0),
                100 + index * 4 + report, 1000 + index * 2000, 0x12345678)
            packet += struct.pack("!BBH", event, 10 | (0x80 if end else 0), 1280 if end else 160)
            udp("192.0.2.3", "192.0.2.1", 40000, 40002, packet)
    path = tmp_path / "menu.pcap"
    with path.open("wb") as stream:
        stream.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for index, packet in enumerate(packets):
            stream.write(struct.pack("<IIII", 1, index * 20000, len(packet), len(packet)))
            stream.write(packet)
    if fault:
        with pytest.raises(RuntimeError, match="differ|End report"):
            await menu_wire_dtmf(path, "192.0.2.3")
    else:
        result = await menu_wire_dtmf(path, "192.0.2.3")
        assert result["rfc4733"] and len(result["events"]) == 8
        assert result["events"][0]["end_packets"] == 3
        assert result["events"][-1]["end_packets"] == 0


def test_rtp_sample_keeps_both_directions_and_running_state():
    assert parse_rtp_sample("rtp: running=YES remote_sip_port=5060 remote_rtp_port=40000 tx_packets=23 rx_packets=19 tx_bytes=800") == {
        "running": True, "tx_packets": 23, "rx_packets": 19}
    with pytest.raises(ValueError):
        parse_rtp_sample("audio active=YES")


def test_fixture_credentials_have_private_permissions(tmp_path):
    p = tmp_path / "private/accounts.json"
    write_private(p, {"password": "fixture-only"})
    assert p.stat().st_mode & 0o777 == 0o600
    assert json.loads(p.read_text())["password"] == "fixture-only"


@pytest.mark.parametrize("line,cseq,expected", [("SIP/2.0 180 Ringing", "1 INVITE", True),
    ("SIP/2.0 200 OK", "2 PRACK", True), ("SIP/2.0 200 OK", "1 INVITE", False),
    ("SIP/2.0 487 Request Terminated", "1 INVITE", False), ("CANCEL sip:test SIP/2.0", "1 CANCEL", False)])
def test_pointer_confirmation_applies_to_the_same_still_ringing_invite(line, cseq, expected):
    records = [{"line": line, "headers": {"Call-ID": "fixture", "CSeq": cseq}}]
    assert dialog_still_ringing(records, "fixture") is expected
    assert dialog_still_ringing(records, "another-call") is True


async def test_cleanup_only_removes_new_uuid_owned_accounts(tmp_path):
    from unittest.mock import AsyncMock
    fixture = HomeFixture(None, tmp_path, "unique")
    fixture.baseline = {"existing"}
    fixture.owned = {"new": {"display_name": "HIL unique A"}}
    fixture.action = AsyncMock(side_effect=[{"accounts": [{"username": "existing", "display_name": "Keep"},
        {"username": "new", "display_name": "HIL unique A"}]}, None])
    await fixture.cleanup_accounts()
    assert fixture.action.await_args_list[1].args == ("remove_account", {"username": "new"})


async def test_cleanup_refuses_changed_or_existing_identity(tmp_path):
    from unittest.mock import AsyncMock
    fixture = HomeFixture(None, tmp_path, "unique")
    fixture.baseline = {"existing"}
    fixture.owned = {"existing": {"display_name": "Keep"}}
    fixture.action = AsyncMock(return_value={"accounts": [{"username": "existing", "display_name": "Keep"}]})
    with pytest.raises(RuntimeError):
        await fixture.cleanup_accounts()
    assert fixture.action.await_count == 1


def test_mwi_trace_retains_only_message_summary_and_no_authorization():
    wire = ('NOTIFY sip:phone@192.0.2.3 SIP/2.0\r\nEvent: message-summary\r\n'
        'Call-ID: fixture\r\nCSeq: 4 NOTIFY\r\nAuthorization: Digest secret=hidden\r\n\r\n'
        'Messages-Waiting: yes\r\nVoice-Message: 1/0\r\n').encode()
    record = notification_record(wire.hex(), '192.0.2.1', '12.5')
    assert record['waiting'] == 'yes' and record['cseq'] == ['4', 'NOTIFY']
    assert 'hidden' not in json.dumps(record) and 'Authorization' not in json.dumps(record)
    assert notification_record(wire.replace(b'message-summary', b'refer').hex(), '192.0.2.1', '12.5') is None


async def test_mwi_requires_ack_from_the_expected_device_and_fresh_cseq(tmp_path):
    from types import SimpleNamespace
    trace = MwiTrace(SimpleNamespace(address='192.0.2.3'), tmp_path)
    notify = {'timestamp': 12.5, 'source': '192.0.2.1', 'line': 'NOTIFY sip:phone SIP/2.0',
        'call_id': 'fixture', 'cseq': ['4', 'NOTIFY'], 'event': 'message-summary', 'waiting': 'yes'}
    ack = {**notify, 'source': '192.0.2.9', 'line': 'SIP/2.0 200 OK', 'event': '', 'waiting': None}
    trace.records.extend([notify, ack])
    with pytest.raises(TimeoutError):
        await trace.waiting('yes', since=12, timeout=.001)
    ack['source'] = '192.0.2.3'
    ack['cseq'] = ['3', 'NOTIFY']
    with pytest.raises(TimeoutError):
        await trace.waiting('yes', since=12, timeout=.001)
    ack['cseq'] = ['4', 'NOTIFY']
    assert (await trace.waiting('yes', since=12))['notify_accepted'] is True
    with pytest.raises(TimeoutError):
        await trace.waiting('yes', since=13, timeout=.001)


def test_backend_recovery_matches_current_dialogs_and_ignores_orphan_server_leases(tmp_path):
    from types import SimpleNamespace
    trace = MwiTrace(SimpleNamespace(address='192.0.2.3'), tmp_path)
    for package, target in [('message-summary', 'sip:phone@example.test'), ('dialog', 'sip:101@example.test')]:
        old = {'timestamp': 12, 'source': '192.0.2.3', 'line': 'SUBSCRIBE sip:server SIP/2.0',
            'event': package, 'call_id': 'old-' + package, 'cseq': ['4', 'SUBSCRIBE'],
            'waiting': None, 'to_uri': target, 'to_tag': 'previous-server-tag'}
        fresh = {**old, 'timestamp': 13, 'call_id': 'new-' + package, 'cseq': ['1', 'SUBSCRIBE'], 'to_tag': ''}
        notify = {**fresh, 'timestamp': 14, 'source': '192.0.2.1', 'line': 'NOTIFY sip:phone SIP/2.0', 'cseq': ['1', 'NOTIFY']}
        trace.records.extend([old, fresh, notify])
    assert trace.recovered_subscriptions(11) is None
    for notify in list(trace.records):
        if notify['line'].startswith('NOTIFY '):
            trace.records.append({**notify, 'source': '192.0.2.3', 'line': 'SIP/2.0 200 OK'})
    # Historical leases from an older firmware have no current-client refresh
    # and must not become additional recovery requirements.
    trace.records.append({**trace.records[-1], 'call_id': 'orphaned-old-firmware'})
    assert trace.recovered_subscriptions(11) == {'current_client_subscriptions': 2,
        'accepted_notifications': 2, 'packages': ['dialog', 'message-summary']}
    assert trace.recovered_subscriptions(15) is None


def test_baresip_wav_source_survives_long_absolute_capture_paths(tmp_path):
    from tools.ha_voip_lab.phone_services_hil import baresip_audio_source_path
    cwd = tmp_path / ("long-workspace-" * 10) / "test_captures"
    voice = cwd / "phone-features" / "hardware-final" / "peer-source.wav"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"fixture PCM")
    assert len(bytes(voice)) >= 128
    source = baresip_audio_source_path(voice, cwd)
    # Model baresip config_audio.src_dev[128], including its terminating NUL.
    stored = source.encode()[:127].decode()
    assert stored == source and (cwd / stored).read_bytes() == voice.read_bytes()


def test_baresip_rejects_source_that_would_still_be_truncated(tmp_path):
    from tools.ha_voip_lab.phone_services_hil import baresip_audio_source_path
    voice = tmp_path / ("a" * 128) / "peer-source.wav"
    with pytest.raises(ValueError, match="baresip's audio_source"):
        baresip_audio_source_path(voice, tmp_path)


@pytest.mark.parametrize("initial_id", ["old-capture", ""])
async def test_screenshot_waits_for_initial_api_state_before_requesting_capture(tmp_path, monkeypatch, initial_id):
    import asyncio
    from importlib.util import module_from_spec, spec_from_file_location
    import io
    import os
    from pathlib import Path
    from types import SimpleNamespace
    source = Path(os.environ.get("VOIP_STACK_REPO", Path(__file__).parents[2] / "esphome-voip-stack"))
    spec = spec_from_file_location("screenshot_qualification", source / "tools/qualify_sip_registration_esp.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    witness = module.EspWitness(SimpleNamespace())
    entity = type("TextSensorInfo", (), {"object_id": "screenshot_id", "name": "Screenshot ID", "key": 7})()
    state_type = type("TextSensorState", (), {})
    witness.entities = [entity]
    witness.services = {"take_screenshot": "capture-action"}

    def deliver(value):
        state = state_type()
        state.key, state.state = 7, value
        witness.on_state(state)

    async def initial_subscription():
        await asyncio.sleep(0)
        deliver(initial_id)

    calls = []
    async def capture(service, data):
        # An empty initial value is still an observed state, not a missing one.
        assert witness.values[("TextSensorState", 7)] == initial_id
        calls.append((service, data))
        asyncio.get_running_loop().call_soon(deliver, "new-capture")
    witness.client.execute_service = capture
    fetched = []
    def fetch(url, timeout):
        fetched.append(url)
        assert url.endswith("?capture=new-capture") and timeout == 10
        return io.BytesIO(b"new screenshot")
    monkeypatch.setattr(module, "urlopen", fetch)
    subscription = asyncio.create_task(initial_subscription())
    output = tmp_path / "screen.jpg"
    result = await witness.screenshot("192.0.2.3", output)
    await subscription
    assert result["capture_id"] == "new-capture"
    assert calls == [("capture-action", {})] and len(fetched) == 1
    assert output.read_bytes() == b"new screenshot"
