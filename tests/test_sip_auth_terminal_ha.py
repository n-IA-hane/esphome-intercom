"""A terminal authentication failure completes its existing INVITE owner."""

import asyncio

import pytest

from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_client import SipCallClient

from .test_automation_originate_ha import lab as lab


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_rejected_digest_finishes_invite_before_close(lab, transport):
    caller = SipCallClient(local_ip="127.0.0.1", local_name="Rejected account",
        local_uri_user="bedroom0", username="bedroom0", password="incorrect-fixture-password",
        local_sip_port=0, local_rtp_port=36542, signaling_transport=transport,
        supported_formats=[AudioFormat(16000, frame_ms=20)])
    try:
        result = await caller.invite(target="bedroom1", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert result == "auth_required_unsupported" and caller.last_sip_status_code == 401
        assert not caller._invite_transaction_active, "Final 401 must not leave a cancellable INVITE"
        async with asyncio.timeout(.25):
            await caller.close()
        assert caller._close_task.done()
        assert not any(phone.invites for phone in lab.phones)
        assert not lab.calls.sessions
    finally:
        await caller.close()
        # Keep a failing regression witness from leaking its bounded fallback
        # task into the next test. The assertions above still expose the bug.
        if caller._close_task is not None and not caller._close_task.done():
            caller._close_task.cancel()
            await asyncio.gather(caller._close_task, return_exceptions=True)
