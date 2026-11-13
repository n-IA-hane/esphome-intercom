"""A native SIP leg can be parked and explicitly retrieved by the same HA account."""
import asyncio
import json
from types import SimpleNamespace
import time

import pytest
from homeassistant.config_entries import ConfigSubentry

from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_browser_logical_availability_ha import end_call

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_c"}], indirect=True)
async def test_explicit_card_directed_pickup_uses_account_ha_leg(trunks, registered_phones, tmp_path):
    from custom_components.voip_stack import audio_ws_view
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.ha_bindings import HaCardBindings
    from custom_components.voip_stack.store import config_entry

    runtime = trunks
    caller, native, ringing = registered_phones
    entry = config_entry(runtime.hass)
    account = runtime.calls.component("registrar").accounts["transfer_b"]
    runtime.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone",
        title="transfer_b", unique_id="phone:transfer_b", data={
            "endpoint_id": "transfer_b", "kind": "sip_account", "name": "transfer_b", "username": "transfer_b",
            "password": account.password, "enabled": True,
            "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:transfer_b"}]}))
    runtime.runtime.endpoints.update("transfer_b", device_id="same-account-device")
    runtime.runtime.endpoints.update("transfer_c", device_id="ringing-account-device")
    owner = HaCardBindings(runtime.hass, entry, runtime.runtime,
        lambda ids: recompute_account_availability(runtime.hass, ids))
    runtime.runtime.ha_bindings = owner
    await owner.async_start()
    recompute_account_availability(runtime.hass)
    evidence = {"mode": "directed", "same_account": "transfer_b", "home_requests": 0}
    try:
        await asyncio.to_thread(caller.command, "/dial transfer_c")
        async with asyncio.timeout(3):
            while not any(item.state == "remote_ringing" for item in runtime.calls.sessions.values()):
                await asyncio.sleep(.01)
        session = next(iter(runtime.calls.sessions.values()))
        token = session.token
        await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "same-account-device", "target_device_id": "ringing-account-device",
            "call_id": token.call_id, "expected_generation": token.generation,
            "media_client_id": "same-account-browser-client", "answer_timeout": 2},
            blocking=True, return_response=True)
        media = audio_ws_view._active_softphone_media_session(runtime.hass, "transfer_b")
        evidence.update(HA_media_descriptor=media is not None,
            native_redialled="Call established" in native.read(),
            original_target_answered="Call established" in ringing.read(),
            same_session=runtime.calls.get_session(token.call_id, generation=token.generation) is session)
        assert media is not None, evidence
        assert media.call_id == token.call_id and evidence["same_session"]
        assert not evidence["native_redialled"] and not evidence["original_target_answered"], evidence
        await asyncio.to_thread(caller.wait_for, "Call established", 2)
        assert caller.read().count("Call established") == 1
        assert runtime.calls.resource_for(token.call_id, "pickup") is None
    finally:
        await end_call(runtime, caller)
        await owner.async_close()
        runtime.runtime.ha_bindings = None
        evidence["final_quiescent"] = (not runtime.calls.sessions and not runtime.calls.endpoint_claims_snapshot()
            and not runtime.calls.relays_snapshot() and not runtime.runtime.rtp_port_pool["used"])
        assert evidence["final_quiescent"]
        out = tmp_path
        (out / f'canonical-directed-pickup-{time.time_ns()}.json').write_text(json.dumps(evidence, indent=2) + '\n')


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_native_park_then_explicit_card_pickup_uses_same_account_ha_leg(trunks, registered_phones, monkeypatch, tmp_path):
    from custom_components.voip_stack import audio_ws_view, audio_ws
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.ha_bindings import HaCardBindings
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.websocket_api import _account_browser_media
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.router import RouteAction, RouteDecision

    runtime = trunks
    caller, native, _other = registered_phones
    entry = config_entry(runtime.hass)
    account = runtime.calls.component("registrar").accounts["transfer_b"]
    runtime.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone",
        title="transfer_b", unique_id="phone:transfer_b", data={
            "endpoint_id": "transfer_b", "kind": "sip_account", "name": "transfer_b", "username": "transfer_b",
            "password": account.password, "enabled": True,
            "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:transfer_b"}]}))
    runtime.runtime.endpoints.update("transfer_b", device_id="same-account-device")
    owner = HaCardBindings(runtime.hass, entry, runtime.runtime,
        lambda ids: recompute_account_availability(runtime.hass, ids))
    runtime.runtime.ha_bindings = owner
    await owner.async_start()
    recompute_account_availability(runtime.hass)
    evidence = {"same_account": "transfer_b", "real_physical_device": False, "home_requests": 0}
    media_task = None

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    try:
        await asyncio.to_thread(caller.dial, "transfer_b")
        await asyncio.to_thread(native.wait_for, "Call established", 3)
        await wait_for(lambda: any(session.state == "in_call" for session in runtime.calls.sessions.values()))
        session = next(iter(runtime.calls.sessions.values()))
        token = session.token
        assert not _account_browser_media(runtime.runtime, runtime.calls, "transfer_b", session.call_id)
        # A card observing the SIP call can park it without taking over its media.
        parked = await runtime.hass.services.async_call("voip_stack", "park", {
            "device_id": "same-account-device", "call_id": token.call_id, "expected_generation": token.generation},
            blocking=True, return_response=True)
        lot = get_parking_lot(runtime.hass)
        assert lot.calls[parked["slot"]].session is session
        assert session.owner == "parking" and session.live
        assert not runtime.runtime.endpoints.get("transfer_b").active_call_id
        decision = RouteDecision(RouteAction.ANSWER_HA, target="transfer_b",
            entry=RosterEntry("transfer_b", metadata={"endpoint_id": "transfer_b", "local_ha": True}))
        # An internal decision alone cannot opt an account into a local answer.
        with pytest.raises(ValueError, match="current pickup claim"):
            await runtime.calls.forward_call(call_id=token.call_id, destination="transfer_b",
                _resolved_decision=decision)
        claim = lot.claim(parked["slot"], "wrong-endpoint", browser_endpoint_id="transfer_c")
        assert not claim.authorizes_browser_answer(session, "transfer_b")
        assert not claim.authorizes_browser_answer(object(), "transfer_c")
        with pytest.raises(ValueError, match="current pickup claim"):
            await runtime.calls.forward_call(call_id=token.call_id, destination="transfer_b",
                _resolved_decision=decision)
        claim.rollback()
        # Losing this exact reservation across route discovery must also abort.
        from custom_components.voip_stack import call_forwarder
        claim = lot.claim(parked["slot"], "stale-claim", browser_endpoint_id="transfer_b")
        original_discovery = call_forwarder._async_build_peer_snapshot
        async def lose_claim(hass):
            claim.rollback()
            return await original_discovery(hass)
        with monkeypatch.context() as patch:
            patch.setattr(call_forwarder, "_async_build_peer_snapshot", lose_claim)
            with pytest.raises(ValueError, match="current pickup claim"):
                await runtime.calls.forward_call(call_id=token.call_id, destination="transfer_b",
                    _resolved_decision=decision)
        assert lot.calls[parked["slot"]].claim is None and session.owner == "parking" and session.live
        evidence["unclaimed_wrong_endpoint_and_stale_overrides_rejected"] = True
        native_established = native.read().count("Call established")
        await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "same-account-device", "slot": parked["slot"], "call_id": token.call_id,
            "expected_generation": token.generation, "media_client_id": "same-account-browser-client",
            "answer_timeout": 2}, blocking=True, return_response=True)
        media = audio_ws_view._active_softphone_media_session(runtime.hass, "transfer_b")
        evidence.update(original_call_id=token.call_id, generation=token.generation,
            original_session_alive=runtime.calls.get_session(token.call_id, generation=token.generation) is session and session.live,
            canonical_kind=runtime.runtime.endpoints.get("transfer_b").kind.value,
            HA_media_descriptor=media is not None,
            native_redialled=native.read().count("Call established") > native_established,
            caller_established_once=caller.read().count("Call established") == 1)
        assert media is not None, evidence
        assert media.call_id == token.call_id and evidence["original_session_alive"], evidence
        assert not evidence["native_redialled"] and evidence["caller_established_once"], evidence
        assert evidence["canonical_kind"] == "sip_account" and not lot.calls

        class Browser:
            def __init__(self):
                self.messages, self.frames, self.ready = asyncio.Queue(), asyncio.Queue(), asyncio.Event()
                self.closed = False
            def force_close(self):
                self.closed = True
                self.messages.put_nowait(SimpleNamespace(type=8))
            def __aiter__(self): return self
            async def __anext__(self): return await self.messages.get()
            async def send_json(self, _value): self.ready.set()
            async def send_bytes(self, value): self.frames.put_nowait(value)

        browser = Browser()
        media_task = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser, media, endpoint_id="transfer_b"))
        await asyncio.wait_for(browser.ready.wait(), 2)
        async with asyncio.timeout(3):
            while not any(audio_ws.decode_audio_frame(await browser.frames.get())):
                pass
        evidence["caller_nonzero_PCM_received_by_HA"] = True
        # Parking follows the actual browser media owner; native retrieval has
        # no HA-client intent and must remain a SIP leg on this same account.
        parked_again = await runtime.hass.services.async_call("voip_stack", "park", {
            "device_id": "same-account-device", "call_id": token.call_id,
            "expected_generation": token.generation}, blocking=True, return_response=True)
        await asyncio.wait_for(asyncio.shield(media_task), 2)
        assert browser.closed and lot.calls[parked_again["slot"]].session is session
        await asyncio.to_thread(native.dial, parked_again["slot"])
        await wait_for(lambda: not lot.calls and session.live and session.state == "in_call")
        await wait_for(lambda: native.read().count("Call established") == native_established + 1)
        assert runtime.calls.get_session(token.call_id, generation=token.generation) is session
        assert not _account_browser_media(runtime.runtime, runtime.calls, "transfer_b", token.call_id)
        assert audio_ws_view._active_softphone_media_session(runtime.hass, "transfer_b") is None
        assert caller.read().count("Call established") == 1
        relay = runtime.calls.resource_for(token.call_id, "relay")
        await wait_for(lambda: relay.left_tx_packets > 0 and relay.right_tx_packets > 0)
        evidence["HA_park_then_same_account_native_pickup"] = True
        evidence["reverse_bidirectional_RTP"] = True
    finally:
        if media_task is not None:
            media_task.cancel()
            await asyncio.gather(media_task, return_exceptions=True)
        await end_call(runtime, caller)
        await owner.async_close()
        runtime.runtime.ha_bindings = None
        evidence["final_quiescent"] = (not runtime.calls.sessions and not runtime.calls.endpoint_claims_snapshot()
            and not runtime.calls.relays_snapshot() and not runtime.runtime.rtp_port_pool["used"])
        assert evidence["final_quiescent"]
        out = tmp_path
        out.mkdir(parents=True, exist_ok=True)
        (out / f'canonical-same-account-pickup-{time.time_ns()}.json').write_text(json.dumps(evidence, indent=2) + '\n')
