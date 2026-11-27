"""Real PCAP parsing preserves Digest transactions in the measured SIP gate."""

import shutil
import socket
import struct

import pytest

from tools import hil_measurement as hil
from tools.sip_video_peer import _DigestSession, _challenge_ack, sip, sip_auth


pytestmark = pytest.mark.skipif(not shutil.which("tshark") or not shutil.which("capinfos"),
                                reason="Actual PCAP readers are required")


def authenticated_capture(path, challenges, *, missing_ack=False, reused_cseq=False):
    caller, callee = "192.0.2.10", "192.0.2.20"
    uri, call_id = "sip:phone@192.0.2.20", "hil-auth-transactions"
    headers = [("Via", f"SIP/2.0/UDP {caller}:5060;branch=z9hG4bK-initial"),
               ("From", f"<sip:caller@{caller}>;tag=caller"), ("To", f"<{uri}>"),
               ("Call-ID", call_id), ("CSeq", "17 INVITE"), ("Max-Forwards", "70"),
               ("Contact", f"<sip:caller@{caller}:5060>"), ("Content-Type", "application/sdp")]
    body = (f"v=0\r\no=- 1 1 IN IP4 {caller}\r\ns=Wire fixture\r\nc=IN IP4 {caller}\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=sendrecv\r\n").encode()
    request = sip.build_request("INVITE", uri, headers, body)
    wire = []
    digest = _DigestSession(sip_auth.SipDigestCredentials("caller", "fixture-only"))
    tracker = sip_auth.DigestChallengeTracker()
    for index, status in enumerate(challenges):
        wire.append((caller, callee, request))
        challenge = f'Digest realm="qualification", nonce="nonce-{index}", algorithm=MD5, qop="auth"'
        response = sip.build_uas_response(sip.parse_message(request), status, "Authentication required",
            to_tag="peer", extra_headers=(("Proxy-Authenticate" if status == 407 else "WWW-Authenticate", challenge),))
        wire.append((callee, caller, response))
        if not missing_ack:
            wire.append((caller, callee, _challenge_ack(request, sip.parse_message(response))))
        request, _sequence, _branch = digest.retry(request, sip.parse_message(response), tracker)
    if reused_cseq:
        parsed = sip.parse_message(request)
        request = sip.build_request("INVITE", uri,
            [(name, "17 INVITE" if name.lower() == "cseq" else value)
             for name, value in parsed.headers if name.lower() != "content-length"], parsed.body)
    wire.append((caller, callee, request))
    parsed = sip.parse_message(request)
    sequence = sip.parse_cseq(parsed.header("CSeq")).number
    accepted = sip.build_uas_response(parsed, 200, "OK", to_tag="peer", body=body)
    wire.append((callee, caller, accepted))

    def in_dialog(method, number):
        values = [(name, f"{number} {method}" if name.lower() == "cseq" else
                   sip.parse_message(accepted).header("To") if name.lower() == "to" else
                   f"SIP/2.0/UDP {caller}:5060;branch={sip.make_branch()}" if name.lower() == "via" else value)
                  for name, value in parsed.headers
                  if name.lower() not in {"content-type", "content-length", "authorization", "proxy-authorization"}]
        return sip.build_request(method, uri, values)

    wire.append((caller, callee, in_dialog("ACK", sequence)))
    bye = in_dialog("BYE", sequence + 1)
    wire += [(caller, callee, bye), (callee, caller, sip.build_uas_response(sip.parse_message(bye), 200, "OK", to_tag="peer"))]
    raw = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for index, (source, target, message) in enumerate(wire):
        udp = struct.pack("!HHHH", 5060, 5060, len(message) + 8, 0) + message
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, len(udp) + 20, 0, 0, 64, 17, 0,
                         socket.inet_aton(source), socket.inet_aton(target))
        packet = bytes(12) + b"\x08\x00" + ip + udp
        raw += struct.pack("<IIII", 1_700_000_000, index * 10_000, len(packet), len(packet)) + packet
    path.write_bytes(raw)
    session = {"sip_call_ids": [call_id], "request_uris": {call_id: uri}, "terminal": "caller"}
    endpoints = {"caller": {"host": caller}, "callee": {"host": callee}}
    return session, {"wire": hil.packet_observations(path, (5060,))}, endpoints


@pytest.mark.parametrize("challenges", [(401,), (407,), (401, 407)])
def test_measured_gate_accepts_authenticated_initial_invite(tmp_path, challenges):
    session, report, endpoints = authenticated_capture(tmp_path / "auth.pcap", challenges)
    hil._validate_sip(session, report, endpoints, [])


@pytest.mark.parametrize("fault", [{"missing_ack": True}, {"reused_cseq": True}])
def test_measured_gate_rejects_incomplete_or_reused_challenge_transactions(tmp_path, fault):
    session, report, endpoints = authenticated_capture(tmp_path / "invalid.pcap", (401,), **fault)
    with pytest.raises(hil.MeasurementError):
        hil._validate_sip(session, report, endpoints, [])
