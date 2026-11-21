"""A failed independent SIP Contact cannot consume a shared account's HA fork."""

import asyncio
import json

import pytest

from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.store import config_entry
from tests.test_canonical_account_calls_ha import (
    canonical_browser as canonical_browser,
    logical_browser as logical_browser,
    registered_phones as registered_phones,
    trunks as trunks,
    lab as lab,
)
from tests.test_browser_logical_availability_ha import public_phone, resources
from tests.support.shared_codec_browser import Card
from tests.support.shared_codec_peer import (
    CASES,
    CodecPeer,
    audio_sdp,
    decode_named_rtp,
    measure_tone,
)
from tests.test_shared_account_codec_media_ha import (
    until,
    received_seconds,
    measure_browser,
)

pytestmark = [pytest.mark.ha, pytest.mark.browser_real]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 30}], indirect=True)
@pytest.mark.parametrize("bad_response", ["488", "200-unoffered-PCMA"])
async def test_failed_sip_contact_leaves_shared_card_answerable(
    canonical_browser,
    hass_ws_client,
    hass_admin_user,
    tmp_path,
    socket_enabled,
    bad_response,
):
    lab = canonical_browser
    case = CASES[0]
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "audio_codecs": ["PCMU"], "video_codecs": []}
    )
    lab.runtime.endpoints.update("casa", name="Home reception")
    registrar = lab.calls.component("registrar")
    shared = lab.browser_subentry.data
    registrar.update_accounts(
        [
            *registrar.accounts.values(),
            SipAccount(
                shared["username"],
                shared["name"],
                shared["password"],
                extension=str(shared.get("extension") or ""),
            ),
        ]
    )
    caller = CodecPeer(case, "transfer_a", "isolated-test-only")
    callee = CodecPeer(case, shared["username"], shared["password"])
    card = Card()
    evidence = {"bad_response": bad_response}
    try:
        for peer in (caller, callee):
            await lab.hass.loop.create_datagram_endpoint(
                lambda peer=peer: peer, local_addr=("127.0.0.1", 0)
            )
            await peer.register(lab.runtime.transport_config["sip_port"])
        await card.start(lab, hass_ws_client, tmp_path / "chromium", hass_admin_user)
        await caller.dial(lab.runtime.transport_config["sip_port"])
        await asyncio.wait_for(callee.ringing.wait(), 5)
        await card.command("wait-answer")
        phone = await public_phone(lab)
        assert phone["state"] == "ringing"
        call_id = phone["calls"][0]["call_id"]
        invite, address = callee.current
        assert "PCMA" not in invite.body.decode()
        evidence["offered_sdp"] = invite.body.decode()
        if bad_response == "488":
            callee.answer(invite, address, status=488)
            callee.current = None
            await asyncio.wait_for(callee.acked.wait(), 3)
            assert not callee.active and not callee.ended.is_set()
        else:
            callee.receive_sdp = audio_sdp(CASES[1], callee.media_port)
            callee.answer(invite, address, body=callee.receive_sdp)
            callee.active = True
            await asyncio.wait_for(callee.acked.wait(), 3)
            await asyncio.wait_for(callee.ended.wait(), 3)
            assert not callee.active and callee.current is None
            evidence["invalid_sdp"] = callee.receive_sdp.decode()
        await card.command("wait-answer")
        ringing = await card.command("stats")
        methods = [
            raw.split(b" ", 1)[0].decode()
            for raw in callee.signalling
            if not raw.startswith(b"SIP/2.0")
        ]
        assert methods.count("ACK") == 1
        assert methods.count("BYE") == (
            1 if bad_response == "200-unoffered-PCMA" else 0
        )
        evidence["peer_received_request_methods"] = methods
        evidence["after_bad_contact"] = ringing
        assert ringing["card"]["state"] == "ringing"
        assert ringing["card"]["caller"] == "transfer_a"
        assert ringing["card"]["callee"] == "Home reception"
        assert (await public_phone(lab))["state"] == "ringing"
        session = lab.calls.get_session(call_id)
        assert session is not None and "casa" in session.endpoint_claims
        assert session.legs["browser:casa"].state == "ringing"
        assert not caller.active
        await card.command("answer")
        await caller.established()
        await caller.start_media(450)
        await until(lambda: received_seconds(caller) >= 0.85)
        await caller.stop_media()
        active = await card.command("stats")
        assert active["errors"] == []
        assert active["card"]["state"] == "in_call"
        evidence["card_receive_tone"] = measure_browser(active, call_id, "rx", 450)
        evidence["card_microphone_tone"] = measure_browser(active, call_id, "tx", 650)
        decoded = await decode_named_rtp(
            case, caller.received, caller.receive_sdp, tmp_path / "caller-receive"
        )
        evidence["caller_tone"] = measure_tone(decoded, 8000, 650)
        await card.command("hangup")
        await asyncio.wait_for(caller.ended.wait(), 5)
        await until(lambda: resources(lab)["call_scoped_quiescent"])
        assert not lab.calls.sessions
        assert resources(lab)["call_scoped_quiescent"]
        evidence["final_resources"] = resources(lab)
    finally:
        (tmp_path / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
        for session in tuple(lab.calls.sessions.values()):
            barrier = lab.calls.request_termination(
                session.call_id, TerminationIntent("test_done")
            )
            if barrier is not None:
                await barrier
        await card.close()
        for peer in (caller, callee):
            await peer.close()
        assert resources(lab)["call_scoped_quiescent"]
