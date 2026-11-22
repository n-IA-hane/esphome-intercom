"""Legacy intercom peers answer only an explicit request on the SIP wire."""

import asyncio
import re

import pytest

from custom_components.voip_stack import paging
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_client import SipCallClient
from .test_automation_originate_ha import assert_call_resources_released, tts_fixture
from .test_paging_ha import announce, manager
from .test_paging_authorization_ha import prepare_paging
from .test_queue_call_ha import lab as lab, queue_lab as queue_lab, trunks as trunks
from .test_sip_capabilities_ha import wait_until

pytestmark = pytest.mark.ha


def require_header_autoanswer(phones, monkeypatch, header):
    """Keep the independent UDP phones ringing until their intercom header arrives."""
    patterns = {
        "Call-Info": rb"(?im)^Call-Info: <sip:[^>\r\n]+>;answer-after=0\r$",
        "Alert-Info": rb"(?im)^Alert-Info: <http://127\.0\.0\.1>;info=alert-autoanswer;delay=0\r$",
    }
    pattern = patterns[header]
    for phone in phones:
        original = phone.datagram_received
        phone.auto_answer = False

        def receive(raw, address, phone=phone, original=original):
            if raw.startswith(b"INVITE "):
                phone.auto_answer = re.search(pattern, raw.split(b"\r\n\r\n", 1)[0]) is not None
            original(raw, address)

        monkeypatch.setattr(phone, "datagram_received", receive)


@pytest.mark.parametrize("header", ["Call-Info", "Alert-Info"])
async def test_announcement_reaches_header_controlled_intercom_peer(lab, monkeypatch, header):
    owner = manager(lab, monkeypatch)
    stream = tts_fixture(monkeypatch, lab)
    require_header_autoanswer(lab.phones, monkeypatch, header)
    result = await asyncio.wait_for(paging.async_announce(announce(lab, answer_timeout=1)), 6)
    assert [target["status"] for target in result["targets"]] == ["completed", "completed"], result
    assert len(stream.calls) == 1
    assert all(phone.answered.is_set() and phone.bye.is_set() for phone in lab.phones)
    assert all(not phone.audio.empty() for phone in lab.phones)
    assert not owner.jobs
    assert_call_resources_released(lab)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("header", ["Call-Info", "Alert-Info"])
async def test_verified_source_paging_reaches_header_controlled_peer(queue_lab, monkeypatch, header):
    lab = queue_lab
    owner = await prepare_paging(lab)
    require_header_autoanswer(lab.phones, monkeypatch, header)
    client, _audio = await lab.caller()
    try:
        result = await client.invite(target="402", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3,
            audio_direction="sendonly")
        if result == "ringing":
            result = await client.wait_for_final(timeout=3)
        assert result == "in_call"
        assert all(phone.answered.is_set() for phone in lab.phones)
    finally:
        await client.terminate()
    await wait_until(lambda: not owner.jobs and not lab.calls.sessions)
    assert all(phone.bye.is_set() for phone in lab.phones)
    assert_call_resources_released(lab)


@pytest.mark.parametrize("answer_mode", ["", "Manual"])
async def test_ordinary_and_manual_invites_keep_the_phone_ringing(lab, monkeypatch, answer_mode):
    phone = lab.phones[0]
    require_header_autoanswer([phone], monkeypatch, "Alert-Info")
    client = SipCallClient(local_ip="127.0.0.1", local_name="Caller", local_sip_port=0,
        local_rtp_port=phone.rtp_socket.getsockname()[1],
        supported_formats=[AudioFormat(16000, "s16le", 1, 20)])
    try:
        result = await client.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=phone.transport.get_extra_info("sockname")[1], timeout=1,
            answer_mode=answer_mode)
        assert result == "ringing"
        assert not phone.answered.is_set()
        request = phone.invites[0]
        assert request.header("Answer-Mode") == answer_mode
        assert not request.header("Call-Info") and not request.header("Alert-Info")
        phone.answer()
        assert await client.wait_for_final(timeout=2) == "in_call"
    finally:
        await client.terminate()
        await client.close()
    assert phone.bye.is_set()
