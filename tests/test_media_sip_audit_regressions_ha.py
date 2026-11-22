"""Production API regressions for the November 22 media and SIP audit."""

import asyncio
from contextlib import asynccontextmanager
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.local_call_media import LocalAudioContract, LocalCallMedia
from custom_components.voip_stack.sip_listener import SipInviteResult, SipUdpEndpoint

pytestmark = [pytest.mark.ha, pytest.mark.integration]


def description(formats, *, port=40000, ptime=20):
    rows = ["v=0", "o=- 1 1 IN IP4 192.0.2.2", "s=Audit regression",
            "c=IN IP4 192.0.2.2", "t=0 0",
            f"m=audio {port} RTP/AVP " + " ".join(str(pt) for pt, _rate, _flow in formats)]
    for pt, rate, flow in formats:
        rows.append(f"a=rtpmap:{pt} L16/{rate}/1")
        if flow:
            rows.append(f"a=x-voip-stack-flow:{pt} {flow}")
    if any(flow for _pt, _rate, flow in formats):
        rows.append("a=x-voip-stack-independent-ptime:1")
    return "\r\n".join((*rows, f"a=ptime:{ptime}", "a=sendrecv", ""))


def apply_answer(offer, answer):
    return sdp.negotiate_answer_directional(answer,
        [AudioFormat(48000, frame_ms=10), AudioFormat(8000, frame_ms=20)],
        [AudioFormat(16000, frame_ms=20), AudioFormat(8000, frame_ms=20)],
        local_offer_sdp=offer, local_capabilities=HA_AUDIO_CAPABILITIES)


def test_answer_cannot_reverse_the_exact_offers_payload_roles():
    offer = description(((96, 48000, "send"), (97, 16000, "recv")))
    reversed_answer = description(((110, 16000, "recv"), (111, 48000, "send")))
    assert apply_answer(offer, reversed_answer) is None


@pytest.mark.parametrize("renumber", [False, True])
def test_inverse_directional_answer_keeps_offer_receive_numbers(renumber):
    offer = description(((96, 48000, "send"), (97, 16000, "recv")))
    tx, rx = (110, 111) if renumber else (96, 97)
    answer = description(((tx, 48000, "recv"), (rx, 16000, "send")), ptime=10)
    selected = apply_answer(offer, answer)
    assert selected is not None
    assert (selected.send.sample_rate, selected.send.payload_type) == (48000, tx)
    assert (selected.recv.sample_rate, selected.recv.payload_type) == (16000, 97)
    assert [fmt.sample_rate for fmt in selected.send_formats] == [48000]
    assert [fmt.sample_rate for fmt in selected.recv_formats] == [16000]


def test_answer_alternatives_intersect_both_exact_offer_roles():
    offer = description(((96, 48000, "send"), (97, 16000, "recv"), (98, 8000, "sendrecv")))
    answer = description(((110, 48000, "sendrecv"), (111, 16000, "sendrecv"),
                          (112, 8000, "sendrecv")), ptime=10)
    selected = apply_answer(offer, answer)
    assert selected is not None
    assert (selected.send.sample_rate, selected.recv.sample_rate) == (48000, 16000)
    assert [(fmt.payload_type, fmt.sample_rate) for fmt in selected.send_formats] == [
        (110, 48000), (112, 8000)]
    assert [(fmt.payload_type, fmt.sample_rate) for fmt in selected.recv_formats] == [
        (97, 16000), (98, 8000)]


def test_standard_answer_without_private_roles_still_accepts_renumbering():
    selected = apply_answer(description(((96, 8000, ""),)),
                            description(((110, 8000, ""),)))
    assert selected is not None
    assert selected.send.payload_type == 110 and selected.recv.payload_type == 96


def packet(*, ssrc=100, pt=96, samples=320, malformed=False):
    payload = b"\x01" if malformed else struct.pack(f"!{samples}h", *([1234] * samples))
    return struct.pack("!BBHII", 0x80, pt, 1, 0, ssrc) + payload


