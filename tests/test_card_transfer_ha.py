"""The dashboard transfer action with independent SIP phones and RTP."""

import asyncio
import math
import struct
import wave
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack import softphone_originate

pytestmark = pytest.mark.ha


async def wait_for(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_a"}], indirect=True)
@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("incoming", [False, True])
async def test_browser_transfer_service_moves_peer_to_registered_phone(
    registered_phones, trunks, monkeypatch, tmp_path, reject, incoming
):
    runtime = trunks
    endpoint = PhoneEndpoint(
        endpoint_id="transfer-browser",
        name="Operator",
        device_id="transfer-browser-device",
        kind=EndpointKind.BROWSER,
        availability="available",
        capabilities=frozenset({"audio", "dtmf"}),
    )
    runtime.runtime.endpoints.register(endpoint)
    runtime.roster.append(
        RosterEntry(
            endpoint.name,
            metadata={"local_ha": True, "endpoint_id": endpoint.endpoint_id},
        )
    )
    runtime.hass.states.async_set(
        "sensor.voip_phonebook",
        "ready",
        {"roster_json": dump_roster_json(runtime.roster)},
    )
    monkeypatch.setattr(
        softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1")
    )
    monkeypatch.setattr(
        softphone_originate, "_resolve_target_device", AsyncMock(return_value=None)
    )
    if incoming:
        await asyncio.to_thread(registered_phones[0].command, "/dial Operator")
        await wait_for(
            lambda: runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
        )
        call_id = runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
        await runtime.hass.services.async_call(
            "voip_stack",
            "answer",
            {
                "device_id": endpoint.device_id,
                "call_id": call_id,
                "media_client_id": "transfer-browser-media",
            },
            blocking=True,
        )
    else:
        await runtime.hass.services.async_call(
            "voip_stack",
            "call",
            {
                "device_id": endpoint.device_id,
                "destination": "transfer_a",
                "media_client_id": "transfer-browser-media",
            },
            blocking=True,
        )
        call_id = runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
        # The call action may return on 180. A real manual-answer peer makes
        # that contract deterministic instead of relying on auto-answer timing.
        with pytest.raises(ServiceValidationError, match="established call"):
            await runtime.hass.services.async_call(
                "voip_stack", "transfer",
                {"device_id": endpoint.device_id, "call_id": call_id,
                 "destination": "transfer_b"},
                blocking=True, return_response=True,
            )
        await asyncio.to_thread(registered_phones[0].command, "/accept")
    await wait_for(
        lambda: (session := runtime.calls.get_session(call_id)) is not None
        and session.live and session.state == "in_call"
    )
    from custom_components.voip_stack.dialog_info import snapshot

    dialogs = snapshot(runtime.runtime, endpoint.endpoint_id)
    assert len(dialogs) == 1
    assert dialogs[0].peer_endpoint_id == "transfer_a"
    assert dialogs[0].peer_user == "transfer_a"
    runtime.runtime.endpoints.update("transfer_c", device_id="other-phone")
    with pytest.raises(ServiceValidationError, match="does not own"):
        await runtime.hass.services.async_call(
            "voip_stack",
            "transfer",
            {
                "device_id": "other-phone",
                "call_id": call_id,
                "destination": "transfer_b",
            },
            blocking=True,
            return_response=True,
        )
    if reject:
        runtime.runtime.endpoints.update("transfer_b", dnd=True)
    response = await runtime.hass.services.async_call(
        "voip_stack",
        "transfer",
        {
            "device_id": endpoint.device_id,
            "call_id": call_id,
            "destination": "transfer_b",
        },
        blocking=True,
        return_response=True,
    )
    if reject:
        assert not response["success"] and response["status"] == 486, response
        assert runtime.calls.get_session(call_id).live
        if incoming:
            assert runtime.calls.component("udp_listener").remote_uri_for_call(call_id)
        else:
            assert runtime.calls.sip_client_for(call_id).dialog is not None
        await runtime.hass.services.async_call(
            "voip_stack", "hangup", {"device_id": endpoint.device_id}, blocking=True
        )
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get("used")
        return
    assert response["success"], str(response)
    await wait_for(lambda: call_id not in runtime.calls.sessions)
    for peer in registered_phones[:2]:
        await asyncio.to_thread(peer.read)
        assert "established" in peer.output.lower(), peer.output
    remaining = next(iter(runtime.calls.sessions.values()))
    relay = runtime.calls.resource_for(remaining.call_id, "relay")
    await wait_for(lambda: relay.left_tx_packets >= 30 and relay.right_tx_packets >= 30)
    await asyncio.to_thread(registered_phones[0].hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get("used")

    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    for name, expected in (("transfer_a", 550), ("transfer_b", 450)):
        found = False
        for path in (tmp_path / name).glob("*-dec.wav"):
            with wave.open(str(path), "rb") as audio:
                rate = audio.getframerate()
                samples = struct.unpack(
                    f"<{audio.getnframes()}h", audio.readframes(audio.getnframes())
                )[-1600:]
            if len(samples) < 800:
                continue
            powers = {
                frequency: abs(
                    sum(
                        value
                        * complex(
                            math.cos(2 * math.pi * frequency * index / rate),
                            math.sin(2 * math.pi * frequency * index / rate),
                        )
                        for index, value in enumerate(samples)
                    )
                )
                for frequency in (450, 550, 650)
            }
            found |= (
                max(powers, key=powers.get) == expected and powers[expected] > 10000
            )
        assert found, f"{name} did not receive the transferred peer's audio"


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("operator", ["local-a", "local-b"])
@pytest.mark.parametrize("reject", [False, True, "prepare"])
async def test_local_pair_transfer_to_sip_keeps_the_retained_phone(
    trunks, registered_phones, operator, reject, tmp_path, monkeypatch
):
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge,
        start_local_softphone_call,
    )

    runtime = trunks
    for identity in ("local-a", "local-b"):
        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id=identity,
                device_id="device-" + identity,
                name=identity,
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf", "video"}),
            )
        )
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(
        runtime.hass,
        "local-a",
        "local-b",
        caller_owner_id="owner-a",
        request_video=True,
    )
    bridge.answer(call.call_id, "local-b", "owner-b")
    session = runtime.calls.get_session(call.call_id)
    from custom_components.voip_stack.websocket_api import CALL_EVENT
    from custom_components.voip_stack.automation_routing import automation_event_type

    events = []
    detach = runtime.hass.bus.async_listen(
        CALL_EVENT, lambda event: events.append(automation_event_type(event.data))
    )
    leases = {
        identity: bridge.acquire_media(call.call_id, identity, owner)
        for identity, owner in (("local-a", "owner-a"), ("local-b", "owner-b"))
    }
    retained = "local-b" if operator == "local-a" else "local-a"
    if reject is True:
        runtime.runtime.endpoints.update("transfer_c", dnd=True)
    elif reject == "prepare":
        from custom_components.voip_stack.sip_client import SipCallClient

        async def fail(_self, **_kwargs):
            raise OSError("injected destination INVITE failure")

        monkeypatch.setattr(SipCallClient, "invite", fail)
    try:
        result = await runtime.hass.services.async_call(
            "voip_stack",
            "transfer",
            {
                "device_id": "device-" + operator,
                "destination": "transfer_c",
                "call_id": call.call_id,
            },
            blocking=True,
            return_response=True,
        )
        if reject:
            assert not result["success"] and result["status"] == (
                480 if reject == "prepare" else 486
            )
            assert session.live and session.state == "in_call"
            assert not {"answered", "connected"}.intersection(events)
            assert bridge.get_call(call.call_id).callee_state.value == "in_call"
            pcm = struct.pack("<480h", *([7000] * 480))
            bridge.send_audio(call.call_id, operator, leases[operator].token, pcm)
            assert (
                bridge.receive_audio_nowait(
                    call.call_id, retained, leases[retained].token
                )
                == pcm
            )
            await runtime.hass.services.async_call(
                "voip_stack",
                "hangup",
                {"device_id": "device-" + operator},
                blocking=True,
            )
        else:
            assert result["success"], result
            assert (
                runtime.calls.get_session(call.call_id) is session
                and session.state == "in_call"
            )
            assert bridge.get_call(call.call_id) is None
            assert not runtime.runtime.endpoints.get(operator).active_call_id
            assert (
                runtime.runtime.endpoints.get(retained).active_call_id == call.call_id
            )
            try:
                await runtime.hass.services.async_call(
                    "voip_stack",
                    "hangup",
                    {
                        "device_id": "device-" + operator,
                        "call_id": call.call_id,
                    },
                    blocking=True,
                )
            except ServiceValidationError:
                pass
            assert session.live, (
                "A retired phone must not hang up the retained conversation"
            )
            state = await runtime.hass.services.async_call("voip_stack", "get_phone_state", {
                "device_id": "device-" + retained,
            }, blocking=True, return_response=True)
            assert len(state["calls"]) == 1, "Media and signaling legs must not appear as two calls"
            from custom_components.voip_stack.websocket_api import _ha_softphone_state
            assert _ha_softphone_state(runtime.hass, retained)["target_device_id"] == runtime.runtime.endpoints.get("transfer_c").device_id
            from aiohttp import WSMsgType
            from types import SimpleNamespace
            from custom_components.voip_stack import audio_ws, audio_ws_view

            media = audio_ws_view._active_softphone_media_session(
                runtime.hass, retained
            )
            assert media is not None

            class Browser:
                def __init__(self):
                    self.messages, self.audio = asyncio.Queue(), asyncio.Queue()
                    self.ready = asyncio.Event()

                def force_close(self):
                    self.messages.put_nowait(
                        SimpleNamespace(type=WSMsgType.CLOSE, data=None, extra=None)
                    )

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    return await self.messages.get()

                async def send_json(self, value):
                    self.ready.set()

                async def send_bytes(self, value):
                    await self.audio.put(value)

            browser = Browser()
            worker = asyncio.create_task(
                audio_ws_view._run_audio_session(
                    runtime.hass, browser, media, endpoint_id=retained
                )
            )
            try:
                await asyncio.wait_for(browser.ready.wait(), 2)
                fmt = media.send_format.audio_format
                for index in range(30):
                    samples = [
                        int(
                            7000
                            * math.sin(
                                2
                                * math.pi
                                * 733
                                * (index * fmt.nominal_frame_samples + i)
                                / fmt.sample_rate
                            )
                        )
                        for i in range(fmt.nominal_frame_samples)
                    ]
                    await browser.messages.put(
                        SimpleNamespace(
                            type=WSMsgType.BINARY,
                            data=audio_ws.encode_audio_frame(
                                struct.pack(f"<{len(samples)}h", *samples)
                            ),
                            extra=None,
                        )
                    )
                    await asyncio.sleep(fmt.frame_ms / 1000)
                incoming = []
                while not browser.audio.empty():
                    frame = audio_ws.decode_audio_frame(browser.audio.get_nowait())
                    incoming.extend(struct.unpack(f"<{len(frame) // 2}h", frame))
                assert len(incoming) >= 800 and max(abs(x) for x in incoming) > 1000
                await asyncio.to_thread(registered_phones[2].hangup)
                await asyncio.wait_for(worker, 3)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await asyncio.to_thread(registered_phones[2].close)
            received = []
            for path in (tmp_path / "transfer_c").glob("*-dec.wav"):
                with wave.open(str(path), "rb") as audio:
                    rate = audio.getframerate()
                    received.extend(
                        struct.unpack(
                            f"<{audio.getnframes()}h",
                            audio.readframes(audio.getnframes()),
                        )
                    )
            assert len(received) > 800
            powers = {
                frequency: abs(
                    sum(
                        value
                        * complex(
                            math.cos(2 * math.pi * frequency * i / rate),
                            math.sin(2 * math.pi * frequency * i / rate),
                        )
                        for i, value in enumerate(received)
                    )
                )
                for frequency in (450, 650, 733)
            }
            assert max(powers, key=powers.get) == 733
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get("used")
    finally:
        detach()
        if session.live:
            await runtime.calls.terminate_call_wait(
                session.call_id, reason="test_complete"
            )


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_c"}], indirect=True)
@pytest.mark.parametrize("operator", ["local-a", "local-b"])
@pytest.mark.parametrize("outcome", ["cancel", "video_change"])
async def test_cancel_local_transfer_resumes_the_original_conversation(
    trunks, registered_phones, operator, outcome
):
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge,
        start_local_softphone_call,
    )

    runtime = trunks
    for name in ("local-a", "local-b"):
        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id=name,
                device_id="device-" + name,
                name=name,
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf", "video"}),
            )
        )
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(
        runtime.hass,
        "local-a",
        "local-b",
        caller_owner_id="owner-a",
        request_video=True,
    )
    bridge.answer(call.call_id, "local-b", "owner-b")
    session = runtime.calls.get_session(call.call_id)
    leases = {
        name: bridge.acquire_media(call.call_id, name, owner)
        for name, owner in (("local-a", "owner-a"), ("local-b", "owner-b"))
    }
    task = asyncio.create_task(
        runtime.hass.services.async_call(
            "voip_stack",
            "transfer",
            {
                "device_id": "device-" + operator,
                "call_id": call.call_id,
                "destination": "transfer_c",
            },
            blocking=True,
            return_response=True,
        )
    )
    try:
        await wait_for(
            lambda: any(
                leg.endpoint_id == "transfer_c" and leg.state == "remote_ringing"
                for leg in session.legs.values()
            )
        )
        if outcome == "cancel":
            task.cancel()
        else:
            retained = "local-b" if operator == "local-a" else "local-a"
            bridge.set_video_send(call.call_id, retained, True)
            await asyncio.to_thread(registered_phones[2].command, "/accept")
        response = (
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 4)
        )[0]
        if outcome == "cancel":
            assert isinstance(response, asyncio.CancelledError)
        else:
            assert not response["success"]
            assert bridge.get_call(call.call_id).video_active
        assert (
            session.live
            and session.owner == "local_bridge"
            and session.state == "in_call"
        )
        assert not runtime.runtime.endpoints.get("transfer_c").active_call_id
        assert not runtime.runtime.rtp_port_pool.get("used")
        for sender, receiver, value in (
            ("local-a", "local-b", 7000),
            ("local-b", "local-a", 9000),
        ):
            pcm = struct.pack("<480h", *([value] * 480))
            bridge.send_audio(call.call_id, sender, leases[sender].token, pcm)
            assert (
                bridge.receive_audio_nowait(
                    call.call_id, receiver, leases[receiver].token
                )
                == pcm
            )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if session.live:
            await runtime.calls.terminate_call_wait(
                session.call_id, reason="test_complete"
            )
    assert not runtime.calls.sessions


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("operator", ["local-a", "local-b"])
@pytest.mark.parametrize("blocked", ["", "local-a", "local-b"])
async def test_local_pair_transfer_to_trunk_preserves_participant_access(
    trunks, operator, blocked
):
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.store import store_manual_roster_entries

    runtime = trunks
    for name in ("local-a", "local-b"):
        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id=name,
                device_id="device-" + name,
                name=name,
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
    store_manual_roster_entries(runtime.hass, [RosterEntry("Outside", number="12345")])
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(
        runtime.hass, "local-a", "local-b", caller_owner_id="owner-a"
    )
    bridge.answer(call.call_id, "local-b", "owner-b")
    session = runtime.calls.get_session(call.call_id)
    if blocked:
        await runtime.hass.services.async_call(
            "voip_stack",
            "set_external_call_access",
            {"device_id": "device-" + blocked, "allowed": False},
            blocking=True,
        )
    try:
        if blocked:
            with pytest.raises(ServiceValidationError):
                await runtime.hass.services.async_call(
                    "voip_stack",
                    "transfer",
                    {
                        "device_id": "device-" + operator,
                        "call_id": call.call_id,
                        "destination": "Outside",
                    },
                    blocking=True,
                    return_response=True,
                )
            assert (
                session.live
                and session.owner == "local_bridge"
                and session.state == "in_call"
            )
            assert not any(phone.invites for phone in runtime.phones)
            assert bridge.get_call(call.call_id).callee_state.value == "in_call"
        else:
            result = await runtime.hass.services.async_call(
                "voip_stack",
                "transfer",
                {
                    "device_id": "device-" + operator,
                    "call_id": call.call_id,
                    "destination": "Outside",
                },
                blocking=True,
                return_response=True,
            )
            assert result["success"], result
            assert len(runtime.phones[0].invites) == 1 and not runtime.phones[1].invites
            assert runtime.phones[0].last_request.uri.startswith("sip:12345@")
            assert session.metadata["outbound_trunk_id"] == "home"
            runtime.phones[0].remote_hangup()
            await wait_for(lambda: not runtime.calls.sessions)
            assert not runtime.runtime.rtp_port_pool.get("used")
            from custom_components.voip_stack.runtime_data import sip_trunks

            assert sip_trunks(runtime.hass)["home"].ready
            assert not sip_trunks(runtime.hass)["home"]._outbound_dialogs
    finally:
        if session.live:
            await runtime.calls.terminate_call_wait(
                session.call_id, reason="test_complete"
            )
