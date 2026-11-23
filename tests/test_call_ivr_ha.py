"""Keypad interruption crosses real RTP and retains the triggering command."""

import asyncio
from dataclasses import replace
import struct

import pytest

from custom_components.voip_stack.call_ivr import CallIVR
from custom_components.voip_stack.core import rtp
from custom_components.voip_stack.endpoint_session import TerminationIntent
from .test_automation_call import application as application

pytestmark = pytest.mark.ha


async def test_rtp_dtmf_interrupts_prompt_without_losing_digit_or_ending_call(application):
    app, registry, peer, _released = application
    from custom_components.voip_stack.core.media import negotiator
    offer = app.invite.remote_sdp.replace(b"RTP/AVP 96", b"RTP/AVP 96 101") + b"a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-15\r\n"
    events = negotiator.negotiate_dtmf(offer,
        send_clock_rate=app.invite.send_format.rtp_clock_rate,
        receive_clock_rate=app.invite.recv_format.rtp_clock_rate)
    app.invite = replace(app.invite, remote_sdp=offer, dtmf_direction=events)
    await app.answer()
    ivr = CallIVR(app.media, app.session.create_task)
    app.media.on_dtmf = lambda _side, digit, _transport: ivr.feed(digit)
    closed = asyncio.Event()

    async def speech():
        try:
            while True:
                yield struct.pack("<320h", *([1800] * 320))
        finally:
            closed.set()

    playback = asyncio.create_task(ivr.play(speech()))
    try:
        # The existing RTP sender maintains its clock with silence before
        # the prompt is queued. Wait for the first actual prompt packet.
        async with asyncio.timeout(2):
            while True:
                packet = rtp.parse_packet(await app.hass.loop.sock_recv(peer, 4096))
                if any(packet.payload):
                    break
        for sequence, end in enumerate((False, True, True)):
            payload = struct.pack("!BBH", 2, (0x80 if end else 0) | 10, 800)
            raw = rtp.build_packet(rtp.RtpPacket(101, sequence, 1234, 991, payload))
            await app.hass.loop.sock_sendto(peer, raw, ("127.0.0.1", app.media.local_rtp_port))
        assert await asyncio.wait_for(playback, 2) is False
        assert closed.is_set()
        assert await ivr.next_digit(timeout=1) == "2"
        assert ivr.digits.empty(), "Repeated RFC 4733 end packets are one key press"
        assert app.media.tx_queue.empty()
        assert app.session.live and not app.media.closed.is_set()
    finally:
        await ivr.close()
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


async def test_prequeued_selection_survives_prompt_and_uses_explicit_terminator(application):
    app, registry, _peer, _released = application
    await app.answer()
    ivr = CallIVR(app.media, app.session.create_task)
    started = False

    async def speech():
        nonlocal started
        started = True
        yield b"\x00\x00" * 320

    try:
        for digit in "01#2":
            assert ivr.feed(digit)
        assert not await ivr.play(speech())
        assert not started
        assert await ivr.next_digit() == "0"
        assert await ivr.collect() == "1"
        assert await ivr.next_digit() == "2"
    finally:
        await ivr.close()
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))
