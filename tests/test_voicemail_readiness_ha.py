"""Mailbox setup owns its deadline; a ready conversation has no setup limit."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import voicemail_menu
from custom_components.voip_stack.browser_playback import BrowserPlayback
from custom_components.voip_stack.roster import RosterEntry

from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("scenario", ["ready_after_six_seconds", "never_ready", "answer_stalled", "hangup"])
async def test_menu_readiness_budget_and_call_cleanup(lab, monkeypatch, scenario):
    session = lab.calls.upsert("mailbox-readiness", state="in_call", owner="voicemail")
    playback = BrowserPlayback()
    connection = object()
    playback.attach(connection)
    media = SimpleNamespace(private_dtmf=False, discard_output=Mock())
    menu = voicemail_menu.VoicemailMenu(lab.hass, session, None, RosterEntry("mailbox"),
                                       "127.0.0.1", media=media, playback=playback)
    entered = asyncio.Event()

    async def answer(self):
        entered.set()
        if scenario == "answer_stalled":
            await asyncio.Event().wait()

    authorize = AsyncMock(return_value=False)
    monkeypatch.setattr(voicemail_menu.VoicemailMenu, "answer", answer)
    monkeypatch.setattr(voicemail_menu.VoicemailMenu, "_authorize", authorize)
    # Exercise real asyncio expiry without a thirty-second wait on failures.
    if scenario != "ready_after_six_seconds":
        monkeypatch.setattr(voicemail_menu, "VOICEMAIL_MENU_SETUP_TIMEOUT", .03, raising=False)
    requests = []
    original = lab.calls.request_termination

    def terminate(call_id, intent, **kwargs):
        requests.append(intent.reason)
        return original(call_id, intent, **kwargs)

    monkeypatch.setattr(lab.calls, "request_termination", terminate)
    task = session.create_task(menu.run(), name="mailbox-readiness")
    try:
        await entered.wait()
        if scenario == "ready_after_six_seconds":
            await asyncio.sleep(6)
            assert not task.done()
            playback.ready(connection)
        elif scenario == "hangup":
            await lab.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
        if scenario != "hangup":
            await asyncio.wait_for(asyncio.shield(task), .5)
        else:
            await asyncio.gather(task, return_exceptions=True)
        await lab.hass.async_block_till_done()
        if scenario in {"never_ready", "answer_stalled"}:
            assert requests == ["voicemail_menu_timeout"]
            authorize.assert_not_awaited()
            playback.ready(connection)
            assert not session.live
        elif scenario == "ready_after_six_seconds":
            authorize.assert_awaited_once()
            assert requests == ["voicemail_menu_complete"]
        if menu.ivr is not None:
            assert menu.ivr.closed
        assert menu.ready.is_set()
        assert not lab.calls.sessions
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if session.live:
            await lab.calls.terminate_call_wait(session.call_id, reason="test_cleanup")
        await playback.close("test_cleanup")
