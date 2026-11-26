"""SIP dialog adapters preserve the media engine's selected event contract."""

from __future__ import annotations

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat, PcmFormat
from custom_components.voip_stack.sip_client import SipCallClient

pytestmark = pytest.mark.ha
PCM = AudioFormat(8000, PcmFormat.S16LE, 1, 20)


class DatagramRecorder:
    def __init__(self):
        self.sent = []

    def sendto(self, data, destination):
        self.sent.append((data, destination))


def description(host, port, *, event_clock=8000, event_payload=101, events="0-16"):
    payloads = f"8 {event_payload}" if event_clock else "8"
    body = (f"v=0\r\no=- 1 1 IN IP4 {host}\r\ns=Contract\r\nc=IN IP4 {host}\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP {payloads}\r\na=rtpmap:8 PCMA/8000\r\n")
    if event_clock:
        body += f"a=rtpmap:{event_payload} telephone-event/{event_clock}\r\na=fmtp:{event_payload} {events}\r\n"
    return body + "a=ptime:20\r\na=sendrecv\r\n"


def client_for(offer):
    client = SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=5060,
        local_rtp_port=40000, supported_formats=[PCM])
    client.transport = DatagramRecorder()
    client._invite_cseq = 7
    client._local_sdp_body = offer
    return client


def response_for(client, answer):
    return sip.parse_message(sip.build_response(200, "OK", [
        ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKcontract"),
        ("From", "<sip:HA@127.0.0.1>;tag=local"),
        ("To", "<sip:phone@127.0.0.2>;tag=remote"),
        ("Contact", "<sip:phone@127.0.0.2:5060>"),
        ("Call-ID", client.dialog_ids.call_id), ("CSeq", "7 INVITE"),
        ("Content-Type", "application/sdp")], answer.encode()))


def commit(client, answer):
    return client._commit_200_ok(response_for(client, answer), "phone", "127.0.0.2", 5060,
        "sip:phone@127.0.0.2:5060", "sip:HA@127.0.0.1:5060", "sip:phone@127.0.0.2:5060")


CASES = [
    pytest.param({}, {"event_payload": 121}, (121, 101), id="directional-payloads"),
    pytest.param({}, {"event_clock": 0}, None, id="removed-events"),
    pytest.param({}, {"event_clock": 16000}, None, id="unselected-clock"),
    pytest.param({"events": "0-3"}, {"events": "4-7"}, None, id="disjoint-events"),
    pytest.param({"event_clock": 0}, {}, None, id="no-local-events"),
]


@pytest.mark.parametrize("offered,answered,expected", CASES)
@pytest.mark.parametrize("adapter", ["initial", "delayed", "reoffer"])
async def test_answer_adapters_keep_selected_dtmf_contract(adapter, offered, answered, expected):
    offer = description("127.0.0.1", 40000, **offered)
    answer = description("127.0.0.2", 41000, **answered)
    if adapter == "initial":
        client = client_for(offer)
        assert commit(client, answer)
        candidate = client.dialog
        assert [sip.parse_message(data).method for data, _ in client.transport.sent] == ["ACK"]
    else:
        client = client_for(description("127.0.0.1", 40000, event_clock=0))
        assert commit(client, description("127.0.0.2", 41000, event_clock=0))
        if adapter == "delayed":
            ack = sip.parse_message(sip.build_request("ACK", "sip:HA@127.0.0.1:5060", [
                ("From", "<sip:phone@127.0.0.2>;tag=remote"),
                ("To", "<sip:HA@127.0.0.1>;tag=local"),
                ("Call-ID", client.dialog_ids.call_id), ("CSeq", "8 ACK"),
                ("Content-Type", "application/sdp"),
            ], answer.encode()))
            prepared = client._answer_remote_offer(ack, local_offer_sdp=offer, current=client.dialog)
            assert prepared is not None
            candidate = prepared[0]
        else:
            candidate = client._dialog_candidate_from_answer(client.dialog, offer,
                response_for(client, answer), local_video_rtp_port=0,
                offered_video_formats=(), video_direction="inactive", session_version=1)
    assert candidate is not None
    contract = candidate.dtmf_direction
    if expected is None:
        assert contract is None
        assert candidate.dtmf_payload_type is candidate.send_dtmf_payload_type is None
        assert not candidate.recv_dtmf_formats and not candidate.send_dtmf_formats
    else:
        assert contract is not None
        assert (contract.send.payload_type, contract.recv.payload_type) == expected
        assert contract.send.events == contract.recv.events == frozenset(range(16))
        assert (contract.send.sample_rate, contract.recv.sample_rate) == (8000, 8000)


async def test_capability_only_adapter_uses_the_core_without_manufacturing_an_offer():
    client = client_for("")
    assert commit(client, description("127.0.0.2", 41000))
    events = client.dialog.dtmf_direction
    assert events is not None
    assert events.send.payload_type == events.recv.payload_type == 101
    assert events.send.events == events.recv.events == frozenset(range(16))
