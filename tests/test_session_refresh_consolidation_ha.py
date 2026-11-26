"""Session refresh uses one existing dialog offer owner and its original lease."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.sip_transaction import SessionTimerDriver
from custom_components.voip_stack.sip_client import SipCallClient, SipDialog

pytestmark = pytest.mark.ha


def response(status, *, headers=()):
    return sip.parse_message(sip.build_response(status, "Response", headers))


@pytest.mark.parametrize("rejected", [405, 501])
async def test_update_rejection_retries_invite_and_remembers_method_for_same_dialog(rejected):
    state = sip.SipSessionTimer(interval=90, deadline=190)
    sent = []

    async def send(method, *, extra_headers):
        sent.append(method)
        return response(rejected) if len(sent) == 1 else response(200, headers=(("Session-Expires", "90;refresher=uac"),))

    driver = SessionTimerDriver(state, send, "uac", lambda: 100.0)
    assert await driver.refresh() == "refreshed"
    assert sent == ["UPDATE", "INVITE"]
    assert state.refresh_method == "INVITE"
    assert await driver.refresh() == "refreshed"
    assert sent == ["UPDATE", "INVITE", "INVITE"]


async def test_expired_session_never_sends_a_late_refresh():
    state = sip.SipSessionTimer(interval=90, deadline=100)
    send = AsyncMock()
    assert await SessionTimerDriver(state, send, "uac", lambda: 100.0).refresh() == "session_timer_failed"
    send.assert_not_awaited()


async def test_glare_retry_never_extends_original_expiration(monkeypatch):
    state = sip.SipSessionTimer(interval=90, deadline=190, refresh_at=145)
    now = 145.0
    sleeps = []
    sent = []

    async def sleep(delay):
        nonlocal now
        sleeps.append(delay)
        now += delay

    async def send(method, *, extra_headers):
        sent.append(method)
        return response(491)

    monkeypatch.setattr("custom_components.voip_stack.core.sip_transaction.asyncio.sleep", sleep)
    assert await SessionTimerDriver(state, send, "uac", lambda: now, "INVITE").refresh() == "waiting"
    assert len(sent) == 2 and len(sleeps) == 1
    assert state.deadline == state.refresh_at == 190


@pytest.mark.parametrize("status", [408, 481])
async def test_missing_or_lost_dialog_is_terminal_without_method_retry(status):
    state = sip.SipSessionTimer(interval=90, deadline=190)
    send = AsyncMock(return_value=response(status))
    assert await SessionTimerDriver(state, send, "uac", lambda: 100).refresh() == "session_timer_failed"
    send.assert_awaited_once()


async def test_audio_only_refresh_reuses_invite_offer_answer_ack_and_reader():
    sent = []
    client = SipCallClient(local_ip="192.0.2.1", local_name="HA", local_sip_port=5060,
        local_rtp_port=42000, supported_send_formats=[sdp.AudioFormat(8000)],
        supported_recv_formats=[sdp.AudioFormat(8000)])
    client.dialog_ids.remote_tag = "peer"
    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=Refresh\r\n"
             "c=IN IP4 192.0.2.1\r\nt=0 0\r\nm=audio 42000 RTP/AVP 96\r\n"
             "a=rtpmap:96 L16/8000/1\r\na=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n")
    from custom_components.voip_stack.audio_rtcp import remote_audio_rtcp_fields
    answer = offer.replace("192.0.2.1", "192.0.2.2").replace("audio 42000", "audio 40000")
    dialog = SipDialog(target="peer", remote_host="192.0.2.2", remote_sip_port=5060,
        remote_rtp_host="192.0.2.2", remote_rtp_port=40000, local_rtp_port=42000,
        call_id=client.dialog_ids.call_id, local_uri="sip:ha@192.0.2.1",
        remote_uri="sip:peer@192.0.2.2", remote_target_uri="sip:peer@192.0.2.2",
        send_format=fmt, recv_format=fmt, recv_formats=(fmt,), send_formats=(fmt,),
        remote_tag="peer", local_sdp_session_id=1, local_sdp_session_version=1,
        local_sdp_body=offer, **remote_audio_rtcp_fields(answer))
    client.dialog = dialog
    dialog.session_timer.configure(sip.SipSessionExpires(90, "uac"), local_role="uac",
                                   now=asyncio.get_running_loop().time())
    responses = asyncio.Queue()
    released = []

    def send(raw):
        request = sip.parse_message(raw)
        sent.append(request)
        if request.method == "INVITE":
            assert request.body.decode() == offer
            responses.put_nowait((sip.build_uas_response(request, 200, "OK", to_tag="peer",
                contact_uri="sip:peer@192.0.2.2", body=answer.encode()), ("192.0.2.2", 5060)))
        return True

    client.use_reused_signaling_flow(send=send, responses=responses,
        close=lambda: released.append(True), udp_next_hop=("192.0.2.2", 5060))
    try:
        reply = await client._send_session_refresh("INVITE", extra_headers=(("Session-Expires", "90;refresher=uac"),))
        assert reply is not None and reply.status_code == 200
        assert [message.method for message in sent] == ["INVITE", "ACK"]
        assert sent[0].header("CSeq").split()[0] == sent[1].header("CSeq").split()[0]
        assert client._dialog_dispatcher is not None
        assert client._prepared_reinvite is None and client.dialog is not dialog
        assert client.dialog.send_format == fmt and client.dialog.recv_format == fmt
        assert "m=video" not in client.dialog.local_sdp_body
    finally:
        client.dialog = None
        await client.close()
    assert released == [True]
