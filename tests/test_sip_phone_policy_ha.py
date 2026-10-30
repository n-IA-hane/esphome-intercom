"""Physical SIP phones use the same no-answer policy as dashboard phones."""

import asyncio

import pytest

from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
from custom_components.voip_stack.store import config_entry
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("destination_kind", ["sip_account", "browser"])
async def test_native_sip_noanswer_forward_preserves_caller_and_cleanup(
    trunks, registered_phones, destination_kind
):
    runtime = trunks
    destination_id = "transfer_b"
    if destination_kind == "browser":
        from custom_components.voip_stack.phone_config import endpoint_from_data
        from custom_components.voip_stack.roster import RosterEntry
        destination_id = "casa"
        logical = endpoint_from_data(config_entry(runtime.hass), {
            "endpoint_id": destination_id, "name": destination_id, "kind": "browser",
        })
        runtime.runtime.endpoints.register(logical)
        runtime.roster.append(RosterEntry(destination_id,
            metadata={"local_ha": True, "endpoint_id": destination_id}))
        assert logical.is_available and not runtime.runtime.softphone_presence
    runtime.runtime.endpoints.update(
        destination_id,
        policy=PhonePolicy.from_data(
            {
                "ringing_timeout": 1,
                "no_answer": {"kind": "contact", "target": "transfer_c"},
            }
        ),
    )
    setup_phone_policy_events(runtime.hass, config_entry(runtime.hass))
    caller, original, replacement = registered_phones
    await asyncio.to_thread(caller.command, f"/dial {destination_id}")
    async with asyncio.timeout(3):
        while not runtime.calls.sessions:
            await asyncio.sleep(0.01)
    source = next(iter(runtime.calls.sessions.values()))
    identity = source.call_id
    generation = source.generation
    async with asyncio.timeout(4):
        while "Call established" not in replacement.read():
            await asyncio.sleep(0.01)
    async with asyncio.timeout(3):
        while source.state != "in_call" or "Call established" not in caller.read():
            await asyncio.sleep(0.01)
    assert (
        runtime.calls.get_session(identity) is source
        and source.generation == generation
    )
    assert source.state == "in_call"
    assert not runtime.runtime.endpoints.get(destination_id).active_call_id
    assert runtime.runtime.endpoints.get("transfer_c").active_call_id == identity
    assert caller.read().count("Call established") == 1
    assert "Call established" not in original.read()
    relay = runtime.calls.resource_for(identity, "relay")
    async with asyncio.timeout(3):
        while relay.left_tx_packets < 30 or relay.right_tx_packets < 30:
            await asyncio.sleep(0.01)
    await asyncio.to_thread(replacement.hangup)
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(0.01)
    assert not runtime.runtime.rtp_port_pool["used"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_final_sip_answer_wins_noanswer_timer_before_media_commit(
    trunks, registered_phones, monkeypatch
):
    from custom_components.voip_stack import outbound_bridge_commit

    runtime = trunks
    runtime.runtime.endpoints.update(
        "transfer_b",
        policy=PhonePolicy.from_data(
            {
                "ringing_timeout": 1,
                "no_answer": {"kind": "contact", "target": "transfer_c"},
            }
        ),
    )
    setup_phone_policy_events(runtime.hass, config_entry(runtime.hass))
    entered, release = asyncio.Event(), asyncio.Event()
    original = outbound_bridge_commit.async_start_sip_bridge_media

    async def delayed(relay):
        entered.set()
        await release.wait()
        return await original(relay)

    monkeypatch.setattr(outbound_bridge_commit, "async_start_sip_bridge_media", delayed)
    caller, receiver, unused = registered_phones
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    try:
        await asyncio.wait_for(entered.wait(), 3)
        session = next(iter(runtime.calls.sessions.values()))
        client = runtime.calls.sip_client_for(
            runtime.calls.bridge_link_for(session.call_id)
        )
        assert client.dialog is not None
        await asyncio.sleep(1.1)
        assert session.live, (
            "A media-start delay was mistaken for an unanswered SIP call"
        )
        assert "Incoming call" not in unused.read()
        release.set()
        async with asyncio.timeout(3):
            while session.state != "in_call":
                await asyncio.sleep(0.01)
        await asyncio.to_thread(caller.hangup)
        async with asyncio.timeout(3):
            while runtime.calls.sessions:
                await asyncio.sleep(0.01)
    finally:
        release.set()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("destination_kind", ["sip_account", "browser", "browser_with_esp_events"])
async def test_native_sip_noanswer_reaches_own_mailbox(
    trunks, registered_phones, tmp_path, monkeypatch, destination_kind
):
    from homeassistant.components import tts
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import callback
    from custom_components.voip_stack.mailbox import (
        get_archive,
        EVENT_VOICEMAIL_RECEIVED,
    )
    from custom_components.voip_stack.websocket_api import CALL_EVENT
    import wave
    import numpy as np

    runtime = trunks
    destination_id = "transfer_b"
    if destination_kind.startswith("browser"):
        from dataclasses import replace
        from custom_components.voip_stack.phone_config import endpoint_from_data
        from custom_components.voip_stack.roster import RosterEntry

        destination_id = "casa"
        logical = endpoint_from_data(config_entry(runtime.hass), {
            "endpoint_id": destination_id, "name": destination_id, "kind": "browser",
        })
        runtime.runtime.endpoints.register(replace(logical, device_id="browser-casa"))
        assert logical.is_available and not runtime.runtime.softphone_presence
        runtime.roster.append(RosterEntry(
            destination_id, metadata={"local_ha": True, "endpoint_id": destination_id},
        ))
    runtime.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "voicemail": {
                "enabled": True,
                "tts_entity_id": "tts.test",
                "language": "en",
            },
        },
    )
    runtime.hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            subentry_type="phone",
            title=destination_id,
            unique_id=f"phone:{destination_id}",
            data={
                "endpoint_id": destination_id,
                "name": destination_id,
                "username": destination_id,
                "kind": "browser" if destination_kind.startswith("browser") else "sip_account",
                "voicemail": {"enabled": True, "max_seconds": 2},
            },
        ),
    )
    runtime.runtime.endpoints.update(
        destination_id,
        policy=PhonePolicy.from_data(
            {
                "ringing_timeout": 1,
                "no_answer": {"kind": "voicemail", "target": destination_id},
            }
        ),
    )
    setup_phone_policy_events(runtime.hass, entry)

    class Speech:
        def async_set_message(self, _text):
            pass

        async def async_stream_result(self):
            yield bytes(640)

    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Speech())
    recording, saved = asyncio.Event(), asyncio.Event()
    events = []

    @callback
    def state(event):
        if event.data.get("event_type") == "voicemail_recording":
            recording.set()

    @callback
    def message(event):
        events.append(event.data)
        saved.set()

    remove_state = runtime.hass.bus.async_listen(CALL_EVENT, state)
    remove_saved = runtime.hass.bus.async_listen(EVENT_VOICEMAIL_RECEIVED, message)
    try:
        await asyncio.to_thread(registered_phones[0].command, f"/dial {destination_id}")
        if destination_kind == "browser_with_esp_events":
            # Baresip supplies the real SIP/RTP leg. Mirror the ESP caller's
            # state through the production bridge, as a physical phone does
            # after receiving 180 Ringing. Only entity discovery is adapted.
            from unittest.mock import AsyncMock
            from custom_components.voip_stack import esphome_state_bridge

            async with asyncio.timeout(2):
                while not any(s.state == "ringing" for s in runtime.calls.sessions.values()):
                    await asyncio.sleep(0.01)
            source = next(s for s in runtime.calls.sessions.values() if s.state == "ringing")
            monkeypatch.setattr(esphome_state_bridge, "_get_voip_devices", AsyncMock(return_value=[{
                "device_id": "esp-caller", "name": "Waveshare",
                "entities": {"voip_state": "sensor.esp_voip_state"},
            }]))
            before_event = (source.event_context.state, source.event_context.sequence)
            await esphome_state_bridge.async_emit_state_event(
                runtime.hass, "sensor.esp_voip_state", "remote_ringing", "calling",
                expected_endpoint_id="transfer_a", expected_call_id=source.call_id,
            )
            assert source.state == "ringing"
            assert (source.event_context.state, source.event_context.sequence) == before_event
        await asyncio.wait_for(recording.wait(), 3)
        assert not runtime.runtime.endpoints.get(destination_id).active_call_id
        await asyncio.wait_for(saved.wait(), 4)
        assert len(events) == 1
        archive = await get_archive(runtime.hass)
        messages = archive.list(destination_id)
        assert len(messages) == 1 and messages[0]["caller"] == "transfer_a"
        with wave.open(
            str(archive.segment_path(messages[0]["id"], destination_id)), "rb"
        ) as wav:
            rate = wav.getframerate()
            samples = np.frombuffer(
                wav.readframes(wav.getnframes()), dtype="<i2"
            ).astype(float)
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        peak = float(np.argmax(spectrum[1:]) + 1) * rate / len(samples)
        assert abs(peak - 450) < 5
        async with asyncio.timeout(3):
            while runtime.calls.sessions:
                await asyncio.sleep(0.01)
        assert not runtime.runtime.rtp_port_pool["used"]
    finally:
        remove_state()
        remove_saved()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_native_automation_can_speak_to_unanswered_sip_caller(
    trunks, registered_phones, tmp_path, monkeypatch
):
    import math
    import struct
    import wave
    import numpy as np
    from homeassistant.components import tts
    from custom_components.voip_stack.automation_context import (
        CallExecution,
        current_execution,
    )

    runtime = trunks

    class Speech:
        def async_set_message(self, _text):
            pass

        async def async_stream_result(self):
            for _ in range(30):
                yield struct.pack(
                    "<320h",
                    *(
                        int(7000 * math.sin(2 * math.pi * 1100 * i / 16000))
                        for i in range(320)
                    ),
                )

    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Speech())
    caller, original, _unused = registered_phones
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    async with asyncio.timeout(3):
        while not any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        ):
            await asyncio.sleep(0.01)
    session = next(iter(runtime.calls.sessions.values()))
    token = current_execution.set(
        CallExecution(session.token, {"automation_control": "routable"})
    )
    try:
        await runtime.hass.services.async_call(
            "voip_stack",
            "tts_say",
            {"tts_entity_id": "tts.test", "message": "The kitchen is not answering"},
            blocking=True,
        )
        assert session.owner == "automation"
        assert not runtime.runtime.endpoints.get("transfer_b").active_call_id
        assert caller.read().count("Call established") == 1
        assert "Call established" not in original.read()
        await runtime.hass.services.async_call(
            "voip_stack", "hangup", {}, blocking=True
        )
        assert not runtime.calls.sessions and not runtime.runtime.rtp_port_pool["used"]
    finally:
        current_execution.reset(token)
    await asyncio.to_thread(caller.close)
    found = False
    for path in (tmp_path / "transfer_a").glob("*-dec.wav"):
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            samples = np.frombuffer(
                wav.readframes(wav.getnframes()), dtype="<i2"
            ).astype(float)
        if len(samples) < 800:
            continue
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        frequency = float(np.argmax(spectrum[1:]) + 1) * rate / len(samples)
        found |= abs(frequency - 1100) < 5
    assert found


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("selection", ["phone", "native_trigger"])
async def test_public_forward_controls_only_its_native_phone_call(
    trunks, registered_phones, selection
):
    from homeassistant.exceptions import ServiceValidationError
    from custom_components.voip_stack.automation_context import (
        CallExecution,
        current_execution,
    )

    runtime = trunks
    runtime.runtime.endpoints.update("transfer_b", device_id="receiving-phone")
    runtime.runtime.endpoints.update("transfer_c", device_id="other-phone")
    caller, original, replacement = registered_phones
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    async with asyncio.timeout(3):
        while not any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        ):
            await asyncio.sleep(0.01)
    session = next(iter(runtime.calls.sessions.values()))
    with pytest.raises(ServiceValidationError, match="does not own"):
        await runtime.hass.services.async_call(
            "voip_stack",
            "forward",
            {
                "device_id": "other-phone",
                "call_id": session.call_id,
                "destination": "transfer_a",
            },
            blocking=True,
        )
    assert (
        session.live
        and runtime.runtime.endpoints.get("transfer_b").active_call_id
        == session.call_id
    )
    binding = None
    if selection == "native_trigger":
        binding = current_execution.set(
            CallExecution(session.token, {"automation_control": "routable"})
        )
    try:
        await runtime.hass.services.async_call(
            "voip_stack",
            "forward",
            {
                "destination": "transfer_c",
                **({"device_id": "receiving-phone"} if selection == "phone" else {}),
            },
            blocking=True,
        )
    finally:
        if binding is not None:
            current_execution.reset(binding)
    async with asyncio.timeout(3):
        while (
            session.state != "in_call" or "Call established" not in replacement.read()
        ):
            await asyncio.sleep(0.01)
    assert runtime.runtime.endpoints.get("transfer_c").active_call_id == session.call_id
    assert not runtime.runtime.endpoints.get("transfer_b").active_call_id
    await asyncio.to_thread(caller.hangup)
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(0.01)
    assert not runtime.runtime.rtp_port_pool["used"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_native_unanswered_automation_overrides_phone_default(
    trunks, registered_phones, monkeypatch
):
    from datetime import timedelta
    from homeassistant.components import tts
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components.voip_stack.trigger import VoipCallTrigger

    runtime = trunks
    runtime.runtime.endpoints.update(
        "transfer_b",
        policy=PhonePolicy.from_data(
            {
                "ringing_timeout": 1,
                "no_answer": {"kind": "contact", "target": "transfer_c"},
            }
        ),
    )
    setup_phone_policy_events(runtime.hass, config_entry(runtime.hass))

    class Speech:
        def async_set_message(self, _text):
            pass

        async def async_stream_result(self):
            yield b"\x40\x00" * 3200

    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Speech())
    complete = asyncio.Event()
    tasks = []

    async def action():
        try:
            await runtime.hass.services.async_call(
                "voip_stack",
                "tts_say",
                {
                    "tts_entity_id": "tts.test",
                    "message": "Automation owns the fallback",
                },
                blocking=True,
            )
            return object()
        finally:
            complete.set()

    def runner(_payload, _description, _context):
        task = asyncio.create_task(action())
        tasks.append(task)
        return task

    trigger = VoipCallTrigger(
        runtime.hass,
        TriggerConfig(
            "voip_stack.call_unanswered",
            options={
                "destination": "transfer_b",
                "for": timedelta(seconds=1),
            },
        ),
    )
    detach = await trigger.async_attach_runner(runner)
    try:
        await asyncio.to_thread(registered_phones[0].command, "/dial transfer_b")
        await asyncio.wait_for(complete.wait(), 4)
        await asyncio.gather(*tasks)
        assert len(tasks) == 1
        assert "Incoming call" not in registered_phones[2].read()
        async with asyncio.timeout(3):
            while runtime.calls.sessions:
                await asyncio.sleep(0.01)
        assert not runtime.runtime.rtp_port_pool["used"]
    finally:
        detach()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_caller_hangup_waits_for_retired_destination_and_never_dials_next(
    trunks, registered_phones, monkeypatch
):

    runtime = trunks
    runtime.runtime.endpoints.update("transfer_b", device_id="receiving-phone")
    caller, original, unused = registered_phones
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    async with asyncio.timeout(3):
        while not any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        ):
            await asyncio.sleep(0.01)
    session = next(iter(runtime.calls.sessions.values()))
    client = runtime.calls.sip_client_for(
        runtime.calls.bridge_link_for(session.call_id)
    )
    entered, release = asyncio.Event(), asyncio.Event()
    terminate = client.terminate

    async def delayed():
        entered.set()
        await release.wait()
        await terminate()

    monkeypatch.setattr(client, "terminate", delayed)
    from custom_components.voip_stack.sip_client import SipCallClient

    dial_entered, dial_release = asyncio.Event(), asyncio.Event()
    invite = SipCallClient.invite

    async def wait_before_sending(client, *args, **kwargs):
        if kwargs.get("target") == "transfer_c":
            dial_entered.set()
            await dial_release.wait()
        return await invite(client, *args, **kwargs)

    monkeypatch.setattr(SipCallClient, "invite", wait_before_sending)
    forwarding = asyncio.create_task(
        runtime.hass.services.async_call(
            "voip_stack",
            "forward",
            {"device_id": "receiving-phone", "destination": "transfer_c"},
            blocking=True,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(dial_entered.wait(), 2)
        await asyncio.to_thread(caller.hangup)
        await asyncio.wait_for(session.termination_started.wait(), 2)
        await asyncio.sleep(0)
        assert not session.terminated.is_set()
        assert any(
            resource.name.startswith("retired_destination:")
            for resource in session.resources
        )
        release.set()
        await forwarding
        await asyncio.wait_for(session.terminated.wait(), 2)
        assert "Incoming call" not in unused.read()
        assert not runtime.calls.sessions and not runtime.runtime.rtp_port_pool["used"]
    finally:
        release.set()
        dial_release.set()
        await asyncio.gather(forwarding, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_slow_retired_transaction_does_not_delay_replacement_audio(
    trunks, registered_phones, monkeypatch
):
    from custom_components.voip_stack.call_scope import call_belongs_to_endpoint
    from custom_components.voip_stack.dialog_info import snapshot

    runtime = trunks
    runtime.runtime.endpoints.update("transfer_b", device_id="receiving-phone")
    caller, old_phone, new_phone = registered_phones
    await asyncio.to_thread(caller.command, "/dial transfer_b")

    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    await wait_for(
        lambda: any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        )
    )
    session = next(iter(runtime.calls.sessions.values()))
    client = runtime.calls.sip_client_for(
        runtime.calls.bridge_link_for(session.call_id)
    )
    entered, release = asyncio.Event(), asyncio.Event()
    terminate = client.terminate

    async def delayed():
        entered.set()
        await release.wait()
        await terminate()

    monkeypatch.setattr(client, "terminate", delayed)
    try:
        await runtime.hass.services.async_call(
            "voip_stack",
            "forward",
            {"device_id": "receiving-phone", "destination": "transfer_c"},
            blocking=True,
        )
        await asyncio.wait_for(entered.wait(), 2)
        await wait_for(lambda: session.state == "in_call")
        relay = runtime.calls.resource_for(session.call_id, "relay")
        await wait_for(
            lambda: relay.left_tx_packets >= 20 and relay.right_tx_packets >= 20
        )
        assert not call_belongs_to_endpoint(
            runtime.calls, session.call_id, "transfer_b"
        )
        assert snapshot(runtime.runtime, "transfer_b") == ()
        assert (
            runtime.runtime.endpoints.get("transfer_b").active_call_id
            == session.call_id
        )
        await asyncio.to_thread(old_phone.hangup)
        before = relay.left_tx_packets
        await wait_for(lambda: relay.left_tx_packets > before + 10)
        assert session.live
        release.set()
        await wait_for(
            lambda: not runtime.runtime.endpoints.get("transfer_b").active_call_id
        )
        assert (
            runtime.runtime.endpoints.get("transfer_c").active_call_id
            == session.call_id
        )
        await asyncio.to_thread(new_phone.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool["used"]
    finally:
        release.set()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("source_kind", ["sip", "browser"])
async def test_native_sip_busy_response_uses_phone_busy_destination(
    trunks, registered_phones, monkeypatch, source_kind
):
    runtime = trunks
    runtime.runtime.endpoints.update(
        "transfer_b",
        policy=PhonePolicy.from_data(
            {
                "busy": {"kind": "contact", "target": "transfer_c"},
            }
        ),
    )
    setup_phone_policy_events(runtime.hass, config_entry(runtime.hass))
    caller, original, replacement = registered_phones
    if source_kind == "browser":
        from unittest.mock import AsyncMock
        from custom_components.voip_stack import softphone_originate
        from custom_components.voip_stack.phone_endpoint import (
            PhoneEndpoint,
            EndpointKind,
        )
        from custom_components.voip_stack.roster import dump_roster_json
        from custom_components.voip_stack.local_softphone_runtime import (
            async_setup_local_softphone_bridge,
        )

        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id="browser-source",
                name="browser-source",
                device_id="browser-device",
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
        async_setup_local_softphone_bridge(runtime.hass)
        runtime.hass.states.async_set(
            "sensor.voip_phonebook",
            "ready",
            {"roster_json": dump_roster_json(runtime.roster)},
        )
        monkeypatch.setattr(
            softphone_originate,
            "_ha_advertise_host",
            AsyncMock(return_value="127.0.0.1"),
        )
        monkeypatch.setattr(
            softphone_originate, "_resolve_target_device", AsyncMock(return_value=None)
        )
        await runtime.hass.services.async_call(
            "voip_stack",
            "call",
            {
                "device_id": "browser-device",
                "destination": "transfer_b",
                "media_client_id": "browser-owner",
            },
            blocking=True,
        )
    else:
        await asyncio.to_thread(caller.command, "/dial transfer_b")
    async with asyncio.timeout(3):
        while not any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        ):
            await asyncio.sleep(0.01)
    session = next(iter(runtime.calls.sessions.values()))
    await asyncio.to_thread(original.hangup)
    async with asyncio.timeout(3):
        while (
            "Call established" not in replacement.read() or session.state != "in_call"
        ):
            await asyncio.sleep(0.01)
    assert session.live and session.state == "in_call"
    await asyncio.to_thread(replacement.hangup)
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(0.01)
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(
    "status,rule,forwarded",
    [
        (486, "busy", True),
        (480, "unavailable", True),
        (503, "unavailable", True),
        (403, "unavailable", False),
        (603, "busy", False),
    ],
)
@pytest.mark.parametrize("source_kind", ["sip", "browser"])
async def test_immediate_sip_failure_obeys_only_matching_phone_policy(
    trunks, registered_phones, monkeypatch, status, rule, forwarded, source_kind
):
    from custom_components.voip_stack.core import sip

    runtime = trunks
    endpoint = runtime.runtime.endpoints.get("sip:bedroom0")
    runtime.runtime.endpoints.update(
        endpoint.endpoint_id,
        availability="available",
        policy=PhonePolicy.from_data(
            {rule: {"kind": "contact", "target": "transfer_c"}}
        ),
    )
    phone = runtime.phones[0]
    from custom_components.voip_stack.roster import RosterEntry

    runtime.roster.append(
        RosterEntry(
            "bedroom0",
            sip_uri=f"sip:bedroom0@127.0.0.1:{phone.transport.get_extra_info('sockname')[1]}",
            metadata={
                "endpoint_id": "sip:bedroom0",
                "endpoint_kind": "sip_account",
                "registered": True,
            },
        )
    )
    receive = phone.datagram_received

    def reject(raw, address):
        request = sip.parse_message(raw)
        if request.method != "INVITE":
            receive(raw, address)
            return
        phone.invites.append(request)
        headers = [
            (name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")
        ]
        headers.append(("To", request.header("To") + ";tag=reject"))
        phone.transport.sendto(
            sip.build_response(
                status,
                {
                    486: "Busy Here",
                    480: "Temporarily Unavailable",
                    503: "Service Unavailable",
                    403: "Forbidden",
                    603: "Decline",
                }[status],
                headers,
            ),
            address,
        )

    monkeypatch.setattr(phone, "datagram_received", reject)
    caller, _, replacement = registered_phones
    if source_kind == "browser":
        from unittest.mock import AsyncMock
        from custom_components.voip_stack import softphone_originate
        from custom_components.voip_stack.phone_endpoint import (
            PhoneEndpoint,
            EndpointKind,
        )
        from custom_components.voip_stack.roster import dump_roster_json
        from custom_components.voip_stack.local_softphone_runtime import (
            async_setup_local_softphone_bridge,
        )

        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id="browser-source",
                name="browser-source",
                device_id="browser-device",
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
        async_setup_local_softphone_bridge(runtime.hass)
        runtime.hass.states.async_set(
            "sensor.voip_phonebook",
            "ready",
            {"roster_json": dump_roster_json(runtime.roster)},
        )
        monkeypatch.setattr(
            softphone_originate,
            "_ha_advertise_host",
            AsyncMock(return_value="127.0.0.1"),
        )
        monkeypatch.setattr(
            softphone_originate, "_resolve_target_device", AsyncMock(return_value=None)
        )
        await runtime.hass.services.async_call(
            "voip_stack",
            "call",
            {
                "device_id": "browser-device",
                "destination": "bedroom0",
                "media_client_id": "browser-owner",
            },
            blocking=True,
        )
    else:
        await asyncio.to_thread(caller.command, "/dial bedroom0")
    async with asyncio.timeout(4):
        while not phone.invites:
            await asyncio.sleep(0.01)
        if forwarded:
            while "Call established" not in replacement.read():
                await asyncio.sleep(0.01)
            assert len(runtime.calls.sessions) == 1
            await asyncio.to_thread(replacement.hangup)
        while runtime.calls.sessions:
            await asyncio.sleep(0.01)
    if not forwarded:
        assert "Incoming call" not in replacement.read()
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_noanswer_policy_tracks_destination_leg_of_answered_caller(
    trunks, registered_phones
):
    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "parking": {
                "enabled": True,
                "timeout": 1,
                "return_timeout": 10,
                "return_destination": "transfer_b",
            },
        },
    )
    runtime.runtime.endpoints.update(
        "transfer_b",
        policy=PhonePolicy.from_data(
            {
                "ringing_timeout": 1,
                "no_answer": {"kind": "contact", "target": "transfer_c"},
            }
        ),
    )
    setup_phone_policy_events(runtime.hass, entry)
    caller, original, replacement = registered_phones
    await asyncio.to_thread(caller.dial, "700")
    session = next(iter(runtime.calls.sessions.values()))
    async with asyncio.timeout(4):
        while "Call established" not in replacement.read() or session.owner != "bridge":
            await asyncio.sleep(0.01)
    assert session.live and session.state == "in_call"
    assert caller.read().count("Call established") == 1
    assert "Call established" not in original.read()
    await asyncio.to_thread(replacement.hangup)
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(0.01)
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_sip_browser_answer_confirms_the_same_phone_leg(trunks, registered_phones):
    """Card answer and BLF/get_phone_state must describe the same live call."""
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.phone_state import phone_states
    from custom_components.voip_stack.dialog_info import snapshot

    runtime = trunks
    runtime.runtime.endpoints.register(PhoneEndpoint(
        endpoint_id="casa", name="casa", device_id="browser-casa",
        kind=EndpointKind.BROWSER, availability="available",
    ))
    runtime.roster.append(RosterEntry("casa", metadata={"local_ha": True, "endpoint_id": "casa"}))
    caller = registered_phones[0]
    await asyncio.to_thread(caller.command, "/dial casa")
    try:
        async with asyncio.timeout(3):
            while phone_states(runtime.hass)["casa"]["state"] != "ringing":
                await asyncio.sleep(.01)
        source = next(iter(runtime.calls.sessions.values()))
        await runtime.hass.services.async_call("voip_stack", "answer", {
            "device_id": "browser-casa", "call_id": source.call_id,
            "media_client_id": "answer-proof",
        }, blocking=True)
        assert source.state == "in_call"
        assert phone_states(runtime.hass)["casa"]["state"] == "in_call"
        assert all(dialog.state == "confirmed" for dialog in snapshot(runtime.runtime, "casa"))
        assert source.legs["browser:casa"].state == "in_call"
    finally:
        await asyncio.to_thread(caller.hangup)
        async with asyncio.timeout(3):
            while runtime.calls.sessions:
                await asyncio.sleep(.01)
    assert not runtime.calls.endpoint_claims_snapshot()
    assert not runtime.runtime.rtp_port_pool.get('used')