@asynccontextmanager
async def local_media():
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20, max_frame_ms=20)
    contract = LocalAudioContract("audit-media", "192.0.2.2", 40000, fmt, fmt,
                                  local_audio_direction="recvonly")
    # No transport is started; this invokes the actual receive owner directly.
    reservation = SimpleNamespace(release=lambda: None)
    media = LocalCallMedia(None, invite=contract, local_rtp_port=42000,
                          reservation=reservation, on_complete=None)
    try:
        yield media
    finally:
        await media.stop()


async def test_malformed_first_packet_cannot_pin_the_legitimate_rtp_source():
    async with local_media() as media:
        media.handle_rtp(packet(ssrc=999, malformed=True), ("192.0.2.2", 40001))
        assert media.remote_ssrc is None and media.remote_rtp_port == 40000
        media.handle_rtp(packet(), ("192.0.2.2", 40000))
        assert media.remote_ssrc == 100 and media.remote_rtp_port == 40000
        assert media.counters["rtp_rx"] == 1
        assert media.counters["drop_decode"] == 1 and media.counters["drop_ssrc"] == 0


async def test_only_valid_same_ssrc_media_can_rebind_the_symmetric_port():
    async with local_media() as media:
        media.handle_rtp(packet(), ("192.0.2.2", 40000))
        media.handle_rtp(packet(malformed=True), ("192.0.2.2", 40001))
        assert media.remote_rtp_port == 40000
        media.handle_rtp(packet(), ("192.0.2.2", 40002))
        assert media.remote_ssrc == 100 and media.remote_rtp_port == 40002
        media.handle_rtp(packet(ssrc=999), ("192.0.2.2", 40003))
        assert media.remote_ssrc == 100 and media.remote_rtp_port == 40002
        assert media.counters["drop_decode"] == media.counters["drop_ssrc"] == 1


async def test_valid_short_packet_establishes_source_before_emitting_a_pcm_frame():
    async with local_media() as media:
        media._accepting_input = True
        media.handle_rtp(packet(samples=160), ("192.0.2.2", 40001))
        assert media.remote_ssrc == 100 and media.remote_rtp_port == 40001
        assert media.counters["rtp_rx"] == 1 and media.counters["drop_decode"] == 0
        assert media.rx_queue.empty()
        media.handle_rtp(packet(samples=160), ("192.0.2.2", 40001))
        assert media.counters["rtp_rx"] == 2
        assert media.rx_queue.get_nowait() == struct.pack("<320h", *([1234] * 320))


@pytest.mark.parametrize(("host", "pt", "counter"), [
    ("192.0.2.3", 96, "drop_addr"), ("192.0.2.2", 97, "drop_payload_type")])
async def test_unnegotiated_ip_or_payload_cannot_establish_rtp_source(host, pt, counter):
    async with local_media() as media:
        media.handle_rtp(packet(ssrc=999, pt=pt), (host, 40001))
        assert media.remote_ssrc is None and media.remote_rtp_port == 40000
        assert media.counters[counter] == 1
        media.handle_rtp(packet(), ("192.0.2.2", 40000))
        assert media.remote_ssrc == 100 and media.counters["rtp_rx"] == 1


