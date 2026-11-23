"""Independent wire inputs for RFC 3515 name-addresses and SIP URI headers."""
from .voip_phase1_support import sip, sip_transfer
import pytest

@pytest.mark.parametrize('value', [
    '<sip:b@example.test?Replaces=call+id%3Bto-tag%3Dt%3Bfrom-tag%3Df>',
    '"Bob" <sip:b@example.test?Replaces=call%2Bid%3Bto-tag%3Dt%3Bfrom-tag%3Df>',
])
def test_refer_preserves_call_id_and_extracts_display_name(value):
    target = sip_transfer.parse_refer_to(value)
    assert target.uri == 'sip:b@example.test'
    assert target.replaces.call_id == 'call+id'

@pytest.mark.parametrize('headers', [(), (('Refer-To','<sip:b@example.test>'),('r','<sip:c@example.test>')),
                                   (('Refer-To','<sip:b@example.test>, <sip:c@example.test>'),)])
def test_refer_requires_exactly_one_target(headers):
    request = sip.SipMessage(method='REFER', headers=headers)
    with pytest.raises(sip.SipError):
        sip_transfer.request_refer_to(request)

def test_compact_headers_are_canonical():
    request = sip.SipMessage(method='REFER', headers=(('r','<sip:b@example.test>'),('o','refer;id=12')))
    assert sip_transfer.request_refer_to(request).uri == 'sip:b@example.test'
    assert request.header_values('Event') == ['refer;id=12']

@pytest.mark.asyncio
@pytest.mark.parametrize('response', [None, 481, 500])
async def test_reporter_failure_does_not_cancel_accepted_refer(response):
    actions, reports = [], []
    async def action():
        actions.append(True)
        return 200
    async def send(headers, body):
        reports.append((sip_transfer.parse_sipfrag_status(body),
                        dict(headers)["Subscription-State"].startswith("terminated")))
        return sip.SipMessage(status_code=response, reason='Result') if response else None
    notifier = sip_transfer.ReferNotifier(None, 1, send)
    try:
        result = await sip_transfer.run_refer_action(action, notifier)
    finally:
        await notifier.close()
    assert result == 200 and actions == [True]
    assert reports == [(100, False)]

@pytest.mark.asyncio
async def test_refer_action_deadline_reports_failure_and_drains_reporter():
    import asyncio
    reports = []
    async def action():
        await asyncio.Future()
    async def send(headers, body):
        reports.append((sip_transfer.parse_sipfrag_status(body),
                        dict(headers)["Subscription-State"].startswith("terminated")))
        return sip.SipMessage(status_code=200, reason='OK')
    notifier = sip_transfer.ReferNotifier(None, 1, send)
    try:
        result = await sip_transfer.run_refer_action(action, notifier, timeout=0.01)
    finally:
        await notifier.close()
    assert result == 408
    assert reports == [(100, False), (408, True)]


def test_referred_by_token_is_preserved_without_becoming_required():
    body = b'opaque signed payload +/==\r\n'
    headers = (('r', '<sip:b@example.test>'), ('b', '<sip:a@example.test>;cid=proof'),
               ('Content-Type', 'application/pkcs7-mime'), ('Content-ID', '<proof>'))
    request = sip.SipMessage(method='REFER', headers=headers, body=body)
    target = sip_transfer.request_refer_to(request)
    assert target.referred_by == '<sip:a@example.test>;cid=proof'
    assert target.referral_body == body
    assert target.referral_content_type == 'application/pkcs7-mime'
    assert target.referral_body_headers == (('Content-ID', '<proof>'),)
    plain = sip_transfer.request_refer_to(sip.SipMessage(method='REFER', headers=(('Refer-To','<sip:b@example.test>'),)))
    assert plain.uri == target.uri and not plain.referred_by and not plain.referral_body


@pytest.mark.asyncio
async def test_notify_validates_target_refresh_before_completing_transfer():
    from custom_components.voip_stack.core.sip_dialog import DialogKey, SubscriptionDialog
    subscription = sip_transfer.ReferSubscription()
    subscription.bind(7, first=True)
    subscription.dialog = SubscriptionDialog(key=DialogKey("transfer", "local", "remote"),
        local_uri="sip:ha@example.test", remote_uri="sip:peer@example.test",
        remote_target_uri="sip:peer@old.example.test", remote_cseq=0)
    def notification(cseq, contact, status):
        return sip.SipMessage(method="NOTIFY", headers=(
            ("Call-ID", "transfer"), ("From", "<sip:peer@example.test>;tag=remote"),
            ("To", "<sip:ha@example.test>;tag=local"), ("CSeq", f"{cseq} NOTIFY"),
            ("Event", "refer;id=7"), ("Subscription-State", "terminated" if status == 200 else "active;expires=60"),
            ("Contact", contact), ("Content-Type", "message/sipfrag")),
            body=f"SIP/2.0 {status} Result\r\n".encode())
    try:
        assert subscription.receive_dialog_notify(notification(1, "*", 200))[0] == 400
        assert not subscription.result.done()
        assert subscription.dialog.remote_cseq == 0
        assert subscription.receive_dialog_notify(notification(2, "<sip:peer@new.example.test>", 100))[0] == 200
        assert subscription.dialog.remote_target_uri == "sip:peer@new.example.test"
        assert subscription.receive_dialog_notify(notification(3, "<sip:peer@final.example.test>", 200))[0] == 200
        assert subscription.dialog.remote_target_uri == "sip:peer@final.example.test"
        assert (await subscription.result).accepted
    finally:
        subscription.close()
