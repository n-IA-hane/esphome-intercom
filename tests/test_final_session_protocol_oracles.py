"""Independent SIP/DTMF capture mutation oracles, with no physical claim."""

import struct

import pytest

from qualification.final_session import decode_dtmf_events, validate_notify, validate_register_pair


def sip(line, headers, body=""):
    raw = body.encode()
    return (line + "\r\n" + "\r\n".join(f"{name}: {value}" for name, value in headers.items())
            + f"\r\nContent-Length: {len(raw)}\r\n\r\n").encode() + raw


@pytest.mark.parametrize("fault", [None, "call-id", "cseq", "branch", "aor", "contact", "expired", "invite"])
def test_register_final_is_bound_to_exact_account_and_transaction(fault):
    headers = {"Call-ID": "registration", "CSeq": "2 REGISTER", "To": "<sip:7101@qa>",
               "Contact": "<sip:7101@192.0.2.1:5060>;expires=120", "Via": "SIP/2.0/UDP 192.0.2.1;branch=z9hG4bK-one"}
    request = sip("REGISTER sip:qa SIP/2.0", headers)
    modified = dict(headers)
    if fault == "call-id":
        modified["Call-ID"] = "unrelated"
    elif fault == "cseq":
        modified["CSeq"] = "3 REGISTER"
    elif fault == "branch":
        modified["Via"] = modified["Via"].replace("one", "two")
    elif fault == "aor":
        modified["To"] = "<sip:ha@home>"
    elif fault == "contact":
        modified["Contact"] = "<sip:7102@192.0.2.2>;expires=120"
    elif fault == "expired":
        modified["Contact"] = modified["Contact"].replace("120", "0")
    elif fault == "invite":
        modified["CSeq"] = "2 INVITE"
    response = sip("SIP/2.0 200 Registration accepted", modified)
    if fault:
        with pytest.raises(ValueError):
            validate_register_pair(request, response, expected_aor="sip:7101@qa", expected_contact="sip:7101@192.0.2.1:5060")
    else:
        assert validate_register_pair(request, response, expected_aor="sip:7101@qa",
            expected_contact="sip:7101@192.0.2.1:5060")["expires"] == 120


def test_register_capture_accepts_only_the_explicitly_expected_rewritten_binding():
    sent = {"Call-ID": "registration", "CSeq": "2 REGISTER", "To": "<sip:7101@qa>",
            "Contact": "<sip:7101@192.0.2.1:5060>",
            "Via": "SIP/2.0/UDP 192.0.2.1:5060;branch=z9hG4bK-one;rport"}
    received = {**sent, "Contact": "<sip:7101@198.51.100.1:54321>;expires=120",
                "Via": sent["Via"] + "=54321;received=198.51.100.1"}
    result = validate_register_pair(sip("REGISTER sip:qa SIP/2.0", sent),
        sip("SIP/2.0 200 OK", received), expected_aor="sip:7101@qa",
        expected_contact="sip:7101@198.51.100.1:54321",
        expected_request_contact="sip:7101@192.0.2.1:5060")
    assert result["expires"] == 120
    with pytest.raises(ValueError):
        validate_register_pair(sip("REGISTER sip:qa SIP/2.0", sent),
            sip("SIP/2.0 200 OK", received), expected_aor="sip:7101@qa",
            expected_contact="sip:7102@198.51.100.1:54321",
            expected_request_contact="sip:7101@192.0.2.1:5060")


@pytest.mark.parametrize("fault", [None, "owner", "call-id", "event", "count", "length"])
def test_mwi_requires_real_scoped_notify_and_counts(fault):
    headers = {"Call-ID": "mwi-subscription", "CSeq": "7 NOTIFY", "Event": "message-summary",
               "Subscription-State": "active;expires=100", "Content-Type": "application/simple-message-summary"}
    body = "Messages-Waiting: yes\r\nMessage-Account: sip:ha@home\r\nVoice-Message: 2/1\r\n"
    if fault == "owner":
        body = body.replace("ha@home", "qa@qa")
    elif fault == "call-id":
        headers["Call-ID"] = "retired-subscription"
    elif fault == "event":
        headers["Event"] = "dialog"
    elif fault == "count":
        body = body.replace("2/1", "invalid")
    packet = sip("NOTIFY sip:device SIP/2.0", headers, body)
    if fault == "length":
        packet += b"extra"
    if fault:
        with pytest.raises(ValueError):
            validate_notify(packet, call_id="mwi-subscription", event="message-summary", entity_uri="sip:ha@home")
    else:
        assert validate_notify(packet, call_id="mwi-subscription", event="message-summary",
            entity_uri="sip:ha@home") == {"waiting": True, "new": 2, "old": 1}


@pytest.mark.parametrize("version,state,previous,valid", [(0, "full", None, True), (1, "partial", 0, True),
    (0, "partial", None, False), (3, "partial", 1, False), (1, "full", 2, False)])
def test_blf_full_partial_versions_cannot_reuse_an_old_snapshot(version, state, previous, valid):
    body = f'<dialog-info xmlns="urn:ietf:params:xml:ns:dialog-info" version="{version}" state="{state}" entity="sip:desk@home"><dialog id="a"><state>early</state></dialog></dialog-info>'
    packet = sip("NOTIFY sip:device SIP/2.0", {"Call-ID": "dialog-sub", "CSeq": "4 NOTIFY", "Event": "dialog",
        "Subscription-State": "active", "Content-Type": "application/dialog-info+xml"}, body)
    if valid:
        assert validate_notify(packet, call_id="dialog-sub", event="dialog", entity_uri="sip:desk@home",
            previous_version=previous)["dialogs"] == ["early"]
    else:
        with pytest.raises(ValueError):
            validate_notify(packet, call_id="dialog-sub", event="dialog", entity_uri="sip:desk@home", previous_version=previous)


@pytest.mark.parametrize("fault", [None, "reserved", "duration", "unfinished", "conflict"])
def test_repeated_dtmf_end_packets_are_one_digit_and_failed_events_stay_failed(fault):
    def packet(seq, duration, end=False, flags=0):
        return struct.pack("!BBHII", 0x80, 101, seq, 16000, 8) + struct.pack("!BBH", 5, (128 if end else 0) | 10 | flags, duration)
    packets = [packet(1, 160), packet(2, 320), packet(3, 480, True), packet(4, 480, True), packet(5, 480, True)]
    packets.append(packets[-1])
    if fault == "reserved":
        packets[0] = packet(1, 160, flags=64)
    elif fault == "duration":
        packets[-1] = packet(6, 320, True)
    elif fault == "unfinished":
        packets = packets[:2]
    elif fault == "conflict":
        packets.append(packet(5, 600, True))
    if fault:
        with pytest.raises(ValueError):
            decode_dtmf_events(packets, payload_type=101)
    else:
        assert decode_dtmf_events(packets, payload_type=101) == "5"