@asynccontextmanager
async def reliable_endpoint(*, update=None, send_ok=True):
    sent = []
    offer = description(((96, 16000, ""),))
    answer = offer.replace("192.0.2.2", "192.0.2.1").replace("40000", "42000")
    commit, rollback, finalize = AsyncMock(), AsyncMock(), AsyncMock()

    async def update_media(previous, updated, method):
        return SipInviteResult(200, "OK", answer_sdp=answer,
            commit=commit, rollback=rollback, finalize=finalize)

    update = AsyncMock(side_effect=update or update_media)

    def capture(raw, address):
        sent.append(raw)
        response = sip.parse_message(raw)
        return send_ok or response.header("CSeq") != "2 PRACK"

    async def invite(_invite):
        return SipInviteResult(183, "Session Progress", answer_sdp=answer, defer_final=True)

    endpoint = SipUdpEndpoint(local_ip="192.0.2.1", local_rtp_port=42000,
        supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=invite,
        on_media_update=update, send_override=capture)
    address = ("192.0.2.2", 5060)
    uri = "sip:receiver@192.0.2.1"
    headers = [("Via", "SIP/2.0/UDP 192.0.2.2:5060;branch=z9hG4bK-audit-invite;rport"),
        ("From", "<sip:caller@192.0.2.2>;tag=remote"), ("To", f"<{uri}>"),
        ("Contact", "<sip:caller@192.0.2.2:5060>"), ("Call-ID", "audit-prack"),
        ("CSeq", "1 INVITE"), ("Supported", "100rel"), ("Content-Type", "application/sdp")]

    async def submit(raw):
        assert endpoint.submit_datagram(raw, address)
        await asyncio.gather(*tuple(endpoint._request_tasks))

    try:
        await submit(sip.build_request("INVITE", uri, headers, offer.encode()))
        provisional = next(sip.parse_message(raw) for raw in sent
                           if sip.parse_message(raw).status_code == 183)
        prack_headers = [(name, value) for name, value in headers
                         if name not in {"Via", "To", "CSeq", "Supported"}]
        prack_headers.extend((("Via", "SIP/2.0/UDP 192.0.2.2:5060;branch=z9hG4bK-audit-prack;rport"),
            ("To", provisional.header("To")), ("CSeq", "2 PRACK"),
            ("RAck", provisional.header("RSeq") + " 1 INVITE")))
        raw = sip.build_request("PRACK", uri, prack_headers, offer.encode())
        yield SimpleNamespace(endpoint=endpoint, submit=submit, raw=raw, sent=sent,
            update=update, commit=commit, rollback=rollback, finalize=finalize, answer=answer)
    finally:
        endpoint.cancel_request_tasks()
        await endpoint.wait_closed()
        assert not endpoint._reliable_provisionals
        assert all(task.done() for task in (*endpoint._request_tasks, *endpoint._maintenance_tasks))


async def test_duplicate_prack_replays_the_complete_sdp_response_after_packet_loss():
    async with reliable_endpoint() as peer:
        await peer.submit(peer.raw)
        # The peer ignores the first response, then retransmits the identical request.
        await peer.submit(peer.raw)
        responses = [raw for raw in peer.sent if sip.parse_message(raw).header("CSeq") == "2 PRACK"]
        assert len(responses) == 2
        first = sip.parse_message(responses[0])
        assert first.status_code == 200 and first.body == peer.answer.encode()
        assert first.header("Content-Type") == "application/sdp"
        assert first.header("Content-Length") == str(len(first.body))
        assert responses[0] == responses[1]
        peer.update.assert_awaited_once()
        peer.commit.assert_awaited_once()
        peer.finalize.assert_awaited_once()
        peer.rollback.assert_not_awaited()


async def test_changed_transaction_cannot_replay_a_completed_prack_answer():
    async with reliable_endpoint() as peer:
        await peer.submit(peer.raw)
        await peer.submit(peer.raw.replace(b"z9hG4bK-audit-prack", b"z9hG4bK-other-prack"))
        last = sip.parse_message(peer.sent[-1])
        assert last.status_code == 481 and not last.body
        peer.update.assert_awaited_once()


async def test_failed_prack_send_rolls_back_and_does_not_replay_an_uncommitted_answer():
    async with reliable_endpoint(send_ok=False) as peer:
        await peer.submit(peer.raw)
        peer.rollback.assert_awaited_once()
        peer.commit.assert_not_awaited()
        await peer.submit(peer.raw)
        assert sip.parse_message(peer.sent[-1]).status_code == 481
        peer.update.assert_awaited_once()


async def test_cancelled_prack_callback_releases_endpoint_tasks_without_an_answer():
    entered = asyncio.Event()

    async def update(*_args):
        entered.set()
        await asyncio.Event().wait()

    async with reliable_endpoint(update=update) as peer:
        assert peer.endpoint.submit_datagram(peer.raw, ("192.0.2.2", 5060))
        await entered.wait()
        peer.endpoint.cancel_request_tasks()
        await peer.endpoint.wait_closed()
        assert not [raw for raw in peer.sent if sip.parse_message(raw).header("CSeq") == "2 PRACK"]
        assert not peer.endpoint.completed_pracks
