"""Established calls reach mailbox deposits through real transfer owners."""

import asyncio
import math
import wave

import pytest
from homeassistant.components import tts
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback

from custom_components.voip_stack.mailbox import EVENT_VOICEMAIL_RECEIVED, get_archive
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.websocket_api import CALL_EVENT
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_voicemail_ha import configured_tts_provider as configured_tts_provider
from .test_browser_logical_availability_ha import resources

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("operation", ["refer", "service"])
async def test_established_call_transfer_to_mailbox_saves_caller_voice_and_reuses_accounts(
    trunks, registered_phones, tmp_path, monkeypatch, operation,
):
    import struct

    lab = trunks
    entry = config_entry(lab.hass)
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en"}})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone",
        title="Mailbox recipient", unique_id="phone:transfer_c", data={
            "endpoint_id": "transfer_c", "name": "transfer_c", "username": "transfer_c", "kind": "sip_account",
            "voicemail": {"enabled": True, "max_seconds": 2}}))
    class Speech:
        def async_set_message(self, _text):
            pass
        async def async_stream_result(self):
            yield bytes(640)
    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Speech())
    recording, saved = asyncio.Event(), asyncio.Queue()
    @callback
    def event(value):
        if value.data.get("event_type") == "voicemail_recording":
            recording.set()
    @callback
    def received(value):
        saved.put_nowait(value.data)
    detach = lab.hass.bus.async_listen(CALL_EVENT, event)
    detach_saved = lab.hass.bus.async_listen(EVENT_VOICEMAIL_RECEIVED, received)
    caller, operator, unused = registered_phones
    try:
        await asyncio.to_thread(caller.dial, "transfer_b")
        original = next(iter(lab.calls.sessions.values()))
        assert original.state == "in_call"
        if operation == "refer":
            await asyncio.to_thread(operator.command,
                "/transfer sip:voicemail.transfer_c@127.0.0.1:" + str(lab.runtime.transport_config["sip_port"]))
        else:
            result = await lab.hass.services.async_call("voip_stack", "transfer", {
                "device_id": "transfer_b", "call_id": original.call_id,
                "destination": "voicemail.transfer_c"}, blocking=True, return_response=True)
            assert result["success"], result
        await asyncio.wait_for(recording.wait(), 4)
        message = await asyncio.wait_for(saved.get(), 4)
        assert message["mailbox_id"] == "transfer_c"
        archive = await get_archive(lab.hass)
        assert len(archive.list("transfer_c")) == 1
        with wave.open(str(archive.segment_path(message["message_id"], "transfer_c")), "rb") as audio:
            rate = audio.getframerate()
            samples = struct.unpack("<" + str(audio.getnframes()) + "h", audio.readframes(audio.getnframes()))[-1600:]
        power = {frequency: abs(sum(value * complex(math.cos(2 * math.pi * frequency * index / rate),
            math.sin(2 * math.pi * frequency * index / rate)) for index, value in enumerate(samples)))
            for frequency in (450, 550, 650)}
        assert len(samples) >= 800 and power[450] > max(power[550], power[650]) * 5
        assert "Incoming call" not in unused.read()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        assert resources(lab)["call_scoped_quiescent"]
        # Mailbox routing must not remain attached to either reused account.
        await asyncio.to_thread(caller.dial, "transfer_b")
        async with asyncio.timeout(3):
            while not lab.calls.sessions or not all(item.state == "in_call" for item in lab.calls.sessions.values()):
                await asyncio.sleep(.01)
        ordinary = next(iter(lab.calls.sessions.values()))
        assert ordinary.owner == "bridge" and ordinary.state == "in_call"
        relay = lab.calls.resource_for(ordinary.call_id, "relay")
        async with asyncio.timeout(3):
            while relay.left_tx_packets < 10 or relay.right_tx_packets < 10:
                await asyncio.sleep(.01)
        await asyncio.to_thread(caller.hangup)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        assert resources(lab)["call_scoped_quiescent"]
    finally:
        detach()
        detach_saved()
