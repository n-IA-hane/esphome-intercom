"""A real REFER result cannot revoke its already claimed Replaces grant."""

import asyncio
from dataclasses import replace

import pytest

from custom_components.voip_stack import call_transfer, media_renegotiation
from .test_sip_phone_transfers_ha import registered_phones as registered_phones, establish_consultation
from .test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_refer_result_keeps_grant_until_real_replacement_media_commits(
    trunks, registered_phones, monkeypatch,
):
    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    relay = lab.calls.resource_for(consultation.call_id, "relay")
    entered, finish_media = asyncio.Event(), asyncio.Event()
    refer_finished, return_to_referrer = asyncio.Event(), asyncio.Event()
    prepare = media_renegotiation.async_prepare_media_update
    transfer = call_transfer.async_transfer_target

    async def prepare_media(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        if args[3].replaces is None or result.commit is None:
            return result
        async def commit():
            entered.set()
            await finish_media.wait()
            await result.commit()
        return replace(result, commit=commit)

    async def relay_refer(*args, **kwargs):
        result = await transfer(*args, **kwargs)
        # The actual REFER/NOTIFY owner and its finally have returned. Hold only
        # the caller's observation, while the existing media owner commits.
        refer_finished.set()
        await return_to_referrer.wait()
        return result

    monkeypatch.setattr(media_renegotiation, "async_prepare_media_update", prepare_media)
    monkeypatch.setattr(call_transfer, "async_transfer_target", relay_refer)
    try:
        await asyncio.to_thread(registered_phones[1].command, "/atransferexec")
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(refer_finished.wait(), 3)
        grant = lab.calls.resource_for(consultation.call_id, "replacement_grant")
        assert grant is not None and grant.current() and grant.pending_call_id
        replacement_id = grant.pending_call_id
        assert lab.calls.get_session(original).live and consultation.live
        finish_media.set()
        async with asyncio.timeout(3):
            while not lab.calls.get_session(replacement_id).answer_committed:
                await asyncio.sleep(.01)
        assert not grant.current()
        assert lab.calls.resource_for(replacement_id, "relay") is relay
        return_to_referrer.set()
        async with asyncio.timeout(3):
            while len(lab.calls.sessions) != 1:
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)
        assert tuple(lab.calls.sessions) == (replacement_id,)
        await asyncio.to_thread(registered_phones[2].hangup)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        assert not list(lab.calls.resource_items("replacement_grant"))
        assert not lab.runtime.rtp_port_pool.get("used")
        assert not lab.calls.endpoint_claims_snapshot()
    finally:
        finish_media.set()
        return_to_referrer.set()
