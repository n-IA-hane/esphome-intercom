"""Observe accepted SIP I/O through the production transport capture boundary."""

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.sip_capture import SipCapture


@pytest.fixture
async def sip_messages(monkeypatch):
    """Record both UDP and TCP without replacing their actual send owners."""
    capture = SipCapture()
    messages = []
    record = capture.record

    def observe(data, local, remote, tcp, outgoing):
        try:
            message = sip.parse_message(data)
        except sip.SipError:
            pass
        else:
            messages.append((message, remote, tcp, outgoing))
        record(data, local, remote, tcp, outgoing)

    monkeypatch.setattr(capture, "record", observe)
    capture.start()
    try:
        yield messages
    finally:
        capture.clear()
