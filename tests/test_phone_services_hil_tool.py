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


def test_snapshot_preserves_uidvalidity_and_playback_state():
    s = parse_snapshot("[I][phone.hil] contacts=8 imap=ready available=YES total=1 page=0 uidv=42 selected=7 player=1")
    assert s == {"contacts": 8, "imap": "ready", "available": True, "total": 1, "page": 0, "uidv": 42, "selected": 7, "player": 1}


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
    assert notification_record(wire.replace(b'message-summary', b'dialog').hex(), '192.0.2.1', '12.5') is None


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
